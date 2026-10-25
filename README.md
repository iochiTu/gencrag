# Gen C Beauty Product Assistant

A chat assistant for sales staff (phone, iPad, desktop). Staff describe a customer in plain English and
get products to offer, best match first with a few choices per routine step, or ask questions about a
product. No model training: your PostgreSQL holds the products; the AI only picks and explains from a
shortlist that already passed the safety, stock, expiry and budget rules.

| Page | Who | What |
| --- | --- | --- |
| `/` | Staff (own login) | Chat assistant, installable on iOS/Android home screens |
| `/admin` | Managers (`ADMIN_PASSWORD`) | Ranking strategy, Excel product import, products & campaigns, staff accounts, analytics |
| `/docs` | Developers | API documentation |

```
staff message ─> chat (app/chat.py): remembers the customer, follow-ups, product questions
                  └─> parse (keywords + AI)            app/parsing.py
                  └─> SQL filters + vector search      app/db.py        stock, budget, skin type, allergens, expiry
                  └─> business score                   app/scoring.py   fit, margin, priority, campaigns (admin sliders)
                  └─> AI picks + reasons, validated    app/recommender.py
                  └─> analytics_events + recommendation_log
```

| `LLM_PROVIDER` | Chat model | Embeddings | Use for |
| --- | --- | --- | --- |
| `ollama` | `qwen3.5:9b` (local) | `bge-m3` (local, 1024-d) | Local testing, no cloud |
| `bedrock` | Claude Sonnet 5 / Haiku 4.5, Qwen3, DeepSeek V3.2 | Cohere Embed v4 (1024-d) | Production on AWS |
| `foundry` | Your Microsoft Foundry deployments (GPT-4.1/5, DeepSeek, Llama, Grok, ...) | e.g. `text-embedding-3-large` at 1024-d | Production on Azure |
| `mock` | none (rule-based) | hash vectors | Tests |

## 1. Local testing (Ollama + Qwen 3.5 9B)

### macOS

For an environment already set up locally, start Docker Desktop, then run:

```bash
cd /path/to/gencrag
bash scripts/start-local.sh
```

