"""Admin portal API (/admin/api/*): ranking strategy, product priorities & campaigns, activity.

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

from .config import settings
from .db import connect
from .models import RecommendRequest
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
       ((p.price - p.cost) / NULLIF(p.price, 0))::float AS margin_pct, p.stock_qty, p.is_active,
       COALESCE(br.priority, 3) AS priority, COALESCE(br.campaign_boost, 0)::float AS campaign_boost,
       br.valid_from, br.valid_to, COALESCE(br.excluded, FALSE) AS excluded,
       (br.campaign_boost > 0 AND (br.valid_from IS NULL OR br.valid_from <= current_date)
            AND (br.valid_to IS NULL OR br.valid_to >= current_date)) AS boost_active,
       br.updated_by, br.updated_at,
       COALESCE(s.recommended, 0) AS recommended_30d, COALESCE(s.sold, 0) AS sold_30d
FROM products p
LEFT JOIN business_rules br USING (sku)
LEFT JOIN (
    SELECT x->>'sku' AS sku, count(*) AS recommended, count(*) FILTER (WHERE l.feedback = 'sold') AS sold
    FROM recommendation_log l, jsonb_array_elements(l.picks) x
    WHERE l.created_at > now() - interval '30 days'
    GROUP BY 1
) s ON s.sku = p.sku
ORDER BY p.category, p.sku
"""


@router.get("/products")
def get_products(_: str = Depends(require_admin)):
    with connect() as conn:
        rows = conn.execute(PRODUCTS_SQL).fetchall()
    for r in rows:
        for k in ("valid_from", "valid_to", "updated_at"):
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
            SELECT avg(((pr.price - pr.cost) / NULLIF(pr.price, 0)))::float AS picked_margin
            FROM recommendation_log l, jsonb_array_elements(l.picks) x JOIN products pr ON pr.sku = x->>'sku'
            WHERE l.created_at > now() - make_interval(days => %(d)s)""", p).fetchone()
        c = conn.execute("SELECT avg(((price - cost) / NULLIF(price, 0)))::float AS catalog_margin "
                         "FROM products WHERE is_active").fetchone()
        top = conn.execute("""
            SELECT x->>'sku' AS sku, pr.name, pr.category, count(*) AS recommended,
                   count(*) FILTER (WHERE l.feedback = 'sold') AS sold,
                   count(*) FILTER (WHERE l.feedback = 'rejected') AS rejected,
                   ((pr.price - pr.cost) / NULLIF(pr.price, 0))::float AS margin_pct
            FROM recommendation_log l, jsonb_array_elements(l.picks) x JOIN products pr ON pr.sku = x->>'sku'
            WHERE l.created_at > now() - make_interval(days => %(d)s)
            GROUP BY 1, 2, 3, pr.price, pr.cost ORDER BY recommended DESC, sold DESC LIMIT 10""", p).fetchall()
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
