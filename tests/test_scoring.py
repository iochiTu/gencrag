from app.models import CustomerProfile
from app.recommender import llm_rerank, sanitize
from app.scoring import Weights, build_steps, pick_diverse, score_candidates


def cand(sku, cat, concerns, margin, prio=3, sim=0.5, boost=0.0, ff=True):
    return dict(sku=sku, name=sku, brand="B", category=cat, routine_step={"toner": 2, "serum": 3, "cream": 5}[cat],
                price=20.0, stock_qty=5, description="", target_concerns=concerns, fragrance_free=ff,
                margin_pct=margin, priority=prio, campaign_boost=boost, similarity=sim,
                key_ingredients=["Panthenol"])


def test_fit_beats_margin():
    prof = CustomerProfile(concerns=["dryness"])
    s = score_candidates([cand("FIT", "serum", ["dryness"], 0.5), cand("RICH", "serum", ["acne"], 0.9, prio=5)], prof, Weights())
    assert s[0]["sku"] == "FIT"


def test_margin_breaks_ties():
    prof = CustomerProfile(concerns=["dryness"])
    s = score_candidates([cand("LOW", "serum", ["dryness"], 0.5), cand("HIGH", "serum", ["dryness"], 0.8)], prof, Weights())
    assert s[0]["sku"] == "HIGH"


def test_pick_diverse_builds_routine():
    prof = CustomerProfile(concerns=["dryness"])
    s = score_candidates([cand("S1", "serum", ["dryness"], 0.8), cand("S2", "serum", ["dryness"], 0.7),
                          cand("T1", "toner", ["dryness"], 0.5), cand("C1", "cream", ["dryness"], 0.5)], prof)
    assert {c["category"] for c in pick_diverse(s, 3)} == {"serum", "toner", "cream"}


class FakeLLM:
    name = "fake"
    def __init__(self, reply): self.reply = reply
    def chat(self, system, user, role="rank"): return self.reply
    def model_label(self, role): return "fake"


def _skus(steps):
    return [[o["sku"] for o in st["options"]] for st in steps]


def test_rerank_drops_invented_skus_and_tops_up():
    prof = CustomerProfile(concerns=["dryness"])
    s = score_candidates([cand("S1", "serum", ["dryness"], 0.8), cand("S2", "serum", ["dryness"], 0.7),
                          cand("T1", "toner", ["dryness"], 0.5), cand("C1", "cream", ["dryness"], 0.5)], prof)
    reply = ('{"steps":[{"category":"cream","options":[{"sku":"FAKE-999","reason":"x"},'
             '{"sku":"C1","reason":"This cream treats dryness"},{"sku":"S1","reason":"wrong category"}]},'
             '{"category":"serum","options":[{"sku":"S2","reason":"lighter"}]}]}')
    steps, tip, used = llm_rerank(FakeLLM(reply), prof, s, 3, options=2)
    assert used
    flat = [x for st in _skus(steps) for x in st]
    assert "FAKE-999" not in flat and len(flat) == len(set(flat))
    by_cat = {st["category"]: st for st in steps}
    assert [o["sku"] for o in by_cat["serum"]["options"]][:1] in (["S2"], ["S1"])
    assert set(o["sku"] for o in by_cat["serum"]["options"]) == {"S1", "S2"}   # topped up to 2 options
    assert "toner" in by_cat                                                   # missing step topped up
    cream = by_cat["cream"]["options"][0]
    assert "treats" not in cream["reason"] and "helps with" in cream["reason"]
    assert [st["routine_step"] for st in steps] == sorted(st["routine_step"] for st in steps)


def test_rerank_accepts_old_flat_format():
    prof = CustomerProfile(concerns=["dryness"])
    s = score_candidates([cand("S1", "serum", ["dryness"], 0.8), cand("T1", "toner", ["dryness"], 0.5)], prof)
    steps, _, used = llm_rerank(FakeLLM('{"picks":[{"sku":"T1","reason":"a"},{"sku":"S1","reason":"b"}]}'), prof, s, 2, 1)
    assert used and _skus(steps) == [["T1"], ["S1"]]


def test_rerank_garbage_uses_fallback():
    prof = CustomerProfile(concerns=["dryness"])
    s = score_candidates([cand("S1", "serum", ["dryness"], 0.8), cand("T1", "toner", ["dryness"], 0.5)], prof)
    steps, _, used = llm_rerank(FakeLLM("not json"), prof, s, 2)
    assert not used and len(steps) == 2 and all(o["reason"] for st in steps for o in st["options"])


def test_build_steps_offers_alternatives_but_not_weak_fillers():
    prof = CustomerProfile(concerns=["dryness", "redness"])
    s = score_candidates([cand("S1", "serum", ["dryness", "redness"], 0.8), cand("S2", "serum", ["dryness"], 0.7),
                          cand("S3", "serum", [], 0.1, prio=1), cand("C1", "cream", ["dryness"], 0.5)], prof)
    steps = build_steps(s, steps=4, options=3)
    serum = next(st for st in steps if st["category"] == "serum")
    assert [c["sku"] for c in serum["options"]] == ["S1", "S2"]                # S3 too weak to offer
    one = build_steps([c for c in s if c["category"] == "serum"], steps=4, options=3, min_ratio=0)
    assert len(one) == 1 and len(one[0]["options"]) == 3                       # single category -> one step


def test_age_outside_range_ranks_lower():
    prof = CustomerProfile(concerns=["wrinkles"], age=25)
    young = {**cand("Y", "cream", ["wrinkles"], 0.5), "min_age": None, "max_age": None}
    mature = {**cand("M", "cream", ["wrinkles"], 0.5), "min_age": 40, "max_age": None}
    assert score_candidates([mature, young], prof)[0]["sku"] == "Y"


def test_unknown_margin_is_neutral():
    prof = CustomerProfile(concerns=["dryness"])
    rows = score_candidates([cand("A", "serum", ["dryness"], 0.9), cand("B", "serum", ["dryness"], 0.5),
                             {**cand("U", "serum", ["dryness"], 0.0), "margin_pct": None}], prof)
    assert next(r for r in rows if r["sku"] == "U")["margin_norm"] == 0.5


def test_sanitize():
    assert "helps with" in sanitize("This treats acne")
