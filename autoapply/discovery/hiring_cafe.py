"""
hiring.cafe aggregator.

Keyless POST search that normalizes postings from 100k+ company ATS boards and
returns full descriptions — the highest coverage-per-line source available.
"""

from __future__ import annotations

from typing import List, Optional

from rich.console import Console

from autoapply.discovery import http
from autoapply.discovery.base import RawJob, clean_html, truncate_description

console = Console()

SEARCH_URL = "https://hiring.cafe/api/search-jobs"
_PAGE_SIZE = 40


def _build_payload(query: str, locations: list[str], page: int) -> dict:
    return {
        "size": _PAGE_SIZE,
        "page": page,
        "searchState": {
            "searchQuery": query,
            "locations": [
                {"formatted_address": loc, "types": ["place"]} for loc in locations[:3]
            ],
            "workplaceTypes": ["Remote", "Hybrid", "Onsite"],
            "sortBy": "date",
        },
    }


def _parse_job(item: dict, query: str) -> Optional[RawJob]:
    info = item.get("job_information") or {}
    processed = item.get("v5_processed_job_data") or item.get("processed_job_data") or {}
    company_data = item.get("v5_processed_company_data") or item.get("company") or {}

    title = (info.get("title") or processed.get("core_job_title") or "").strip()
    company = (company_data.get("name") or item.get("company_name") or "").strip()
    url = (item.get("apply_url") or item.get("job_url") or "").strip()
    if not (title and company and url):
        return None

    description = clean_html(info.get("description") or item.get("description") or "")

    location = processed.get("formatted_workplace_location") or ""
    if not location:
        city = processed.get("requirements_summary") or ""
        location = city
    workplace = (processed.get("workplace_type") or "").lower()

    salary_min = processed.get("yearly_min_compensation")
    salary_max = processed.get("yearly_max_compensation")

    return RawJob(
        external_id=f"hiringcafe_{item.get('id') or abs(hash(url)) % 10**12}",
        source="hiring_cafe",
        title=title,
        company=company,
        location=location or "Not specified",
        is_remote=workplace == "remote",
        job_url=url,
        apply_url=url,
        description=truncate_description(description) if description else None,
        salary_min=float(salary_min) if isinstance(salary_min, (int, float)) else None,
        salary_max=float(salary_max) if isinstance(salary_max, (int, float)) else None,
        salary_currency="USD" if salary_min or salary_max else None,
        posted_at=(item.get("estimated_publish_date") or "")[:10] or None,
        employment_type=(processed.get("commitment") or [None])[0]
        if isinstance(processed.get("commitment"), list) else processed.get("commitment"),
        source_query=query,
    )


def fetch_hiring_cafe_jobs(
    roles: list[str],
    locations: list[str] | None = None,
    max_results: int = 200,
    max_pages: int = 3,
) -> List[RawJob]:
    """Search hiring.cafe for each target role."""
    locations = locations or ["India"]
    jobs: List[RawJob] = []
    seen: set[str] = set()

    for role in roles[:6]:
        for page in range(max_pages):
            data = http.post_json(
                SEARCH_URL,
                json=_build_payload(role, locations, page),
                headers={"Content-Type": "application/json"},
                timeout=25,
                host_delay=0.6,
            )
            if not isinstance(data, dict):
                break
            results = data.get("results") or data.get("hits") or []
            if not results:
                break
            for item in results:
                if not isinstance(item, dict):
                    continue
                job = _parse_job(item, role)
                if job and job.job_url not in seen:
                    seen.add(job.job_url)
                    jobs.append(job)
            if len(jobs) >= max_results:
                break
        if len(jobs) >= max_results:
            break

    console.print(f"[cyan]hiring.cafe:[/cyan] {len(jobs)} jobs")
    return jobs[:max_results]
