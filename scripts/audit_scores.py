#!/usr/bin/env python3
"""
Read-only scoring audit. Produces a markdown report used as the before/after
baseline when comparing scoring v1 against v2.

    python scripts/audit_scores.py > /tmp/audit_before.md
"""

import argparse
import sqlite3
import statistics
import sys

DEFAULT_DB = "data/autoapply.db"

SENIOR_KEYWORDS = (
    "senior", "staff", "principal", "lead", "architect",
    "manager", "director", " vp",
)
GEO_MARKERS = (
    "united states", "us only", "usa only", "u.s. only", "eu only",
    "europe only", "must be located in the us", "emea only", "us-based",
    "based in the us", "authorized to work in the united states",
    "visa sponsorship is not available", "no visa sponsorship",
)


def clip(value, n: int) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:n]


def table(rows, headers) -> None:
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        cells = ["NULL" if c is None else str(c).replace("|", "\\|") for c in row]
        print("| " + " | ".join(cells) + " |")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = lambda sql, params=(): con.execute(sql, params).fetchall()

    cols = {r[1] for r in q("PRAGMA table_info(jobs)")}
    has_v2 = "score_version" in cols

    print("# AutoAppy scoring audit\n")

    print("## 1. Schema\n")
    tables = [r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("Tables: " + ", ".join(tables) + "\n")

    print("## 2. Row counts\n")
    print(f"jobs total: **{q('SELECT COUNT(*) FROM jobs')[0][0]}**\n")
    table(
        q("SELECT COALESCE(status,'NULL'), COUNT(*) FROM jobs GROUP BY 1 ORDER BY 2 DESC"),
        ["status", "count"],
    )
    if has_v2:
        table(
            q("SELECT COALESCE(score_version,'unscored'), COUNT(*) FROM jobs GROUP BY 1 ORDER BY 1"),
            ["score_version", "count"],
        )

    print("## 3. Description quality by source\n")
    rows = []
    for source, count in q("SELECT COALESCE(source,'NULL'), COUNT(*) FROM jobs GROUP BY 1 ORDER BY 2 DESC"):
        thin = q(
            "SELECT COUNT(*) FROM jobs WHERE COALESCE(source,'NULL')=? "
            "AND (description IS NULL OR length(description)<50)",
            (source,),
        )[0][0]
        lens = [
            r[0] for r in q(
                "SELECT length(description) FROM jobs "
                "WHERE COALESCE(source,'NULL')=? AND description IS NOT NULL",
                (source,),
            )
        ]
        rows.append((
            source, count, thin,
            round(statistics.mean(lens), 1) if lens else "-",
            int(statistics.median(lens)) if lens else "-",
        ))
    table(rows, ["source", "jobs", "desc NULL/<50", "avg len", "median len"])

    print("## 4. Score distribution\n")
    table(
        q("SELECT COUNT(*), MIN(match_score), MAX(match_score), ROUND(AVG(match_score),2) "
          "FROM jobs WHERE match_score IS NOT NULL"),
        ["scored", "min", "max", "avg"],
    )
    print(f"match_score == 0 exactly: **{q('SELECT COUNT(*) FROM jobs WHERE match_score=0')[0][0]}**\n")
    table(
        q("""SELECT CASE WHEN match_score>=90 THEN '90-100'
                 ELSE CAST(CAST(match_score/10 AS INT)*10 AS TEXT)||'-'||
                      CAST(CAST(match_score/10 AS INT)*10+9 AS TEXT) END,
                 COUNT(*)
             FROM jobs WHERE match_score IS NOT NULL
             GROUP BY 1 ORDER BY MIN(match_score)"""),
        ["bucket", "count"],
    )

    print("## 5. Truncation cap\n")
    print(f"- description >= 2900 chars: **{q('SELECT COUNT(*) FROM jobs WHERE length(description)>=2900')[0][0]}**")
    print(f"- description 2900-3100 chars: **{q('SELECT COUNT(*) FROM jobs WHERE length(description) BETWEEN 2900 AND 3100')[0][0]}**\n")

    extra = ", score_version, verdict, score_model, hard_gate_failures" if has_v2 else ""
    select_cols = (
        "id, title, company, source, match_score, tfidf_score, seniority_level, "
        "length(description) AS dlen, description, score_reasoning, skill_gaps, "
        "location, is_remote" + extra
    )

    def detail(rows, desc_chars):
        if not rows:
            print("_none_\n")
            return
        for r in rows:
            keys = r.keys()
            print(f"#### [{r['id']}] {r['title']} — {r['company']}")
            meta = (
                f"- source=`{r['source']}` score=**{r['match_score']}** "
                f"tfidf={r['tfidf_score']} seniority={r['seniority_level']} "
                f"desc_len={r['dlen']} location={r['location']} remote={r['is_remote']}"
            )
            if "score_version" in keys:
                meta += f" v={r['score_version']} verdict={r['verdict']} model={r['score_model']}"
            print(meta)
            print(f"- desc: {clip(r['description'], desc_chars)}")
            print(f"- reasoning: {clip(r['score_reasoning'], 600)}")
            print(f"- skill_gaps: {clip(r['skill_gaps'], 400)}")
            if "hard_gate_failures" in keys and r["hard_gate_failures"]:
                print(f"- gates: {clip(r['hard_gate_failures'], 400)}")
            print()

    print("## 6. Top 20 by score\n")
    detail(q(f"SELECT {select_cols} FROM jobs WHERE match_score IS NOT NULL "
             "ORDER BY match_score DESC, id LIMIT 20"), 400)

    print("## 7. Random 20 in the 55-80 review band\n")
    detail(q(f"SELECT {select_cols} FROM jobs WHERE match_score BETWEEN 55 AND 80 "
             "ORDER BY RANDOM() LIMIT 20"), 600)

    print("## 8a. Score >= 70 with over-senior titles (should be penalised)\n")
    where = " OR ".join("LOWER(title) LIKE ?" for _ in SENIOR_KEYWORDS)
    detail(q(f"SELECT {select_cols} FROM jobs WHERE match_score>=70 AND ({where}) "
             "ORDER BY match_score DESC LIMIT 10",
             tuple(f"%{k}%" for k in SENIOR_KEYWORDS)), 300)

    print("## 8b. Score >= 65 with US/EU-only signals (should be gated)\n")
    geo_where = " OR ".join(
        "LOWER(COALESCE(description,'')) LIKE ? OR LOWER(COALESCE(location,'')) LIKE ?"
        for _ in GEO_MARKERS
    )
    geo_params = tuple(x for k in GEO_MARKERS for x in (f"%{k}%", f"%{k}%"))
    detail(q(f"SELECT {select_cols} FROM jobs WHERE match_score>=65 AND ({geo_where}) "
             "ORDER BY match_score DESC LIMIT 10", geo_params), 300)

    print("## 8c. Score >= 60 on a thin description (<400 chars)\n")
    detail(q(f"SELECT {select_cols} FROM jobs WHERE match_score>=60 "
             "AND length(description) < 400 ORDER BY match_score DESC LIMIT 10"), 400)

    print("## 9. Companies\n")
    print(f"distinct companies: **{q('SELECT COUNT(DISTINCT company) FROM jobs')[0][0]}**\n")
    table(
        q("SELECT company, COUNT(*), ROUND(AVG(match_score),1) FROM jobs "
          "GROUP BY company ORDER BY 2 DESC LIMIT 15"),
        ["company", "jobs", "avg score"],
    )

    print("## 10. Discovery timeline\n")
    table(q("SELECT MIN(discovered_at), MAX(discovered_at), SUM(discovered_at IS NULL) FROM jobs"),
          ["min", "max", "null"])
    last7 = q("SELECT COUNT(*) FROM jobs WHERE discovered_at >= datetime('now','-7 days')")[0][0]
    print(f"last 7 days: **{last7}**\n")
    table(
        q("SELECT date(discovered_at), COUNT(*) FROM jobs WHERE discovered_at IS NOT NULL "
          "GROUP BY 1 ORDER BY 1 DESC LIMIT 14"),
        ["date", "count"],
    )

    if "source_runs" in tables:
        print("## 11. Source health (last 50 runs)\n")
        table(
            q("SELECT source, COUNT(*), SUM(jobs_returned), SUM(error IS NOT NULL) "
              "FROM source_runs GROUP BY source ORDER BY 3 DESC"),
            ["source", "runs", "total jobs", "errors"],
        )

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
