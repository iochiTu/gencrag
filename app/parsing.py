"""Turn the employee's free-text note into a CustomerProfile.

Two layers: a deterministic keyword parser (always runs, safe), plus an LLM parse that
catches phrasing the keywords miss. Safety-relevant fields (avoid list) are UNIONED, so
the LLM can add a restriction but never remove one. Budgets come from the regex when found.
"""
from __future__ import annotations

import re

from .models import AVOID, CATEGORIES, CONCERNS, SKIN_TYPES, CustomerProfile
from .providers import Provider, extract_json

SKIN_KW = {
    "combination": ["combination", "combo skin", "t-zone", "t zone", "tzone"],
    "oily": ["oily skin", "oily", "greasy skin"],
    "dry": ["dry skin", "dry"],
    "normal": ["normal skin"],
}
SENSITIVE_KW = ["sensitive", "reactive", "easily irritated"]
CONCERN_KW = {
    "acne": ["acne", "breakout", "pimple", "blemish", "spots and bumps", "zits"],
    "dryness": ["dryness", "dehydrat", "flaky", "flaking", "feels tight", "tightness", "parched", "dry patches"],
    "redness": ["redness", "red ", "rosacea", "irritat", "flushing"],
    "pigmentation": ["dark spot", "pigment", "melasma", "sun spot", "uneven tone", "uneven skin tone", "acne marks"],
    "wrinkles": ["wrinkle", "fine line", "aging", "ageing", "firm", "sagging", "elasticity"],
    "pores": ["pore", "blackhead"],
    "dullness": ["dull", "glow", "radiance", "brighten", "tired-looking"],
    "oiliness": ["oiliness", "sebum", "shine", "shiny", "excess oil"],
}
AVOID_KW = {
    "pregnancy": ["pregnan", "breastfeed", "nursing", "expecting"],
    "fragrance": ["fragrance", "perfume", "scent"],
    "retinoid": ["retinol", "retinoid", "retinal", "tretinoin"],
    "aha": ["no aha", "avoid aha", "aha allergy", "glycolic allergy"],
    "bha": ["no bha", "avoid bha", "bha allergy", "salicylic allergy", "aspirin allergy"],
    "alcohol": ["alcohol"],
    "essential_oil": ["essential oil", "tea tree allergy"],
}
CATEGORY_KW = {
    "cleanser": ["cleanser", "face wash", "cleansing"],
    "toner": ["toner"],
    "essence": ["essence"],
    "serum": ["serum", "ampoule"],
    "cream": ["cream", "moisturi", "lotion"],
    "sunscreen": ["sunscreen", "sun cream", "spf", "sunblock"],
    "mask": ["mask"],
}

# "dry" / "oily" also appear inside concern words; only treat them as skin type in these phrasings
_SKIN_PHRASE = re.compile(r"\b(dry|oily|combination|normal)\b(?=\s*(skin|skinned|type|,|\.|$|and|with|but))")


def _has(text: str, words: list[str]) -> bool:
    return any(w in text for w in words)


_AMOUNT = r"[$€£]?\s*(\d+(?:\.\d+)?)\s*(k)?\s*(?:usd|cad|eur|gbp|krw|won|dollars?|bucks)?"


def _num(m: re.Match) -> float:
    return float(m.group(1)) * (1000 if m.group(2) else 1)


def parse_budget(text: str) -> tuple[float | None, float | None]:
    """'under $30' -> (None, 30); '$20-40' -> (20, 40); 'at least 25 dollars' -> (25, None)."""
    t = text.lower().replace(",", "")
    m = re.search(_AMOUNT + r"\s*(?:-|to|–)\s*" + _AMOUNT, t)
    if m and (re.search(r"[$€£]|usd|cad|eur|gbp|krw|won|dollar|bucks|budget|price", t)):
        lo = float(m.group(1)) * (1000 if m.group(2) else 1)
        hi = float(m.group(3)) * (1000 if m.group(4) else 1)
        return min(lo, hi), max(lo, hi)
    m = re.search(r"(under|below|less than|max(?:imum)?|up to|no more than|budget(?: of| is)?|<)\s*" + _AMOUNT, t)
    if m:
        return None, float(m.group(2)) * (1000 if m.group(3) else 1)
    m = re.search(r"(over|above|at least|min(?:imum)?|more than|>)\s*" + _AMOUNT, t)
    if m:
        return float(m.group(2)) * (1000 if m.group(3) else 1), None
    m = re.search(r"[$€£]\s*(\d+(?:\.\d+)?)(k)?|(\d+(?:\.\d+)?)(k)?\s*(?:usd|cad|eur|gbp|krw|won|dollars?|bucks)\b", t)
    if m:
        val = float(m.group(1) or m.group(3)) * (1000 if (m.group(2) or m.group(4)) else 1)
        return None, val
    return None, None


