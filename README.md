# 🤖 AutoAppy — Automated Job Application System

> **Zero-cost, self-hosted, AI-powered job application automation.**
> Built specifically for Tamilarasan S — Backend & AI Engineer, targeting Bangalore + Remote roles.

---

## What It Does

| Phase | What happens |
|---|---|
| **1. Discovery** | Pulls jobs from 8 sources **concurrently** — Remotive, RemoteOK, Arbeitnow, Adzuna, Greenhouse API, Lever API, **Ashby API** (new!), and LinkedIn/Indeed via JobSpy |
| **2. Pre-filter** | Title-based hard reject (non-tech roles) + company blacklist — saves LLM quota |
| **3. Scoring** | LLM scores each JD against your resume (0–100) with **7-day result caching** — same JD never scored twice |
| **4. Tailoring** | LLM rewrites your resume summary (mirroring JD language), generates ATS plain-text + HTML + PDF versions, and writes a 3-paragraph cover letter (`.txt` + `.html`) |
| **5. Application** | Submits via Greenhouse Playwright → Lever Playwright → Ashby → LinkedIn Easy Apply → Manual alert |
| **6. Tracking** | SQLite + Streamlit dashboard: every application, interview stage, follow-up date, manual status override |
| **7. Alerts** | **Telegram** (instant, free) + email notifications for manual apply needed + daily digest after each run |

**Total LLM cost: $0** (uses Gemini Flash + Groq free tiers)

---

## Quick Start

### 1. Clone & Set Up

```bash
cd /path/to/AutoAppy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure API Keys

```bash
cp .env.example .env
```

Edit `.env` with your keys:

```env
# Required — get free at aistudio.google.com (no credit card)
GEMINI_API_KEY=your_key_here

# Recommended fallback — get free at console.groq.com (no credit card)
GROQ_API_KEY=your_key_here

# Optional — for Telegram notifications (highly recommended!)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Optional — for broader job coverage
ADZUNA_APP_ID=your_id
ADZUNA_APP_KEY=your_key

# For LinkedIn automation (stored locally only)
LINKEDIN_EMAIL=your@email.com
LINKEDIN_PASSWORD=yourpassword
```

### 3. Review Your Profile

Check `config.yaml` — your roles, keywords, and locations are pre-configured.
Check `master_resume.json` — your full experience data is pre-populated.

### 4. Run

```bash
# Full pipeline (discover → score → tailor → apply)
python run.py

# Test first — fill forms but don't submit
python run.py --dry-run

# Just find jobs, don't score or apply yet
python run.py --discover-only

# Find + score, don't apply yet
python run.py --score-only

# Apply to already-scored jobs (no re-discovery needed!)
python run.py --apply-only

# View your dashboard
python run.py --dashboard
# or: streamlit run autoapply/dashboard/app.py

