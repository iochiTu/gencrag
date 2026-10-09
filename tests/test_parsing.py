from app.parsing import keyword_parse, parse_budget, parse_text, merge_profiles
from app.models import CustomerProfile


def test_budget():
    assert parse_budget("under $30 please") == (None, 30)
    assert parse_budget("budget $20-40") == (20, 40)
    assert parse_budget("at least 25 dollars") == (25, None)
    assert parse_budget("max 45 CAD") == (None, 45)
    assert parse_budget("around $35") == (None, 35)
    assert parse_budget("no budget given, 3 products") == (None, None)


def test_keyword_parse():
    p = keyword_parse("Dry skin, flaky and some redness. She is pregnant and wants to avoid retinol. Under $30.")
    assert p.skin_type == "dry"
    assert {"dryness", "redness"} <= set(p.concerns)
    assert {"pregnancy", "retinoid"} <= set(p.avoid)
    assert "retinoid" in p.avoid_flags()
    assert p.budget_max == 30


def test_skin_type_words_inside_concerns():
    p = keyword_parse("Combination skin with an oily T-zone, dry patches on cheeks")
    assert p.skin_type == "combination" and "dryness" in p.concerns


def test_sunscreen_is_not_cream():
    assert keyword_parse("oily skin, needs a sun cream").categories == ["sunscreen"]
    assert keyword_parse("oily skin, sunscreen and a light moisturizer").categories == ["cream", "sunscreen"]


class FakeLLM:
    name = "fake"
    def __init__(self, reply): self.reply = reply
    def chat(self, system, user, role="rank"): return self.reply
    def model_label(self, role): return "fake"


def test_llm_cannot_remove_restrictions_or_add_unknown_values():
    llm = FakeLLM('<think>x</think>{"skin_type":"oily","concerns":["acne","glitter"],"avoid":[],"budget_max":50}')
    p = parse_text(llm, "Pregnant customer with breakouts, under $20")
    assert "pregnancy" in p.avoid            # keyword restriction kept
    assert "glitter" not in p.concerns       # unknown value dropped
    assert p.budget_max == 20                # regex budget wins over the LLM


def test_llm_fills_gaps():
    p = parse_text(FakeLLM('{"skin_type":"oily","concerns":["pores"]}'), "her T zone gets shiny by noon")
    assert p.skin_type == "combination" and {"oiliness", "pores"} <= set(p.concerns)


def test_bad_llm_output_falls_back():
    p = parse_text(FakeLLM("sorry, I cannot"), "oily skin with large pores")
    assert p.skin_type == "oily" and "pores" in p.concerns


def test_form_wins_over_text():
    form = CustomerProfile(skin_type="normal", avoid=["fragrance"])
    m = merge_profiles(form, keyword_parse("dry skin, hates alcohol in products"))
    assert m.skin_type == "normal"
    assert set(m.avoid) == {"fragrance", "alcohol"}
