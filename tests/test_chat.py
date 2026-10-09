"""Staff accounts, chat conversations and analytics, end-to-end (needs TEST_DATABASE_URL; wipes that DB)."""
import os

import pytest

from app import auth
from app.chat import match_products, merge_chat
from app.models import CustomerProfile

DB = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def test_password_hashing_and_tokens():
    h = auth.hash_password("rose-cica-123")
    assert h.startswith("pbkdf2_sha256$") and auth.verify_password("rose-cica-123", h)
    assert not auth.verify_password("wrong", h) and not auth.verify_password("x", "garbage")
    t = auth.make_staff_token(7, 3)
    assert auth.read_staff_token(t) == (7, 3) and auth.read_staff_token(t[:-2] + "00") is None
    assert auth.read_staff_token("Mina.1.2.3") is None


def test_merge_chat_follow_ups():
    old = CustomerProfile(skin_type="dry", concerns=["dryness"], avoid=["pregnancy"], budget_max=40)
    new = merge_chat(old, CustomerProfile(concerns=["redness"]), "also some redness", {})
    assert new.skin_type == "dry" and new.concerns == ["dryness", "redness"] and new.budget_max == 40
    new = merge_chat(new, CustomerProfile(avoid=["pregnancy"]), "she is not pregnant anymore", {})
    assert "pregnancy" not in new.avoid
    new = merge_chat(new, CustomerProfile(), "cheaper options please", {"last_prices": [12, 28, 30]})
    assert new.budget_max < 30
    new = merge_chat(new, CustomerProfile(categories=["serum"]), "only serums", {})
    assert new.categories == ["serum"]
    new = merge_chat(new, CustomerProfile(), "show the full routine, no budget", {})
    assert new.categories == [] and new.budget_max is None


@pytest.fixture(scope="module")
def client():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL not set")
    from fastapi.testclient import TestClient
    from app import db, ingest
    from app.main import app
    db.run_sql_file("schema.sql")
    db.run_sql_file("seed_sample.sql")
    with db.connect() as c:
        c.execute("DELETE FROM staff_users")
    ingest.embed_products(reembed=True)
    return TestClient(app)


def say(client, text, conv=None):
    r = client.post("/api/chat", json={"message": text, "conversation_id": conv})
    assert r.status_code == 200, r.text
    d = r.json()
    return d["conversation"]["id"], d["messages"][1]


@DB
def test_login_flow_and_password_change(client):
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 401
    with __import__("app.db", fromlist=["connect"]).connect() as conn:
        auth.create_user(conn, "Mina", "Mina Park", "temp-pass-1")          # must change at first login
    assert client.post("/api/login", json={"username": "mina", "password": "nope"}).status_code == 401
    r = client.post("/api/login", json={"username": "MINA", "password": "temp-pass-1"})
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    assert client.post("/api/password", json={"current": "temp-pass-1", "new": "1234"}).status_code == 400
    r = client.post("/api/password", json={"current": "temp-pass-1", "new": "matcha-rose-77"})
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is False
    me = client.get("/api/me").json()
    assert me["user"]["display_name"] == "Mina Park" and me["brand"]["name"]