# Run daily on a schedule (9 AM IST)
python run.py --schedule
```

---

## Free LLM Providers (No Credit Card Needed)

| Provider | Free Limit | Get Key |
|---|---|---|
| **Google Gemini Flash** | 1,500 req/day, 1M context | [aistudio.google.com](https://aistudio.google.com) |
| **Groq** (Llama 3.3 70B) | 1,000 req/day, 100K tokens | [console.groq.com](https://console.groq.com) |
| **GitHub Models** | 150–1,000 req/day | [github.com/marketplace/models](https://github.com/marketplace/models) |

The system automatically fails over between providers. **LLM results are cached for 7 days** — same job description won't be re-scored, saving your daily quota.

---

## Free Job Sources

| Source | What You Get | Key Required |
|---|---|---|
| **Remotive** | Remote tech jobs globally | ❌ No |
| **RemoteOK** | Remote jobs, tagged | ❌ No |
| **Arbeitnow** | Europe/Remote, visa flags | ❌ No |
| **Greenhouse** | Direct from company ATS | ❌ No |
| **Lever** | Direct from company ATS | ❌ No |
| **Ashby** ⭐ New | Direct from Ashby ATS (Linear, Ramp, etc.) | ❌ No |
| **Adzuna** | Aggregated, India focus | ✅ Free (1k/mo) |
| **JobSpy** | LinkedIn + Indeed scraper | ❌ No |

> All 8 sources now run **concurrently** (parallel HTTP) — discovery is ~5x faster than before.

---

## Project Structure

```
AutoAppy/
├── run.py                        # Main entry point
├── config.yaml                   # Your preferences (edit this)
├── master_resume.json            # Your raw resume data (edit this)
├── .env                          # API keys (never commit this)
├── requirements.txt
│
├── autoapply/
│   ├── discovery/                # Job fetching from all sources (concurrent)
│   │   ├── remotive.py
│   │   ├── arbeitnow.py
│   │   ├── remoteok.py
│   │   ├── adzuna.py
│   │   ├── ashby.py              # ⭐ New: Ashby ATS direct API
│   │   ├── ats_direct.py         # Greenhouse + Lever direct APIs
│   │   ├── jobspy_scraper.py     # LinkedIn/Indeed scraper
│   │   └── deduplicator.py       # Cross-source dedup + keyword filter
│   │
│   ├── scoring/                  # LLM scoring engine
│   │   ├── llm_client.py         # Multi-provider LLM with failover
│   │   ├── scorer.py             # Job ↔ Resume matching (with cache)
│   │   ├── cache.py              # ⭐ New: SQLite LLM response cache (7-day TTL)
│   │   └── prompts.py            # Dynamic prompts (reads from resume, not hardcoded)
│   │
│   ├── generator/                # Document generation
│   │   ├── resume_builder.py     # Tailored HTML + PDF + ATS plain-text
│   │   └── cover_letter.py       # Targeted cover letters (.txt + .html)
│   │
│   ├── applicator/               # Application submission
│   │   ├── greenhouse_api.py     # Greenhouse (kept for reference)
│   │   ├── lever_api.py          # Lever (kept for reference)
│   │   ├── playwright_runner.py  # Greenhouse/Lever/LinkedIn Playwright (event-loop safe)
│   │   └── __init__.py           # Application orchestrator
│   │
│   ├── tracker/                  # Database layer
│   │   ├── models.py             # SQLAlchemy models (interview stages, screenshot path)
│   │   └── db.py                 # CRUD operations (SQLAlchemy 2.x, thread-safe)
│   │
│   ├── notifier/                 # ⭐ New: Notification system
│   │   ├── telegram.py           # Telegram Bot (instant alerts, free)
│   │   └── digest.py             # Daily digest after each pipeline run
│   │
│   ├── utils/                    # ⭐ New: Utilities
│   │   └── logger.py             # Rotating file logger + structured log events
│   │
│   └── dashboard/
│       └── app.py                # Enhanced Streamlit dashboard
│
├── data/
│   ├── autoapply.db              # SQLite database (auto-created)
│   └── llm_cache.db              # LLM response cache (auto-created)
│
└── outputs/
    ├── resumes/                  # Generated resumes (.html, .pdf, _ats.txt)
    └── cover_letters/            # Generated cover letters (.txt, .html)
```

---

## Scoring Logic

```
Job Description + Your Resume → LLM → Score (0-100)
                              ↑
                     (Cache checked first)

Score ≥ 75  → AUTO-APPLY  (tailored resume + cover letter → submitted)
Score 60-74 → REVIEW      (documents generated → you apply manually)
Score < 60  → SKIP        (silently ignored)
```

The LLM determines which resume **variant** to use:
- `backend` — emphasizes distributed systems, pipelines, APIs
- `ai_ml` — emphasizes LLMs, RAG, NLP, ML initiatives
- `balanced` — balanced backend + AI profile
- `data_infra` — emphasizes search infrastructure, data scale

---

## Application Priority

```
1. Greenhouse Playwright  → Fill web form directly
2. Lever Playwright       → Fill web form directly
3. LinkedIn Easy Apply    → Browser automation
4. Manual alert           → Telegram/email + console (docs already prepared)
```

---

## Dashboard

```bash
streamlit run autoapply/dashboard/app.py
# Opens at http://localhost:8501
```

### What's in the Dashboard

- **Live pipeline control** — Discover / Score / Apply / Dry Run / Apply-Only buttons
- **Auto-apply ready jobs** — Top-scored jobs with one-click Apply / Interview / Skip buttons
- **Manual apply queue** — Documents already prepared; just click and submit
- **🆕 Interview pipeline tracker** — Track stage (Phone → Technical → Offer), add notes
- **🆕 Manual status override** — Mark any job as Applied / Interview / Skipped directly
- **🆕 Source analytics** — Which sources give the most qualified matches
- **🆕 LLM cache stats** — How many LLM calls were saved today
- **Follow-up reminders** — Applied jobs approaching 7-day follow-up window
- **Pipeline run history** — Duration, discovered, scored, applied per run

---

## Telegram Notifications (Setup 5 min)

Get instant mobile alerts when a manual application is needed:

```bash
# 1. Message @BotFather on Telegram
#    → /newbot → get TELEGRAM_BOT_TOKEN

