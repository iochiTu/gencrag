"""Staff accounts for the chat app: password hashing, signed session cookies, and the /api auth endpoints.

Each employee has a username + password (created by a manager in the admin portal, Staff accounts tab,
or with `python -m app.users add <username> "<Full name>"`). Sessions are signed HTTP-only cookies that
stay valid for STAFF_SESSION_DAYS so phones do not need to log in every day. Resetting a password or
"sign out everywhere" bumps the user's session_version, which invalidates old cookies.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .analytics import track
from .config import settings
from .db import connect

router = APIRouter(prefix="/api", tags=["staff"])
STAFF_COOKIE = "kb_staff"
PBKDF2_ITERATIONS = 310_000
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split("$")
        new = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(new, base64.b64decode(dk))
    except Exception:
        return False


def check_new_password(pw: str) -> None:
    if len(pw) < 8:
        raise HTTPException(400, "The password must be at least 8 characters.")
    if pw.isdigit() or pw.lower() in ("password", "12345678", "qwertyui", "gencbeauty"):
        raise HTTPException(400, "Please choose a less guessable password.")


def temp_password() -> str:
    words = ["rose", "cica", "aloe", "lotus", "honey", "pearl", "mint", "peach", "matcha", "jade"]
    return f"{secrets.choice(words)}-{secrets.choice(words)}-{secrets.randbelow(900) + 100}"


# ------------------------------------------------------------------ signed tokens
def _key() -> bytes:
    raw = settings.secret_key or ("derived:" + settings.admin_password)
    return hashlib.sha256(raw.encode()).digest()


def sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_staff_token(user_id: int, version: int) -> str:
    exp = int(time.time()) + settings.staff_session_days * 86400
    payload = f"s{user_id}.{version}.{exp}"
    return f"{payload}.{sign(payload)}"


def read_staff_token(token: str | None) -> tuple[int, int] | None:
    try:
        uid, version, exp, sig = (token or "").split(".")
        payload = f"{uid}.{version}.{exp}"
        if not uid.startswith("s") or not hmac.compare_digest(sig, sign(payload)) or int(exp) < time.time():
            return None
        return int(uid[1:]), int(version)
    except Exception:
        return None


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def set_session(response: Response, request: Request, user: dict) -> None:
    response.set_cookie(STAFF_COOKIE, make_staff_token(user["id"], user["session_version"]), httponly=True,
                        samesite="lax", secure=_secure(request), max_age=settings.staff_session_days * 86400)


# ------------------------------------------------------------------ users
USER_COLS = "id, username, display_name, role, is_active, must_change_password, session_version, last_login_at, created_at"


def create_user(conn, username: str, display_name: str, password: str, role: str = "staff",
                created_by: str | None = None, must_change: bool = True) -> dict:
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise ValueError("Username: 2-40 characters, lower-case letters, numbers, dot, dash or underscore.")
    if conn.execute("SELECT 1 FROM staff_users WHERE username = %s", (username,)).fetchone():
        raise ValueError(f"The username '{username}' is already taken.")
    return conn.execute(
        f"""INSERT INTO staff_users (username, display_name, password_hash, role, must_change_password, created_by)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING {USER_COLS}""",
        (username, display_name.strip() or username, hash_password(password), role, must_change, created_by)).fetchone()


def require_staff(request: Request) -> dict:
    parsed = read_staff_token(request.cookies.get(STAFF_COOKIE))
    if not parsed:
        raise HTTPException(401, "Please sign in.")
    with connect() as conn:
        user = conn.execute(f"SELECT {USER_COLS} FROM staff_users WHERE id = %s", (parsed[0],)).fetchone()
    if not user or not user["is_active"] or user["session_version"] != parsed[1]:
        raise HTTPException(401, "Your session has ended. Please sign in again.")
    return user


def public_user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "role": u["role"],
            "must_change_password": u["must_change_password"]}


_failures: dict[str, list[float]] = defaultdict(list)


class LoginBody(BaseModel):
    username: str = Field(..., max_length=60)
    password: str = Field(..., max_length=200)


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    key = f"{request.client.host if request.client else '?'}|{body.username.strip().lower()}"
    _failures[key] = [t for t in _failures[key] if t > time.time() - 900]
    if len(_failures[key]) >= 8:
        raise HTTPException(429, "Too many attempts. Wait 15 minutes or ask a manager to reset your password.")
    with connect() as conn:
        u = conn.execute(f"SELECT {USER_COLS}, password_hash FROM staff_users WHERE username = %s",
                         (body.username.strip().lower(),)).fetchone()
        if not u or not u["is_active"] or not verify_password(body.password, u["password_hash"]):
            _failures[key].append(time.time())
            raise HTTPException(401, "Wrong username or password.")
        conn.execute("UPDATE staff_users SET last_login_at = now() WHERE id = %s", (u["id"],))
        track(conn, "login", user=u, meta={"user_agent": request.headers.get("user-agent", "")[:200]})
    _failures.pop(key, None)
    set_session(response, request, u)
    return {"user": public_user(u)}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(STAFF_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(require_staff)):
    return {"user": public_user(user), "brand": {"logo_url": settings.brand_logo_url, "name": settings.brand_name},
            "currency": settings.currency}


class PasswordBody(BaseModel):
    current: str
    new: str = Field(..., max_length=200)


@router.post("/password")
def change_password(body: PasswordBody, request: Request, response: Response, user: dict = Depends(require_staff)):
    check_new_password(body.new)
    with connect() as conn:
        row = conn.execute("SELECT password_hash FROM staff_users WHERE id = %s", (user["id"],)).fetchone()
        if not verify_password(body.current, row["password_hash"]):
            raise HTTPException(400, "The current password is not correct.")
        u = conn.execute(
            f"""UPDATE staff_users SET password_hash = %s, must_change_password = FALSE,
                    session_version = session_version + 1 WHERE id = %s RETURNING {USER_COLS}""",
            (hash_password(body.new), user["id"])).fetchone()
    set_session(response, request, u)          # this device stays signed in; other devices are signed out
    return {"user": public_user(u)}