Open http://localhost:8000 for chat or http://localhost:8000/admin for the manager portal.
Local test login details are saved in `.local/local-access.md` when provided during setup.
The launcher starts PostgreSQL and Ollama if needed, then runs the chatbot in the foreground;
Ctrl+C stops the chatbot. It preserves existing products and accounts.
Project-local Ollama models live in `.local/models`, and its log is `.local/logs/ollama.log`.
If a database port is occupied, set `POSTGRES_PORT` in `.env` and use that same port in
`DATABASE_URL` (this Mac's local setup uses `55432`).
The configured Mac setup uses `qwen3.5:9b-q4_K_M`, an explicit Qwen 9B quantization,
for both `OLLAMA_CHAT_MODEL` and `OLLAMA_PARSE_MODEL`.
To stop PostgreSQL, run `docker compose stop`; to stop the project-local Ollama server,
run `kill "$(cat .local/ollama.pid)"` if this launcher started it.

For a fresh Mac setup, install Python 3.11+, Docker Desktop, and Ollama, then:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env  # set ADMIN_PASSWORD and a random SECRET_KEY
ollama pull qwen3.5:9b
ollama pull bge-m3
docker compose up -d
.venv/bin/python -m app.ingest --init --seed --embed  # new, empty database only
.venv/bin/python -m app.users add tester "Local Tester"
bash scripts/start-local.sh
```

The launcher uses `.local/models` when it starts Ollama. If you pulled models using an installed
Ollama, keep that Ollama server running before launching (so it uses the same model store).

### Windows

Requirements: Python 3.11+, Docker Desktop, [Ollama for Windows](https://ollama.com/download).
`qwen3.5:9b` is about 6.6 GB: runs well on a GPU with 8 GB+ VRAM, or on CPU with 16 GB RAM (slower).

```powershell
cd C:\RAG\kbeauty-recommender
ollama pull qwen3.5:9b
ollama pull bge-m3
docker compose up -d                              # PostgreSQL 16 + pgvector
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env                            # then set ADMIN_PASSWORD and SECRET_KEY
python -m app.ingest --init --seed --embed        # tables + fictional sample catalog
python -m app.users add mina "Mina Park"          # first staff login (asks for a password)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 (chat) and http://localhost:8000/admin (managers).
Upgrading an existing database: run `python -m app.ingest --init` once (adds new tables and columns,
keeps your data). Never run `--seed` on real data: it wipes the product tables.

## 2. The chat app (staff)

- **Sign in** with the account a manager created (Admin > Staff accounts, or `python -m app.users add`).
  First sign-in asks for a new password. Phones stay signed in for `STAFF_SESSION_DAYS` (30).
- **Describe the customer**: "Combination skin, breakouts and redness, 24, under $30". The answer shows
  routine steps; each step has up to 3 choices (best match first) with reasons, benefits, key
  ingredients and warnings ("ingredient list not on file", "expires in 20 days", "only 2 left").
- **Refine** in follow-up messages or tap a suggestion: "cheaper options", "she is pregnant",
  "she's not pregnant", "only serums", "full routine", "more choices", "no budget limit".
- **Remove a constraint** with the × on a profile chip in the latest result. Recommendations refresh
  using the remaining skin type, concerns, age, budget, ingredient restrictions and categories.
- **Ask about products**: "Is the Retinol Repair Serum OK during pregnancy?", "What's the difference
  between the Cica Repair Ampoule and the Spot Serum?" Answers use only the product data on file.
- **New customer** clears what the chat remembered. Past chats are listed in the menu.
- Stock filtering remains active. Sale-recording APIs are retained for a future purchase feature;
  product cards currently have no purchase button.

Design follows gencbeauty.com: white pages, navy (#0F0F2D) bars, uppercase Nunito buttons, Cabin text,
pill search-style input, green prices. Logo from `BRAND_LOGO_URL` (falls back to a text wordmark).

**Install on phones and iPads** (needs HTTPS, see section 9):
iPhone/iPad: open the site in Safari > Share > *Add to Home Screen*.
Android: open in Chrome > menu > *Install app* (or *Add to Home screen*).
It then opens full screen like an app. Layout: phones get a slide-out menu and swipeable product
choices; iPad landscape and desktops get a sidebar with chat history.

## 3. Admin portal (managers, `/admin`)

| Tab | What managers can do |
| --- | --- |
| Ranking strategy | Presets or two sliders: **customer fit vs. business value** (max 70% business) and **profit margin vs. product priority**. Choices per step (1-4). Minimum skin match. AI on/off. Hide products expiring within N days. Products without an ingredient list: hide from customers who must avoid something (default) or show with a warning. Preview a test customer before saving. |
| Import products | Upload Excel/CSV, check what will change, import (section 4). |
| Products & campaigns | Priority 1-5, campaign boosts with dates, hide. Click a product to see and correct what the AI read (category, skin types, concerns, avoid, ages, expiry, benefits). Filter "only products to check". Retry making products searchable if the model was down. |
| Staff accounts | Add accounts (temporary password shown once), reset passwords (signs out everywhere), disable/enable. Shows last sign-in and usage. |
| Activity & analytics | Chat analytics (section 6), recommendations & feedback, most recommended products, change log. |

Every change is logged with the manager's name and time. Put the service behind HTTPS in production.

## 4. Updating products from Excel (operations staff)

1. **Fill the sheet.** Only **SKU** is required as a column. A new product needs **SKU, Name, Price**
   (a Description is strongly advised). Add **Margin (%)** and **Priority** for business ranking.
   Optional: Cost (margin fallback for older catalogs), Stock, Brand, Category, Active,
   **Expiry**, **Ingredients**. Column names can vary ("Product Code", "Retail Price", "Details", "Best
   before", "INCI" all work). *Download Excel template* gives a sheet with instructions;
   `samples/sample_products.xlsx` is an example.
2. **Write good descriptions**: what it does, who it is for (skin types, age), who should avoid it, and the
   ingredient list after `Ingredients:` (or in an Ingredients column).
3. **Check the file**: new / changed / unchanged products, problems by Excel row number, and active
   products missing from the file, before anything is saved.
4. **Import**: the AI reads only new or changed descriptions. Progress is shown.
5. **Review** products marked **check** and correct them if needed.

**Margin (%)** accepts `40%`, an Excel percentage cell, or plain `40` for a 40% margin (range 0–100%).
Direct margin overrides the calculation from Price and Cost, so price changes do not change that margin.
**Priority** accepts whole numbers from 1 to 5: 1 = lowest, 3 = normal (default), 5 = highest.
Give popular products a higher priority to favor them among suitable choices. The Ranking strategy sliders
control how much margin and manual priority affect recommendations. Price only filters budget eligibility;
Qwen receives no product price in its ranking shortlist. Safety and suitability filters always apply.
You can use just `SKU, Name, Price, Description, Margin (%), Priority`; Cost is not needed when Margin is supplied.

### Missing or messy data is fine

| In the file | What happens |
| --- | --- |
| Empty cell, `N/A`, `-`, `TBD`, `#N/A`, `#VALUE!` | Treated as "not given". For an existing product the current value is kept. |
| Bad optional value (margin outside 0–100%, priority outside 1–5, cost "abc", stock "lots", unreadable date) | Warning, value ignored, the row is still imported. |
| New product without name or price | That row is skipped with an explanation; the rest is imported. |
| No description | Imported, the AI reads only the name, marked *check*. |
| No ingredient list | Imported, marked *check*. Safety flags are also read from the description text ("with 0.3% retinol"; "fragrance-free" does not count). By default hidden from customers who must avoid an ingredient; setting in Ranking strategy. |
| No expiry date | Treated as not expiring. Expired products, and (setting) those expiring within 30 days, are never recommended. |
| Dates | Excel date cells, `2027-06-30`, `20270630`, `06/2027`, `Mar 2027`, `31/03/2027` are read exactly. Ambiguous `03/04/2027` follows `DATE_ORDER` (MDY default) and shows a warning. Month-only = end of that month. |
| A file with SKU plus Price, Stock, Margin or Priority | Updates just those values. Empty cells preserve existing values; margin/priority-only changes do not re-read descriptions or re-embed products. |
| Notes or totals rows without SKU/name | Ignored. Duplicate SKUs: the second row is skipped. |
| AI model down during import | Products are saved using rules only; *Retry now* in Products makes them searchable later. |

Ingredient safety flags (fragrance, retinoids, AHA, BHA, alcohol, essential oils) are rule-based, never
left to the AI. Retinoids always mean "avoid in pregnancy". The AI can add a restriction but never remove one.

## 5. Model providers

**Amazon Bedrock**: `LLM_PROVIDER=bedrock`, `aws configure` (or an IAM role), set `BEDROCK_*` ids
(`aws bedrock list-inference-profiles --region ap-northeast-2`).

**Microsoft Foundry**: `LLM_PROVIDER=foundry` and in `.env`:
```
FOUNDRY_ENDPOINT=https://<your-resource>.openai.azure.com/openai/v1/
FOUNDRY_AUTH=key                      # or entra (az login / managed identity; pip install azure-identity)
FOUNDRY_API_KEY=...
FOUNDRY_CHAT_DEPLOYMENT=gpt-4.1-mini  # deployment NAME from the Foundry portal (case-sensitive)
FOUNDRY_PARSE_DEPLOYMENT=             # optional cheaper deployment for reading notes/descriptions
FOUNDRY_EMBED_DEPLOYMENT=text-embedding-3-large
```
Works with models on Foundry's OpenAI-compatible endpoint (GPT, DeepSeek, Llama, Grok, ...). Models that
reject JSON mode or temperature are retried automatically; rate limits (429) are retried with back-off.
Embeddings are requested at `EMBED_DIM` (1024); a model with a fixed different size is reported clearly.

After switching provider or embedding model: `python -m app.ingest --reembed`.

## 6. Analytics

Every chat message, recommendation, product question, sale, feedback and sign-in is written to
`analytics_events` (who, when, what was asked, the customer profile, products shown, result, response
time, whether the AI or the rules answered). Logging never blocks the chat.

Admin > Activity & analytics shows: messages, conversations, active staff, recommendations, product
questions, no-result rate, response time (average and 90th percentile), AI share, sales; chats per day;
usage per staff member; what staff asked for; top skin concerns and skin types; most asked-about
products; **searches with no suitable product** (catalog gaps); errors. **Export CSV** downloads the
raw events (UTF-8, opens in Excel) for Power BI or other tools. `recommendation_log` keeps every
recommendation with its candidates and feedback; `chat_messages` keeps the full conversations.

## 7. Tuning

Ranking strategy in the admin portal. Priority (1-5) and campaign boosts per product. Feedback and sales
from staff appear in Activity.

## 8. Safety rules built in

- Out of stock, inactive, hidden, expired (or expiring soon), over budget, wrong skin type, or containing
  an ingredient the customer must avoid -> never shown, whatever the margin.
- The AI can add restrictions while parsing but never remove one; budgets come from the rule parser.
- Any SKU the AI returns that is not on the shortlist is dropped; prices come from the database.
- Medical wording ("treats", "cures", "heals") is rewritten. Product answers use only data on file.
- If the model is down or returns bad output, staff still get a rule-based answer.

## 9. Running for phones (network and HTTPS)

`uvicorn app.main:app --host 0.0.0.0 --port 8000` makes the app reachable on your network
(http://<pc-ip>:8000). Browsers only allow *install to home screen* and secure cookies over **HTTPS**, so
for real use put it behind HTTPS: a reverse proxy (Caddy, nginx, IIS) with a certificate, or a cloud host
(Azure App Service / Container Apps, AWS App Runner / ECS). Sessions use HTTP-only cookies and are marked
secure automatically behind HTTPS.

## 10. Tests

```powershell
pytest -q                                                      # unit tests, no DB needed
set TEST_DATABASE_URL=postgresql://rag:rag@localhost:5432/kbeauty_test
pytest -q                                                      # + end-to-end (wipes that DB!)
```
Create the test DB first: `docker compose exec db createdb -U rag kbeauty_test`.