# 2. Message your new bot once (any text)

# 3. Get your chat ID
python -c "from autoapply.notifier.telegram import get_chat_id; get_chat_id()"

# 4. Add to .env
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

You'll receive:
- 📋 **Manual apply needed** — with URL and document paths
- ✅ **Auto-applied** — confirmation per successful submission
- 📊 **Daily digest** — end-of-run summary after each pipeline run

---

## Adding More Target Companies

Edit `config.yaml`:

```yaml
sources:
  # Greenhouse
  greenhouse_targets:
    companies:
      - "your-company"       # boards.greenhouse.io/{slug}

  # Lever
  lever_targets:
    companies:
      - "your-company"       # jobs.lever.co/{slug}

  # Ashby (many top startups)
  ashby_targets:
    companies:
      - "your-company"       # jobs.ashbyhq.com/{slug}

  # Company blacklist
search:
  exclude_companies:
    - "Company A"             # Never show jobs from this company
```

---

## LLM Cache

The system caches scoring results to **avoid wasting your daily LLM quota**:

```
Same JD + same resume profile → Cache hit → No LLM call needed
Cache TTL: 7 days
Cache location: data/llm_cache.db
```

You can see cache hit stats in the dashboard under **🧠 LLM Cache Stats**.

---

## Daily Schedule

Enable in `config.yaml`:
```yaml
scheduler:
  enabled: true
  run_time: "09:00"       # Runs at 9 AM IST every day
  timezone: "Asia/Kolkata"
```

Then run:
```bash
python run.py --schedule
```

Or add to cron:
```bash
0 9 * * * cd /path/to/AutoAppy && .venv/bin/python run.py >> logs/cron.log 2>&1
```

---

## Privacy & Safety

- **Credentials never leave your machine** — LinkedIn password only used by Playwright locally
- **No cloud dependency** — fully self-hosted, SQLite database
- **Rate limiting** — max 20 applications/day by default (configurable)
- **Human-like behavior** — random delays between actions to avoid detection
- **Dry run mode** — test everything without submitting a single application
- **Company blacklist** — never apply to specific companies
- **No data training** — Groq and Gemini Flash (with proper settings) don't train on your data

---

## Troubleshooting

**No LLM keys → scoring skipped**
```bash
cat .env | grep API_KEY
```

**JobSpy rate limited**
```bash
# Reduce results or disable in config.yaml
sources:
  jobspy:
    results_wanted: 10
```

**LinkedIn login fails**
- Run with `headless: false` in config.yaml to see the browser
- Solve any CAPTCHA manually on first run — session is saved for future runs

**WeasyPrint not working (PDF)**
```bash
sudo apt-get install python3-weasyprint libpangocairo-1.0-0
# Or just use HTML resumes — works fine for uploading
```

**Telegram alerts not working**
```bash
# Check bot token and chat ID
python -c "from autoapply.notifier.telegram import get_chat_id; get_chat_id()"
```

---

## Built With (All Free)

- **LiteLLM** — unified LLM interface with failover
- **Playwright** — browser automation
- **JobSpy** — multi-board job scraper
- **SQLAlchemy** — database ORM (2.x compatible)
- **Streamlit** — dashboard
- **WeasyPrint** — HTML→PDF
- **APScheduler** — daily scheduling
- **Rich** — beautiful CLI output
- **nest_asyncio** — event loop safety (Streamlit + Playwright)

---

*AutoAppy — Zero cost. Full control. Ship more applications.*
