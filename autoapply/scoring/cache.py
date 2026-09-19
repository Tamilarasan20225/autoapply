"""
LLM Response Cache for AutoAppy.

Caches scoring results in SQLite to avoid re-scoring the same job description
against the same resume. This is especially valuable when:
  - Running the pipeline multiple times (daily re-runs)
  - Testing with the same job listings
  - Resume hasn't changed since last run

Cache key: SHA256(jd_hash + resume_hash)
Cache TTL: 7 days (jobs older than this are re-scored)
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Default cache DB path (can be same as main DB or separate)
_CACHE_DB_PATH = "data/llm_cache.db"
_CACHE_TTL_DAYS = 7


def _get_conn(db_path: str = _CACHE_DB_PATH) -> sqlite3.Connection:
    """Get a SQLite connection for the cache."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_cache(db_path: str = _CACHE_DB_PATH) -> None:
    """Create the cache table if it doesn't exist."""
    conn = _get_conn(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_cache (
            cache_key   TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            hit_count   INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def _make_cache_key(jd: str, resume_summary: str, skills_str: str, variant_hint: str = "",
                    resume_id: str = "", domain: str = "") -> str:
    """Generate a stable cache key from job description + resume snapshot + domain.
    Domain is included so different users with different domains get separate cache
    entries even for the same job (different system prompts → different scores).
    """
    jd_norm = " ".join(jd.split())[:2000]
    resume_norm = " ".join(resume_summary.split())[:500]
    raw = f"{jd_norm}|||{resume_norm}|||{skills_str}|||{variant_hint}|||{resume_id}|||{domain}"
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_get(
    jd: str,
    resume_summary: str,
    skills_str: str,
    variant_hint: str = "",
    db_path: str = _CACHE_DB_PATH,
    ttl_days: int = _CACHE_TTL_DAYS,
    resume_id: str = "",
    domain: str = "",
) -> Optional[dict]:
    """
    Retrieve a cached LLM score result.

    Returns:
        Parsed result dict if cache hit and not expired, else None.
    """
    try:
        init_cache(db_path)
        key = _make_cache_key(jd, resume_summary, skills_str, variant_hint, resume_id, domain)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()

        conn = _get_conn(db_path)
        row = conn.execute(
            "SELECT result_json, created_at FROM llm_cache WHERE cache_key = ? AND created_at > ?",
            (key, cutoff),
        ).fetchone()

        if row:
            # Increment hit counter
            conn.execute("UPDATE llm_cache SET hit_count = hit_count + 1 WHERE cache_key = ?", (key,))
            conn.commit()
            conn.close()
            return json.loads(row["result_json"])

        conn.close()
        return None
    except Exception:
        return None


def cache_set(
    jd: str,
    resume_summary: str,
    skills_str: str,
    result: dict,
    variant_hint: str = "",
    db_path: str = _CACHE_DB_PATH,
    resume_id: str = "",
    domain: str = "",
) -> None:
    """
    Store an LLM score result in cache.
    """
    try:
        init_cache(db_path)
        key = _make_cache_key(jd, resume_summary, skills_str, variant_hint, resume_id, domain)
        conn = _get_conn(db_path)
        conn.execute("""
            INSERT OR REPLACE INTO llm_cache (cache_key, result_json, created_at, hit_count)
            VALUES (?, ?, ?, 0)
        """, (key, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass  # Cache failures are non-fatal


def cache_stats(db_path: str = _CACHE_DB_PATH) -> dict:
    """Return cache statistics for dashboard display."""
    try:
        init_cache(db_path)
        conn = _get_conn(db_path)
        total = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        total_hits = conn.execute("SELECT SUM(hit_count) FROM llm_cache").fetchone()[0] or 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_CACHE_TTL_DAYS)).isoformat()
        expired = conn.execute(
            "SELECT COUNT(*) FROM llm_cache WHERE created_at <= ?", (cutoff,)
        ).fetchone()[0]
        conn.close()
        return {
            "total_cached": total,
            "total_hits": total_hits,
            "expired": expired,
            "active": total - expired,
        }
    except Exception:
        return {"total_cached": 0, "total_hits": 0, "expired": 0, "active": 0}


def purge_expired(db_path: str = _CACHE_DB_PATH) -> int:
    """Remove expired cache entries. Returns count of deleted rows."""
    try:
        init_cache(db_path)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_CACHE_TTL_DAYS)).isoformat()
        conn = _get_conn(db_path)
        cursor = conn.execute("DELETE FROM llm_cache WHERE created_at <= ?", (cutoff,))
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count
    except Exception:
        return 0
