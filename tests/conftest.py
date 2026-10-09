"""Test settings are applied before any app module is imported.

End-to-end tests (test_integration.py, test_admin_api.py) run only when TEST_DATABASE_URL is set.
WARNING: they wipe and re-seed the product tables in that database.
"""
import os

if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["LLM_PROVIDER"] = "mock"
os.environ["ADMIN_PASSWORD"] = "test-pass"
os.environ["SECRET_KEY"] = "test-secret"


def staff_login(client, username="tester", password="test-pass-123"):
    """Create (once) and sign in a staff account on a TestClient. Needs TEST_DATABASE_URL."""
    from app import auth
    from app.db import connect
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM staff_users WHERE username = %s", (username,)).fetchone():
            auth.create_user(conn, username, username.title(), password, must_change=False)
        conn.execute("UPDATE staff_users SET is_active = TRUE, password_hash = %s WHERE username = %s",
                     (auth.hash_password(password), username))
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["user"]
