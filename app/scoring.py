"""Deterministic business scoring. The LLM never sets margin or priority; this module does."""
from __future__ import annotations

from dataclasses import dataclass

from .models import CustomerProfile


@dataclass(frozen=True)
class Weights:
    """Default = the 'Balanced' preset. Managers change it in the admin portal (app/strategy.py)."""
    fit: float = 0.455
    sim: float = 0.195
    margin: float = 0.20
    prio: float = 0.15


def skin_fit(profile: CustomerProfile, cand: dict) -> float:
    """Share of the customer's concerns this product targets (0-1), small bonus for fragrance-free on sensitive skin."""
    if profile.concerns:
        fit = len(set(profile.concerns) & set(cand.get("target_concerns") or [])) / len(profile.concerns)
    else:
        fit = 0.5
    if profile.sensitive and cand.get("fragrance_free"):
        fit += 0.1
    lo, hi = cand.get("min_age"), cand.get("max_age")
    if profile.age and ((lo and profile.age < lo) or (hi and profile.age > hi)):
        fit *= 0.5                                   # outside the stated age range: allowed, but ranked lower
    return min(fit, 1.0)


def score_candidates(cands: list[dict], profile: CustomerProfile, w: Weights | None = None) -> list[dict]:
    w = w or Weights()
    if not cands:
        return []
    known = [float(c["margin_pct"]) for c in cands if c.get("margin_pct") is not None]
    lo, hi = (min(known), max(known)) if known else (0.0, 0.0)
    out = []
    for c in cands:
        m = c.get("margin_pct")
        fit = skin_fit(profile, c)
        sim = max(0.0, min(1.0, float(c.get("similarity") or 0)))
        # Neither direct margin nor cost supplied -> neutral (neither pushed nor penalised).
        margin_n = 0.5 if m is None or hi <= lo else (float(m) - lo) / (hi - lo)
        prio_n = (int(c.get("priority") or 3) - 1) / 4
        boost = float(c.get("campaign_boost") or 0)
        score = w.fit * fit + w.sim * sim + w.margin * margin_n + w.prio * prio_n + boost
        out.append({**c,
                    "skin_fit": round(fit, 3), "similarity": round(sim, 3),
                    "margin_norm": round(margin_n, 3), "priority_norm": round(prio_n, 3),
                    "business_score": round((w.margin * margin_n + w.prio * prio_n) / (w.margin + w.prio)
                                            + boost, 3) if w.margin + w.prio else round(boost, 3),
                    "score": round(score, 4)})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def pick_diverse(scored: list[dict], n: int) -> list[dict]:
    """Best item per category first (builds a routine), then fill by score."""
    picks, seen = [], set()
    for c in scored:
        if c["category"] not in seen:
            picks.append(c)
            seen.add(c["category"])
        if len(picks) == n:
            return picks
    for c in scored:
        if len(picks) == n:
            break
        if c not in picks:
            picks.append(c)
    return picks


def build_steps(scored: list[dict], steps: int, options: int, min_ratio: float = 0.6) -> list[dict]:
    """Deterministic routine: one step per category (best categories first), each with up to `options`
    products from that category. An alternative is offered only if it scores at least `min_ratio` of the
    step's best product, so staff are not shown weak fillers. Steps are returned in routine order."""
    out: list[dict] = []
    for c in scored:
        step = next((s for s in out if s["category"] == c["category"]), None)
        if step is None:
            if len(out) < steps:
                out.append({"category": c["category"], "routine_step": c["routine_step"], "options": [c]})
        elif len(step["options"]) < options and c["score"] >= step["options"][0]["score"] * min_ratio:
            step["options"].append(c)
    return sorted(out, key=lambda s: s["routine_step"])
