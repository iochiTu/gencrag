"""PostgreSQL + pgvector access. All product, margin and priority data stays here."""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from .config import ROOT, settings


def connect() -> psycopg.Connection:
    conn = psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=True)
    register_vector(conn)
    return conn


def run_sql_file(name: str) -> None:
    sql = (ROOT / "sql" / name).read_text(encoding="utf-8")
    sql = sql.replace("{{EMBED_DIM}}", str(settings.embed_dim))
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(sql)


CANDIDATE_SQL = """
SELECT p.sku, p.name, p.brand, p.category, p.routine_step,
       p.price::float AS price, p.stock_qty, p.description,
       ((p.price - p.cost) / NULLIF(p.price, 0))::float AS margin_pct,
       COALESCE(br.priority, 3) AS priority,
       CASE WHEN br.sku IS NOT NULL
             AND (br.valid_from IS NULL OR br.valid_from <= current_date)
             AND (br.valid_to   IS NULL OR br.valid_to   >= current_date)
            THEN br.campaign_boost::float ELSE 0 END AS campaign_boost,
       a.suitable_skin_types, a.target_concerns, a.fragrance_free,
       a.min_age, a.max_age, a.key_benefits, p.expiry_date,
       ing.inci IS NOT NULL AS ingredients_known,
       COALESCE(ing.inci, '{}') AS key_ingredients,
       1 - (p.embedding <=> %(qvec)s) AS similarity
FROM products p
JOIN product_attributes a USING (sku)
LEFT JOIN business_rules br USING (sku)
LEFT JOIN LATERAL (
    SELECT array_agg(i.inci_name ORDER BY pi.position) AS inci
    FROM product_ingredients pi JOIN ingredients i ON i.id = pi.ingredient_id
    WHERE pi.sku = p.sku
) ing ON TRUE
WHERE p.is_active
  AND NOT COALESCE(br.excluded, FALSE)
  AND (p.stock_qty IS NULL OR p.stock_qty > 0)
  AND p.embedding IS NOT NULL
  AND p.price BETWEEN %(pmin)s AND %(pmax)s
  AND (%(skin)s::text IS NULL OR %(skin)s::text = ANY(a.suitable_skin_types))
  AND (NOT %(sensitive)s OR 'sensitive' = ANY(a.suitable_skin_types))
  AND NOT (a.avoid_for && %(avoid)s::text[])
  AND NOT (COALESCE(a.text_flags, '{}') && %(avoid)s::text[])
  AND (p.expiry_date IS NULL OR p.expiry_date >= current_date + %(min_shelf)s::int)
  AND (NOT %(hide_unknown)s OR cardinality(%(avoid)s::text[]) = 0 OR ing.inci IS NOT NULL)
  AND NOT EXISTS (
        SELECT 1 FROM product_ingredients pi2 JOIN ingredients i2 ON i2.id = pi2.ingredient_id
        WHERE pi2.sku = p.sku AND i2.flags && %(avoid)s::text[])
  AND (cardinality(%(cats)s::text[]) = 0 OR p.category = ANY(%(cats)s::text[]))
ORDER BY p.embedding <=> %(qvec)s
LIMIT %(k)s
"""


def fetch_candidates(conn, *, qvec, skin, sensitive, avoid, cats, pmin, pmax, k,
                     min_shelf: int = 0, hide_unknown: bool = True) -> list[dict[str, Any]]:
    """Hard filters (stock, price, skin type, contraindications, expiry) + vector similarity, in one query.
    Missing data is handled safely: no expiry date = not tracked; no ingredient list = hidden from customers
    who must avoid something (unless the manager chose to show it with a warning)."""
    params = dict(
        qvec=np.asarray(qvec, dtype=np.float32),
        skin=skin,
        sensitive=bool(sensitive),
        avoid=list(avoid),
        cats=list(cats),
        pmin=float(pmin),
        pmax=float(pmax),
        k=int(k),
        min_shelf=int(min_shelf),
        hide_unknown=bool(hide_unknown),
    )
    return list(conn.execute(CANDIDATE_SQL, params).fetchall())


def log_recommendation(conn, *, employee_id, request, profile, candidates, picks, model, used_llm) -> int:
    row = conn.execute(
        """INSERT INTO recommendation_log
               (employee_id, request, profile, candidates, picks, model, used_llm)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (employee_id, json.dumps(request, ensure_ascii=False), json.dumps(profile, ensure_ascii=False),
         json.dumps(candidates, ensure_ascii=False), json.dumps(picks, ensure_ascii=False), model, used_llm),
    ).fetchone()
    return row["id"]


def save_feedback(conn, log_id: int, feedback: str, note: str | None, sku: str | None = None) -> bool:
    cur = conn.execute(
        "UPDATE recommendation_log SET feedback = %s, feedback_note = %s, feedback_sku = %s WHERE id = %s",
        (feedback, note, sku, log_id),
    )
    return cur.rowcount == 1
