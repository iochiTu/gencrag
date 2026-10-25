"""Product import from Excel / CSV for non-technical staff.

Flow (driven from the admin portal, Import tab):
  1. read_table()   parse the uploaded file, map headers, validate every row
  2. diff_rows()    compare with the database: new / changed / unchanged / missing
  3. run_job()      background job: save products, let the AI read new or changed descriptions
                    (category, skin types, concerns, age, ingredients, key benefits), apply
                    deterministic ingredient safety flags, then embed for search.

Only four columns are required: SKU, Name, Price, Description. The description should say what
the product does, who it is for (skin type, age, concerns), who should avoid it, and list the
ingredients after "Ingredients:".
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .models import AVOID, CATEGORIES, CONCERNS, ROUTINE_STEP
from .parsing import CONCERN_KW
from .providers import Provider, extract_json

MAX_ROWS = 5000
SKIN_ALL = ["dry", "oily", "combination", "normal", "sensitive"]

# ------------------------------------------------------------------ columns
COLUMNS = {   # canonical -> accepted header spellings (lower-case, spaces/underscores ignored)
    "sku":         ["sku", "productcode", "itemcode", "code", "itemno", "productid", "articleno"],
    "name":        ["name", "productname", "itemname", "title", "product"],
    "price":       ["price", "retailprice", "sellingprice", "msrp", "rrp", "unitprice", "saleprice"],
    "description": ["description", "productdescription", "details", "desc", "info", "productinfo"],
    "margin":      ["margin", "marginpct", "marginpercent", "marginpercentage", "profitmargin", "grossmargin"],
    "priority":    ["priority", "productpriority", "manualpriority", "recommendationpriority", "popularity"],
    "cost":        ["cost", "unitcost", "costprice", "cogs", "purchaseprice"],
    "stock":       ["stock", "qty", "quantity", "stockqty", "inventory", "onhand", "available"],
    "brand":       ["brand", "brandname", "line"],
    "category":    ["category", "type", "producttype", "step"],
    "active":      ["active", "isactive", "enabled", "status", "selling"],
    "expiry":      ["expiry", "expirydate", "expiration", "expirationdate", "exp", "expdate", "expires",
                    "bestbefore", "bestbeforedate", "bbd", "useby", "usebydate"],
    "ingredients": ["ingredients", "ingredientlist", "inci", "incilist", "fullingredients", "composition"],
}
REQUIRED = ["sku"]                                   # the only column a file must have (e.g. a price-update sheet)
NEW_PRODUCT_NEEDS = ["name", "price"]               # a NEW product also needs these; description is strongly advised

# Cell values that mean "nothing here" (typed by people or produced by Excel formulas)
BLANKS = {"", "n/a", "na", "n.a.", "-", "--", "---", "none", "null", "nil", "tbd", "tba", "?", "unknown", "x",
          "#n/a", "#value!", "#ref!", "#div/0!", "#name?", "#num!", "#null!", "#spill!", "#calc!"}

CATEGORY_WORDS = [  # checked in this order (sunscreen before cream, mask before cream, ...)
    ("sunscreen", ["sunscreen", "sun cream", "sun screen", "spf", "sun stick", "sun essence", "sunblock", "uv protect"]),
    ("mask",      ["mask", "sheet pack", "wash-off pack", "sleeping pack", "pack"]),
    ("cleanser",  ["cleanser", "cleansing", "face wash", "facial wash", "foam", "makeup remover"]),
    ("toner",     ["toner", "tonic", "toning", "skin softener", "pad"]),
    ("essence",   ["essence", "first treatment"]),
    ("serum",     ["serum", "ampoule", "ampule", "booster", "concentrate"]),
    ("cream",     ["cream", "moisturizer", "moisturiser", "lotion", "emulsion", "balm", "moisturizing gel"]),
]
CATEGORY_SYNONYMS = {"moisturizer": "cream", "moisturiser": "cream", "lotion": "cream", "emulsion": "cream",
                     "ampoule": "serum", "ampule": "serum", "spf": "sunscreen", "sun cream": "sunscreen",
                     "sunblock": "sunscreen", "face wash": "cleanser", "foam": "cleanser", "pack": "mask",
                     "sheet mask": "mask", "eye cream": "cream"}

# Ingredient safety flags are rule-based (never left to the AI): substring match on the INCI name
FLAG_RULES: dict[str, list[str]] = {
    "fragrance":     ["fragrance", "parfum", "perfume", "aroma", "linalool", "limonene", "citronellol",
                      "geraniol", "eugenol", "coumarin", "citral", "hexyl cinnamal", "benzyl salicylate"],
    "retinoid":      ["retinol", "retinal", "retinyl", "retinoate", "tretinoin", "adapalene"],
    "aha":           ["glycolic acid", "lactic acid", "mandelic acid", "malic acid", "tartaric acid"],
    "bha":           ["salicylic acid", "betaine salicylate", "willow bark", "salix alba"],
    "alcohol":       ["alcohol denat", "denatured alcohol", "ethanol", "sd alcohol", "isopropyl alcohol"],
    "essential_oil": ["essential oil", "tea tree", "melaleuca", "lavandula", "lavender oil", "eucalyptus",
                      "peppermint", "mentha piperita", "rosemary leaf oil", "peel oil", "bergamot", "ylang"],
}
STRONG_FLAGS = {"retinoid", "aha", "bha", "fragrance", "essential_oil"}


def _key(h: Any) -> str:
    return re.sub(r"[\s_\-\.\*/()%]+", "", str(h or "")).lower()


def _map_headers(header: list[Any]) -> dict[str, int]:
    found: dict[str, int] = {}
    for idx, h in enumerate(header):
        k = _key(h)
        for canon, names in COLUMNS.items():
            if canon not in found and k in names:
                found[canon] = idx
    return found


# ------------------------------------------------------------------ reading
def _rows_from_xlsx(data: bytes) -> list[list[Any]]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    best: list[list[Any]] = []
    try:
        for ws in wb.worksheets:
            cells = list(ws.iter_rows())
            rows = [[c.value for c in r] for r in cells]
            for i, r in enumerate(rows[:15]):
                m = _map_headers(r)
                if "sku" in m and len(m) >= 2:
                    if "margin" in m:
                        col = m["margin"]
                        for values, cell_row in zip(rows[i + 1:], cells[i + 1:]):
                            c = cell_row[col]
                            # Excel stores 40% as 0.4; bare numbers use percentage points.
                            if isinstance(c.value, (int, float)) and not isinstance(c.value, bool) and "%" in c.number_format:
                                values[col] = f"{c.value * 100}%"
                    return rows[i:]
            best = best or rows
        return best
    finally:
        wb.close()


def _rows_from_csv(data: bytes) -> list[list[Any]]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [r for r in csv.reader(io.StringIO(text), dialect)]


def blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip().strip("'").lower() in BLANKS) or (
        not isinstance(v, (int, float, datetime, date)) and str(v).strip().lower() in BLANKS)


def _text(v: Any) -> str | None:
    if blank(v):
        return None
    s = str(v).replace("\r\n", "\n").replace("\x00", "").strip().lstrip("'").strip()
    return s or None


def _money(v: Any) -> float | None:
    if blank(v):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.,\-]", "", str(v))
    if s.count(",") and s.count("."):
        s = s.replace(",", "")
    elif s.count(",") == 1 and len(s.split(",")[1]) == 2:
        s = s.replace(",", ".")                     # 12,50 -> 12.50
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _margin(v: Any) -> float | None:
    """Percentage points (40 or '40%') to a stored fraction (0.4)."""
    if blank(v) or isinstance(v, bool):
        return None
    s = str(v).strip().rstrip("%").strip().replace(",", ".")
    try:
        pct = float(s)
    except ValueError:
        return None
    return pct / 100 if math.isfinite(pct) and 0 <= pct <= 100 else None


def _priority(v: Any) -> int | None:
    if blank(v) or isinstance(v, bool):
        return None
    try:
        n = float(str(v).strip())
    except ValueError:
        return None
    return int(n) if math.isfinite(n) and n.is_integer() and 1 <= n <= 5 else None


def _int(v: Any) -> int | None:
    m = _money(v)
    return int(m) if m is not None else None


def _sku(v: Any) -> str:
    if blank(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)                                  # Excel turns 1001 into 1001.0
    return str(v).strip().lstrip("'").strip()


def _bool(v: Any) -> bool | None:
    if blank(v):
        return None
    s = str(v).strip().lower()
    if s in ("1", "yes", "y", "true", "active", "on", "selling", "live", "enabled"):
        return True
    if s in ("0", "no", "n", "false", "inactive", "discontinued", "off", "stop", "stopped", "disabled", "delisted"):
        return False
    return None


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _month_end(y: int, m: int) -> date:
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))


def parse_date(v: Any, order: str = "MDY") -> tuple[date | None, str | None]:
    """Returns (date, note). note explains a guess ("03/04/2027 read as 4 Mar 2027") or why it failed.
    Month-only values (03/2027, Mar 2027) mean the end of that month."""
    if blank(v):
        return None, None
    if isinstance(v, datetime):
        return v.date(), None
    if isinstance(v, date):
        return v, None
    if isinstance(v, (int, float)) and 20000 < v < 80000:          # Excel serial number that lost its format
        return date(1899, 12, 30) + timedelta(days=int(v)), None
    s = str(v).strip().lower().replace(".", "/").replace("-", "/").replace(" ", "/")
    s = re.sub(r"/+", "/", s)
    try:
        if m := re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", s):                  # 2027/03/31 (ISO, Korean)
            return date(int(m[1]), int(m[2]), int(m[3])), None
        if m := re.fullmatch(r"(\d{4})/(\d{1,2})", s):                              # 2027/03
            return _month_end(int(m[1]), int(m[2])), None
        if m := re.fullmatch(r"(\d{1,2})/(\d{4})", s):                              # 03/2027
            return _month_end(int(m[2]), int(m[1])), None
        if m := re.fullmatch(r"(\d{8})", s):                                         # 20270331
            return date(int(s[:4]), int(s[4:6]), int(s[6:])), None
        if m := re.fullmatch(r"([a-z]{3})[a-z]*/(\d{4})", s):                        # mar 2027
            return _month_end(int(m[2]), _MONTHS[m[1]]), None
        if m := re.fullmatch(r"(\d{1,2})/([a-z]{3})[a-z]*/(\d{2,4})", s):           # 31 mar 2027
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(y, _MONTHS[m[2]], int(m[1])), None
        if m := re.fullmatch(r"([a-z]{3})[a-z]*/(\d{1,2})/(\d{2,4})", s):           # mar 31 2027
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(y, _MONTHS[m[1]], int(m[2])), None
        if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s):                 # 03/04/2027
            a, b, y = int(m[1]), int(m[2]), int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            if a > 12:
                return date(y, b, a), None
            if b > 12:
                return date(y, a, b), None
            d = date(y, a, b) if order == "MDY" else date(y, b, a)
            note = None if a == b else f"{v} read as {d.strftime('%d %b %Y')} ({'month/day' if order == 'MDY' else 'day/month'}); write dates as 2027-03-31 to avoid doubt"
            return d, note
    except (ValueError, KeyError):
        pass
    return None, f"could not read the date {v!r}"


def normalise_category(v: Any) -> str | None:
    s = (_text(v) or "").lower()
    if not s:
        return None
    if s in CATEGORIES:
        return s
    if s in CATEGORY_SYNONYMS:
        return CATEGORY_SYNONYMS[s]
    for cat, words in CATEGORY_WORDS:
        if any(w in s for w in words):
            return cat
    return None


def read_table(filename: str, data: bytes, date_order: str = "MDY") -> dict:
    """Parse and check every row without ever failing on a single bad cell.

    Empty cells, "N/A", "-" and Excel errors (#N/A, #VALUE!) count as "not given". For a product that already
    exists, a value that is not given keeps what is on file. Bad optional values become warnings and are
    ignored. Only a missing/duplicate SKU, or a NEW product without a name or price, stops a row
    (decided in finalise_rows, which knows the database)."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        raw = _rows_from_xlsx(data)
    elif name.endswith((".csv", ".txt")):
        raw = _rows_from_csv(data)
    else:
        raise ValueError("Please upload an Excel file (.xlsx) or a CSV file.")
    if not raw:
        raise ValueError("The file is empty.")
    cols = _map_headers(raw[0])
    if "sku" not in cols:
        raise ValueError("Missing column: SKU. The first row must contain the column names "
                         "(download the template to see them).")
    if len(cols) < 2:
        raise ValueError("Only a SKU column was found. Add at least one more column such as Name, Price or Description.")

    rows, issues, seen = [], [], {}
    for n, r in enumerate(raw[1:], start=2):        # n = the row number staff see in Excel
        get = lambda c: r[cols[c]] if c in cols and cols[c] < len(r) else None
        if all(blank(x) for x in r):
            continue
        errs, warns = [], []
        raw_price, raw_cost, raw_stock = get("price"), get("cost"), get("stock")
        price, cost, stock = _money(raw_price), _money(raw_cost), _int(raw_stock)
        raw_margin = get("margin")
        margin = _margin(raw_margin)
        if not blank(raw_margin) and margin is None:
            warns.append(f"Margin {raw_margin!r} must be a percentage from 0% to 100%; ignored")
        raw_priority = get("priority")
        priority = _priority(raw_priority)
        if not blank(raw_priority) and priority is None:
            warns.append(f"Priority {raw_priority!r} must be a whole number from 1 to 5 (5 = highest); ignored")
        if not blank(raw_price) and (price is None or price < 0):
            warns.append(f"Price {raw_price!r} is not a number; ignored")
            price = None
        if not blank(raw_cost) and (cost is None or cost < 0):
            warns.append(f"Cost {raw_cost!r} is not a number; ignored")
            cost = None
        if not blank(raw_stock) and (stock is None or stock < 0):
            warns.append(f"Stock {raw_stock!r} is not a whole number; ignored")
            stock = None
        expiry, note = parse_date(get("expiry"), date_order) if "expiry" in cols else (None, None)
        if note:
            warns.append(("Expiry " + note) if expiry else f"Expiry {note}; ignored")
        active = _bool(get("active")) if "active" in cols else None
        if "active" in cols and not blank(get("active")) and active is None:
            warns.append(f"Active {get('active')!r} not understood (use yes/no); ignored")
        category = normalise_category(get("category")) if "category" in cols else None
        if "category" in cols and not blank(get("category")) and category is None:
            warns.append(f"Unknown category {get('category')!r}; the AI will decide")
        description = _text(get("description"))
        ing_col = _text(get("ingredients"))
        if description and len(description) > 8000:
            warns.append("Description is longer than 8,000 characters; the rest is cut off")
            description = description[:8000]
        row = {"row": n, "sku": _sku(get("sku")), "name": _text(get("name")), "price": price, "cost": cost,
               "margin_pct": margin,
               "priority": priority,
               "stock": stock, "brand": _text(get("brand")), "category": category, "active": active,
               "description": description, "ingredients_text": ing_col,
               "expiry": expiry.isoformat() if expiry else None}

        if not row["sku"]:
            if not row["name"]:
                continue                                # notes, totals or spacer rows: nothing to import
            errs.append("SKU is empty")
        elif row["sku"] in seen:
            errs.append(f"SKU {row['sku']} appears twice (also row {seen[row['sku']]}); this row is skipped")
        if description and len(description) < 60:
            warns.append("Description is very short; the AI may not find skin types or concerns")
        if (description or ing_col) and not ing_col and not parse_ingredients(description or ""):
            warns.append('No ingredient list (add "Ingredients: ..." to the description or an Ingredients column)')
        if cost is not None and price is not None and cost > price:
            warns.append("Cost is higher than price")
        if expiry and expiry < date.today():
            warns.append(f"Expiry {expiry.isoformat()} is in the past; this product will not be recommended")
        if row["sku"] and row["sku"] not in seen:
            seen[row["sku"]] = n
        row["ok"] = not errs
        row["warnings"] = warns
        rows.append(row)
        issues += [{"row": n, "sku": row["sku"], "level": "error", "message": m} for m in errs]
        if not errs:
            issues += [{"row": n, "sku": row["sku"], "level": "warning", "message": m} for m in warns]
        if len(rows) > MAX_ROWS:
            raise ValueError(f"The file has more than {MAX_ROWS} products. Split it into smaller files.")
    if not rows:
        raise ValueError("No product rows found under the header row.")
    return {"rows": rows, "issues": issues, "columns": sorted(cols), "optional_missing":
            [c for c in COLUMNS if c not in cols and c not in REQUIRED]}


def finalise_rows(conn, parsed: dict) -> None:
    """Decide per row with the database: new products need a name and price; for existing products an
    empty name/price/description keeps the value on file."""
    existing = {r["sku"] for r in conn.execute("SELECT sku FROM products").fetchall()}
    for r in parsed["rows"]:
        if not r["ok"]:
            continue
        is_new = r["sku"] not in existing
        r["is_new"] = is_new
        if is_new:
            missing = [f for f in NEW_PRODUCT_NEEDS if r.get(f) is None]
            if missing:
                r["ok"] = False                          # earlier warnings stay: they explain the cause
                parsed["issues"].append({"row": r["row"], "sku": r["sku"], "level": "error", "message":
                                         "New product needs " + " and ".join(m.title() for m in missing) + "; row skipped"})
            elif not r.get("description") and not r.get("ingredients_text"):
                parsed["issues"].append({"row": r["row"], "sku": r["sku"], "level": "warning", "message":
                                         "No description: the product is imported, but the AI only has its name; please review"})
        else:
            kept = [f for f in ("name", "price", "description") if f in parsed["columns"] and r.get(f) is None]
            if kept:
                parsed["issues"].append({"row": r["row"], "sku": r["sku"], "level": "warning", "message":
                                         "Empty " + ", ".join(kept) + ": current value kept"})
    parsed["issues"].sort(key=lambda i: (i["row"], i["level"]))


def full_description(description: str | None, ingredients_text: str | None) -> str:
    """Description as stored: an Ingredients column is appended as 'Ingredients: ...' unless already there."""
    d = (description or "").strip()
    if ingredients_text and not parse_ingredients(d):
        d = (d + "\n" if d else "") + "Ingredients: " + ingredients_text.strip()
    return d


def desc_hash(name: str, description: str) -> str:
    return hashlib.sha256(f"{name}\n{description}".encode()).hexdigest()[:16]


def diff_rows(conn, rows: list[dict]) -> dict:
    db = {r["sku"]: r for r in conn.execute(
        "SELECT p.sku, p.name, p.price::float AS price, p.cost::float AS cost, p.margin_pct::float AS margin_pct, p.stock_qty, p.brand, p.category, "
        "p.is_active, p.description, p.description_hash, p.expiry_date, COALESCE(br.priority, 3) AS priority, "
        "a.sku IS NOT NULL AS has_attrs FROM products p "
        "LEFT JOIN product_attributes a USING (sku) LEFT JOIN business_rules br USING (sku)").fetchall()}
    new, changed, unchanged = [], [], []
    for r in rows:
        if not r["ok"]:
            continue
        old = db.get(r["sku"])
        if not old:
            new.append({"row": r["row"], "sku": r["sku"], "name": r["name"], "price": r["price"]})
            continue
        name = r["name"] or old["name"]
        desc = full_description(r["description"], r.get("ingredients_text")) if (r["description"] or r.get("ingredients_text")) else old["description"]
        fields = []
        if r["name"] and old["name"] != r["name"]:
            fields.append("name")
        if r["price"] is not None and (old["price"] is None or abs(old["price"] - r["price"]) > 0.004):
            fields.append(f"price {old['price']} → {r['price']}")
        if old["description_hash"] != desc_hash(name, desc):
            fields.append("description")
        if r["cost"] is not None and (old["cost"] is None or abs(old["cost"] - r["cost"]) > 0.004):
            fields.append("cost")
        if r.get("margin_pct") is not None and (old["margin_pct"] is None or abs(old["margin_pct"] - r["margin_pct"]) > 0.0000005):
            fields.append(f"margin → {r['margin_pct'] * 100:g}%")
        if r.get("priority") is not None and old["priority"] != r["priority"]:
            fields.append(f"priority {old['priority']} → {r['priority']}")
        if r["stock"] is not None and old["stock_qty"] != r["stock"]:
            fields.append(f"stock {old['stock_qty']} → {r['stock']}")
        if r["brand"] and old["brand"] != r["brand"]:
            fields.append("brand")
        if r["category"] and old["category"] != r["category"]:
            fields.append(f"category {old['category']} → {r['category']}")
        if r["active"] is not None and old["is_active"] != r["active"]:
            fields.append("active" if r["active"] else "inactive")
        if r.get("expiry") and (old["expiry_date"] is None or old["expiry_date"].isoformat() != r["expiry"]):
            fields.append(f"expiry → {r['expiry']}")
        (changed if fields else unchanged).append(
            {"row": r["row"], "sku": r["sku"], "name": name, "changes": fields})
    in_file = {r["sku"] for r in rows if r["sku"]}
    missing = [{"sku": s, "name": o["name"]} for s, o in db.items() if s not in in_file and o["is_active"]]
    return {"new": new, "changed": changed, "unchanged": unchanged, "missing": missing,
            "errors": sum(1 for r in rows if not r["ok"])}


# ------------------------------------------------------------------ description reading
_ING_RE = re.compile(r"(?:full\s+)?(?:ingredients?|inci)(?:\s+list)?\s*[:：\-]\s*(.+?)(?:\n\s*\n|\n[A-Z][A-Za-z ]{2,30}:|$)",
                     re.I | re.S)


def parse_ingredients(description: str) -> list[str]:
    m = _ING_RE.search(description or "")
    if not m:
        return []
    text = m.group(1).replace("\n", " ")
    parts, depth, cur = [], 0, ""
    for ch in text:                                  # split on commas outside parentheses
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch in ",;" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip(" .*")
        if 1 < len(p) <= 120 and p.lower() not in (x.lower() for x in out):
            out.append(p)
    return out[:80]


def ingredient_flags(inci: str) -> list[str]:
    s = inci.lower()
    flags = [f for f, words in FLAG_RULES.items() if any(w in s for w in words)]
    if s.strip() == "alcohol":
        flags.append("alcohol")
    return flags


TEXT_FLAG_WORDS: dict[str, list[str]] = {
    "fragrance":     ["fragrance", "fragranced", "parfum", "perfume", "perfumed", "scented"],
    "retinoid":      ["retinol", "retinal", "retinoid", "retinoids", "retinyl palmitate", "tretinoin"],
    "aha":           ["aha", "ahas", "glycolic acid", "lactic acid", "mandelic acid", "alpha hydroxy acid"],
    "bha":           ["bha", "bhas", "salicylic acid", "beta hydroxy acid"],
    "alcohol":       ["alcohol denat", "denatured alcohol", "ethanol"],
    "essential_oil": ["essential oil", "essential oils", "tea tree oil", "lavender oil"],
}
_NEG_BEFORE = re.compile(r"(?:\bno|\bwithout|\bfree (?:of|from)|\bzero|\bnon|\bexcludes?)\b[^.;:]{0,18}$")
_NEG_AFTER = re.compile(r"^\s*-?\s*(?:free|0\s*%)")


def text_flags(description: str) -> list[str]:
    """Safety flags mentioned in the description text (outside the ingredient list), e.g. 'with 0.3% retinol'.
    Negated mentions ('fragrance-free', 'no alcohol', 'without retinol') do not count."""
    t = _sentences_without_ingredients(description or "").lower()
    found = set()
    for flag, words in TEXT_FLAG_WORDS.items():
        for w in words:
            for m in re.finditer(rf"\b{re.escape(w)}\b", t):
                if not (_NEG_BEFORE.search(t[max(0, m.start() - 30):m.start()]) or _NEG_AFTER.match(t[m.end():m.end() + 8])):
                    found.add(flag)
    return sorted(found)


def _sentences_without_ingredients(desc: str) -> str:
    m = _ING_RE.search(desc or "")
    return (desc[: m.start()] if m else desc).strip()


_NEG = r"(?:not|never|avoid|don't|do not|unsuitable|isn't)\b[^.]{0,40}"


def rules_extract(name: str, description: str, ingredients: list[str]) -> dict:
    """Deterministic reading of a description. Used alone when the AI is unavailable, and merged with the AI."""
    body = _sentences_without_ingredients(description)
    t = f" {name} {body} ".lower()
    category = next((c for c, ws in CATEGORY_WORDS if any(w in name.lower() for w in ws)), None) \
        or next((c for c, ws in CATEGORY_WORDS if any(w in t for w in ws)), None) or "other"
    skin = []
    if re.search(r"all skin types?|every skin type|any skin type|all types of skin", t):
        skin = list(SKIN_ALL)
    for st in ["dry", "oily", "combination", "normal", "sensitive"]:
        if re.search(rf"\b{st}\b[^.]{{0,30}}skin|skin[^.]{{0,15}}\b{st}\b|\b{st}\b,", t) and st not in skin:
            skin.append(st)
    for st in list(skin):                            # "not for very dry or sensitive skin"
        if re.search(_NEG + rf"\b{st}\b", t):
            skin.remove(st)
    concerns = [c for c, ws in CONCERN_KW.items() if any(w in t for w in ws)]
    avoid = []
    if re.search(r"pregnan|breastfeed|nursing", t) and not re.search(r"(pregnancy|pregnant)[- ](safe|friendly)|safe (during|for) pregnan", t):
        avoid.append("pregnancy")
    min_age = max_age = None
    m = re.search(r"\b(\d{2})\s*\+|\b(?:over|above|aged)\s+(\d{2})\b|\b(\d{2})\s*(?:years)?\s*and (?:over|up|older)", t)
    if m:
        min_age = int(next(g for g in m.groups() if g))
    m = re.search(r"\bages?\s*(\d{2})\s*(?:-|–|to)\s*(\d{2})\b|\b(\d{2})\s*(?:-|–|to)\s*(\d{2})\s*(?:years|y/o)", t)
    if m:
        a, b = [int(x) for x in m.groups() if x][:2]
        min_age, max_age = min(a, b), max(a, b)
    if re.search(r"\bfor teens?\b|\bteen(age)? skin\b|\bteenagers? only\b", t) and "adult" not in t and min_age is None:
        min_age, max_age = 13, 19
    if re.search(r"mature skin|aging skin|ageing skin", t) and min_age is None:
        min_age = 40
    texture = next((w for w in ["gel", "foam", "oil", "balm", "mist", "stick", "sheet", "watery", "lotion",
                                "cream", "essence"] if re.search(rf"\b{w}\b", t)), None)
    flags = {f for i in ingredients for f in ingredient_flags(i)}
    fragrance_free = "fragrance" not in flags and (bool(ingredients) or bool(re.search(r"fragrance[- ]free|unscented|no fragrance", t)))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if 12 < len(s.strip()) < 160
                 and not re.search(r"\b(not|avoid|pregnan|patch test|consult|discontinue|warning|caution)\b", s.lower())]
    return {"category": category, "suitable_skin_types": skin, "target_concerns": concerns, "avoid_for": avoid,
            "min_age": min_age, "max_age": max_age, "texture": texture, "fragrance_free": fragrance_free,
            "key_benefits": sentences[:2], "ingredients": ingredients}


