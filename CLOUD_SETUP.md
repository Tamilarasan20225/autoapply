# ☁️ AutoAppy Cloud Setup — GitHub Actions (Free, No Credit Card)

This guide sets up **AutoAppy** to run **daily job discovery + scoring** on
GitHub Actions for free. Your local machine handles the dashboard and applying.

---

## 🏗️ Architecture

```
☁️  GitHub Actions (Cloud — Free)        💻  Your Local Machine
────────────────────────────────          ──────────────────────────────
Runs daily at 9:00 AM IST                 Run anytime you want
  python run.py --score-only
                                          View dashboard:
Commits updated DB → repo ──── git pull ──→  streamlit run autoapply/dashboard/app.py

                                          Apply to scored jobs:
                                            python run.py --apply-only
```

---

## 📋 One-Time Setup

### Step 1 — Push your repo to GitHub

```bash
cd /path/to/AutoAppy
git init                          # if not already a git repo
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/autoapply.git
git push -u origin main
```

> **IMPORTANT**: The `.gitignore` already excludes `.env` — your API keys are safe.
> But make sure `data/autoapply.db` is tracked (remove it from `.gitignore` if needed).

### Step 2 — Update .gitignore to track the database

Edit `.gitignore` and **remove or comment out** this line:
```
# data/*.db        ← comment this out so the DB is committed
```

Then commit:
```bash
git add .gitignore
git commit -m "chore: track autoapply.db in repo"
git push
```

### Step 3 — Create a GitHub Personal Access Token (PAT)

The workflow needs to push the updated DB back to your repo.

1. Go to: **GitHub → Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens**
2. Click **"Generate new token"**
3. Set:
   - **Name**: `AutoAppy Actions`
   - **Expiration**: 1 year
   - **Repository access**: Only select repositories → choose your `autoapply` repo
   - **Permissions**:
     - `Contents` → **Read and Write**
4. Click **Generate token** and copy it

### Step 4 — Add Secrets to GitHub

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these secrets:

| Secret Name | Value | Required |
|-------------|-------|----------|
| `GH_PAT` | Your PAT from Step 3 | ✅ Yes |
| `GEMINI_API_KEY` | From aistudio.google.com | ✅ Yes |
| `GROQ_API_KEY` | From console.groq.com | ✅ Recommended |
| `ADZUNA_APP_ID` | From developer.adzuna.com | Optional |
| `ADZUNA_APP_KEY` | From developer.adzuna.com | Optional |
| `SERPER_API_KEY` | From serper.dev | Optional |
| `TELEGRAM_BOT_TOKEN` | From @BotFather | Optional |
| `TELEGRAM_CHAT_ID` | Your chat ID | Optional |

### Step 5 — Verify the workflow runs

1. Go to your repo → **Actions** tab
2. Click **"AutoAppy Daily Pipeline"**
3. Click **"Run workflow"** → **Run workflow** (manual trigger to test)
4. Watch the logs — should take 5–15 minutes

---

## 📅 Schedule

The workflow runs automatically at:
- **3:30 AM UTC = 9:00 AM IST** every day

To change the time, edit `.github/workflows/daily-pipeline.yml`:
```yaml
- cron: '30 3 * * *'   # minute hour * * *  (UTC time)
```

---

## 💻 Daily Local Workflow

Each morning after the cloud run:

```bash
# 1. Pull latest DB from cloud
./sync_db.sh

# 2. View the dashboard
streamlit run autoapply/dashboard/app.py

# 3. Apply to jobs you like
python run.py --apply-only

# 4. Or run a full local pipeline
python run.py
```

---

## 🔧 Manual Triggers

You can trigger runs manually from GitHub Actions UI:

| Mode | What it does |
|------|-------------|
| `discover-only` | Only fetch jobs, no scoring (saves LLM quota) |
| `score-only` | Discover + score with LLM (default daily mode) |
| `rescore` | Re-score all previously scored jobs |
| `refresh` | Discover fresh jobs + score newest 48h batch |

Go to: **Actions → AutoAppy Manual Run → Run workflow → Choose mode**

---

## 📊 View Results Without Local Setup

After each run, GitHub Actions uploads a **CSV artifact**:

1. Go to **Actions → Latest run → Artifacts**
2. Download `autoapply-run-XXX`
3. Open `latest_jobs.csv` in Excel/Google Sheets

The CSV contains: job title, company, score, status, reasoning, URL.

---

## ❗ Important Notes

### Sources disabled in cloud (require Playwright)
These are automatically skipped in `--score-only` mode since they need a browser:
- LinkedIn Easy Apply
- Workday applications
- ICIMS applications

### Sources that work in cloud ✅
All HTTP-API based sources work fine:
- Remotive, RemoteOK, Adzuna
- Greenhouse, Lever, Ashby (direct ATS APIs)
- JobSpy (LinkedIn/Indeed scraping via HTTP)
- SmartRecruiters, Career Page Prober

### LLM Free Tier Limits
- **Gemini Flash**: 1,500 requests/day — covers ~150 jobs scored/day
- **Groq**: 1,000 requests/day fallback — covers ~100 jobs/day

With `--score-only` running once daily, you'll comfortably stay within free limits.

---

## 🔒 Security

- API keys are stored as **GitHub Secrets** — encrypted, never logged
- The `.env` file is in `.gitignore` — never committed
- DB contains only public job data + your scores — safe to commit
- `master_resume.json` is committed (it's your public resume data)
