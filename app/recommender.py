"""The recommendation pipeline: parse -> SQL filters + vector search -> business score -> LLM rerank -> validate."""
from __future__ import annotations

import json
import re
import time

from .config import settings
from .db import fetch_candidates, log_recommendation
from .models import CONCERN_LABEL, CustomerProfile, RecommendRequest
from .parsing import merge_profiles, parse_text
from .providers import Provider, extract_json
from .scoring import pick_diverse, score_candidates
from .strategy import RankingSettings, load_ranking

NO_LIMIT = 1e12


def concern_ingredients(conn, concerns: list[str]) -> list[str]:
    if not concerns:
        return []
    rows = conn.execute(
        "SELECT unnest(helpful_ingredients) AS i FROM skin_concern_map WHERE concern = ANY(%s)", (concerns,)
    ).fetchall()
    return list(dict.fromkeys(r["i"] for r in rows))


def build_query_text(profile: CustomerProfile, helpful: list[str]) -> str:
    parts = []
    if profile.skin_type:
        parts.append(f"skin type: {profile.skin_type}")
    if profile.sensitive:
        parts.append("sensitive skin, gentle, fragrance free")
    if profile.concerns:
        parts.append("concerns: " + ", ".join(CONCERN_LABEL[c] for c in profile.concerns))
    if helpful:
        parts.append("ingredients: " + ", ".join(helpful))
    if profile.note:
        parts.append(profile.note)
    return ". ".join(parts) or "everyday skincare routine"


RANK_SYSTEM = """You are a skincare advisor helping a store employee of a K-beauty brand.
You get a customer profile and a numbered shortlist of products that ALREADY passed all safety,
stock and budget checks. Choose the best {n} products for this customer.

Rules:
- Choose ONLY SKUs from the shortlist. Never invent products, prices or ingredients.
- Prefer a routine: different categories (cleanser, toner, serum, cream, sunscreen) unless the
  customer asked for one category.
- Customer fit comes first. When two products fit about equally, prefer the higher business_score.
- Reasons: one short sentence each, in English, about how the product helps THIS customer,
  mentioning a key ingredient. No medical claims (say "helps with", never "treats" or "cures").
- Return ONLY JSON: {{"picks": [{{"sku": "...", "reason": "..."}}], "routine_tip": "..."}}"""


def _shortlist_for_llm(shortlist: list[dict]) -> str:
    lines = []
    for i, c in enumerate(shortlist, 1):
        lines.append(json.dumps({
            "n": i, "sku": c["sku"], "name": c["name"],
            "category": c["category"], "price": c["price"],
            "targets": c.get("target_concerns"), "key_ingredients": (c.get("key_ingredients") or [])[:4],
            "about": (c.get("description") or "")[:220], "business_score": c["business_score"],
        }))
    return "\n".join(lines)


MEDICAL = [(r"\btreats?\b", "helps with"), (r"\btreating\b", "helping with"), (r"\bcures?\b", "helps with"),
           (r"\bheals?\b", "soothes"), (r"\beliminates?\b", "reduces"), (r"\bclinically proven\b", "formulated")]


def sanitize(text: str) -> str:
    for pat, rep in MEDICAL:
        text = re.sub(pat, rep, text, flags=re.I)
    return text.strip()[:300]


def template_reason(c: dict, profile: CustomerProfile) -> str:
    hits = [x for x in profile.concerns if x in (c.get("target_concerns") or [])]
    ing = " and ".join((c.get("key_ingredients") or [])[:2])
    focus = " and ".join(CONCERN_LABEL[x] for x in hits) or "daily care"
    return f"{c['category'].title()} for {focus}, with {ing}." if ing else f"{c['category'].title()} for {focus}."


def llm_rerank(provider: Provider, profile: CustomerProfile, shortlist: list[dict], n: int, use_llm: bool = True):
    """Returns (picks with reasons, routine_tip, used_llm). Falls back to deterministic picks on any problem."""
    by_sku = {c["sku"]: c for c in shortlist}
    if use_llm:
        system = RANK_SYSTEM.format(n=n)
        user = ("Customer profile:\n" + profile.model_dump_json(exclude={"note"}) +
                (f"\nEmployee note: {profile.note}" if profile.note else "") +
                "\n\nShortlist (one JSON per line):\n" + _shortlist_for_llm(shortlist))
        try:
            data = extract_json(provider.chat(system, user, role="rank")) or {}
        except Exception:
            data = {}
        picks, seen = [], set()
        for p in data.get("picks") or []:
            sku = str(p.get("sku", "")).strip() if isinstance(p, dict) else ""
            if sku in by_sku and sku not in seen:       # drop anything not on the shortlist
                seen.add(sku)
                picks.append({**by_sku[sku], "reason": sanitize(str(p.get("reason") or "")) or template_reason(by_sku[sku], profile)})
            if len(picks) == n:
                break
        if len(picks) >= min(n, len(shortlist), 2):
            if len(picks) < min(n, len(shortlist)):     # top up from the deterministic order
                for c in pick_diverse([c for c in shortlist if c["sku"] not in seen], n - len(picks)):
                    picks.append({**c, "reason": template_reason(c, profile)})
            return picks, sanitize(str(data.get("routine_tip") or "")), True
    picks = [{**c, "reason": template_reason(c, profile)} for c in pick_diverse(shortlist, n)]
    return picks, "", False