EXTRACT_SYSTEM = f"""You read one K-beauty product's name and description and return facts as JSON for a
recommendation database. Use ONLY what the text says; never invent ingredients, ages or claims.
Return ONLY a JSON object with these keys:
  "category": one of {CATEGORIES}
  "texture": short word or null (gel, cream, foam, watery, oil, balm, sheet, ...)
  "suitable_skin_types": list from ["dry","oily","combination","normal","sensitive"]; "all skin types" = all five
  "target_concerns": list from {CONCERNS}
  "avoid_for": list from ["pregnancy"] - only if the text says to avoid it during pregnancy/breastfeeding
  "min_age": integer or null, "max_age": integer or null (only if the text states an age or age group)
  "key_benefits": 2-3 short selling points a sales assistant can say (max 14 words each, from the text)
  "ingredients": the ingredient list in order, if the text has one, else []"""


def ai_extract(provider: Provider, name: str, description: str) -> dict | None:
    try:
        return extract_json(provider.chat(EXTRACT_SYSTEM, f"Name: {name}\nDescription:\n{description[:6000]}", role="parse"))
    except Exception:
        return None


def _list(v, allowed) -> list[str]:
    if not isinstance(v, list):
        return []
    return [x for x in dict.fromkeys(str(i).strip().lower() for i in v) if x in allowed]


