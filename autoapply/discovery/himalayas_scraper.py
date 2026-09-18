"""
Himalayas.app Job Scraper - Remote-first tech jobs, free, no auth required.

Himalayas is a remote job board focused on tech.
API: GET https://himalayas.app/jobs/api?q={keyword}&limit={n}
Response: {totalCount, jobs: [{title, companyName, applicationLink, guid, description, ...}]}
Free, no API key needed. 100k+ active remote jobs.
"""

import time
import requests
from typing import List
from rich.console import Console
from autoapply.discovery.base import RawJob, truncate_description, clean_html

console = Console()
HIMALAYAS_API = "https://himalayas.app/jobs/api"
HIMALAYAS_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0", "Accept": "application/json"}


def fetch_himalayas_jobs(roles=None, max_results=100):
    """
    Fetch remote tech jobs from Himalayas.app public API.
    Searches multiple role keywords and deduplicates by guid.

    Args:
        roles: List of role keywords to search for
        max_results: Maximum total jobs to return

    Returns:
        List of RawJob objects (all remote=True)
    """
    jobs = []
    seen_guids = set()
    search_terms = roles[:4] if roles else ["backend engineer", "software engineer", "python developer", "ai engineer"]

    for term in search_terms:
        try:
            params = {"q": term, "limit": min(50, max_results)}
            r = requests.get(HIMALAYAS_API, params=params, headers=HIMALAYAS_HEADERS, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
            job_list = data.get("jobs", [])

            for item in job_list:
                guid = item.get("guid", "") or item.get("applicationLink", "")
                if not guid or guid in seen_guids:
                    continue
                seen_guids.add(guid)

                title = item.get("title", "").strip()
                if not title:
                    continue

                company = item.get("companyName", "").strip() or "Unknown"
                job_url = item.get("applicationLink", "") or item.get("guid", "")
                if not job_url:
                    continue

                # Location from restrictions
                loc_raw = item.get("locationRestrictions", "Worldwide")
                if isinstance(loc_raw, str) and loc_raw.startswith("["):
                    import ast
                    try:
                        locs = ast.literal_eval(loc_raw)
                        location = ", ".join(locs) if locs else "Worldwide"
                    except Exception:
                        location = loc_raw.strip("[]'\"") or "Worldwide"
                else:
                    location = str(loc_raw) or "Worldwide"

                # Description
                desc_raw = item.get("description", "") or item.get("excerpt", "")
                description = truncate_description(clean_html(desc_raw)) if desc_raw else None

                # Dates - pubDate is Unix timestamp
                posted_at = None
                pub_ts = item.get("pubDate", "")
                if pub_ts:
                    try:
                        from datetime import datetime
                        posted_at = datetime.fromtimestamp(int(pub_ts)).strftime("%Y-%m-%d")
                    except Exception:
                        pass

                # Salary
                sal_min = item.get("minSalary")
                sal_max = item.get("maxSalary")
                currency = item.get("currency", "USD") or "USD"

                # Seniority (already a list from API)
                seniority_raw = item.get("seniority", [])
                if isinstance(seniority_raw, list):
                    seniority = seniority_raw[0].lower() if seniority_raw else None
                elif isinstance(seniority_raw, str):
                    seniority = seniority_raw.lower() or None
                else:
                    seniority = None

                job = RawJob(
                    external_id=f"himalayas_{abs(hash(guid)) % 10**12}",
                    source="himalayas",
                    title=title,
                    company=company,
                    location=location,
                    is_remote=True,
                    job_url=job_url,
                    apply_url=job_url,
                    description=description,
                    posted_at=posted_at,
                    salary_min=float(sal_min) if sal_min else None,
                    salary_max=float(sal_max) if sal_max else None,
                    salary_currency=currency,
                    seniority=seniority,
                    source_query=term,
                )
                jobs.append(job)

                if len(jobs) >= max_results:
                    break

            time.sleep(0.5)

        except requests.exceptions.Timeout:
            console.print(f"[dim]Himalayas: Timeout for '{term}'[/dim]")
        except Exception as e:
            console.print(f"[dim]Himalayas: {type(e).__name__}: {str(e)[:60]}[/dim]")

        if len(jobs) >= max_results:
            break

    console.print(f"[cyan]Himalayas:[/cyan] Found {len(jobs)} remote jobs")
    return jobs[:max_results]