def build_profile(provider: Provider, req: RecommendRequest, use_llm: bool) -> CustomerProfile:
    profile = req.profile or CustomerProfile()
    if req.text and req.text.strip():
        parsed = parse_text(provider, req.text, use_llm=use_llm)
        profile = merge_profiles(req.profile, parsed) if req.profile else parsed
    return profile


def retrieve(conn, provider: Provider, profile: CustomerProfile) -> list[dict]:
    """SQL hard filters + vector search. Independent of the ranking strategy."""
    helpful = concern_ingredients(conn, profile.concerns)
    qvec = provider.embed([build_query_text(profile, helpful)], kind="query")[0]
    if len(qvec) != settings.embed_dim:
        raise RuntimeError(f"Embedding size {len(qvec)} != EMBED_DIM {settings.embed_dim}; check your embedding model.")
    return fetch_candidates(
        conn, qvec=qvec, skin=profile.skin_type, sensitive=profile.sensitive,
        avoid=profile.avoid_flags(), cats=profile.categories,
        pmin=profile.budget_min or 0, pmax=profile.budget_max or NO_LIMIT, k=settings.retrieve_k,
    )


def rank(cands: list[dict], profile: CustomerProfile, ranking: RankingSettings) -> list[dict]:
    """Business score with the manager's strategy; drop poor skin matches if a minimum fit is set."""
    scored = score_candidates(cands, profile, ranking.weights())
    if profile.concerns and ranking.min_skin_fit > 0:
        scored = [c for c in scored if c["skin_fit"] >= ranking.min_skin_fit]
    return scored


def recommend(conn, provider: Provider, req: RecommendRequest) -> dict:
    t0 = time.perf_counter()
    ranking = load_ranking(conn)
    profile = build_profile(provider, req, use_llm=ranking.use_llm_rerank)
    scored = rank(retrieve(conn, provider, profile), profile, ranking)
    shortlist = scored[: settings.shortlist_size]

    if not shortlist:
        if conn.execute("SELECT count(*) AS n FROM products WHERE embedding IS NOT NULL").fetchone()["n"] == 0:
            return {"status": "not_ready", "profile": profile.model_dump(),
                    "message": "No products have embeddings yet. Run: python -m app.ingest --embed"}
        return {"status": "no_match", "profile": profile.model_dump(),
                "message": "No in-stock product matches these conditions. Try a wider budget or category."}

    picks, tip, used_llm = llm_rerank(provider, profile, shortlist, req.top_n, use_llm=ranking.use_llm_rerank)
    picks.sort(key=lambda c: c["routine_step"])

    public = [{
        "sku": c["sku"], "name": c["name"], "brand": c.get("brand"), "category": c["category"],
        "routine_step": c["routine_step"], "price": c["price"], "stock_qty": c["stock_qty"],
        "key_ingredients": (c.get("key_ingredients") or [])[:4],
        "reason": c["reason"],
        **({"debug": {k: c[k] for k in ("score", "skin_fit", "similarity", "margin_norm", "priority_norm",
                                         "campaign_boost", "margin_pct")}} if req.debug else {}),
    } for c in picks]

    log_id = log_recommendation(
        conn, employee_id=req.employee_id, request=req.model_dump(exclude={"profile"}),
        profile=profile.model_dump(),
        candidates=[{"sku": c["sku"], "score": c["score"]} for c in shortlist],
        picks=[{"sku": c["sku"], "reason": c["reason"]} for c in picks],
        model=provider.model_label("rank") if used_llm else "fallback", used_llm=used_llm,
    )
    return {
        "status": "ok", "log_id": log_id, "profile": profile.model_dump(), "recommendations": public,
        "routine_tip": tip, "total": round(sum(c["price"] for c in picks), 2), "currency": settings.currency,
        "used_llm": used_llm, "model": provider.model_label("rank") if used_llm else "rule-based fallback",
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def preview(conn, provider: Provider, req: RecommendRequest, current: RankingSettings, proposed: RankingSettings) -> dict:
    """Admin preview: the same customer ranked under two strategies. No LLM, nothing logged."""
    profile = build_profile(provider, req, use_llm=False)
    cands = retrieve(conn, provider, profile)

    def view(r: RankingSettings) -> dict:
        scored = rank(cands, profile, r)
        picks = {c["sku"] for c in pick_diverse(scored[: settings.shortlist_size], req.top_n)}
        return {"weights": r.describe(), "rows": [{
            "rank": i + 1, "sku": c["sku"], "name": c["name"], "category": c["category"], "price": c["price"],
            "score": c["score"], "skin_fit": c["skin_fit"], "margin_pct": round(c["margin_pct"] or 0, 3),
            "priority": c["priority"], "campaign_boost": c["campaign_boost"], "picked": c["sku"] in picks,
        } for i, c in enumerate(scored[: settings.shortlist_size])]}

    return {"profile": profile.model_dump(), "current": view(current), "proposed": view(proposed),
            "currency": settings.currency}