def _age(v) -> int | None:
    try:
        v = int(v)
        return v if 10 <= v <= 100 else None
    except (TypeError, ValueError):
        return None


def extract_attributes(provider: Provider | None, name: str, description: str, category_hint: str | None = None,
                       use_ai: bool = True) -> dict:
    """AI + rules, merged safety-first. Returns attributes + ingredient list + flags + review status."""
    regex_ings = parse_ingredients(description)
    rules = rules_extract(name, description, regex_ings)
    ai = ai_extract(provider, name, description) if (use_ai and provider is not None) else None
    notes = []

    if ai:
        ai_cat = str(ai.get("category") or "").lower()
        category = category_hint or (ai_cat if ai_cat in CATEGORIES else None) or rules["category"]
        skin = _list(ai.get("suitable_skin_types"), SKIN_ALL) or rules["suitable_skin_types"]
        concerns = list(dict.fromkeys(_list(ai.get("target_concerns"), CONCERNS) + rules["target_concerns"]))
        avoid = list(dict.fromkeys(_list(ai.get("avoid_for"), AVOID) + rules["avoid_for"]))
        min_age, max_age = _age(ai.get("min_age")) or rules["min_age"], _age(ai.get("max_age")) or rules["max_age"]
        texture = (str(ai.get("texture")).strip().lower()[:30] if ai.get("texture") else None) or rules["texture"]
        benefits = [str(b).strip()[:140] for b in (ai.get("key_benefits") or []) if str(b).strip()][:3] or rules["key_benefits"]
        ingredients = regex_ings or [str(i).strip()[:120] for i in (ai.get("ingredients") or []) if str(i).strip()][:80]
        extracted_by = "ai+rules"
    else:
        category = category_hint or rules["category"]
        skin, concerns, avoid = rules["suitable_skin_types"], rules["target_concerns"], rules["avoid_for"]
        min_age, max_age, texture = rules["min_age"], rules["max_age"], rules["texture"]
        benefits, ingredients, extracted_by = rules["key_benefits"], regex_ings, "rules"
        if use_ai and provider is not None:
            notes.append("AI could not read this description; rules only")

    tflags = text_flags(description)
    flags = sorted({f for i in ingredients for f in ingredient_flags(i)} | set(tflags))
    explicit_sensitive = bool(re.search(r"\bsensitive\b", description.lower())) and not re.search(_NEG + r"sensitive", description.lower())
    if "sensitive" in skin and STRONG_FLAGS & set(flags) and not explicit_sensitive:
        skin.remove("sensitive")                    # "all skin types" but contains strong actives / fragrance
    if "retinoid" in flags and "pregnancy" not in avoid:
        avoid.append("pregnancy")
    if min_age and max_age and min_age > max_age:
        min_age, max_age = max_age, min_age
    fragrance_free = "fragrance" not in flags and (rules["fragrance_free"] or (ai or {}).get("fragrance_free") is True)

    if not (description or "").strip():
        notes.append("no description; only the name was read")
    if not ingredients:
        notes.append("no ingredient list" + (f" (description mentions: {', '.join(tflags)})" if tflags else "")
                     + "; allergy filters rely on the description only")
    if category == "other":
        notes.append("category unclear")
    if not skin:
        notes.append("no skin types found")
    if not concerns:
        notes.append("no skin concerns found")
    return {"category": category, "routine_step": ROUTINE_STEP[category], "suitable_skin_types": skin,
            "target_concerns": concerns, "avoid_for": avoid, "min_age": min_age, "max_age": max_age,
            "texture": texture, "fragrance_free": fragrance_free, "key_benefits": benefits,
            "ingredients": ingredients, "flags": flags, "text_flags": tflags, "needs_review": bool(notes),
            "review_note": "; ".join(notes) or None, "extracted_by": extracted_by}


