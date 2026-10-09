# K-Beauty Product Recommender (starter)

Staff-facing recommender: customer skin profile + budget in, 3–5 ranked products with reasons out.
No model training. Your PostgreSQL holds products, ingredients, margin and priority; an LLM only
reranks and explains a shortlist that already passed your safety, stock and budget rules.
Everything is English: product names, INCI ingredient names, notes and the generated reasons.
Prices are stored in one currency, set with `CURRENCY` in `.env` (default USD).

```
employee note / form
   -> parse (keywords + LLM)            app/parsing.py
   -> SQL hard filters + pgvector search app/db.py
   -> business score (fit, margin, priority, campaign)   app/scoring.py
   -> LLM rerank + reasons, validated   app/recommender.py
   -> response + recommendation_log
```

| Mode | Chat model | Embeddings | Use for |
| --- | --- | --- | --- |
| `LLM_PROVIDER=ollama` | `qwen3.5:9b` (local) | `bge-m3` (local, 1024-d) | Local testing, no cloud |
| `LLM_PROVIDER=bedrock` | Claude Sonnet 5 / Haiku 4.5, or Qwen3 / DeepSeek V3.2 | Cohere Embed v4 (1024-d) | Production |
| `LLM_PROVIDER=mock` | none (rule-based fallback) | hash vectors | CI, DB testing |

## 1. Local test on Windows (Ollama + Qwen 3.5 9B)

