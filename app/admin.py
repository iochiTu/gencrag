"""Admin portal API (/admin/api/*): ranking strategy, product priorities & campaigns, Excel import,
product details, activity.

Login: one shared ADMIN_PASSWORD (from .env) plus the manager's name, which is written to the
audit log with every change. The session is a signed, HTTP-only cookie.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections import defaultdict
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator

from . import importer
from .config import settings
from .db import connect
from .ingest import EmbedError, embed_products
from .models import AVOID, CATEGORIES, CONCERNS, RecommendRequest
from .providers import ProviderError
from .recommender import preview
from .strategy import MAX_BUSINESS_SHARE, PRESETS, RankingSettings, audit, load_ranking, save_ranking

router = APIRouter(prefix="/admin/api", tags=["admin"])
COOKIE = "kb_admin"
_provider = None          # set by main.py so the admin preview uses the same model provider


def set_provider(p) -> None:
    global _provider
    _provider = p


# ------------------------------------------------------------------ auth
def _key() -> bytes:
    raw = settings.secret_key or ("derived:" + settings.admin_password)
    return hashlib.sha256(raw.encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(name: str) -> str:
    exp = int(time.time()) + settings.admin_session_hours * 3600
    payload = f"{base64.urlsafe_b64encode(name.encode()).decode()}.{exp}"
    return f"{payload}.{_sign(payload)}"


def read_token(token: str | None) -> str | None:
    try:
        b64name, exp, sig = (token or "").split(".")
        payload = f"{b64name}.{exp}"
        if not hmac.compare_digest(sig, _sign(payload)) or int(exp) < time.time():
            return None
        return base64.urlsafe_b64decode(b64name.encode()).decode()
    except Exception:
        return None


def _portal_enabled() -> None:
    if not settings.admin_password:
        raise HTTPException(503, "Admin portal is disabled. Set ADMIN_PASSWORD in .env and restart.")


def require_admin(request: Request) -> str:
    _portal_enabled()
    name = read_token(request.cookies.get(COOKIE))
    if not name:
        raise HTTPException(401, "Please sign in.")
    return name


_failures: dict[str, list[float]] = defaultdict(list)


class Login(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    password: str


@router.post("/login")
def login(body: Login, request: Request, response: Response):
    _portal_enabled()
    ip = request.client.host if request.client else "?"
    recent = [t for t in _failures[ip] if t > time.time() - 600]
    _failures[ip] = recent
    if len(recent) >= 5:
        raise HTTPException(429, "Too many failed attempts. Try again in 10 minutes.")
    if not hmac.compare_digest(body.password.encode(), settings.admin_password.encode()):
        _failures[ip].append(time.time())
        raise HTTPException(401, "Wrong password.")
    name = body.name.strip()
    response.set_cookie(COOKIE, make_token(name), httponly=True, samesite="strict",
                        max_age=settings.admin_session_hours * 3600)
    return {"name": name}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/me")
def me(name: str = Depends(require_admin)):
    return {"name": name, "currency": settings.currency}


# ------------------------------------------------------------------ ranking strategy
@router.get("/ranking")
def get_ranking(_: str = Depends(require_admin)):
    with connect() as conn:
        r = load_ranking(conn)
        row = conn.execute("SELECT updated_by, updated_at FROM app_settings WHERE key = 'ranking'").fetchone()
    return {"settings": r.model_dump(), "weights": r.describe(), "presets": PRESETS,
            "max_business_share": MAX_BUSINESS_SHARE,
            "updated_by": row["updated_by"] if row else None,
            "updated_at": row["updated_at"].isoformat() if row else None}


def _normalise(r: RankingSettings) -> RankingSettings:
    """A preset name only sticks if the sliders still match it."""
    p = PRESETS.get(r.preset)
    if p and (p["business_share"], p["priority_share"]) != (r.business_share, r.priority_share):
        return r.model_copy(update={"preset": "custom"})
    return r


@router.put("/ranking")
def put_ranking(body: RankingSettings, name: str = Depends(require_admin)):
    with connect() as conn:
        saved = save_ranking(conn, _normalise(body), name)
    return {"settings": saved.model_dump(), "weights": saved.describe()}


class PreviewBody(BaseModel):
    proposed: RankingSettings
    request: RecommendRequest


@router.post("/preview")
def post_preview(body: PreviewBody, _: str = Depends(require_admin)):
    if not body.request.profile and not (body.request.text or "").strip():
        raise HTTPException(400, "Describe a test customer first.")
    try:
        with connect() as conn:
            return preview(conn, _provider, body.request, load_ranking(conn), body.proposed)
    except ProviderError as e:
        raise HTTPException(503, str(e))


# ------------------------------------------------------------------ products: priority & campaigns
PRODUCTS_SQL = """
SELECT p.sku, p.name, p.brand, p.category, p.price::float AS price, p.cost::float AS cost,
       COALESCE(p.margin_pct, (p.price - p.cost) / NULLIF(p.price, 0))::float AS margin_pct, p.stock_qty, p.is_active,
       a.suitable_skin_types, a.target_concerns, a.avoid_for, a.min_age, a.max_age,
       COALESCE(a.needs_review, a.sku IS NULL) AS needs_review, a.review_note, a.extracted_by,
       p.embedding IS NOT NULL AS searchable, p.expiry_date, COALESCE(a.text_flags, '{}') AS text_flags,
       EXISTS (SELECT 1 FROM product_ingredients pi WHERE pi.sku = p.sku) AS ingredients_known,
       COALESCE(br.priority, 3) AS priority, COALESCE(br.campaign_boost, 0)::float AS campaign_boost,
       br.valid_from, br.valid_to, COALESCE(br.excluded, FALSE) AS excluded,
       (br.campaign_boost > 0 AND (br.valid_from IS NULL OR br.valid_from <= current_date)
            AND (br.valid_to IS NULL OR br.valid_to >= current_date)) AS boost_active,
       br.updated_by, br.updated_at,
       COALESCE(s.recommended, 0) AS recommended_30d, COALESCE(s.sold, 0) AS sold_30d
