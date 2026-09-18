"""
Jooble Job Search API — Free tier: 500 requests/month.
Jooble is a global job aggregator with strong India coverage.

API endpoint: https://jooble.org/api/{API_KEY}
Method: POST with JSON body
Free tier: Register at https://jooble.org/api/registered to get a free key.
"""

import time
import requests
from typing import List, Optional
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description, clean_html
from autoapply.utils.key_pool import KeyPool, load_keys_from_env

console = Console()

JOOBLE_API_BASE = "https://jooble.org/api/{api_key}"


def fetch_jooble_jobs(
    roles: list[str],
    locations: list[str] | None = None,
    api_key: str | None = None,
    max_results: int = 200,
) -> List[RawJob]:
    """
    Fetch jobs from Jooble API for multiple roles and locations.

    Args:
        roles: List of role titles to search for
        locations: List of locations (Bangalore, Remote, India)
        api_key: Jooble API key (or set JOOBLE_API_KEY / JOOBLE_API_KEY_2... env vars)
        max_results: Max jobs to return total

    Returns:
        List of RawJob objects
    """
    if api_key:
        pool = KeyPool([{"key": api_key}])
    else:
        pool = KeyPool(load_keys_from_env("JOOBLE_API_KEY"))

    if len(pool) == 0:
        console.print("[dim]Jooble: No API key configured (JOOBLE_API_KEY) — skipping[/dim]")
        return []

    jobs: List[RawJob] = []
    seen_urls: set = set()

    # Target locations for India
    target_locations = locations or ["India", "Bangalore", "Remote"]

    # Use top 4 roles to stay within 500 req/month budget
    top_roles = roles[:4]

    for role in top_roles:
        for location in target_locations[:2]:  # 2 locations per role = 8 queries max
            key_entry = pool.get()
            if key_entry is None:
                console.print("[yellow]Jooble: All API keys exhausted/cooling down — stopping[/yellow]")
                return jobs[:max_results]
            url = JOOBLE_API_BASE.format(api_key=key_entry["key"])
            try:
                payload = {
                    "keywords": role,
                    "location": location if location.lower() not in ("remote", "hybrid") else "India",
                    "page": 1,
                    "searchMode": 1,
                }
                response = requests.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=20,
                )
                if response.status_code == 403:
                    console.print("[yellow]Jooble: Invalid API key or quota exceeded — trying next key[/yellow]")
                    pool.mark_bad(key_entry)
                    continue
                if response.status_code != 200:
                    console.print(f"[dim]Jooble HTTP {response.status_code} for {role}/{location}[/dim]")
                    continue

                data = response.json()
                job_list = data.get("jobs", [])

                for item in job_list:
                    job_url = item.get("link", "").strip()
                    if not job_url or job_url in seen_urls:
                        continue
                    seen_urls.add(job_url)

                    title = item.get("title", "").strip()
                    if not title:
                        continue

                    company = item.get("company", "").strip() or "Unknown"
                    loc = item.get("location", "").strip() or location
                    description = clean_html(item.get("snippet", "") or "")
                    posted_at = item.get("updated", "").strip()[:10] or None
                    salary_str = item.get("salary", "").strip() or None
                    is_remote = (
                        "remote" in title.lower()
                        or "remote" in loc.lower()
                        or "work from home" in (description or "").lower()
                    )

                    job = RawJob(
                        external_id=f"jooble_{abs(hash(job_url)) % 10**12}",
                        source="jooble",
                        title=title,
                        company=company,
                        location=loc,
                        is_remote=is_remote,
                        job_url=job_url,
                        apply_url=job_url,
                        description=truncate_description(description) if description else None,
                        posted_at=posted_at,
                        source_query=f"{role}/{location}",
                    )
                    jobs.append(job)

                    if len(jobs) >= max_results:
                        break

                time.sleep(0.5)  # Polite delay

            except requests.exceptions.Timeout:
                console.print(f"[dim]Jooble: Timeout for {role}/{location}[/dim]")
            except Exception as e:
                console.print(f"[dim]Jooble: {type(e).__name__}: {e}[/dim]")

            if len(jobs) >= max_results:
                break
        if len(jobs) >= max_results:
            break

    console.print(f"[cyan]Jooble:[/cyan] Found {len(jobs)} jobs")
    return jobs[:max_results]