@DB
def test_chat_conversation_flow_and_analytics(client):
    client.post("/api/login", json={"username": "mina", "password": "matcha-rose-77"})
    conv, m = say(client, "hello")
    assert m["payload"]["type"] == "text" and "Tell me about the customer" in m["content"]
    conv, m = say(client, "Combination skin with breakouts and redness, 24 years old, under $30", conv)
    p = m["payload"]
    assert p["type"] == "recommendation" and p["steps"] and m["log_id"]
    assert "Combination skin" in p["profile_chips"] and "≤ $30.00" in p["profile_chips"]
    assert all(o["price"] <= 30 for st in p["steps"] for o in st["options"])
    first_prices = [st["options"][0]["price"] for st in p["steps"]]

    conv, m = say(client, "Cheaper options", conv)
    prices = [o["price"] for st in m["payload"]["steps"] for o in st["options"]]
    assert max(prices) < max(first_prices)

    conv, m = say(client, "She is pregnant", conv)
    assert "Pregnancy-safe" in m["payload"]["profile_chips"]
    assert "SR-003" not in [o["sku"] for st in m["payload"]["steps"] for o in st["options"]]

    conv, m = say(client, "Only serums", conv)               # budget was lowered by "cheaper": nothing fits
    assert "couldn't find" in m["content"] and "No budget limit" in m["payload"]["suggestions"]
    conv, m = say(client, "No budget limit", conv)
    assert [st["category"] for st in m["payload"]["steps"]] == ["serum"] and len(m["payload"]["steps"][0]["options"]) >= 2

    conv, m = say(client, "Is the Retinol 0.3% Repair Serum safe during pregnancy?", conv)
    assert m["payload"]["type"] == "products" and m["payload"]["products"][0]["sku"] == "SR-003"
    assert "Not recommended during pregnancy" in m["content"]

    conv, m = say(client, "What's the difference between the Cica Repair Ampoule and the Niacinamide 10% Spot Serum?", conv)
    assert {x["sku"] for x in m["payload"]["products"]} == {"SR-004", "SR-002"} and "cheaper" in m["content"]

    conv, m = say(client, "New customer", conv)
    assert "Ready for the next customer" in m["content"]
    conv, m = say(client, "oily skin", conv)
    assert "Pregnancy-safe" not in m["payload"].get("profile_chips", [])          # profile was cleared

    # per-option sale feedback through the staff API
    log_id = m["log_id"]
    sku = m["payload"]["steps"][0]["options"][0]["sku"]
    assert client.post("/api/feedback", json={"log_id": log_id, "feedback": "sold", "sku": sku}).status_code == 200

    hist = client.get("/api/conversations").json()["conversations"]
    assert hist[0]["id"] == conv and hist[0]["title"] == "hello"
    full = client.get(f"/api/conversations/{conv}").json()["messages"]
    assert len(full) == 20 and full[0]["role"] == "user"

    from app.db import connect
    with connect() as c:
        ev = c.execute("SELECT event, intent, status, username FROM analytics_events ORDER BY id").fetchall()
    intents = [e["intent"] for e in ev if e["event"] == "chat"]
    assert intents.count("recommendation") >= 4 and "product_question" in intents and "reset" in intents
    assert any(e["event"] == "login" and e["username"] == "mina" for e in ev)
    assert any(e["event"] == "feedback" and e["intent"] == "sold" for e in ev)

    # another staff member cannot open Mina's conversation
    from tests.conftest import staff_login
    staff_login(client, "jun", "jun-password-1")
    assert client.get(f"/api/conversations/{conv}").status_code == 404
    assert client.delete(f"/api/conversations/{conv}").status_code == 404


@DB
def test_admin_manages_staff_accounts(client):
    client.cookies.clear()
    assert client.post("/admin/api/login", json={"name": "Boss", "password": "test-pass"}).status_code == 200
    r = client.post("/admin/api/users", json={"username": "sora", "display_name": "Sora Kim"})
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    assert client.post("/admin/api/users", json={"username": "sora", "display_name": "x"}).status_code == 400
    assert client.post("/admin/api/users", json={"username": "Bad Name!", "display_name": "x"}).status_code == 400
    users = {u["username"]: u for u in client.get("/admin/api/users").json()["users"]}
    assert users["sora"]["must_change_password"] is True
    assert client.post("/api/login", json={"username": "sora", "password": temp}).status_code == 200
    assert client.get("/api/me").status_code == 200
    new_temp = client.post(f"/admin/api/users/{users['sora']['id']}/reset").json()["temporary_password"]
    assert new_temp != temp and client.get("/api/me").status_code == 401        # reset signs out everywhere
    assert client.post("/api/login", json={"username": "sora", "password": new_temp}).status_code == 200
    client.put(f"/admin/api/users/{users['sora']['id']}", json={"is_active": False})
    assert client.get("/api/me").status_code == 401                              # disabled -> signed out
    assert client.post("/api/login", json={"username": "sora", "password": new_temp}).status_code == 401


@DB
def test_product_matching(client):
    from app.db import connect
    with connect() as conn:
        assert match_products(conn, "tell me about sr-003") == ["SR-003"]
        assert match_products(conn, "is the cica ampoule ok?")[0] == "SR-004"
        assert match_products(conn, "dry skin with redness, wants a serum") == []


@DB
def test_admin_analytics_and_csv(client):
    client.cookies.clear()
    client.post("/admin/api/login", json={"name": "Boss", "password": "test-pass"})
    a = client.get("/admin/api/analytics?days=30").json()
    t = a["totals"]
    assert t["chats"] >= 10 and t["active_staff"] >= 1 and t["recommendations"] >= 4 and t["product_questions"] >= 2
    assert t["no_match"] >= 1 and t["sold"] >= 1 and a["daily"] and a["staff"][0]["username"] == "mina"
    assert any(c["concern"] == "acne" for c in a["concerns"]) and a["asked_products"][0]["sku"] in ("SR-003", "SR-004", "SR-002")
    assert a["no_match"] and "serum" in a["no_match"][0]["message"].lower()
    csv = client.get("/admin/api/analytics/export.csv?days=7")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("time_utc,event,staff") and len(lines) > 10
