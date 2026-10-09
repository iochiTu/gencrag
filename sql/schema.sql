-- K-Beauty recommender schema (English labels and INCI ingredient names) (PostgreSQL 16 + pgvector).
-- {{EMBED_DIM}} is replaced by EMBED_DIM from .env when you run: python -m app.ingest --init

CREATE EXTENSION IF NOT EXISTS vector;

-- Catalog: one row per sellable SKU
CREATE TABLE IF NOT EXISTS products (
    sku           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    brand         TEXT,
    category      TEXT NOT NULL CHECK (category IN
                  ('cleanser','toner','essence','serum','cream','sunscreen','mask')),
    routine_step  SMALLINT NOT NULL,           -- 1 cleanser .. 6 sunscreen, 7 mask
    price         NUMERIC(12,2) NOT NULL CHECK (price >= 0),   -- in CURRENCY from .env
    cost          NUMERIC(12,2) NOT NULL CHECK (cost >= 0),
    stock_qty     INTEGER NOT NULL DEFAULT 0,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    description   TEXT NOT NULL,
    embedding     vector({{EMBED_DIM}}),
    embedded_with TEXT,                        -- which embedding model produced the vector
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS products_embedding_hnsw
    ON products USING hnsw (embedding vector_cosine_ops);

-- Skin suitability, maintained by R&D
-- skin types: dry, oily, combination, normal, sensitive
-- concerns:   acne, dryness, redness, pigmentation, wrinkles, pores, dullness, oiliness
-- avoid_for:  pregnancy, rosacea, ...
CREATE TABLE IF NOT EXISTS product_attributes (
    sku                 TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE,
    suitable_skin_types TEXT[] NOT NULL DEFAULT '{}',
    target_concerns     TEXT[] NOT NULL DEFAULT '{}',
    avoid_for           TEXT[] NOT NULL DEFAULT '{}',
    texture             TEXT,
    fragrance_free      BOOLEAN NOT NULL DEFAULT FALSE
);

-- Ingredients; flags drive the safety filters
-- flags: fragrance, retinoid, aha, bha, alcohol, essential_oil
CREATE TABLE IF NOT EXISTS ingredients (
    id        SERIAL PRIMARY KEY,
    inci_name TEXT UNIQUE NOT NULL,
    function  TEXT,
    flags     TEXT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS product_ingredients (
    sku               TEXT REFERENCES products(sku) ON DELETE CASCADE,
    ingredient_id     INTEGER REFERENCES ingredients(id),
    position          SMALLINT NOT NULL DEFAULT 0,   -- order on the INCI list
    concentration_pct NUMERIC,
    PRIMARY KEY (sku, ingredient_id)
);

-- Commercial levers, maintained by sales / merchandising.
-- Margin itself is computed from price and cost.
CREATE TABLE IF NOT EXISTS business_rules (
    sku            TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE,
    priority       SMALLINT NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    campaign_boost NUMERIC  NOT NULL DEFAULT 0,      -- e.g. 0.05 during a promotion
    valid_from     DATE,                              -- boost window (NULL = always)
    valid_to       DATE,
    excluded       BOOLEAN  NOT NULL DEFAULT FALSE,   -- hide from recommendations
    updated_by     TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Added in v0.2 (safe to re-run on an existing database)
ALTER TABLE business_rules ADD COLUMN IF NOT EXISTS excluded BOOLEAN NOT NULL DEFAULT FALSE;

-- Settings changed in the admin portal (e.g. key 'ranking')
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Who changed what in the admin portal
CREATE TABLE IF NOT EXISTS admin_audit (
    id        BIGSERIAL PRIMARY KEY,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor     TEXT NOT NULL,
    area      TEXT NOT NULL,        -- ranking | product
    target    TEXT NOT NULL,        -- 'ranking' or a SKU
    old_value JSONB,
    new_value JSONB
);

-- R&D knowledge: which ingredients help each concern (used to enrich the search query)
CREATE TABLE IF NOT EXISTS skin_concern_map (
    concern             TEXT PRIMARY KEY,
    label               TEXT NOT NULL,
    helpful_ingredients TEXT[] NOT NULL DEFAULT '{}'
);

-- Every recommendation and the employee's feedback, for tuning and audit
CREATE TABLE IF NOT EXISTS recommendation_log (
    id            BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    employee_id   TEXT,
    request       JSONB,
    profile       JSONB,
    candidates    JSONB,
    picks         JSONB,
    model         TEXT,
    used_llm      BOOLEAN,
    feedback      TEXT CHECK (feedback IN ('accepted','swapped','sold','rejected')),
    feedback_note TEXT
);
