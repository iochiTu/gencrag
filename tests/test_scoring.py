from app.models import CustomerProfile
from app.recommender import llm_rerank, sanitize
from app.scoring import Weights, pick_diverse, score_candidates


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


def test_rerank_drops_invented_skus_and_tops_up():
    prof = CustomerProfile(concerns=["dryness"])
    s = score_candidates([cand("S1", "serum", ["dryness"], 0.8), cand("T1", "toner", ["dryness"], 0.5),
                          cand("C1", "cream", ["dryness"], 0.5)], prof)
    reply = '{"picks":[{"sku":"FAKE-999","reason":"x"},{"sku":"C1","reason":"This cream treats dryness"},{"sku":"S1","reason":"good"}]}'
    picks, tip, used = llm_rerank(FakeLLM(reply), prof, s, 3)
    skus = [p["sku"] for p in picks]
    assert used and "FAKE-999" not in skus and len(skus) == 3 and skus[:2] == ["C1", "S1"]
    assert "treats" not in picks[0]["reason"] and "helps with" in picks[0]["reason"]


def test_rerank_garbage_uses_fallback():
    prof = CustomerProfile(concerns=["dryness"])
    s = score_candidates([cand("S1", "serum", ["dryness"], 0.8), cand("T1", "toner", ["dryness"], 0.5)], prof)
    picks, _, used = llm_rerank(FakeLLM("not json"), prof, s, 2)
    assert not used and len(picks) == 2 and all(p["reason"] for p in picks)


def test_sanitize():
    assert "helps with" in sanitize("This treats acne")
