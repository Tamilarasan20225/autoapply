"""
Wellfound (AngelList) Job Scraper.

Wellfound is the go-to platform for startup jobs globally, with strong India coverage.
Scrapes job listings using their public search API (no login needed for public jobs).
"""

import re
import time
import requests
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description, clean_html

console = Console()

WELLFOUND_GRAPHQL = "https://wellfound.com/graphql"
WELLFOUND_SEARCH = "https://wellfound.com/jobs"


def fetch_wellfound_jobs(
    roles: list[str] | None = None,
    locations: list[str] | None = None,
    config: dict | None = None,
    limit: int = 50,
) -> List[RawJob]:
    """
    Scrape Wellfound job listings for startup/tech roles.

    Uses Wellfound's public search API with JSON response.
    Falls back to HTTP scraping if API returns unexpected format.

    Returns:
        List[RawJob]
    """
    jobs: List[RawJob] = []
    seen_ids: set[str] = set()

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://wellfound.com/jobs",
    }

    # Wellfound has a public jobs API endpoint
    # Try fetching the job listings page and parsing the Next.js __NEXT_DATA__
    search_roles = roles[:3] if roles else ["Software Engineer", "Backend Engineer", "Data Engineer"]
    location_filter = "India" if any("bangalore" in (l or "").lower() or "india" in (l or "").lower() for l in (locations or [])) else ""

    for role in search_roles[:2]:  # Limit to 2 roles to avoid rate limiting
        try:
            # Wellfound public search API
            params = {
                "q": role,
                "remote": "true" if not location_filter else "false",
            }
            if location_filter:
                params["l"] = location_filter

            r = requests.get(
                WELLFOUND_SEARCH,
                params=params,
                headers=headers,
                timeout=15,
            )

            if r.status_code != 200:
                console.print(f"[dim]Wellfound HTTP {r.status_code}[/dim]")
                continue

            # Parse __NEXT_DATA__ JSON embedded in HTML
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.DOTALL)
            if not match:
                console.print("[dim]Wellfound: __NEXT_DATA__ not found in response[/dim]")
                continue

            import json
            next_data = json.loads(match.group(1))

            # Navigate the Next.js data structure
            page_props = next_data.get("props", {}).get("pageProps", {})
            job_listings = (
                page_props.get("jobs", []) or
                page_props.get("jobListings", []) or
                next_data.get("props", {}).get("initialState", {}).get("jobs", {}).get("jobListings", []) or
                []
            )

            if not job_listings:
                # Try alternate path
                dehydrated = next_data.get("props", {}).get("dehydratedState", {})
                queries = dehydrated.get("queries", [])
                for q in queries:
                    data = q.get("state", {}).get("data", {})
                    if "jobListings" in data:
                        job_listings = data["jobListings"]
                        break
                    if isinstance(data, dict) and "edges" in data:
                        job_listings = [edge.get("node", {}) for edge in data.get("edges", [])]
                        break

            for item in job_listings[:limit]:
                # Handle both flat and nested formats
                if "jobListing" in item:
                    item = item["jobListing"]

                job_id = str(item.get("id", "") or item.get("slug", ""))
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                title = (item.get("title", "") or item.get("jobTitle", "")).strip()
                if not title:
                    continue

                # Company info
                company_obj = item.get("company", item.get("startup", {}))
                company_name = (company_obj.get("name", "") or company_obj.get("companyName", "")) if company_obj else ""
                company_name = company_name.strip() or "Unknown"

                # Location
                location_obj = item.get("locationNames", item.get("location", ""))
                if isinstance(location_obj, list):
                    location = ", ".join(location_obj[:2])
                else:
                    location = str(location_obj or "Remote")
                is_remote = item.get("remote", False) or "remote" in location.lower()

                # URL
                slug = item.get("slug", job_id)
                startup_slug = (company_obj or {}).get("slug", "")
                job_url = f"https://wellfound.com/jobs/{slug}" if slug else f"https://wellfound.com/company/{startup_slug}/jobs"

                # Description
                desc = item.get("description", item.get("jobDescription", "")) or ""

                # Salary
                sal_min = sal_max = None
                compensation = item.get("compensation", item.get("salary", {}))
                if isinstance(compensation, dict):
                    sal_min = compensation.get("min")
                    sal_max = compensation.get("max")

                job = RawJob(
                    external_id=f"wellfound_{job_id}",
                    source="wellfound",
                    title=title,
                    company=company_name,
                    location=location or "Remote",
                    is_remote=is_remote,
                    job_url=job_url,
                    apply_url=job_url,
                    description=truncate_description(clean_html(desc)),
                    ats_type="other",
                    salary_min=float(sal_min) if sal_min else None,
                    salary_max=float(sal_max) if sal_max else None,
                    salary_currency="USD",
                    source_query=role,
                )
                jobs.append(job)

            time.sleep(1)  # Polite rate limiting

        except Exception as e:
            console.print(f"[dim]Wellfound error for '{role}': {e}[/dim]")

    console.print(f"[cyan]Wellfound:[/cyan] Fetched {len(jobs)} startup jobs")
    return jobs
