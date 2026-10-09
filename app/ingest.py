"""Database setup and embedding.

    python -m app.ingest --init      create tables (safe to re-run)
    python -m app.ingest --seed      load the fictional sample catalog (wipes product tables!)
    python -m app.ingest --embed     embed products that have no vector yet
    python -m app.ingest --reembed   re-embed every product (after changing the embedding model)
"""
from __future__ import annotations

import argparse
import sys

import numpy as np

from .config import settings
from .db import connect, run_sql_file
from .providers import ProviderError, embed_model_label, get_provider

PRODUCT_TEXT_SQL = """
SELECT p.sku, p.name, p.brand, p.category, p.description,
       a.suitable_skin_types, a.target_concerns, a.texture,
       COALESCE((SELECT string_agg(i.inci_name, ', ' ORDER BY pi.position)
                 FROM product_ingredients pi JOIN ingredients i ON i.id = pi.ingredient_id
                 WHERE pi.sku = p.sku), '') AS ingredients
FROM products p LEFT JOIN product_attributes a USING (sku)
WHERE p.is_active {where}
ORDER BY p.sku
"""


def product_text(r: dict) -> str:
    return " | ".join(x for x in [
        r["name"], f"brand {r.get('brand') or ''}", f"category {r['category']}",
        r["description"],
        "skin types: " + ", ".join(r.get("suitable_skin_types") or []),
        "concerns: " + ", ".join(r.get("target_concerns") or []),
        f"texture {r.get('texture') or ''}", "ingredients: " + r["ingredients"],
    ] if x)


def embed_products(reembed: bool = False) -> int:
    provider = get_provider()
    with connect() as conn:
        rows = conn.execute(PRODUCT_TEXT_SQL.format(where="" if reembed else "AND p.embedding IS NULL")).fetchall()
        if not rows:
            print("Nothing to embed.")
            return 0
        texts = [product_text(r) for r in rows]
        print(f"Embedding {len(rows)} products with {provider.name}:{embed_model_label()} ...")
        try:
            vecs = provider.embed(texts, kind="document")
        except ProviderError as e:
            sys.exit(f"ERROR: {e}")
        if len(vecs[0]) != settings.embed_dim:
            sys.exit(f"Embedding size {len(vecs[0])} != EMBED_DIM {settings.embed_dim}. "
                     f"Set EMBED_DIM to {len(vecs[0])} in .env and re-run --init on an empty DB, or pick another model.")
        with conn.cursor() as cur:
            for r, v in zip(rows, vecs):
                cur.execute("UPDATE products SET embedding = %s, embedded_with = %s, updated_at = now() WHERE sku = %s",
                            (np.asarray(v, dtype=np.float32), embed_model_label(), r["sku"]))
        print(f"Done: {len(rows)} products embedded.")
        return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--reembed", action="store_true")
    a = ap.parse_args()
    if not any(vars(a).values()):
        ap.print_help()
        return
    if a.init:
        run_sql_file("schema.sql")
        print("Schema ready.")
    if a.seed:
        run_sql_file("seed_sample.sql")
        print("Sample catalog loaded.")
    if a.embed or a.reembed:
        embed_products(reembed=a.reembed)


if __name__ == "__main__":
    main()
