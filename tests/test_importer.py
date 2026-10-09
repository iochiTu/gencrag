"""Excel/CSV import: reading, validation, description reading, and the full admin flow."""
import base64
import io
import os
import time

import pytest

from app import importer
from app.importer import extract_attributes, ingredient_flags, parse_ingredients, read_table


def xlsx(rows, headers=("SKU", "Name", "Price", "Description")):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_template_round_trips():
    p = read_table("template.xlsx", importer.template_xlsx())
    assert len(p["rows"]) == 2 and all(r["ok"] for r in p["rows"]) and not p["issues"]
    assert p["rows"][0]["category"] == "serum" and p["rows"][0]["cost"] == 7.2


def test_header_synonyms_prices_and_numeric_skus():
    data = xlsx([(1001, "Toner", "$18.50", "x" * 80 + " Ingredients: Water, Glycerin"),
                 ("A-2", "Cream", "1,299.00", "y" * 80 + " Ingredients: Water")],
                headers=("Item Code", "Product Name", "Retail Price", "Details"))
    rows = read_table("f.xlsx", data)["rows"]
    assert [r["sku"] for r in rows] == ["1001", "A-2"] and [r["price"] for r in rows] == [18.5, 1299.0]


def test_validation_messages_use_excel_row_numbers():
    data = xlsx([("S1", "A", 10, "desc " * 20), ("S1", "B", 12, "desc " * 20), ("", "C", 5, "d"),
                 ("S3", "D", "free", "desc " * 20), ("S4", "", 5, "")])
    p = read_table("f.xlsx", data)
    errs = {(i["row"], i["message"].split(" (")[0]) for i in p["issues"] if i["level"] == "error"}
    assert (3, "SKU S1 appears twice") in errs and (4, "SKU is empty") in errs
    assert (5, "Price is missing or not a number") in errs and (6, "Name is empty") in errs
    assert [r["ok"] for r in p["rows"]] == [True, False, False, False, False]


def test_missing_required_column_is_explained():
    with pytest.raises(ValueError, match="Missing column"):
        read_table("f.xlsx", xlsx([("S1", "A", 10)], headers=("SKU", "Name", "Price")))
    with pytest.raises(ValueError, match="Excel"):
        read_table("f.pdf", b"%PDF")


def test_ingredients_and_flags():
    ings = parse_ingredients("Gentle toner.\nIngredients: Water, Glycolic Acid (5%), Parfum, "
                             "Cetearyl Alcohol, Alcohol Denat., Melaleuca Alternifolia (Tea Tree) Leaf Oil.")
    assert ings[:3] == ["Water", "Glycolic Acid (5%)", "Parfum"]
    flags = {i: ingredient_flags(i) for i in ings}
    assert flags["Glycolic Acid (5%)"] == ["aha"] and flags["Parfum"] == ["fragrance"]
    assert flags["Cetearyl Alcohol"] == []                                      # fatty alcohol is fine
    assert flags["Alcohol Denat"] == ["alcohol"]                                # trailing period stripped
    assert "essential_oil" in flags["Melaleuca Alternifolia (Tea Tree) Leaf Oil"]


class FakeLLM:
    name = "fake"
    def __init__(self, reply): self.reply = reply
    def chat(self, system, user, role="rank"): return self.reply
    def model_label(self, role): return "fake"


def test_ai_reading_is_merged_safety_first():
    desc = ("Overnight renewal cream for all skin types, ages 30-55, softens fine lines. "
            "Avoid during pregnancy.\nIngredients: Water, Retinol, Squalane, Fragrance")
    ai = FakeLLM('{"category":"cream","suitable_skin_types":["dry","normal","sensitive"],"target_concerns":["wrinkles"],'
                 '"avoid_for":[],"min_age":30,"max_age":55,"key_benefits":["Softens fine lines overnight"],"ingredients":[]}')
    a = extract_attributes(ai, "Renewal Night Cream", desc)
    assert a["category"] == "cream" and a["extracted_by"] == "ai+rules"
    assert "pregnancy" in a["avoid_for"]                                        # AI missed it; rules + retinoid keep it
    assert "sensitive" not in a["suitable_skin_types"]                         # strong actives, not stated as sensitive-safe
    assert set(a["flags"]) == {"retinoid", "fragrance"} and a["fragrance_free"] is False
    assert (a["min_age"], a["max_age"]) == (30, 55) and a["key_benefits"] == ["Softens fine lines overnight"]
    assert a["ingredients"] == ["Water", "Retinol", "Squalane", "Fragrance"]   # list taken from the text, not the AI


def test_rules_only_when_ai_fails_and_review_flag():
    a = extract_attributes(FakeLLM("sorry"), "Mystery Product", "Nice product for everyone.")
    assert a["extracted_by"] == "rules" and a["needs_review"]
    assert "AI could not read" in a["review_note"] and "no ingredient list" in a["review_note"]
    b = extract_attributes(None, "Calming Toner", "Toner that calms redness. Not suitable for sensitive skin. "
                           "For oily and combination skin.\nIngredients: Water, Salicylic Acid", use_ai=False)
    assert b["category"] == "toner" and "sensitive" not in b["suitable_skin_types"]
    assert {"oily", "combination"} <= set(b["suitable_skin_types"]) and b["flags"] == ["bha"]