def save_attributes(conn, sku: str, a: dict) -> None:
    conn.execute(
        """INSERT INTO product_attributes (sku, suitable_skin_types, target_concerns, avoid_for, texture, fragrance_free,
                                           min_age, max_age, key_benefits, needs_review, review_note, extracted_by, text_flags)
           VALUES (%(sku)s, %(suitable_skin_types)s, %(target_concerns)s, %(avoid_for)s, %(texture)s, %(fragrance_free)s,
                   %(min_age)s, %(max_age)s, %(key_benefits)s, %(needs_review)s, %(review_note)s, %(extracted_by)s,
                   %(text_flags)s)
           ON CONFLICT (sku) DO UPDATE SET suitable_skin_types = EXCLUDED.suitable_skin_types, text_flags = EXCLUDED.text_flags,
               target_concerns = EXCLUDED.target_concerns, avoid_for = EXCLUDED.avoid_for, texture = EXCLUDED.texture,
               fragrance_free = EXCLUDED.fragrance_free, min_age = EXCLUDED.min_age, max_age = EXCLUDED.max_age,
               key_benefits = EXCLUDED.key_benefits, needs_review = EXCLUDED.needs_review,
               review_note = EXCLUDED.review_note, extracted_by = EXCLUDED.extracted_by""",
        {"text_flags": [], **a, "sku": sku})
    conn.execute("UPDATE products SET category = %s, routine_step = %s, embedding = NULL, updated_at = now() WHERE sku = %s",
                 (a["category"], ROUTINE_STEP[a["category"]], sku))
    if "ingredients" in a:
        conn.execute("DELETE FROM product_ingredients WHERE sku = %s", (sku,))
        for pos, inci in enumerate(a["ingredients"], start=1):
            ing = conn.execute(
                """INSERT INTO ingredients (inci_name, flags) VALUES (%s, %s)
                   ON CONFLICT (inci_name) DO UPDATE SET
                       flags = ARRAY(SELECT DISTINCT unnest(ingredients.flags || EXCLUDED.flags))
                   RETURNING id""", (inci, ingredient_flags(inci))).fetchone()
            conn.execute("INSERT INTO product_ingredients (sku, ingredient_id, position) VALUES (%s, %s, %s) "
                         "ON CONFLICT DO NOTHING", (sku, ing["id"], pos))


