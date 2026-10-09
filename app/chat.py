"""Chat API for signed-in staff (/api/chat, /api/conversations).

Each conversation remembers what staff have said about the current customer (skin type, concerns, things
to avoid, age, budget, categories) and refines it with every message:
  "combination skin, breakouts, under $30"  -> recommendation (routine steps with several choices)
  "she's also pregnant" / "cheaper" / "only serums" / "more choices"  -> refined recommendation
  "is the Cica Repair Ampoule OK for sensitive skin?"  -> answer grounded in that product's data
  "new customer"  -> forget the profile
Every turn is written to analytics_events for reporting.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .analytics import track
from .auth import require_staff
from .config import settings
from .db import connect, save_feedback
from .models import CONCERN_LABEL, CustomerProfile, RecommendRequest
from .parsing import parse_text
from .providers import ProviderError, extract_json
from .recommender import recommend, sanitize
from .strategy import load_ranking

router = APIRouter(prefix="/api", tags=["chat"])
log = logging.getLogger("chat")
_provider = None


def set_provider(p) -> None:
    global _provider
    _provider = p


RESET_RE = re.compile(r"^\s*(new customer|next customer|another customer|start over|reset|clear)\b", re.I)
GREETING_RE = re.compile(r"^\s*(hi|hello|hey|yo|good (morning|afternoon|evening)|thanks?|thank you|ok|okay)\b[\s!.,]*$", re.I)
QUESTION_RE = re.compile(r"\?|^\s*(what|how|is|are|can|could|does|do|which|why|when|should|tell me|explain|compare)\b|"
                         r"\b(difference|differ|compare|versus|vs\.?|ingredients?|safe|how to use|suitable|good for|"
                         r"use with|together)\b", re.I)
CHEAPER_RE = re.compile(r"\b(cheaper|less expensive|lower price|more affordable|budget option|too expensive|cheapest)\b", re.I)
MORE_RE = re.compile(r"\b(more (options|choices|products)|other options|alternatives|show more)\b", re.I)
FEWER_RE = re.compile(r"\b(fewer (options|choices)|just one|only one (option|choice)|single option)\b", re.I)
FULL_ROUTINE_RE = re.compile(r"\b(full routine|whole routine|all steps|complete routine|any category)\b", re.I)
NO_BUDGET_RE = re.compile(r"\b(no budget|any price|price doesn'?t matter|no (price )?limit|remove (the )?budget)\b", re.I)
NEG_RE = re.compile(r"\b(not|no longer|isn'?t|is not|not anymore|never)\s+(pregnant|nursing|breast-?feeding|sensitive)", re.I)
GENERIC = {"the", "a", "an", "and", "or", "for", "with", "of", "in", "to", "by", "ml", "spf", "spf50", "new"}
CATEGORY_PLURAL = {"cleanser": "cleansers", "toner": "toners", "essence": "essences", "serum": "serums",
                   "cream": "moisturizers", "sunscreen": "sunscreens", "mask": "masks", "other": "other products"}


# ------------------------------------------------------------------ helpers
def money(v) -> str:
    try:
        return f"${float(v):,.2f}" if settings.currency in ("USD", "CAD", "AUD") else f"{float(v):,.2f} {settings.currency}"
    except (TypeError, ValueError):
        return "-"


def describe(p: CustomerProfile) -> str:
    parts = []
    if p.skin_type:
        parts.append(f"{p.skin_type} skin")
    if p.sensitive:
        parts.append("sensitive")
    if p.concerns:
        parts.append(" & ".join(CONCERN_LABEL[c] for c in p.concerns))
    if p.age:
        parts.append(f"age {p.age}")
    if p.avoid:
        parts.append("avoiding " + ", ".join(a.replace("_", " ").replace("pregnancy", "pregnancy-unsafe") for a in p.avoid))
    if p.budget_max:
        parts.append(f"up to {money(p.budget_max)} each")
    if p.categories:
        parts.append(" & ".join(CATEGORY_PLURAL.get(c, c) for c in p.categories) + " only")
    return ", ".join(parts)


def profile_chips(p: CustomerProfile) -> list[str]:
    out = []
    if p.skin_type:
        out.append(p.skin_type.title() + " skin")
    if p.sensitive:
        out.append("Sensitive")
    out += [CONCERN_LABEL[c].capitalize() for c in p.concerns]
    if p.age:
        out.append(f"Age {p.age}")
    out += ["No " + a.replace("_", " ") if a != "pregnancy" else "Pregnancy-safe" for a in p.avoid]
    if p.budget_max:
        out.append(f"≤ {money(p.budget_max)}")
    out += [CATEGORY_PLURAL.get(c, c).capitalize() + " only" for c in p.categories]
    return out


def merge_chat(old: CustomerProfile, new: CustomerProfile, message: str, state: dict) -> CustomerProfile:
    neg = {m.group(2).lower().replace("-", "") for m in NEG_RE.finditer(message)}
    avoid = [a for a in dict.fromkeys(old.avoid + new.avoid)
             if not (a == "pregnancy" and neg & {"pregnant", "nursing", "breastfeeding"})]
    sensitive = (old.sensitive or new.sensitive) and "sensitive" not in neg
    budget_min, budget_max = old.budget_min, old.budget_max
    if NO_BUDGET_RE.search(message):
        budget_min, budget_max = 0, None
    elif new.budget_max or new.budget_min:
        budget_min, budget_max = new.budget_min or 0, new.budget_max
    elif CHEAPER_RE.search(message) and state.get("last_prices"):
        cheapest = min(state["last_prices"])
        budget_max = round(min(budget_max or 1e9, max(state["last_prices"])) * 0.75, 2)
        budget_max = max(budget_max, round(cheapest * 0.9, 2))
    categories = [] if FULL_ROUTINE_RE.search(message) else (new.categories or old.categories)
    note = (old.note + " | " + message if old.note else message)[-600:]
    return CustomerProfile(skin_type=new.skin_type or old.skin_type, sensitive=sensitive,
                           concerns=list(dict.fromkeys(old.concerns + new.concerns)), avoid=avoid,
                           budget_min=budget_min, budget_max=budget_max, categories=categories,
                           age=new.age or old.age, note=note)


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in GENERIC and not t.isdigit()]


def match_products(conn, message: str, limit: int = 3) -> list[str]:
    """SKUs or product names mentioned in the message (fuzzy on name words). A weaker match whose matched
    words are all explained by better matches is dropped ("Cica Repair Ampoule vs Spot Serum" must not
    also match "Retinol Repair Serum")."""
    msg, mt = message.lower(), set(_tokens(message))
    hits = []
    for r in conn.execute("SELECT sku, name FROM products WHERE is_active").fetchall():
        if re.search(rf"(?<![\w-]){re.escape(r['sku'].lower())}(?![\w-])", msg):
            hits.append((2.0, r["sku"], set()))
            continue
        nt = _tokens(r["name"])
        matched = {t for t in nt if t in mt}
        m = len(matched)
        if nt and ((m >= 2 and m / len(nt) >= 0.6) or (m == len(nt) and m >= 1)):
            hits.append((m / len(nt) + m * 0.01, r["sku"], matched))
    hits.sort(key=lambda h: h[0], reverse=True)
    out, covered = [], set()
    for score, sku, matched in hits:
        if matched and matched <= covered and score < 1:
            continue
        out.append(sku)
        covered |= matched
        if len(out) == limit:
            break
    return out


def product_facts(conn, skus: list[str]) -> list[dict]:
    rows = conn.execute(
        """SELECT p.sku, p.name, p.brand, p.category, p.price::float AS price, p.stock_qty, p.expiry_date, p.description,
                  a.suitable_skin_types, a.target_concerns, a.avoid_for, COALESCE(a.text_flags, '{}') AS text_flags,
                  a.min_age, a.max_age, a.key_benefits,
                  (SELECT array_agg(i.inci_name ORDER BY pi.position) FROM product_ingredients pi
                     JOIN ingredients i ON i.id = pi.ingredient_id WHERE pi.sku = p.sku) AS ingredients,
                  (SELECT array_agg(DISTINCT f) FROM product_ingredients pi JOIN ingredients i ON i.id = pi.ingredient_id,
                     unnest(i.flags) f WHERE pi.sku = p.sku) AS flags
           FROM products p LEFT JOIN product_attributes a USING (sku) WHERE p.sku = ANY(%s)""", (skus,)).fetchall()
    order = {s: i for i, s in enumerate(skus)}
    return sorted(rows, key=lambda r: order.get(r["sku"], 99))


def _ages(r: dict) -> str:
    lo, hi = r.get("min_age"), r.get("max_age")
    return f"ages {lo}-{hi}" if lo and hi else (f"ages {lo}+" if lo else (f"up to age {hi}" if hi else ""))


def product_card(r: dict) -> dict:
    flags = sorted(set((r.get("flags") or []) + (r.get("text_flags") or [])))
    notes = []
    if not r.get("ingredients"):
        notes.append("Ingredient list not on file: check the label")
    if r.get("expiry_date"):
        notes.append(f"Expiry {r['expiry_date'].isoformat()}")
    return {"sku": r["sku"], "name": r["name"], "brand": r.get("brand"), "category": r["category"], "price": r["price"],
            "ages": _ages(r) or None, "key_benefits": (r.get("key_benefits") or [])[:3],
            "key_ingredients": (r.get("ingredients") or [])[:6], "flags": flags, "notes": notes,
            "skin_types": r.get("suitable_skin_types") or [], "concerns": r.get("target_concerns") or []}


QA_SYSTEM = """You help a K-beauty store employee answer a question about specific products.
Use ONLY the product facts given. If the facts do not answer the question, say what is not on file and
suggest checking the label or asking a manager. Be concise (at most 120 words), practical and friendly.
No medical claims: say "helps with", never "treats" or "cures". For pregnancy or allergy questions, point
out relevant ingredients/flags from the facts and suggest the customer checks with their doctor if unsure.
Return ONLY JSON: {"answer": "..."}"""


def _facts_for_llm(rows: list[dict]) -> str:
    out = []
    for r in rows:
        out.append(json.dumps({k: v for k, v in {
            "sku": r["sku"], "name": r["name"], "category": r["category"], "price": r["price"],
            "skin_types": r.get("suitable_skin_types"), "helps_with": r.get("target_concerns"), "ages": _ages(r),
            "not_for": r.get("avoid_for"), "flags": sorted(set((r.get("flags") or []) + (r.get("text_flags") or []))),
            "benefits": r.get("key_benefits"), "ingredients": r.get("ingredients") or "not on file",
            "description": (r.get("description") or "")[:900], "in_stock": r.get("stock_qty"),
        }.items() if v not in (None, [], "")}, default=str))
    return "\n".join(out)


def rule_answer(rows: list[dict], question: str) -> str:
    lines = []
    for r in rows:
        bits = [f"**{r['name']}** ({r['sku']}, {money(r['price'])}) is a {r['category']}"]
        if r.get("suitable_skin_types"):
            bits.append(f" for {', '.join(r['suitable_skin_types'])} skin")
        if r.get("target_concerns"):
            bits.append(f"; it helps with {', '.join(CONCERN_LABEL.get(c, c) for c in r['target_concerns'])}")
        line = "".join(bits) + "."
        if _ages(r):
            line += f" Suited to {_ages(r)}."
        flags = sorted(set((r.get("flags") or []) + (r.get("text_flags") or [])))
        if "pregnancy" in (r.get("avoid_for") or []) or "retinoid" in flags:
            line += " Not recommended during pregnancy or breastfeeding."
        if flags:
            line += f" Contains: {', '.join(f.replace('_', ' ') for f in flags)}."
        if not r.get("ingredients"):
            line += " The ingredient list is not on file, so check the label."
        lines.append(line)
    if len(rows) == 2 and re.search(r"differ|compare|versus|\bvs\b|better|which", question, re.I):
        a, b = rows
        diff = abs(a["price"] - b["price"])
        if diff >= 0.01:
            cheaper = a if a["price"] < b["price"] else b
            lines.append(f"{cheaper['name']} is {money(diff)} cheaper.")
        only_a = set(a.get("target_concerns") or []) - set(b.get("target_concerns") or [])
        only_b = set(b.get("target_concerns") or []) - set(a.get("target_concerns") or [])
        if only_a or only_b:
            lines.append("Main difference: " + "; ".join(
                f"{x['name']} also targets {', '.join(CONCERN_LABEL.get(c, c) for c in o)}"
                for x, o in ((a, only_a), (b, only_b)) if o) + ".")
    return "\n\n".join(lines)


def suggestions(p: CustomerProfile, state: dict, steps: list[dict] | None = None) -> list[str]:
    if not (p.skin_type or p.concerns or p.categories):
        return ["Dry skin", "Oily skin", "Combination skin", "Breakouts", "Dryness", "Fine lines"]
    out = []
    if steps and any(st["options"] for st in steps):
        out.append("Cheaper options")
    if (state.get("options") or 3) < 4:
        out.append("More choices")
    if not p.skin_type:
        out += ["Dry skin", "Oily skin"]
    if not p.sensitive:
        out.append("She has sensitive skin")
    if "pregnancy" not in p.avoid:
        out.append("She is pregnant")
    out.append("Full routine" if p.categories else "Only serums")
    out.append("New customer")
    return out[:6]


WELCOME = ("Tell me about the customer and I'll suggest products, best match first with a few choices per step. "
           "For example: *Combination skin, breakouts and redness, 24, under $30*.\n\n"
           "You can also ask about a product, like *Is the Retinol Repair Serum OK during pregnancy?*")


# ------------------------------------------------------------------ one chat turn
def answer(conn, provider, user: dict, conv: dict, message: str) -> dict:
    """Returns the assistant message dict {content, payload, log_id} and analytics info."""
    ranking = load_ranking(conn)
    state = conv["state"] or {}
    profile = CustomerProfile(**conv["profile"]) if conv["profile"] else CustomerProfile()

    if RESET_RE.search(message):
        return {"intent": "reset", "profile": CustomerProfile(), "state": {},
                "content": "Ready for the next customer. " + WELCOME, "payload": {"type": "text",
                "suggestions": suggestions(CustomerProfile(), {})}}

    skus = match_products(conn, message)
    if skus and (QUESTION_RE.search(message) or len(message.split()) <= 6):
        rows = product_facts(conn, skus)
        text, used_llm = None, False
        if ranking.use_llm_rerank and provider is not None:
            try:
                data = extract_json(provider.chat(QA_SYSTEM, f"Question: {message}\n\nProduct facts (one JSON per line):\n"
                                                  + _facts_for_llm(rows), role="rank")) or {}
                text = sanitize(str(data.get("answer") or ""))[:1200] or None
                used_llm = bool(text)
            except Exception as e:                    # model down: fall back to facts
                log.warning("product question fell back to rules: %s", e)
        return {"intent": "product_question", "profile": profile, "state": state, "used_llm": used_llm,
                "model": provider.model_label("rank") if used_llm else "rules", "skus": skus,
                "content": text or rule_answer(rows, message),
                "payload": {"type": "products", "products": [product_card(r) for r in rows],
                            "suggestions": suggestions(profile, state)}}

    if GREETING_RE.search(message) and not (profile.skin_type or profile.concerns):
        return {"intent": "clarify", "profile": profile, "state": state,
                "content": f"Hi {user['display_name'].split()[0]}! " + WELCOME,
                "payload": {"type": "text", "suggestions": suggestions(profile, state)}}

    parsed = parse_text(provider, message, use_llm=ranking.use_llm_rerank)
    new = merge_chat(profile, parsed, message, state)
    if MORE_RE.search(message):
        state["options"] = min(4, (state.get("options") or ranking.options_per_step) + 1)
    if FEWER_RE.search(message):
        state["options"] = 1
    if not (new.skin_type or new.concerns or new.categories or new.avoid or new.age or new.sensitive):
        return {"intent": "clarify", "profile": new, "state": state,
                "content": "I need a little more about the customer: what's their skin type and main concern? "
                           "Anything to avoid (pregnancy, fragrance...) or a budget helps too.",
                "payload": {"type": "text", "suggestions": suggestions(new, state)}}

    req = RecommendRequest(profile=new, employee_id=user["username"], top_n=state.get("steps", 4),
                           options=state.get("options"))
    res = recommend(conn, provider, req)
    summary = describe(new)
    if res["status"] != "ok":
        return {"intent": "recommendation", "status": "no_match", "profile": new, "state": state,
                "content": f"I couldn't find an in-stock product that fits all of this: {summary}. "
                           "Try a wider budget, a full routine, or remove a restriction.",
                "payload": {"type": "text", "profile_chips": profile_chips(new),
                            "suggestions": ["No budget limit", "Full routine", "New customer"]}}
    steps = res["steps"]
    state.update({"last_prices": [st["options"][0]["price"] for st in steps], "options": res["options_per_step"],
                  "last_skus": [o["sku"] for st in steps for o in st["options"]]})
    multi = any(len(st["options"]) > 1 for st in steps)
    head = (f"For **{summary}**, here's " + (f"a {len(steps)}-step routine" if len(steps) > 1 else "what I'd suggest")
            + (", best match first with other choices to offer." if multi else "."))
    content = head + (f"\n\n{res['routine_tip']}" if res.get("routine_tip") else "")
    return {"intent": "recommendation", "profile": new, "state": state, "used_llm": res["used_llm"],
            "model": res["model"], "skus": state["last_skus"], "log_id": res["log_id"], "content": content,
            "payload": {"type": "recommendation", "steps": steps, "currency": res["currency"],
                        "total": res["total"], "profile_chips": profile_chips(new),
                        "suggestions": suggestions(new, state, steps)}}


# ------------------------------------------------------------------ endpoints
class ChatBody(BaseModel):
    conversation_id: Optional[int] = None
    message: str = Field(..., min_length=1, max_length=2000)


def _conv(conn, conv_id: int, user: dict) -> dict:
    c = conn.execute("SELECT * FROM chat_conversations WHERE id = %s AND user_id = %s AND NOT archived",
                     (conv_id, user["id"])).fetchone()
    if not c:
        raise HTTPException(404, "Conversation not found")
    return c


def _msg_out(m: dict) -> dict:
    return {"id": m["id"], "role": m["role"], "content": m["content"], "payload": m["payload"], "log_id": m["log_id"],
            "created_at": m["created_at"].isoformat()}


@router.post("/chat")
def chat(body: ChatBody, user: dict = Depends(require_staff)):
    t0 = time.perf_counter()
    message = body.message.strip()
    with connect() as conn:
        if body.conversation_id:
            conv = _conv(conn, body.conversation_id, user)
        else:
            conv = conn.execute("INSERT INTO chat_conversations (user_id, title) VALUES (%s, %s) RETURNING *",
                                (user["id"], message[:60])).fetchone()
        um = conn.execute("INSERT INTO chat_messages (conversation_id, role, content) VALUES (%s, 'user', %s) RETURNING *",
                          (conv["id"], message)).fetchone()
        try:
            out = answer(conn, _provider, user, conv, message)
            status = out.get("status", "ok")
        except ProviderError as e:
            log.warning("chat provider error: %s", e)
            out = {"intent": "error", "profile": None, "state": None,
                   "content": "The product search is not available right now (the AI model can't be reached). "
                              "Please try again in a minute, or ask a manager.",
                   "payload": {"type": "text", "error": True}}
            status = "error"
        except Exception as e:
            log.exception("chat failed")
            out = {"intent": "error", "profile": None, "state": None,
                   "content": "Sorry, something went wrong on my side. Please try again.",
                   "payload": {"type": "text", "error": True}, "meta": {"error": f"{type(e).__name__}: {e}"[:300]}}
            status = "error"
        prof = out["profile"].model_dump() if out.get("profile") is not None else conv["profile"]
        am = conn.execute(
            """INSERT INTO chat_messages (conversation_id, role, content, payload, log_id)
               VALUES (%s, 'assistant', %s, %s::jsonb, %s) RETURNING *""",
            (conv["id"], out["content"], json.dumps(out["payload"], default=str), out.get("log_id"))).fetchone()
        conn.execute("UPDATE chat_conversations SET profile = %s::jsonb, state = %s::jsonb, updated_at = now() WHERE id = %s",
                     (json.dumps(prof, default=str), json.dumps(out["state"] if out.get("state") is not None else conv["state"]),
                      conv["id"]))
        steps = (out["payload"] or {}).get("steps") or []
        track(conn, "chat", user=user, conversation_id=conv["id"], intent=out["intent"], message=message,
              profile=prof, skus=out.get("skus"),
              result_count=sum(len(s["options"]) for s in steps) if steps else len((out["payload"] or {}).get("products") or []),
              status=status, latency_ms=int((time.perf_counter() - t0) * 1000), used_llm=out.get("used_llm"),
              model=out.get("model"), log_id=out.get("log_id"), meta=out.get("meta"))
    return {"conversation": {"id": conv["id"], "title": conv["title"]}, "messages": [_msg_out(um), _msg_out(am)]}


@router.get("/conversations")
def conversations(user: dict = Depends(require_staff)):
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.updated_at FROM chat_conversations c
               WHERE c.user_id = %s AND NOT c.archived ORDER BY c.updated_at DESC LIMIT 60""", (user["id"],)).fetchall()
    return {"conversations": [{"id": r["id"], "title": r["title"], "updated_at": r["updated_at"].isoformat()} for r in rows]}