def parse_age(text: str) -> int | None:
    """'35 years old' -> 35, 'in her 40s' -> 45, 'aged 28' -> 28, 'teenager' -> 16."""
    t = text.lower()
    m = re.search(r"\b(\d{2})\s*(?:years?\s*old|y/?o|yrs?\b)|\bage[d]?\s*(\d{2})\b", t)
    if m:
        return int(m.group(1) or m.group(2))
    m = re.search(r"\b(?:early|mid|late)?\s*-?\s*(\d)0s\b", t)
    if m and 1 <= int(m.group(1)) <= 8:
        base = int(m.group(1)) * 10
        return base + (2 if "early" in m.group(0) else 8 if "late" in m.group(0) else 5)
    if re.search(r"\bteen(ager)?s?\b", t):
        return 16
    return None


def _skin_type(t: str) -> str | None:
    if _has(t, SKIN_KW["combination"]):
        return "combination"
    m = _SKIN_PHRASE.search(t)
    return m.group(1) if m else None


def _categories(t: str) -> list[str]:
    # "sun cream" contains "cream": check cream on the text with sunscreen words removed
    no_sun = re.sub(r"sun\s*cream|sunscreen", " ", t)
    return [c for c, ws in CATEGORY_KW.items() if _has(no_sun if c == "cream" else t, ws)]


def keyword_parse(text: str) -> CustomerProfile:
    t = " " + text.lower() + " "
    lo, hi = parse_budget(text)
    return CustomerProfile(
        skin_type=_skin_type(t),
        sensitive=_has(t, SENSITIVE_KW),
        concerns=[c for c, ws in CONCERN_KW.items() if _has(t, ws)],
        avoid=[a for a, ws in AVOID_KW.items() if _has(t, ws)],
        budget_min=lo or 0,
        budget_max=hi,
        categories=_categories(t),
        age=parse_age(text),
        note=text,
    )


PARSE_SYSTEM = f"""You convert a beauty-store employee's note about a customer into JSON.
Return ONLY a JSON object with these keys:
  "skin_type": one of {SKIN_TYPES} or null
  "sensitive": true/false
  "concerns": list from {CONCERNS}
  "avoid": list from {AVOID} (pregnancy if pregnant or breastfeeding)
  "budget_min": number or null
  "budget_max": number or null (max price per product)
  "categories": list from {CATEGORIES}, empty if the customer wants a full routine
  "age": integer or null (customer's age if stated; "in her 30s" -> 35)
Use only the listed values. Do not guess things the note does not say."""


def llm_parse(provider: Provider, text: str) -> dict | None:
    try:
        return extract_json(provider.chat(PARSE_SYSTEM, text, role="parse"))
    except Exception:
        return None


def parse_text(provider: Provider, text: str, use_llm: bool = True) -> CustomerProfile:
    base = keyword_parse(text)
    data = llm_parse(provider, text) if use_llm else None
    if not data:
        return base
    try:
        llm = CustomerProfile(
            skin_type=data.get("skin_type") if data.get("skin_type") in SKIN_TYPES else None,
            sensitive=bool(data.get("sensitive")),
            concerns=data.get("concerns") or [],
            avoid=data.get("avoid") or [],
            categories=data.get("categories") or [],
        )
    except Exception:
        return base
    return base.model_copy(update={
        "skin_type": base.skin_type or llm.skin_type,
        "sensitive": base.sensitive or llm.sensitive,
        "concerns": list(dict.fromkeys(base.concerns + llm.concerns)),
        "avoid": list(dict.fromkeys(base.avoid + llm.avoid)),          # union: never drop a restriction
        "categories": base.categories or llm.categories,
        "budget_min": base.budget_min or _num_or_none(data.get("budget_min")) or 0,
        "budget_max": base.budget_max or _num_or_none(data.get("budget_max")),
        "age": base.age or _age_or_none(data.get("age")),
    })


def merge_profiles(form: CustomerProfile, parsed: CustomerProfile) -> CustomerProfile:
    """Form fields win; parsed text fills the gaps; avoid lists are unioned."""
    return form.model_copy(update={
        "skin_type": form.skin_type or parsed.skin_type,
        "sensitive": form.sensitive or parsed.sensitive,
        "concerns": list(dict.fromkeys(form.concerns + parsed.concerns)),
        "avoid": list(dict.fromkeys(form.avoid + parsed.avoid)),
        "categories": form.categories or parsed.categories,
        "budget_min": form.budget_min or parsed.budget_min,
        "budget_max": form.budget_max or parsed.budget_max,
        "age": form.age or parsed.age,
        "note": " ".join(x for x in [form.note, parsed.note] if x),
    })


def _age_or_none(v) -> int | None:
    try:
        v = int(v)
        return v if 10 <= v <= 100 else None
    except (TypeError, ValueError):
        return None


def _num_or_none(v) -> float | None:
    try:
        v = float(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None
