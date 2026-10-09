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
