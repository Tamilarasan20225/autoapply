"""
Workable Public Widget API Scraper - No auth required per company.

API: GET https://apply.workable.com/api/v1/widget/accounts/{slug}
Response: {"name": "Company", "jobs": [{"title", "shortcode", "url", "country", "city", ...}]}
Free and keyless, one company at a time.
"""

import requests
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from autoapply.discovery.base import RawJob

console = Console()
WORKABLE_API = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
WORKABLE_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0", "Accept": "application/json"}

DEFAULT_TECH_FILTER = [
    "engineer", "developer", "software", "backend", "python", "java",
    "data", "ai", "ml", "api", "platform", "infrastructure", "devops",
    "sre", "fullstack", "full-stack", "architect", "scientist",
]


def _fetch_workable_company(slug, roles_filter=None):
    jobs = []
    try:
        r = requests.get(WORKABLE_API.format(slug=slug), headers=WORKABLE_HEADERS, timeout=15)
        if r.status_code not in (200,):
            return []
        data = r.json()
        company_name = data.get("name", slug.replace("-", " ").title())
        job_list = data.get("jobs", [])
        if not job_list:
            return []
        filter_kws = roles_filter or DEFAULT_TECH_FILTER
        for item in job_list:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "").strip()
            if not title:
                continue
            if filter_kws and not any(kw.lower() in title.lower() for kw in filter_kws):
                continue
            job_url = item.get("url", "") or item.get("shortlink", "")
            apply_url = item.get("application_url", job_url)
            if not job_url:
                sc = item.get("shortcode", "")
                if sc:
                    job_url = f"https://apply.workable.com/j/{sc}"
                    apply_url = f"https://apply.workable.com/j/{sc}/apply"
            if not job_url:
                continue
            country = item.get("country", "")
            city = item.get("city", "") or item.get("state", "")
            loc = ", ".join(x for x in [city, country] if x) or "Remote"
            is_remote = item.get("telecommuting", False) or "remote" in title.lower() or "remote" in loc.lower()
            emp_type = item.get("employment_type", "").lower().replace("_", "-") or None
            dept = item.get("department", "") or item.get("function", "") or None
            pub_date = item.get("published_on", "") or item.get("created_at", "")
            posted_at = str(pub_date)[:10] if pub_date else None
            job_id = item.get("shortcode", item.get("id", ""))
            job = RawJob(
                external_id=f"workable_{slug}_{job_id}",
                source="workable",
                title=title,
                company=company_name,
                location=loc,
                is_remote=is_remote,
                job_url=job_url,
                apply_url=apply_url,
                description=None,
                employment_type=emp_type,
                department=dept,
                posted_at=posted_at,
                ats_type="workable",
                ats_company_slug=slug,
                ats_job_id=str(job_id),
            )
            jobs.append(job)
    except Exception:
        pass
    return jobs


def fetch_workable_jobs(company_slugs, roles_filter=None, max_workers=8):
    """
    Fetch jobs from Workable widget API for a list of company slugs.
    No authentication required - uses public widget endpoint.
    """
    if not company_slugs:
        return []
    all_jobs = []
    found = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_workable_company, slug, roles_filter): slug for slug in company_slugs}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    found += 1
                    all_jobs.extend(result)
            except Exception:
                pass
    console.print(f"[cyan]Workable:[/cyan] {len(all_jobs)} jobs from {found}/{len(company_slugs)} companies")
    return all_jobs