# ------------------------------------------------------------------ background job
_running: dict[int, threading.Thread] = {}
_lock = threading.Lock()


def job_is_alive(job_id: int) -> bool:
    t = _running.get(job_id)
    return bool(t and t.is_alive())


def any_running() -> bool:
    return any(t.is_alive() for t in _running.values())


def start_job(job_id: int, provider: Provider, actor: str, connect, embed) -> None:
    with _lock:
        if any_running():
            raise RuntimeError("Another import is still running. Wait for it to finish.")
        t = threading.Thread(target=run_job, args=(job_id, provider, actor, connect, embed), daemon=True)
        _running[job_id] = t
        t.start()


def run_job(job_id: int, provider: Provider, actor: str, connect, embed) -> None:
    from .strategy import audit, load_ranking
    with connect() as conn:
        job = conn.execute("SELECT * FROM import_jobs WHERE id = %s", (job_id,)).fetchone()
        opts = job["options"] or {}
        rows = [r for r in job["rows"] if r.get("ok")]
        use_ai = load_ranking(conn).use_llm_rerank
        conn.execute("UPDATE import_jobs SET status = 'running', total = %s, done = 0, results = '[]' WHERE id = %s",
                     (len(rows), job_id))
        results, counts = [], {"created": 0, "updated": 0, "unchanged": 0, "analyzed": 0, "needs_review": 0, "failed": 0}
        try:
            for i, r in enumerate(rows, start=1):
                results.append(_import_row(conn, r, provider, use_ai, opts.get("reanalyze_all", False), counts, actor))
                conn.execute("UPDATE import_jobs SET done = %s, results = %s::jsonb, summary = COALESCE(summary, '{}'::jsonb) || %s::jsonb WHERE id = %s",
                             (i, json.dumps(results), json.dumps({"stage": "analyzing", "counts": counts}), job_id))
            deactivated = 0
            if opts.get("deactivate_missing"):
                skus = [r["sku"] for r in job["rows"] if r.get("sku")]
                deactivated = conn.execute("UPDATE products SET is_active = FALSE, updated_at = now() "
                                           "WHERE is_active AND NOT (sku = ANY(%s))", (skus,)).rowcount
            counts["deactivated"] = deactivated
            conn.execute("UPDATE import_jobs SET summary = COALESCE(summary, '{}'::jsonb) || %s::jsonb WHERE id = %s",
                         (json.dumps({"stage": "embedding", "counts": counts}), job_id))
            error = None
            try:
                counts["embedded"] = embed(provider=provider)
            except Exception as e:                   # products are saved; they become searchable after a re-run
                error = f"Products were saved, but making them searchable failed: {e}. Fix the model connection and run the import again."
            status = "failed" if error else "done"
            conn.execute("UPDATE import_jobs SET status = %s, error = %s, finished_at = now(), "
                         "summary = COALESCE(summary, '{}'::jsonb) || %s::jsonb WHERE id = %s",
                         (status, error, json.dumps({"stage": "finished", "counts": counts}), job_id))
            audit(conn, actor, "import", job["filename"] or f"job {job_id}", None, counts)
        except Exception as e:
            conn.execute("UPDATE import_jobs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
                         (f"{e}\n{traceback.format_exc(limit=3)}", job_id))


