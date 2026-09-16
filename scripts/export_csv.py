"""Export latest scored jobs to CSV — called by GitHub Actions after scoring."""
import sqlite3
import csv
from pathlib import Path

db_path = "data/autoapply.db"
if not Path(db_path).exists():
    print("No DB found — skipping export")
    exit(0)

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("""
    SELECT title, company, location, match_score, status,
           tfidf_score, score_reasoning, job_url, discovered_at
    FROM jobs
    WHERE discovered_at >= datetime('now', '-7 days')
    ORDER BY match_score DESC NULLS LAST, discovered_at DESC
    LIMIT 200
""")
rows = cur.fetchall()

Path("outputs").mkdir(exist_ok=True)
with open("outputs/latest_jobs.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["title", "company", "location", "score", "status",
                     "tfidf_score", "reasoning", "url", "discovered_at"])
    writer.writerows(rows)

print(f"Exported {len(rows)} jobs to outputs/latest_jobs.csv")

cur.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY COUNT(*) DESC")
print("\nDB Summary:")
for status, count in cur.fetchall():
    print(f"  {status}: {count}")

cur.execute("SELECT COUNT(*) FROM jobs WHERE discovered_at >= datetime('now', '-24 hours')")
print(f"\nDiscovered in last 24h: {cur.fetchone()[0]}")

cur.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source ORDER BY COUNT(*) DESC LIMIT 15")
print("\nJobs by source:")
for source, count in cur.fetchall():
    print(f"  {source}: {count}")

conn.close()
