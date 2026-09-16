# 🔑 GitHub Multi-Account Setup Guide
# AutoAppy — New Account, Isolated from Copilot Account

This guide sets up a **completely separate** GitHub account for AutoAppy
without affecting your existing Copilot/work GitHub account.

---

## How It Works (The Key Idea)

Your machine uses **SSH aliases** to route each repo to a different GitHub
account using a different SSH key. The accounts never interfere with each other.

```
git@github-autoapply:YOUR_NEW_USERNAME/autoapply.git  → AutoAppy account
git@github.com:YOUR_COPILOT_USERNAME/some-repo.git    → Copilot account (unchanged)
```

---

## Step 1 — Create a New GitHub Account

1. Open a **private/incognito browser window**
2. Go to [github.com/signup](https://github.com/signup)
3. Use a **different email** than your Copilot account
   - You can use `tamilarasan20225@gmail.com`
4. Choose a username (e.g. `Tamilarasan20225`)
5. Complete signup — **no credit card needed**

---

## Step 2 — Add the SSH Key to Your New GitHub Account

Your SSH key is already generated. Copy it:
```bash
cat ~/.ssh/id_ed25519_autoapply.pub
```

In your **new GitHub account**:
1. **Settings → SSH and GPG keys → New SSH key**
2. Title: `AutoAppy Machine Key`
3. Key type: `Authentication Key`
4. Paste the public key → **Add SSH key**

---

## Step 3 — Create the Repository on New GitHub

1. Log into your **new GitHub account**
2. Click **"New repository"**
3. Settings:
   - **Name**: `autoapply`
   - **Visibility**: `Private` ← recommended (resume data is in here)
   - **Do NOT** initialize with README
4. Click **Create repository**

---

## Step 4 — Test SSH Connection

```bash
ssh -T git@github-autoapply
```

Expected: `Hi YOUR_NEW_USERNAME! You've successfully authenticated...`

---

## Step 5 — Add Remote and Push

```bash
cd /home/tamil-21253/Tamil/Experiments/AutoAppy

# Use the SSH alias (NOT github.com directly)
git remote add origin git@github-autoapply:YOUR_NEW_USERNAME/autoapply.git

# Verify
git remote -v

# Push everything
git push -u origin main
```

---

## Step 6 — Add GitHub Secrets for Actions

Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | Where to get it | Required |
|-------------|----------------|----------|
| `GH_PAT` | See Step 7 below | ✅ Yes |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) — free, no CC | ✅ Yes |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) — free, no CC | ✅ Recommended |
| `ADZUNA_APP_ID` | [developer.adzuna.com](https://developer.adzuna.com) | Optional |
| `ADZUNA_APP_KEY` | Same as above | Optional |
| `SERPER_API_KEY` | [serper.dev](https://serper.dev) — 2500 free searches | Optional |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram | Optional |
| `TELEGRAM_CHAT_ID` | From your bot | Optional |

---

## Step 7 — Create a Personal Access Token (PAT)

Needed so GitHub Actions can push the updated DB back to the repo.

1. New account → **Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens**
2. **Generate new token**:
   - Name: `AutoAppy Actions`
   - Expiration: 1 year
   - Repository access: Only `autoapply`
   - Permissions: `Contents` → **Read and Write**
3. Copy it → Add as `GH_PAT` secret

---

## Step 8 — Trigger a Test Run

1. Repo → **Actions → AutoAppy Daily Pipeline → Run workflow**
2. Wait 5–15 min
3. Check that `data/autoapply.db` was committed back and CSV artifact is available

---

## ✅ Verify Accounts Are Isolated

```bash
# AutoAppy repo — uses github-autoapply alias
git -C /home/tamil-21253/Tamil/Experiments/AutoAppy remote -v
# → git@github-autoapply:YOUR_USERNAME/autoapply.git ✅

# Copilot repos use github.com directly — completely unaffected ✅
```

---

## 🖥️ Setting Up on a New Machine in the Future

```bash
# 1. Generate new SSH key on the new machine
ssh-keygen -t ed25519 -C "autoapply-github" -f ~/.ssh/id_ed25519_autoapply -N ""

# 2. Add public key to GitHub account
cat ~/.ssh/id_ed25519_autoapply.pub
# → GitHub → Settings → SSH keys → New SSH key → paste

# 3. Add SSH alias
cat >> ~/.ssh/config << 'EOF'

Host github-autoapply
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_autoapply
    IdentitiesOnly yes
EOF

# 4. Clone the repo
git clone git@github-autoapply:YOUR_NEW_USERNAME/autoapply.git
cd autoapply

# 5. Set up environment
cp .env.example .env          # fill in your API keys
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # only needed for auto-apply

# 6. Sync latest DB from cloud
./sync_db.sh

# Done! Start using it
python run.py --dashboard
```

---

## 📋 Quick Reference — Daily Commands

```bash
./sync_db.sh                                    # pull latest DB from cloud
streamlit run autoapply/dashboard/app.py        # view scored jobs
python run.py --apply-only                      # apply to ready jobs

# Push code changes
git add . && git commit -m "feat: change" && git push   # → AutoAppy account ✅
```