Requirements: Python 3.11+, Docker Desktop, [Ollama for Windows](https://ollama.com/download).
`qwen3.5:9b` is about 6.6 GB: runs well on a GPU with 8 GB+ VRAM, or on CPU with 16 GB RAM (slower,
roughly 20–60 s per recommendation).

```powershell
cd C:\RAG\kbeauty-recommender

# 1) Local models
ollama pull qwen3.5:9b
ollama pull bge-m3

# 2) PostgreSQL 16 + pgvector in Docker
docker compose up -d

# 3) Python environment
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then set ADMIN_PASSWORD and SECRET_KEY

# 4) Create tables, load the fictional sample catalog, embed products
python -m app.ingest --init --seed --embed

# 5) Run
uvicorn app.main:app --port 8000
```

Open http://localhost:8000 . Try the note: `Dry, flaky skin with some redness. She is pregnant. Under $30.`
Tick "Show scores (managers)" to see the score breakdown (margin, priority, fit).

Checks: http://localhost:8000/health shows the provider, models and how many products are embedded.
Admin portal: http://localhost:8000/admin  (section 4)
API docs: http://localhost:8000/docs

If you have less GPU memory, set `OLLAMA_CHAT_MODEL=qwen3.5:4b` in `.env` (and `OLLAMA_PARSE_MODEL`).

## 2. Use your own database

- Point `DATABASE_URL` at your PostgreSQL and run `python -m app.ingest --init` (creates the tables
  if they don't exist; needs the `vector` extension: `CREATE EXTENSION vector;`).
- Easiest: import products from Excel in the admin portal (section 5). Or fill `products`, `product_attributes`, `ingredients`, `product_ingredients`, `business_rules`,
  `skin_concern_map` from your ERP / PIM (a nightly SQL job or ETL). If your tables already exist,
  create SQL **views** with these names and columns instead of copying data.
- **Don't** run `--seed` on a real database: it wipes the product tables.
- Run `python -m app.ingest --embed` after adding products (only new / un-embedded rows are sent).

## 3. Switch to Amazon Bedrock (production)

```powershell
aws configure                       # or an IAM role when deployed in AWS
aws bedrock list-inference-profiles --region ap-northeast-2   # find exact model / profile ids
```
In `.env`: `LLM_PROVIDER=bedrock`, set `BEDROCK_*` ids, then
`python -m app.ingest --reembed` (vectors from different embedding models are not comparable).
Keep `EMBED_DIM=1024` so the same table works for bge-m3 and Cohere Embed v4.

## 4. Admin portal (http://localhost:8000/admin)

Set `ADMIN_PASSWORD` and `SECRET_KEY` in `.env`, restart, and sign in with your name + that password.
Every change is written to the change log with the name and time.

| Tab | What managers can do |
| --- | --- |
| Ranking strategy | Pick a preset (Customer first, Balanced, Profit focus, Push priority products) or move two sliders: **Customer fit vs. Business value** (max 70% business) and **Profit margin vs. Product priority**. Choices per step (1-4). Optional guard: minimum skin match. Turn the AI step on/off. **Preview** a test customer under the saved vs. new strategy before saving. Changes apply to the next recommendation, no restart. |
| Import products | Upload an Excel or CSV file, check what will change, import. See section 5. |
| Products & campaigns | Per product: priority 1-5, campaign boost with start/end dates, hide. Click a product to see what the AI read from its description and correct it. Filter "Only products to check". |
| Activity & feedback | Recommendations, staff using it, feedback rate, sold and "not suitable" rates, average margin of recommended products vs. catalog, top products, recent recommendations, change log. |

Upgrading an existing database from an earlier version: run `python -m app.ingest --init` once
(adds the new tables and columns; existing data is kept).

For production, put the service behind HTTPS (the login cookie is sent on every request).

## 5. Updating products from Excel (for operations staff)

Admin portal > **Import products**. No technical knowledge needed.

1. **Fill the sheet.** Required columns: **SKU, Name, Price, Description**. Optional: Cost (needed for
   margin; without it margin counts as average), Stock (empty = always available), Brand, Category, Active
   (yes/no). Column names can vary ("Product Code", "Retail Price", "Details" also work).
   Click *Download Excel template* for a ready sheet with instructions, or see `samples/sample_products.xlsx`.
2. **Write good descriptions.** The AI reads them. Include what the product does, who it is for (skin types,
   age such as "ages 25-45", "40+", "teens"), who should avoid it ("not recommended during pregnancy",
   "not for sensitive skin"), and the full ingredient list after `Ingredients:`.
3. **Check the file.** Before anything is saved you see new / changed / unchanged products, rows with
   errors (with their Excel row number) and products in the database that are missing from the file.
4. **Import.** Products are saved, the AI reads only new or changed descriptions, and the products become
   searchable. Rows with the same SKU update the existing product.
5. **Review.** Products marked **check** need a look (e.g. no ingredient list, unclear category).
   Click a product to see the description, ingredients with their safety flags, and what the recommender
   uses; correct anything and Save.

What is decided by rules, not the AI: ingredient safety flags (fragrance, retinoids, AHA, BHA, alcohol,
essential oils) come from the ingredient list; retinoids always mean "avoid in pregnancy"; products
with strong actives are not marked safe for sensitive skin unless the description says so. The AI can
add a restriction but never remove one.

Speed: with `qwen3.5:9b` on a GPU, expect a few seconds per product (much slower on CPU); only new or
changed descriptions are read, so later imports are quick.

## 6. Multiple choices per step

Each routine step (cleanser, toner, serum, ...) shows up to N products (Ranking strategy > *Choices per
step*, default 3; staff can change it per request). The best match comes first; other options are only
offered when they score at least 60% of the best one, so staff are not shown weak fillers. Staff can
click *Customer bought this* on the option that sold; Activity counts sales per product.

## 7. Safety rules built in

- Out of stock, inactive, over budget, wrong skin type, or containing an ingredient the customer
  must avoid (pregnancy also excludes retinoids) -> never shown, whatever the margin.
- The LLM can add restrictions while parsing but never remove one; budgets come from the regex parser.
- Any SKU the LLM returns that is not on the shortlist is dropped; prices come from the database.
- Medical wording ("treats", "cures", "heals") is rewritten.
- If the model is down or returns bad JSON, a deterministic routine is returned (`used_llm: false`).

## 8. Tests

```powershell
pytest -q                                                      # unit tests, no DB needed
set TEST_DATABASE_URL=postgresql://rag:rag@localhost:5432/kbeauty_test
pytest -q                                                      # + end-to-end (wipes that DB!)
```
Create the test DB first: `docker compose exec db createdb -U rag kbeauty_test`.
