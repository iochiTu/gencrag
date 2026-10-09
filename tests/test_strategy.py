import pytest
from pydantic import ValidationError

from app.admin import make_token, read_token
from app.models import CustomerProfile
from app.scoring import score_candidates
from app.strategy import PRESETS, RankingSettings


def test_balanced_preset_matches_default_weights():
    w = RankingSettings(**{k: PRESETS["balanced"][k] for k in ("business_share", "priority_share")}).weights()
    assert abs(w.fit - 0.455) < 1e-3 and abs(w.margin - 0.2) < 1e-2 and abs(w.prio - 0.15) < 1e-2
    assert abs(w.fit + w.sim + w.margin + w.prio - 1) < 1e-6


def test_customer_fit_can_never_drop_below_30_percent():
    with pytest.raises(ValidationError):
        RankingSettings(business_share=80)
    w = RankingSettings(business_share=70, priority_share=0).weights()
    assert w.fit + w.sim >= 0.3 - 1e-9


def _c(sku, margin, prio, concerns):
    return dict(sku=sku, category="serum", target_concerns=concerns, fragrance_free=True,
                margin_pct=margin, priority=prio, campaign_boost=0, similarity=0.5)


def test_slider_changes_the_winner():
    prof = CustomerProfile(concerns=["dryness", "redness"])
    cands = [_c("FIT", 0.55, 3, ["dryness", "redness"]),     # perfect fit, lower margin
             _c("PROFIT", 0.85, 3, ["dryness"]),             # half fit, high margin
             _c("PUSH", 0.55, 5, ["dryness"])]               # half fit, priority 5
    top = lambda r: score_candidates(cands, prof, r.weights())[0]["sku"]
    assert top(RankingSettings(business_share=20)) == "FIT"
    assert top(RankingSettings(business_share=70, priority_share=0)) == "PROFIT"
    assert top(RankingSettings(business_share=70, priority_share=100)) == "PUSH"


def test_login_token_roundtrip_and_tamper():
    t = make_token("Mina")
    assert read_token(t) == "Mina"
    assert read_token(t[:-1] + ("0" if t[-1] != "0" else "1")) is None
    assert read_token("garbage") is None and read_token(None) is None