FROM products p
LEFT JOIN business_rules br USING (sku)
LEFT JOIN product_attributes a USING (sku)
LEFT JOIN (
    SELECT x->>'sku' AS sku, count(*) AS recommended,
           count(*) FILTER (WHERE l.feedback = 'sold' AND (l.feedback_sku IS NULL OR l.feedback_sku = x->>'sku')) AS sold
    FROM recommendation_log l, jsonb_array_elements(l.picks) x
    WHERE l.created_at > now() - interval '30 days'
    GROUP BY 1
) s ON s.sku = p.sku
ORDER BY p.is_active DESC, p.category, p.sku
"""


@router.get("/products")
def get_products(_: str = Depends(require_admin)):
    with connect() as conn:
        rows = conn.execute(PRODUCTS_SQL).fetchall()
    for r in rows:
        for k in ("valid_from", "valid_to", "updated_at", "expiry_date"):
            r[k] = r[k].isoformat() if r[k] else None
    return {"products": rows, "currency": settings.currency}


class ProductRule(BaseModel):
    priority: int = Field(3, ge=1, le=5)
    campaign_boost: float = Field(0, ge=0, le=0.3)
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    excluded: bool = False

    @model_validator(mode="after")
    def _dates(self):
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("Campaign end date is before its start date.")
        return self


@router.put("/products/{sku}")
def put_product(sku: str, body: ProductRule, name: str = Depends(require_admin)):
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM products WHERE sku = %s", (sku,)).fetchone():
            raise HTTPException(404, f"Unknown SKU {sku}")
        old = conn.execute("SELECT priority, campaign_boost::float AS campaign_boost, valid_from, valid_to, excluded "
                           "FROM business_rules WHERE sku = %s", (sku,)).fetchone()
        conn.execute(
            """INSERT INTO business_rules (sku, priority, campaign_boost, valid_from, valid_to, excluded, updated_by, updated_at)
               VALUES (%(sku)s, %(priority)s, %(campaign_boost)s, %(valid_from)s, %(valid_to)s, %(excluded)s, %(by)s, now())
               ON CONFLICT (sku) DO UPDATE SET priority = EXCLUDED.priority, campaign_boost = EXCLUDED.campaign_boost,
                   valid_from = EXCLUDED.valid_from, valid_to = EXCLUDED.valid_to, excluded = EXCLUDED.excluded,
                   updated_by = EXCLUDED.updated_by, updated_at = now()""",
            {**body.model_dump(), "sku": sku, "by": name},
        )
        audit(conn, name, "product", sku, old, body.model_dump())
    return {"ok": True}


# ------------------------------------------------------------------ activity & feedback
@router.get("/activity")
def get_activity(days: int = 30, _: str = Depends(require_admin)):
    days = max(1, min(days, 365))
    p = {"d": days}
    with connect() as conn:
        k = conn.execute("""
            SELECT count(*) AS recommendations,
                   count(*) FILTER (WHERE feedback IS NOT NULL) AS with_feedback,
                   count(*) FILTER (WHERE feedback = 'accepted') AS accepted,
                   count(*) FILTER (WHERE feedback = 'swapped')  AS swapped,
                   count(*) FILTER (WHERE feedback = 'sold')     AS sold,
                   count(*) FILTER (WHERE feedback = 'rejected') AS rejected,
                   count(*) FILTER (WHERE used_llm) AS used_llm,
                   count(DISTINCT employee_id) AS employees
            FROM recommendation_log WHERE created_at > now() - make_interval(days => %(d)s)""", p).fetchone()
        m = conn.execute("""
            SELECT avg(COALESCE(pr.margin_pct, (pr.price - pr.cost) / NULLIF(pr.price, 0)))::float AS picked_margin
            FROM recommendation_log l, jsonb_array_elements(l.picks) x JOIN products pr ON pr.sku = x->>'sku'
            WHERE l.created_at > now() - make_interval(days => %(d)s)""", p).fetchone()
        c = conn.execute("SELECT avg(COALESCE(margin_pct, (price - cost) / NULLIF(price, 0)))::float AS catalog_margin "
                         "FROM products WHERE is_active").fetchone()
        top = conn.execute("""
            SELECT x->>'sku' AS sku, pr.name, pr.category, count(*) AS recommended,
                   count(*) FILTER (WHERE l.feedback = 'sold'
                                    AND (l.feedback_sku IS NULL OR l.feedback_sku = x->>'sku')) AS sold,
                   count(*) FILTER (WHERE l.feedback = 'rejected') AS rejected,
                   COALESCE(pr.margin_pct, (pr.price - pr.cost) / NULLIF(pr.price, 0))::float AS margin_pct
            FROM recommendation_log l, jsonb_array_elements(l.picks) x JOIN products pr ON pr.sku = x->>'sku'
            WHERE l.created_at > now() - make_interval(days => %(d)s)
            GROUP BY 1, 2, 3, pr.price, pr.cost, pr.margin_pct ORDER BY recommended DESC, sold DESC LIMIT 10""", p).fetchall()
        recent = conn.execute("""
            SELECT id, created_at, employee_id, profile->>'note' AS note, profile->'concerns' AS concerns,
                   (SELECT array_agg(x->>'sku') FROM jsonb_array_elements(picks) x) AS skus,
                   feedback, model, used_llm
            FROM recommendation_log ORDER BY id DESC LIMIT 25""").fetchall()
        changes = conn.execute("SELECT at, actor, area, target, old_value, new_value FROM admin_audit "
                               "ORDER BY id DESC LIMIT 25").fetchall()
    for r in recent:
        r["created_at"] = r["created_at"].isoformat()
    for r in changes:
        r["at"] = r["at"].isoformat()
    return {"days": days, "kpis": {**k, "picked_margin": m["picked_margin"], "catalog_margin": c["catalog_margin"]},
            "top_products": top, "recent": recent, "changes": changes}


# ------------------------------------------------------------------ Excel import
@router.get("/import/template")
def import_template(_: str = Depends(require_admin)):
    return Response(importer.template_xlsx(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="product-import-template.xlsx"'})


class UploadBody(BaseModel):
    filename: str
    content_b64: str = Field(..., max_length=15_000_000)     # ~10 MB file


@router.post("/import/preview")
def import_preview(body: UploadBody, name: str = Depends(require_admin)):
    try:
        data = base64.b64decode(body.content_b64)
        parsed = importer.read_table(body.filename, data, settings.date_order)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Could not read the file ({type(e).__name__}). Save it as .xlsx or .csv and try again.")
    with connect() as conn:
        return importer.create_preview_job(conn, name, body.filename, parsed)


class RunBody(BaseModel):
    deactivate_missing: bool = False
    reanalyze_all: bool = False


@router.post("/import/{job_id}/run")
def import_run(job_id: int, body: RunBody, name: str = Depends(require_admin)):
    with connect() as conn:
        job = conn.execute("SELECT status FROM import_jobs WHERE id = %s", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "Import not found")
        if job["status"] != "preview":
            raise HTTPException(409, "This file was already imported. Upload it again to re-import.")
        conn.execute("UPDATE import_jobs SET options = %s::jsonb WHERE id = %s", (body.model_dump_json(), job_id))
    try:
        importer.start_job(job_id, _provider, name, connect, embed_products)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "job_id": job_id}


def _job_view(r: dict) -> dict:
    if r["status"] == "running" and not importer.job_is_alive(r["id"]):
        r["status"], r["error"] = "failed", "The server restarted during the import. Upload the file again."
    for k in ("created_at", "finished_at"):
        r[k] = r[k].isoformat() if r.get(k) else None
    return r


@router.get("/import/{job_id}")
def import_status(job_id: int, _: str = Depends(require_admin)):
    with connect() as conn:
        r = conn.execute("SELECT id, created_at, created_by, filename, status, summary, options, total, done, results, "
                         "error, finished_at FROM import_jobs WHERE id = %s", (job_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Import not found")
    return _job_view(r)


@router.get("/import")
def import_list(_: str = Depends(require_admin)):
    with connect() as conn:
        rows = conn.execute("SELECT id, created_at, created_by, filename, status, summary, total, done, error, finished_at "
                            "FROM import_jobs WHERE status <> 'preview' ORDER BY id DESC LIMIT 15").fetchall()
    return {"jobs": [_job_view(r) for r in rows], "running": importer.any_running()}


# ------------------------------------------------------------------ product details (what the AI read)
@router.get("/products/{sku}")
def product_detail(sku: str, _: str = Depends(require_admin)):
    with connect() as conn:
        p = conn.execute(
            """SELECT p.sku, p.name, p.brand, p.category, p.price::float AS price, p.cost::float AS cost, p.stock_qty,
                      COALESCE(p.margin_pct, (p.price - p.cost) / NULLIF(p.price, 0))::float AS margin_pct,
                      p.is_active, p.description, p.embedding IS NOT NULL AS searchable,
                      a.suitable_skin_types, a.target_concerns, a.avoid_for, a.min_age, a.max_age, a.texture,
                      a.fragrance_free, a.key_benefits, a.needs_review, a.review_note, a.extracted_by,
                      p.expiry_date, COALESCE(a.text_flags, '{}') AS text_flags
               FROM products p LEFT JOIN product_attributes a USING (sku) WHERE p.sku = %s""", (sku,)).fetchone()
        if not p:
            raise HTTPException(404, f"Unknown SKU {sku}")
        p["ingredients"] = conn.execute(
            "SELECT i.inci_name, i.flags FROM product_ingredients pi JOIN ingredients i ON i.id = pi.ingredient_id "
            "WHERE pi.sku = %s ORDER BY pi.position", (sku,)).fetchall()
    return {"product": p, "vocab": {"categories": CATEGORIES, "skin_types": importer.SKIN_ALL,
                                    "concerns": CONCERNS, "avoid": AVOID}}


class Attributes(BaseModel):
    category: str
    suitable_skin_types: list[str] = []
    target_concerns: list[str] = []
    avoid_for: list[str] = []
    min_age: Optional[int] = Field(None, ge=10, le=100)
    max_age: Optional[int] = Field(None, ge=10, le=100)
    key_benefits: list[str] = []
    expiry_date: Optional[date] = None
    is_active: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.category not in CATEGORIES:
            raise ValueError("Unknown category")
        self.suitable_skin_types = [x for x in self.suitable_skin_types if x in importer.SKIN_ALL]
        self.target_concerns = [x for x in self.target_concerns if x in CONCERNS]
        self.avoid_for = [x for x in self.avoid_for if x in AVOID]
        self.key_benefits = [b.strip()[:140] for b in self.key_benefits if b.strip()][:4]
        if self.min_age and self.max_age and self.min_age > self.max_age:
            raise ValueError("Minimum age is above maximum age")
        return self


@router.put("/products/{sku}/attributes")
def update_attributes(sku: str, body: Attributes, name: str = Depends(require_admin)):
    from .models import ROUTINE_STEP
    with connect() as conn:
        old = conn.execute("SELECT p.category, p.is_active, a.suitable_skin_types, a.target_concerns, a.avoid_for, "
                           "a.min_age, a.max_age, a.key_benefits FROM products p LEFT JOIN product_attributes a "
                           "USING (sku) WHERE p.sku = %s", (sku,)).fetchone()
        if not old:
            raise HTTPException(404, f"Unknown SKU {sku}")
        cur = conn.execute("SELECT texture, fragrance_free FROM product_attributes WHERE sku = %s", (sku,)).fetchone() or {}
        importer.save_attributes(conn, sku, {
            **body.model_dump(exclude={"is_active"}), "routine_step": ROUTINE_STEP[body.category],
            "texture": cur.get("texture"), "fragrance_free": cur.get("fragrance_free", False),
            "needs_review": False, "review_note": None, "extracted_by": "manual",
            "text_flags": []})               # the editor shows text flags as pre-ticked avoid boxes; staff decide
        conn.execute("UPDATE products SET is_active = %s, expiry_date = %s WHERE sku = %s",
                     (body.is_active, body.expiry_date, sku))
        audit(conn, name, "product", sku, old, body.model_dump())
    warning = None
    try:
        embed_products(provider=_provider)
    except (ProviderError, EmbedError) as e:
        warning = f"Saved, but the product is not searchable until the model is reachable: {e}"
    return {"ok": True, "warning": warning}


@router.post("/embed-missing")
def embed_missing(_: str = Depends(require_admin)):
    """Retry making products searchable (e.g. after the model was unreachable during an import)."""
    try:
        return {"ok": True, "embedded": embed_products(provider=_provider)}
    except (ProviderError, EmbedError) as e:
        raise HTTPException(503, str(e))


# ------------------------------------------------------------------ staff accounts (chat app logins)
from . import auth as staff_auth                          # noqa: E402


@router.get("/users")
def list_users(_: str = Depends(require_admin)):
    with connect() as conn:
        rows = conn.execute(
            """SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.must_change_password, u.last_login_at,
                      u.created_at, u.created_by,
                      (SELECT count(*) FROM analytics_events e WHERE e.user_id = u.id AND e.event = 'chat'
                         AND e.at > now() - interval '30 days') AS chats_30d
               FROM staff_users u ORDER BY u.is_active DESC, u.display_name""").fetchall()
    for r in rows:
        for k in ("last_login_at", "created_at"):
            r[k] = r[k].isoformat() if r[k] else None
    return {"users": rows}


class NewUser(BaseModel):
    username: str = Field(..., max_length=40)
    display_name: str = Field(..., max_length=80)
    role: str = Field("staff", pattern="^(staff|manager)$")
    password: Optional[str] = Field(None, max_length=200)


@router.post("/users")
def add_user(body: NewUser, name: str = Depends(require_admin)):
    pw = body.password or staff_auth.temp_password()
    if body.password:
        staff_auth.check_new_password(pw)
    with connect() as conn:
        try:
            u = staff_auth.create_user(conn, body.username, body.display_name, pw, body.role, created_by=name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit(conn, name, "user", u["username"], None, {"display_name": u["display_name"], "role": u["role"]})
    return {"user": {"id": u["id"], "username": u["username"]}, "temporary_password": pw}


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=80)
    role: Optional[str] = Field(None, pattern="^(staff|manager)$")
    is_active: Optional[bool] = None


@router.put("/users/{user_id}")
def update_user(user_id: int, body: UserUpdate, name: str = Depends(require_admin)):
    with connect() as conn:
        old = conn.execute("SELECT username, display_name, role, is_active FROM staff_users WHERE id = %s", (user_id,)).fetchone()
        if not old:
            raise HTTPException(404, "User not found")
        conn.execute(
            """UPDATE staff_users SET display_name = COALESCE(%s, display_name), role = COALESCE(%s, role),
                   is_active = COALESCE(%s, is_active),
                   session_version = session_version + CASE WHEN %s IS FALSE THEN 1 ELSE 0 END
               WHERE id = %s""", (body.display_name, body.role, body.is_active, body.is_active, user_id))
        audit(conn, name, "user", old["username"], old, body.model_dump(exclude_none=True))
    return {"ok": True}


@router.post("/users/{user_id}/reset")
def reset_user_password(user_id: int, name: str = Depends(require_admin)):
    pw = staff_auth.temp_password()
    with connect() as conn:
        u = conn.execute("""UPDATE staff_users SET password_hash = %s, must_change_password = TRUE,
                                session_version = session_version + 1 WHERE id = %s RETURNING username""",
                         (staff_auth.hash_password(pw), user_id)).fetchone()
        if not u:
            raise HTTPException(404, "User not found")
        audit(conn, name, "user", u["username"], None, {"password": "reset; signed out everywhere"})
    return {"temporary_password": pw}


# ------------------------------------------------------------------ analytics (chat usage) + CSV export
@router.get("/analytics")
def analytics(days: int = 30, _: str = Depends(require_admin)):
    d = {"d": max(1, min(days, 365))}
    W = "at > now() - make_interval(days => %(d)s)"
    with connect() as conn:
        q = lambda sql: conn.execute(sql, d).fetchall()
        totals = conn.execute(f"""
            SELECT count(*) FILTER (WHERE event = 'chat') AS chats,
                   count(DISTINCT conversation_id) FILTER (WHERE event = 'chat') AS conversations,
                   count(DISTINCT user_id) FILTER (WHERE event = 'chat') AS active_staff,
                   count(*) FILTER (WHERE event = 'chat' AND intent = 'recommendation' AND status = 'ok') AS recommendations,
                   count(*) FILTER (WHERE event = 'chat' AND intent = 'product_question') AS product_questions,
                   count(*) FILTER (WHERE event = 'chat' AND status = 'no_match') AS no_match,
                   count(*) FILTER (WHERE event = 'chat' AND status = 'error') AS errors,
                   round(avg(latency_ms) FILTER (WHERE event = 'chat'))::int AS avg_ms,
                   (percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE event = 'chat'))::int AS p90_ms,
                   count(*) FILTER (WHERE event = 'chat' AND used_llm) AS ai_answers,
                   count(*) FILTER (WHERE event = 'chat' AND used_llm IS NOT NULL) AS ai_eligible,
                   count(*) FILTER (WHERE event = 'feedback' AND intent = 'sold') AS sold,
                   count(*) FILTER (WHERE event = 'feedback') AS feedback,
                   count(*) FILTER (WHERE event = 'login') AS logins
            FROM analytics_events WHERE {W}""", d).fetchone()
        daily = q(f"""SELECT to_char(date_trunc('day', at), 'YYYY-MM-DD') AS day,
                         count(*) FILTER (WHERE event = 'chat') AS chats,
                         count(*) FILTER (WHERE event = 'chat' AND intent = 'recommendation' AND status = 'ok') AS recommendations,
                         count(*) FILTER (WHERE event = 'feedback' AND intent = 'sold') AS sold
                      FROM analytics_events WHERE {W} GROUP BY 1 ORDER BY 1""")
        staff = q(f"""SELECT username, count(*) FILTER (WHERE event = 'chat') AS chats,
                         count(*) FILTER (WHERE event = 'chat' AND intent = 'recommendation' AND status = 'ok') AS recommendations,
                         count(*) FILTER (WHERE event = 'chat' AND intent = 'product_question') AS questions,
                         count(*) FILTER (WHERE event = 'feedback' AND intent = 'sold') AS sold,
                         to_char(max(at), 'YYYY-MM-DD HH24:MI') AS last_active
                      FROM analytics_events WHERE {W} AND username IS NOT NULL GROUP BY 1 ORDER BY chats DESC LIMIT 50""")
        intents = q(f"SELECT COALESCE(intent, 'other') AS intent, count(*) AS n FROM analytics_events "
                    f"WHERE {W} AND event = 'chat' GROUP BY 1 ORDER BY n DESC")
        concerns = q(f"""SELECT c AS concern, count(*) AS n FROM analytics_events, jsonb_array_elements_text(profile->'concerns') c
                         WHERE {W} AND event = 'chat' AND intent = 'recommendation' GROUP BY 1 ORDER BY n DESC LIMIT 10""")
        skin = q(f"""SELECT COALESCE(profile->>'skin_type', 'not given') AS skin_type, count(*) AS n FROM analytics_events
                     WHERE {W} AND event = 'chat' AND intent = 'recommendation' GROUP BY 1 ORDER BY n DESC""")
        asked = q(f"""SELECT s AS sku, p.name, count(*) AS n FROM analytics_events, unnest(skus) s
                      LEFT JOIN products p ON p.sku = s
                      WHERE {W} AND event = 'chat' AND intent = 'product_question' GROUP BY 1, 2 ORDER BY n DESC LIMIT 10""")
        gaps = q(f"""SELECT to_char(at, 'YYYY-MM-DD HH24:MI') AS at, username, message, profile FROM analytics_events
                     WHERE {W} AND event = 'chat' AND status = 'no_match' ORDER BY at DESC LIMIT 20""")
        errors = q(f"""SELECT to_char(at, 'YYYY-MM-DD HH24:MI') AS at, username, message, meta FROM analytics_events
                       WHERE {W} AND status = 'error' ORDER BY at DESC LIMIT 10""")
    return {"days": d["d"], "totals": totals, "daily": daily, "staff": staff, "intents": intents, "concerns": concerns,
            "skin_types": skin, "asked_products": asked, "no_match": gaps, "errors": errors}


@router.get("/analytics/export.csv")
def analytics_export(days: int = 90, _: str = Depends(require_admin)):
    import csv
    import io
    days = max(1, min(days, 730))
    with connect() as conn:
        rows = conn.execute(
            """SELECT at, event, username, conversation_id, intent, status, message, profile, skus, result_count,
                      latency_ms, used_llm, model, log_id
               FROM analytics_events WHERE at > now() - make_interval(days => %s) ORDER BY at""", (days,)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "event", "staff", "conversation_id", "intent", "status", "message", "skin_type", "sensitive",
                "concerns", "avoid", "age", "budget_max", "categories", "products", "result_count", "latency_ms",
                "used_ai", "model", "recommendation_id"])
    for r in rows:
        p = r["profile"] or {}
        w.writerow([r["at"].isoformat(), r["event"], r["username"], r["conversation_id"], r["intent"], r["status"],
                    (r["message"] or "").replace("\n", " "), p.get("skin_type"), p.get("sensitive"),
                    ";".join(p.get("concerns") or []), ";".join(p.get("avoid") or []), p.get("age"), p.get("budget_max"),
                    ";".join(p.get("categories") or []), ";".join(r["skus"] or []), r["result_count"], r["latency_ms"],
                    r["used_llm"], r["model"], r["log_id"]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv",            # BOM so Excel reads UTF-8
                    headers={"Content-Disposition": f'attachment; filename="recommender-analytics-{days}d.csv"'})
