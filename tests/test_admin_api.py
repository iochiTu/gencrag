"""Admin portal API end-to-end (needs TEST_DATABASE_URL; wipes that database's product tables)."""
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app import db, ingest
    from app.main import app
    db.run_sql_file("schema.sql")
    db.run_sql_file("seed_sample.sql")
    with db.connect() as c:
        c.execute("DELETE FROM app_settings"); c.execute("DELETE FROM admin_audit")
    ingest.embed_products(reembed=True)
    c = TestClient(app)
    from tests.conftest import staff_login
    staff_login(c)
    return c


def login(client):
    r = client.post("/admin/api/login", json={"name": "Mina", "password": "test-pass"})
    assert r.status_code == 200


def test_requires_login(client):
    from tests.conftest import staff_login
    client.cookies.clear()
    assert client.post("/recommend", json={"text": "dry skin"}).status_code == 401
    assert client.get("/admin/api/ranking").status_code == 401
    assert client.post("/admin/api/login", json={"name": "x", "password": "wrong"}).status_code == 401
    staff_login(client)


def test_ranking_save_applies_to_staff_recommendations(client):
    login(client)
    d = client.get("/admin/api/ranking").json()
    assert d["settings"]["preset"] == "balanced" and d["updated_by"] is None
    body = {**d["settings"], "preset": "balanced", "business_share": 60, "priority_share": 100}
    s = client.put("/admin/api/ranking", json=body).json()
    assert s["settings"]["preset"] == "custom"                       # sliders no longer match the preset
    assert abs(s["weights"]["priority"] - 0.6) < 1e-6
    r = client.post("/recommend", json={"profile": {"skin_type": "oily", "concerns": ["acne"]}, "top_n": 2, "debug": True}).json()
    assert r["status"] == "ok"
    assert client.get("/admin/api/ranking").json()["updated_by"] == "Mina"


def test_preview_compares_strategies(client):
    login(client)
    body = {"proposed": {"business_share": 0, "priority_share": 0, "min_skin_fit": 0, "use_llm_rerank": False},
            "request": {"text": "combination skin with breakouts and pores", "top_n": 4}}
    d = client.post("/admin/api/preview", json=body).json()
    assert d["current"]["rows"] and d["proposed"]["rows"]
    assert d["proposed"]["weights"]["margin"] == 0 and d["current"]["weights"]["priority"] > 0


def test_product_rules_and_audit(client):
    login(client)
    bad = client.put("/admin/api/products/TN-002", json={"priority": 5, "campaign_boost": 0.06,
                                                          "valid_from": "2026-12-01", "valid_to": "2026-11-01"})
    assert bad.status_code == 422
    assert client.put("/admin/api/products/NOPE", json={"priority": 3}).status_code == 404
    ok = client.put("/admin/api/products/TN-002", json={"priority": 5, "campaign_boost": 0.06,
                                                         "valid_from": "2026-01-01", "valid_to": "2026-12-31", "excluded": False})
    assert ok.status_code == 200
    p = {x["sku"]: x for x in client.get("/admin/api/products").json()["products"]}
    assert p["TN-002"]["priority"] == 5 and p["TN-002"]["boost_active"] is True
    changes = client.get("/admin/api/activity?days=30").json()["changes"]
    assert any(c["target"] == "TN-002" and c["actor"] == "Mina" for c in changes)


def test_activity_counts_feedback(client):
    login(client)
    r = client.post("/recommend", json={"text": "dry skin with redness", "top_n": 3, "employee_id": "E9"}).json()
    client.post("/feedback", json={"log_id": r["log_id"], "feedback": "sold"})
    a = client.get("/admin/api/activity?days=7").json()
    assert a["kpis"]["sold"] >= 1 and a["kpis"]["recommendations"] >= 1 and a["top_products"]
