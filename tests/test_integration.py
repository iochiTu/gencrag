"""End-to-end against a real PostgreSQL + pgvector, with the mock provider (no model needed).

WARNING: wipes and re-seeds the product tables. Runs only when TEST_DATABASE_URL is set:
    set TEST_DATABASE_URL=postgresql://rag:rag@localhost:5432/kbeauty_test   (Windows)
"""
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def conn():
    from app import db, ingest
    db.run_sql_file("schema.sql")
    db.run_sql_file("seed_sample.sql")
    db.connect().execute("DELETE FROM app_settings")
    ingest.embed_products(reembed=True)
    with db.connect() as c:
        yield c


def all_skus(r):
    return [o["sku"] for st in r.get("steps", []) for o in st["options"]]


def run(conn, **kw):
    from app.models import RecommendRequest
    from app.providers import MockProvider
    from app.recommender import recommend
    return recommend(conn, MockProvider(), RecommendRequest(**kw))


def test_pregnancy_never_gets_retinol(conn):
    r = run(conn, text="Dry skin, worried about fine lines, currently pregnant", top_n=5)
    assert r["status"] == "ok"
    assert "SR-003" not in all_skus(r)


def test_budget_and_stock_respected(conn):
    conn.execute("UPDATE products SET stock_qty = 0 WHERE sku = 'SR-001'")
    r = run(conn, text="dry skin, dehydrated, under $25", top_n=5)
    conn.execute("UPDATE products SET stock_qty = 70 WHERE sku = 'SR-001'")
    for st in r["steps"]:
        for x in st["options"]:
            assert x["price"] <= 25 and x["sku"] != "SR-001"


def test_sensitive_and_fragrance_filter(conn):
    r = run(conn, profile={"skin_type": "dry", "sensitive": True, "avoid": ["fragrance"], "concerns": ["redness"]}, top_n=4)
    skus = all_skus(r)
    assert skus and not {"TN-001", "CR-002", "CR-003"} & set(skus)


def test_hidden_product_is_never_recommended(conn):
    conn.execute("UPDATE business_rules SET excluded = TRUE WHERE sku = 'SR-004'")
    r = run(conn, profile={"skin_type": "oily", "concerns": ["acne", "redness"]}, top_n=5)
    conn.execute("UPDATE business_rules SET excluded = FALSE WHERE sku = 'SR-004'")
    assert "SR-004" not in all_skus(r)


def test_no_match_message(conn):
    r = run(conn, profile={"skin_type": "oily", "budget_max": 1})
    assert r["status"] == "no_match"


def test_log_written(conn):
    r = run(conn, text="oily skin, pores and shine", top_n=3, employee_id="E01")
    row = conn.execute("SELECT employee_id, used_llm FROM recommendation_log WHERE id = %s", (r["log_id"],)).fetchone()
    assert row["employee_id"] == "E01" and row["used_llm"] is False


def test_multiple_options_per_step(conn):
    r = run(conn, profile={"skin_type": "combination", "concerns": ["acne", "pores", "redness"]}, top_n=4, options=3)
    assert r["status"] == "ok" and r["options_per_step"] == 3
    assert any(len(st["options"]) > 1 for st in r["steps"])
    assert len({st["category"] for st in r["steps"]}) == len(r["steps"])          # one category per step
    for st in r["steps"]:
        assert all(o["category"] == st["category"] for o in st["options"])
        assert [o["option"] for o in st["options"]] == list(range(1, len(st["options"]) + 1))


def test_single_category_request_gives_choices(conn):
    r = run(conn, text="oily skin, looking for a serum", top_n=4, options=3)
    assert [st["category"] for st in r["steps"]] == ["serum"] and len(r["steps"][0]["options"]) >= 2