@router.get("/conversations/{conv_id}")
def conversation(conv_id: int, user: dict = Depends(require_staff)):
    with connect() as conn:
        c = _conv(conn, conv_id, user)
        msgs = conn.execute("SELECT * FROM chat_messages WHERE conversation_id = %s ORDER BY id", (conv_id,)).fetchall()
    return {"conversation": {"id": c["id"], "title": c["title"]}, "messages": [_msg_out(m) for m in msgs]}


@router.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: int, user: dict = Depends(require_staff)):
    with connect() as conn:
        _conv(conn, conv_id, user)
        conn.execute("UPDATE chat_conversations SET archived = TRUE WHERE id = %s", (conv_id,))
    return {"ok": True}


class StaffFeedback(BaseModel):
    log_id: int
    feedback: str = Field(..., pattern="^(accepted|swapped|sold|rejected)$")
    sku: Optional[str] = None
    note: Optional[str] = Field(None, max_length=500)


@router.post("/feedback")
def feedback(body: StaffFeedback, user: dict = Depends(require_staff)):
    with connect() as conn:
        owner = conn.execute("SELECT employee_id FROM recommendation_log WHERE id = %s", (body.log_id,)).fetchone()
        if not owner:
            raise HTTPException(404, "Recommendation not found")
        save_feedback(conn, body.log_id, body.feedback, body.note, body.sku)
        track(conn, "feedback", user=user, intent=body.feedback, skus=[body.sku] if body.sku else None, log_id=body.log_id)
    return {"ok": True}
