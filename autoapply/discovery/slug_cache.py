"""
ATS Slug Cache — Persistent JSON cache for company → ATS slug mappings.

Stores results of ATS probing so we don't re-probe the same companies
every run. Cache entries expire after 7 days for active companies and
30 days for inactive ones (0 jobs found).

Cache file: data/ats_slug_cache.json
Schema per entry:
  {
    "company": "Swiggy",
    "ats_type": "greenhouse",
    "slug": "swiggy",
    "jobs_found": 12,
    "last_probed": "2026-09-16",
    "active": true
  }
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from rich.console import Console

console = Console()

CACHE_FILE = "data/ats_slug_cache.json"
ACTIVE_TTL_DAYS = 7      # Re-probe active companies every 7 days
INACTIVE_TTL_DAYS = 30   # Re-probe inactive companies every 30 days


def _load_cache(cache_path: str = CACHE_FILE) -> dict:
    """Load the slug cache from disk."""
    try:
        p = Path(cache_path)
        if p.exists():
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cache(cache: dict, cache_path: str = CACHE_FILE) -> None:
    """Save the slug cache to disk."""
    try:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        console.print(f"[dim]SlugCache: save error: {e}[/dim]")


def get_cached_slug(company_name: str, cache_path: str = CACHE_FILE) -> Optional[dict]:
    """
    Get cached ATS info for a company if fresh enough.

    Returns:
        dict with keys: ats_type, slug, jobs_found, active
        None if not cached or cache expired
    """
    cache = _load_cache(cache_path)
    key = company_name.strip().lower()
    entry = cache.get(key)
    if not entry:
        return None

    # Check TTL
    last_probed_str = entry.get("last_probed", "")
    if last_probed_str:
        try:
            last_probed = datetime.strptime(last_probed_str, "%Y-%m-%d")
            is_active = entry.get("active", True)
            ttl = ACTIVE_TTL_DAYS if is_active else INACTIVE_TTL_DAYS
            if datetime.now() - last_probed < timedelta(days=ttl):
                return entry
        except Exception:
            pass
    return None  # Expired


def set_cached_slug(
    company_name: str,
    ats_type: str,
    slug: str,
    jobs_found: int = 0,
    cache_path: str = CACHE_FILE,
) -> None:
    """Update the cache with a probing result."""
    cache = _load_cache(cache_path)
    key = company_name.strip().lower()
    cache[key] = {
        "company": company_name,
        "ats_type": ats_type,
        "slug": slug,
        "jobs_found": jobs_found,
        "last_probed": datetime.now().strftime("%Y-%m-%d"),
        "active": jobs_found > 0,
    }
    _save_cache(cache, cache_path)


def add_discovered_slug(
    ats_type: str,
    slug: str,
    company_name: str = "",
    source: str = "serper",
    cache_path: str = CACHE_FILE,
) -> bool:
    """
    Add a newly discovered slug (e.g. from Serper SERP results).
    Only adds if not already cached.

    Returns:
        True if new entry was added, False if already known
    """
    cache = _load_cache(cache_path)
    key = (company_name or slug).strip().lower()
    if key in cache:
        return False
    cache[key] = {
        "company": company_name or slug,
        "ats_type": ats_type,
        "slug": slug,
        "jobs_found": -1,  # -1 = discovered but not yet probed
        "last_probed": "",
        "active": True,
        "discovered_via": source,
    }
    _save_cache(cache, cache_path)
    return True


def get_unprobed_slugs(cache_path: str = CACHE_FILE) -> list[dict]:
    """
    Return all cache entries that have never been probed (jobs_found == -1).
    These are slugs discovered via Serper but not yet fetched for jobs.
    """
    cache = _load_cache(cache_path)
    return [
        entry for entry in cache.values()
        if entry.get("jobs_found", 0) == -1
    ]


def get_all_active_slugs(cache_path: str = CACHE_FILE) -> dict[str, list[str]]:
    """
    Return all active slugs grouped by ATS type.
    Used to supplement hardcoded slug lists in config.

    Returns:
        {"greenhouse": ["slug1", "slug2"], "lever": [...], "ashby": [...]}
    """
    cache = _load_cache(cache_path)
    result: dict[str, list[str]] = {"greenhouse": [], "lever": [], "ashby": [], "workable": []}

    for entry in cache.values():
        if not entry.get("active", True):
            continue
        ats = entry.get("ats_type", "")
        slug = entry.get("slug", "")
        if ats in result and slug:
            result[ats].append(slug)

    return result


def cache_stats(cache_path: str = CACHE_FILE) -> dict:
    """Return statistics about the slug cache."""
    cache = _load_cache(cache_path)
    total = len(cache)
    active = sum(1 for e in cache.values() if e.get("active", True))
    unprobed = sum(1 for e in cache.values() if e.get("jobs_found", 0) == -1)
    by_ats: dict[str, int] = {}
    for e in cache.values():
        ats = e.get("ats_type", "unknown")
        by_ats[ats] = by_ats.get(ats, 0) + 1
    return {
        "total": total,
        "active": active,
        "inactive": total - active - unprobed,
        "unprobed": unprobed,
        "by_ats": by_ats,
    }
