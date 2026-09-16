#!/bin/bash
# sync_db.sh — Pull the latest database from GitHub Actions to your local machine
# Run this before opening the dashboard to get the most recent cloud data
#
# Usage: ./sync_db.sh

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "🔄 Syncing latest AutoAppy database from cloud..."

# Pull latest changes (includes DB committed by GitHub Actions)
git pull --rebase origin main

# Show DB info
if [ -f "data/autoapply.db" ]; then
    DB_SIZE=$(du -sh data/autoapply.db | cut -f1)
    echo "✅ Database synced! Size: $DB_SIZE"

    # Quick stats
    python3 - <<'EOF'
import sqlite3
conn = sqlite3.connect("data/autoapply.db")
cur = conn.cursor()
cur.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY COUNT(*) DESC")
rows = cur.fetchall()
print("\n📊 Job Status Summary:")
for status, count in rows:
    print(f"   {status:15s}: {count}")

cur.execute("SELECT COUNT(*) FROM jobs WHERE match_score IS NOT NULL AND match_score >= 65")
ready = cur.fetchone()[0]
print(f"\n🎯 Jobs ready to apply (score ≥ 65): {ready}")

cur.execute("SELECT COUNT(*) FROM jobs WHERE discovered_at >= datetime('now', '-24 hours')")
fresh = cur.fetchone()[0]
print(f"🆕 Discovered in last 24 hours: {fresh}")
conn.close()
EOF
else
    echo "⚠️  No database found after sync"
fi

echo ""
echo "💡 Next steps:"
echo "   View dashboard : streamlit run autoapply/dashboard/app.py"
echo "   Apply to jobs  : python run.py --apply-only"
echo "   Full local run : python run.py"
