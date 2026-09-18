"""
Adzuna API client — aggregated job board with India coverage.
Free tier: 1,000 API calls/month.
Register at: https://developer.adzuna.com/
"""

import requests
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description
from autoapply.utils.key_pool import KeyPool, load_paired_keys_from_env, paired_env_names

console = Console()

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"


def fetch_adzuna_jobs(
    roles: list[str],
    locations: list[str] | None = None,
    country: str = "in",
    max_results: int = 50,
    app_id: str | None = None,
    app_key: str | None = None,
) -> List[RawJob]:
    """
    Fetch jobs from Adzuna API.

    Args:
        roles: List of job titles to search
        locations: Locations to search (Bangalore, Remote, etc.)
        country: Country code (in=India, gb=UK, us=US)
        max_results: Max results to collect across all queries
        app_id: Adzuna App ID (or set ADZUNA_APP_ID / ADZUNA_APP_ID_2... env vars)
        app_key: Adzuna App Key (or set ADZUNA_APP_KEY / ADZUNA_APP_KEY_2... env vars)

    Returns:
        List of RawJob objects
    """
    if app_id and app_key:
        pool = KeyPool([{"app_id": app_id, "app_key": app_key}])
    else:
        pool = KeyPool(load_paired_keys_from_env(paired_env_names("ADZUNA_APP_ID", "ADZUNA_APP_KEY")))

    if len(pool) == 0:
        console.print("[yellow]Adzuna:[/yellow] No API credentials — skipping. Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env")
        return []


    jobs: List[RawJob] = []
    seen_ids: set[str] = set()

    # Search with each role term
    per_role_limit = max(10, max_results // len(roles))
    location_query = " OR ".join(locations) if locations else "Bangalore"

    for role in roles[:5]:  # Limit API calls
        key_entry = pool.get()
        if key_entry is None:
            console.print("[yellow]Adzuna:[/yellow] All API key pairs exhausted/cooling down — stopping")
            break
        try:
            params = {
                "app_id": key_entry["app_id"],
                "app_key": key_entry["app_key"],
                "results_per_page": min(per_role_limit, 50),
                "what": role,
                "where": "India",
                "content-type": "application/json",
                "sort_by": "date",
            }

            url = f"{ADZUNA_BASE}/{country}/search/1"
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])

            for item in results:
                item_id = str(item.get("id", ""))
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                # Parse location
                loc = ""
                location_obj = item.get("location", {})
                if location_obj:
                    display_locs = location_obj.get("display_name", "")
                    loc = display_locs

                # Parse salary
                salary_min = item.get("salary_min")
                salary_max = item.get("salary_max")

                raw_url = item.get("redirect_url", "")

                # Detect ATS type from the redirect URL where possible
                # Adzuna redirect_url often contains the real ATS URL pattern
                from autoapply.applicator.ats_detector import detect_ats_from_url
                detected_ats = detect_ats_from_url(raw_url) or "other"

                job = RawJob(
                    external_id=f"adzuna_{item_id}",
                    source="adzuna",
                    title=item.get("title", "").strip(),
                    company=item.get("company", {}).get("display_name", "").strip(),
                    location=loc,
                    is_remote="remote" in loc.lower() or "remote" in item.get("title", "").lower(),
                    job_url=raw_url,
                    apply_url=raw_url,  # Will be resolved at apply time via HTTP follow
                    description=truncate_description(item.get("description", "")),
                    salary_min=float(salary_min) if salary_min else None,
                    salary_max=float(salary_max) if salary_max else None,
                    salary_currency="INR",
                    ats_type=detected_ats,
                )
                if job.title and job.company and job.job_url:
                    jobs.append(job)

            if len(jobs) >= max_results:
                break

        except requests.exceptions.ConnectionError:
            console.print("[yellow]Adzuna:[/yellow] Connection failed — skipping")
            break
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429:
                console.print("[yellow]Adzuna:[/yellow] Rate limited — cooling down this key pair, trying next")
                pool.mark_cooldown(key_entry, 3600)
            elif status in (401, 403):
                console.print("[yellow]Adzuna:[/yellow] Auth error — disabling this key pair")
                pool.mark_bad(key_entry)
            else:
                console.print(f"[yellow]Adzuna:[/yellow] HTTP {status} — check your API credentials")
            continue
        except Exception as e:
            console.print(f"[red]Adzuna error:[/red] {e}")
            continue

    console.print(f"[cyan]Adzuna:[/cyan] Fetched {len(jobs)} jobs")
    return jobs[:max_results]
