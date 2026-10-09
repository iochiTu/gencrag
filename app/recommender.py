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
from .scoring import build_steps, score_candidates
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
stock and budget checks. Build a routine of up to {steps} steps for this customer.

Rules:
- One category per step (cleanser, toner, essence, serum, cream, sunscreen, mask). If the customer
  asked for one category, make a single step for it.
- For EACH step give up to {options} options from that category, best first, so the employee can offer
  the customer a choice. Only add an option if it really suits this customer.
- Choose ONLY SKUs from the shortlist. Never invent products, prices, ingredients or claims.
- Base your choice on each product's description, benefits, skin types, ages and ingredients.
- Customer fit comes first. When two products fit about equally, prefer the higher business_score.
- Reason: one short sentence per option, in English, saying why it suits THIS customer and how it
  differs from the other options (e.g. lighter texture, lower price, stronger active).
  No medical claims (say "helps with", never "treats" or "cures").
- Return ONLY JSON:
  {{"steps": [{{"category": "serum", "options": [{{"sku": "...", "reason": "..."}}]}}], "routine_tip": "..."}}"""


def _about(c: dict) -> str:
    d = c.get("description") or ""
    m = re.search(r"(?:full\s+)?(?:ingredients?|inci)(?:\s+list)?\s*[:：\-]", d, re.I)
    return re.sub(r"\s+", " ", d[: m.start()] if m else d).strip()[:320]


def _ages(c: dict) -> str | None:
    lo, hi = c.get("min_age"), c.get("max_age")
    return f"{lo}-{hi}" if lo and hi else (f"{lo}+" if lo else (f"up to {hi}" if hi else None))


def _shortlist_for_llm(shortlist: list[dict]) -> str:
    lines = []
    for i, c in enumerate(shortlist, 1):
        lines.append(json.dumps({k: v for k, v in {
            "n": i, "sku": c["sku"], "name": c["name"], "category": c["category"], "price": c["price"],
            "skin_types": c.get("suitable_skin_types"), "targets": c.get("target_concerns"), "ages": _ages(c),
            "benefits": (c.get("key_benefits") or [])[:3], "key_ingredients": (c.get("key_ingredients") or [])[:5],
            "about": _about(c), "business_score": c["business_score"],
        }.items() if v not in (None, [], "")}))
    return "\n".join(lines)


MEDICAL = [(r"\btreats?\b", "helps with"), (r"\btreating\b", "helping with"), (r"\bcures?\b", "helps with"),
           (r"\bheals?\b", "soothes"), (r"\beliminates?\b", "reduces"), (r"\bclinically proven\b", "formulated")]


def sanitize(text: str) -> str:
    for pat, rep in MEDICAL:
        text = re.sub(pat, rep, text, flags=re.I)
    return text.strip()[:300]


def template_reason(c: dict, profile: CustomerProfile) -> str:
    hits = [x for x in profile.concerns if x in (c.get("target_concerns") or [])]
    focus = " and ".join(CONCERN_LABEL[x] for x in hits)
    benefit = (c.get("key_benefits") or [None])[0]
    if benefit:
        return sanitize(f"For {focus}: {benefit}" if focus else benefit)
    ing = " and ".join((c.get("key_ingredients") or [])[:2])
    focus = focus or "daily care"
    return f"{c['category'].title()} for {focus}, with {ing}." if ing else f"{c['category'].title()} for {focus}."


def _with_reason(c: dict, reason: str | None, profile: CustomerProfile) -> dict:
    return {**c, "reason": sanitize(reason or "") or template_reason(c, profile)}


def llm_rerank(provider: Provider, profile: CustomerProfile, shortlist: list[dict], steps: int, options: int = 3,
               use_llm: bool = True):
    """Returns (steps, routine_tip, used_llm). Each step = {category, routine_step, options:[product + reason]}.
    Anything the model returns that is not on the shortlist is dropped; gaps are filled deterministically."""
    base = build_steps(shortlist, steps, options)
    if use_llm:
        by_sku = {c["sku"]: c for c in shortlist}
        system = RANK_SYSTEM.format(steps=steps, options=options)
        user = ("Customer profile:\n" + profile.model_dump_json(exclude={"note"}) +
                (f"\nEmployee note: {profile.note}" if profile.note else "") +
                "\n\nShortlist (one JSON per line):\n" + _shortlist_for_llm(shortlist))
        try:
            data = extract_json(provider.chat(system, user, role="rank")) or {}
        except Exception:
            data = {}
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) and isinstance(data.get("picks"), list):
            raw_steps = [{"options": [p]} for p in data["picks"]]          # tolerate the older flat format
        out, used = [], set()
        for st in raw_steps or []:
            opts = st.get("options") if isinstance(st, dict) else None
            for o in opts or []:
                sku = str(o.get("sku", "")).strip() if isinstance(o, dict) else ""
                c = by_sku.get(sku)
                if not c or sku in used:
                    continue                                                # unknown or repeated SKU
                step = next((s for s in out if s["category"] == c["category"]), None)
                if step is None:
                    if len(out) >= steps:
                        continue
                    step = {"category": c["category"], "routine_step": c["routine_step"], "options": []}
                    out.append(step)
                if len(step["options"]) < options:
                    step["options"].append(_with_reason(c, o.get("reason"), profile))
                    used.add(sku)
        if out:
            for b in base:                                                  # top up from the deterministic plan
                step = next((s for s in out if s["category"] == b["category"]), None)
                if step is None and len(out) < steps:
                    step = {"category": b["category"], "routine_step": b["routine_step"], "options": []}
                    out.append(step)
                if step is None:
                    continue
                for c in b["options"]:
                    if len(step["options"]) >= options:
                        break
                    if c["sku"] not in used:
                        step["options"].append(_with_reason(c, None, profile))
                        used.add(c["sku"])
            out.sort(key=lambda s: s["routine_step"])
            return out, sanitize(str(data.get("routine_tip") or "")), True
    for b in base:
        b["options"] = [_with_reason(c, None, profile) for c in b["options"]]
    return base, "", False


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

    n_options = req.options or ranking.options_per_step
    steps, tip, used_llm = llm_rerank(provider, profile, shortlist, req.top_n, n_options, use_llm=ranking.use_llm_rerank)

    def public(c: dict, rank_in_step: int) -> dict:
        return {
            "sku": c["sku"], "name": c["name"], "brand": c.get("brand"), "category": c["category"],
            "routine_step": c["routine_step"], "price": c["price"], "stock_qty": c["stock_qty"],
            "key_ingredients": (c.get("key_ingredients") or [])[:5],
            "key_benefits": [b for b in (c.get("key_benefits") or []) if b not in c["reason"]][:3],
            "ages": _ages(c), "option": rank_in_step, "reason": c["reason"],
            **({"debug": {k: c[k] for k in ("score", "skin_fit", "similarity", "margin_norm", "priority_norm",
                                             "campaign_boost", "margin_pct")}} if req.debug else {}),
        }

    out_steps = [{"category": s["category"], "routine_step": s["routine_step"],
                  "options": [public(c, i + 1) for i, c in enumerate(s["options"])]} for s in steps]
    log_id = log_recommendation(
        conn, employee_id=req.employee_id, request=req.model_dump(exclude={"profile"}),
        profile=profile.model_dump(),
        candidates=[{"sku": c["sku"], "score": c["score"]} for c in shortlist],
        picks=[{"sku": o["sku"], "reason": o["reason"], "step": s["category"], "option": o["option"]}
               for s in out_steps for o in s["options"]],
        model=provider.model_label("rank") if used_llm else "fallback", used_llm=used_llm,
    )
    return {
        "status": "ok", "log_id": log_id, "profile": profile.model_dump(), "steps": out_steps,
        "recommendations": [s["options"][0] for s in out_steps],          # first choice of each step
        "routine_tip": tip, "total": round(sum(s["options"][0]["price"] for s in out_steps), 2),
        "currency": settings.currency, "options_per_step": n_options,
        "used_llm": used_llm, "model": provider.model_label("rank") if used_llm else "rule-based fallback",
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def preview(conn, provider: Provider, req: RecommendRequest, current: RankingSettings, proposed: RankingSettings) -> dict:
    """Admin preview: the same customer ranked under two strategies. No LLM, nothing logged."""
    profile = build_profile(provider, req, use_llm=False)
    cands = retrieve(conn, provider, profile)

    def view(r: RankingSettings) -> dict:
        scored = rank(cands, profile, r)
        plan = build_steps(scored[: settings.shortlist_size], req.top_n, req.options or r.options_per_step)
        picks = {c["sku"]: ("first" if i == 0 else "alt") for s in plan for i, c in enumerate(s["options"])}
        return {"weights": r.describe(), "rows": [{
            "rank": i + 1, "sku": c["sku"], "name": c["name"], "category": c["category"], "price": c["price"],
            "score": c["score"], "skin_fit": c["skin_fit"], "margin_pct": None if c["margin_pct"] is None else round(c["margin_pct"], 3),
            "priority": c["priority"], "campaign_boost": c["campaign_boost"], "picked": picks.get(c["sku"]),
        } for i, c in enumerate(scored[: settings.shortlist_size])]}

    return {"profile": profile.model_dump(), "current": view(current), "proposed": view(proposed),
            "currency": settings.currency}