def _import_row(conn, r: dict, provider, use_ai: bool, reanalyze: bool, counts: dict, actor: str = "import") -> dict:
    sku = r["sku"]
    try:
        old = conn.execute("SELECT p.*, p.price::float AS price_f, p.cost::float AS cost_f, p.margin_pct::float AS margin_f, "
                           "COALESCE(br.priority, 3) AS priority, a.sku IS NOT NULL AS has_attrs "
                           "FROM products p LEFT JOIN product_attributes a USING (sku) "
                           "LEFT JOIN business_rules br USING (sku) WHERE p.sku = %s", (sku,)).fetchone()
        given_desc = (full_description(r.get("description"), r.get("ingredients_text"))
                      if (r.get("description") or r.get("ingredients_text")) else None)
        if old is None:
            if r.get("name") is None or r.get("price") is None:
                raise ValueError("New product needs a name and price")
            name, desc = r["name"], given_desc or ""
            h = desc_hash(name, desc)
            cat = r.get("category") or "other"
            conn.execute(
                """INSERT INTO products (sku, name, brand, category, routine_step, price, cost, margin_pct, stock_qty, is_active,
                                         description, description_hash, expiry_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (sku, name, r.get("brand"), cat, ROUTINE_STEP[cat], r["price"], r.get("cost"), r.get("margin_pct"), r.get("stock"),
                 True if r.get("active") is None else r["active"], desc, h, r.get("expiry")))
            action, analyse = "created", True
        else:
            name = r.get("name") or old["name"]                          # empty cell -> keep what is on file
            price = r["price"] if r.get("price") is not None else old["price_f"]
            desc = given_desc if given_desc is not None else (old["description"] or "")
            h = desc_hash(name, desc)
            changed = (old["name"] != name or abs(old["price_f"] - price) > 0.004 or old["description_hash"] != h
                       or (r.get("cost") is not None and (old["cost_f"] is None or abs(old["cost_f"] - r["cost"]) > 0.004))
                       or (r.get("margin_pct") is not None and (old["margin_f"] is None or abs(old["margin_f"] - r["margin_pct"]) > 0.0000005))
                       or (r.get("priority") is not None and old["priority"] != r["priority"])
                       or (r.get("stock") is not None and old["stock_qty"] != r["stock"])
                       or (r.get("brand") and old["brand"] != r["brand"])
                       or (r.get("active") is not None and old["is_active"] != r["active"])
                       or (r.get("category") and old["category"] != r["category"])
                       or (r.get("expiry") and str(old["expiry_date"]) != r["expiry"]))
            conn.execute(
                """UPDATE products SET name = %s, price = %s, description = %s, description_hash = %s,
                       brand = COALESCE(%s, brand), cost = COALESCE(%s, cost), margin_pct = COALESCE(%s, margin_pct), stock_qty = COALESCE(%s, stock_qty),
                       is_active = COALESCE(%s, is_active), expiry_date = COALESCE(%s::date, expiry_date),
                       updated_at = now() WHERE sku = %s""",
                (name, price, desc, h, r.get("brand"), r.get("cost"), r.get("margin_pct"), r.get("stock"), r.get("active"), r.get("expiry"), sku))
            action = "updated" if changed else "unchanged"
            analyse = (reanalyze or not old["has_attrs"] or old["description_hash"] != h
                       or bool(r.get("category") and r["category"] != old["category"]))
            if not analyse and (old["name"] != name or (r.get("brand") and r["brand"] != old["brand"])):
                conn.execute("UPDATE products SET embedding = NULL WHERE sku = %s", (sku,))   # search text changed
        if r.get("priority") is not None and (old is None or old["priority"] != r["priority"]):
            conn.execute(
                """INSERT INTO business_rules (sku, priority, updated_by) VALUES (%s, %s, %s)
                   ON CONFLICT (sku) DO UPDATE SET priority = EXCLUDED.priority,
                       updated_by = EXCLUDED.updated_by, updated_at = now()""", (sku, r["priority"], actor))
        counts[action] += 1
        if not analyse:
            return {"row": r["row"], "sku": sku, "name": name, "action": action, "analyzed": False}
        a = extract_attributes(provider, name, desc, r.get("category"), use_ai=use_ai)
        save_attributes(conn, sku, a)
        counts["analyzed"] += 1
        counts["needs_review"] += int(a["needs_review"])
        return {"row": r["row"], "sku": sku, "name": name, "action": action, "analyzed": True,
                "category": a["category"], "skin": a["suitable_skin_types"], "concerns": a["target_concerns"],
                "avoid": a["avoid_for"], "min_age": a["min_age"], "max_age": a["max_age"],
                "flags": a["flags"], "needs_review": a["needs_review"],
                "review_note": a["review_note"], "by": a["extracted_by"]}
    except Exception as e:
        counts["failed"] += 1
        return {"row": r["row"], "sku": sku, "name": r.get("name"), "action": "failed", "error": str(e)[:300]}


def create_preview_job(conn, actor: str, filename: str, parsed: dict) -> dict:
    finalise_rows(conn, parsed)
    d = diff_rows(conn, parsed["rows"])
    summary = {"new": len(d["new"]), "changed": len(d["changed"]), "unchanged": len(d["unchanged"]),
               "missing": len(d["missing"]), "errors": d["errors"],
               "warnings": sum(1 for i in parsed["issues"] if i["level"] == "warning")}
    job = conn.execute("INSERT INTO import_jobs (created_by, filename, rows, summary) VALUES (%s, %s, %s, %s) RETURNING id",
                       (actor, filename, json.dumps(parsed["rows"]), json.dumps({"preview": summary}))).fetchone()
    return {"job_id": job["id"], "summary": summary, "diff": {k: d[k][:200] for k in ("new", "changed", "missing")},
            "issues": parsed["issues"][:300], "columns": parsed["columns"], "optional_missing": parsed["optional_missing"]}


# ------------------------------------------------------------------ template
def template_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    headers = ["SKU", "Name", "Price", "Description", "Margin (%)", "Priority", "Cost", "Stock", "Brand", "Category", "Active", "Expiry", "Ingredients"]
    ws.append(headers)
    ws.append(["SR-101", "Cica Calming Serum", 24.00,
               "A lightweight serum that calms redness and irritation and helps with breakouts. "
               "Suitable for sensitive, oily and combination skin, ages 18 and over. Fragrance-free.\n"
               "Ingredients: Water, Centella Asiatica Extract, Glycerin, Niacinamide, Panthenol, Madecassoside",
               0.70, 5, 7.20, 120, "Sample", "serum", "yes", "2027-06-30", ""])
    ws.append(["CR-205", "Ginseng Firming Night Cream", 52.00,
               "Rich night cream for mature, dry skin (40+) that softens fine lines and improves firmness. "
               "Not recommended during pregnancy.\n"
               "Ingredients: Water, Ginseng Root Extract, Shea Butter, Retinol, Adenosine, Fragrance",
               0.78, 3, 11.50, 25, "Sample", "", "yes", "", ""])
    for i, w in enumerate([12, 30, 10, 90, 14, 12, 10, 9, 14, 14, 9, 13, 50], start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    req = PatternFill("solid", fgColor="F9D5E1")
    opt = PatternFill("solid", fgColor="EEEEEE")
    for i, c in enumerate(ws[1]):
        c.font = Font(bold=True)
        c.fill = req if i < 4 else opt
    for row in ws.iter_rows(min_row=2, max_row=3):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    for row in (2, 3):
        ws.cell(row=row, column=5).number_format = "0.0%"
    dv = DataValidation(type="list", formula1='"' + ",".join(CATEGORIES) + '"', allow_blank=True)
    dv.add("J2:J5000")
    dv2 = DataValidation(type="list", formula1='"yes,no"', allow_blank=True)
    dv2.add("K2:K5000")
    ws.add_data_validation(dv)
    ws.add_data_validation(dv2)
    priority_dv = DataValidation(type="whole", operator="between", formula1=1, formula2=5, allow_blank=True)
    priority_dv.showErrorMessage = True
    priority_dv.error = "Enter a whole number from 1 to 5 (5 = highest priority)."
    priority_dv.add("F2:F5000")
    ws.add_data_validation(priority_dv)

    how = wb.create_sheet("How to fill")
    lines = [
        ("How to fill the product sheet", True),
        ("", False),
        ("Required (pink): SKU, Name, Price, Description.", False),
        ("Optional (grey): Margin (%), Priority (1-5), Cost (fallback when Margin is unknown), Stock (empty = always available), Brand, Category, Active (yes/no),", False),
        ("  Expiry (e.g. 2027-06-30 or 06/2027; empty = not tracked), Ingredients (if not already in the description).", False),
        ("", False),
        ("Empty cells are fine:", True),
        ("- Leave a cell empty (or write N/A) if you don't know the value. For a product that already exists, an empty cell keeps the current value.", False),
        ("- A NEW product needs at least SKU, Name and Price. Without a description it is imported but marked 'check'.", False),
        ("- Without an ingredient list, the product is hidden from customers who must avoid an ingredient (setting in Ranking strategy).", False),
        ("- A file with SKU plus Price, Stock, Margin or Priority updates just those values.", False),
        ("- Margin: enter 40% (or 40 in a plain numeric cell) for a 40% margin. Use 0% to give zero margin. Valid range: 0% to 100%.", False),
        ("- Margin affects ranking priority. Price only filters the customer's budget. Direct Margin overrides the margin calculated from Price and Cost.", False),
        ("- Priority: 1 = lowest, 3 = normal (default), 5 = highest. Give popular products a higher number to favor them among suitable options.", False),
        ("- Ranking strategy controls the balance of customer fit, margin and manual priority. Safety and budget filters always apply.", False),
        ("- Products past their expiry date, or expiring soon (setting), are not recommended.", False),
        ("", False),
        ("Writing a good Description - the AI reads it to decide who each product is for:", True),
        ("1. What it does: e.g. 'calms redness', 'fades dark spots', 'deep hydration'.", False),
        ("2. Who it is for: skin types (dry, oily, combination, normal, sensitive, or 'all skin types').", False),
        ("3. Age if relevant: e.g. 'ages 18-30', '40+', 'mature skin', 'teens'.", False),
        ("4. Who should avoid it: e.g. 'not recommended during pregnancy', 'not for sensitive skin'.", False),
        ("5. The full ingredient list at the end, starting with 'Ingredients:' and separated by commas.", False),
        ("", False),
        ("Upload: Admin portal > Import products > choose this file > check the preview > Import.", False),
        ("Rows with the same SKU update the existing product. Only new or changed descriptions are re-read by the AI.", False),
    ]
    for text, bold in lines:
        how.append([text])
        how.cell(row=how.max_row, column=1).font = Font(bold=bold, size=13 if bold else 11)
    how.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