# ------------------------------------------------------------------ end-to-end through the admin API
pytestmark_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def client():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL not set")
    from fastapi.testclient import TestClient
    from app import db
    from app.main import app
    db.run_sql_file("schema.sql")
    db.run_sql_file("seed_sample.sql")
    c = TestClient(app)
    assert c.post("/admin/api/login", json={"name": "Ops", "password": "test-pass"}).status_code == 200
    return c


def upload_and_run(client, data, filename="products.xlsx", **opts):
    pv = client.post("/admin/api/import/preview", json={"filename": filename,
                                                        "content_b64": base64.b64encode(data).decode()})
    assert pv.status_code == 200, pv.text
    pv = pv.json()
    r = client.post(f"/admin/api/import/{pv['job_id']}/run", json=opts)
    assert r.status_code == 200, r.text
    for _ in range(100):
        job = client.get(f"/admin/api/import/{pv['job_id']}").json()
        if job["status"] in ("done", "failed"):
            return pv, job
        time.sleep(0.1)
    raise AssertionError("import did not finish")


NEW = [("NW-001", "Cica Calming Serum", 24,
        "Lightweight serum that calms redness and helps with breakouts. For sensitive, oily and combination skin. "
        "Fragrance-free.\nIngredients: Water, Centella Asiatica Extract, Niacinamide, Panthenol"),
       ("NW-002", "Retinol Night Cream", 48,
        "Night cream for dry, mature skin (40+) that softens fine lines. Not recommended during pregnancy.\n"
        "Ingredients: Water, Retinol, Shea Butter, Fragrance"),
       ("NW-003", "Oil-Free Cica Gel Serum", 19,
        "Oil-free gel serum for oily and combination skin with breakouts and redness. Fragrance-free.\n"
        "Ingredients: Water, Centella Asiatica Extract, Zinc PCA, Allantoin")]


@pytestmark_db
def test_import_preview_run_and_recommend(client):
    tmpl = client.get("/admin/api/import/template")
    assert tmpl.status_code == 200 and tmpl.content[:2] == b"PK"
    pv, job = upload_and_run(client, xlsx(NEW))
    assert pv["summary"]["new"] == 3 and pv["summary"]["missing"] == 15        # the 15 seed products not in file
    assert job["status"] == "done", job
    c = job["summary"]["counts"]
    assert c["created"] == 3 and c["analyzed"] == 3 and c["embedded"] >= 3
    res = {r["sku"]: r for r in job["results"]}
    assert res["NW-002"]["category"] == "cream" and "pregnancy" in res["NW-002"]["avoid"]
    detail = client.get("/admin/api/products/NW-002").json()["product"]
    assert [i["inci_name"] for i in detail["ingredients"]][:2] == ["Water", "Retinol"]

    r = client.post("/recommend", json={"profile": {"skin_type": "oily", "sensitive": True,
                                                    "concerns": ["redness", "acne"]}, "top_n": 3, "options": 3}).json()
    skus = [o["sku"] for st in r["steps"] for o in st["options"]]
    assert "NW-001" in skus or "NW-003" in skus
    serum = next(st for st in r["steps"] if st["category"] == "serum")
    assert len(serum["options"]) >= 2                                          # staff get a choice
    preg = client.post("/recommend", json={"text": "dry mature skin, 45, fine lines, pregnant", "top_n": 4}).json()
    assert "NW-002" not in [o["sku"] for st in preg.get("steps", []) for o in st["options"]]


@pytestmark_db
def test_reimport_only_reanalyses_changed_rows_and_can_deactivate(client):
    rows = [list(r) for r in NEW]
    rows[0][2] = 26                                                            # price change only
    rows[2][3] = rows[2][3].replace("breakouts", "breakouts and enlarged pores")   # description change
    pv, job = upload_and_run(client, xlsx(rows), deactivate_missing=True)
    assert pv["summary"]["changed"] == 2 and pv["summary"]["unchanged"] == 1
    c = job["summary"]["counts"]
    assert c["analyzed"] == 1 and c["updated"] == 2 and c["unchanged"] == 1 and c["deactivated"] == 15
    prods = {p["sku"]: p for p in client.get("/admin/api/products").json()["products"]}
    assert prods["NW-001"]["price"] == 26 and prods["CL-001"]["is_active"] is False
    again = client.post(f"/admin/api/import/{pv['job_id']}/run", json={})
    assert again.status_code == 409


@pytestmark_db
def test_manual_edit_of_ai_reading(client):
    body = {"category": "serum", "suitable_skin_types": ["oily", "combination"], "target_concerns": ["acne", "pores"],
            "avoid_for": [], "min_age": 16, "max_age": None, "key_benefits": ["Clears pores", ""], "is_active": True}
    r = client.put("/admin/api/products/NW-003/attributes", json=body)
    assert r.status_code == 200 and r.json()["warning"] is None
    p = client.get("/admin/api/products/NW-003").json()["product"]
    assert p["extracted_by"] == "manual" and p["needs_review"] is False and p["key_benefits"] == ["Clears pores"]
    assert p["searchable"] is True
    bad = client.put("/admin/api/products/NW-003/attributes", json={**body, "min_age": 50, "max_age": 20})
    assert bad.status_code == 422


@pytestmark_db
def test_preview_rejects_bad_files(client):
    r = client.post("/admin/api/import/preview", json={"filename": "x.xlsx", "content_b64": base64.b64encode(b"nope").decode()})
    assert r.status_code == 400 and "Could not read" in r.json()["detail"]
