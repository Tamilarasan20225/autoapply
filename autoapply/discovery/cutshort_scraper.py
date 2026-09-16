"""
Cutshort.io Job Scraper — India-first tech/startup job board.
Fetches jobs via public API and __NEXT_DATA__ fallback scraping.
Populates posted_at, employment_type, skills_required at discovery time.
"""

import time
import re
import requests
from typing import List, Optional
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description, clean_html

console = Console()
CUTSHORT_API = "https://cutshort.io/api/public-api/jobs"
CUTSHORT_SEARCH = "https://cutshort.io/jobs"
_REQUEST_DELAY = 1.5


def _parse_cutshort_job(item: dict, source_role: str) -> Optional[RawJob]:
    """Parse a Cutshort job dict into a RawJob."""
    try:
        job_id = str(item.get("id", item.get("_id", item.get("slug", ""))))
        if not job_id:
            return None
        title = (item.get("title", "") or item.get("designation", "")).strip()
        if not title:
            return None
        company_obj = item.get("company", item.get("org", {})) or {}
        if isinstance(company_obj, str):
            company_name = company_obj
        else:
            company_name = (company_obj.get("name", "") or company_obj.get("companyName", "") or item.get("companyName", "")).strip()
        if not company_name:
            return None
        locations_raw = item.get("locations", item.get("location", []))
        if isinstance(locations_raw, list):
            location_str = ", ".join(str(l) for l in locations_raw[:2] if l)
        elif isinstance(locations_raw, str):
            location_str = locations_raw
        else:
            location_str = ""
        is_remote = (item.get("remote", False) or item.get("workMode", "").lower() in ("remote", "hybrid") or "remote" in location_str.lower())
        slug = item.get("slug", job_id)
        job_url = item.get("url", f"https://cutshort.io/job/{slug}")
        if not job_url.startswith("http"):
            job_url = f"https://cutshort.io{job_url}"
        desc_parts = []
        for field in ["description", "jobDescription", "details", "about"]:
            val = item.get(field, "")
            if val and isinstance(val, str):
                desc_parts.append(clean_html(val))
                break
        skills_raw = item.get("skills", item.get("skillsRequired", []))
        if isinstance(skills_raw, list) and skills_raw:
            skill_names = [s.get("name", s) if isinstance(s, dict) else str(s) for s in skills_raw]
            desc_parts.append(f"Required Skills: {', '.join(skill_names[:15])}")
        description = "\n".join(desc_parts).strip()
        sal_min = sal_max = None
        salary = item.get("salary", item.get("ctc", {}))
        if isinstance(salary, dict):
            sal_min = salary.get("min", salary.get("from"))
            sal_max = salary.get("max", salary.get("to"))
        elif isinstance(salary, (int, float)):
            sal_min = salary
        posted_at = None
        for date_field in ["postedAt", "createdAt", "updatedAt", "publishedAt"]:
            val = item.get(date_field, "")
            if val and isinstance(val, str):
                posted_at = val[:10]
                break
        job_type = item.get("jobType", item.get("employmentType", "")).lower()
        if "contract" in job_type:
            employment_type = "contract"
        elif "intern" in job_type:
            employment_type = "internship"
        elif "part" in job_type:
            employment_type = "part-time"
        else:
            employment_type = "full-time"
        department = item.get("department", item.get("function", "")) or ""
        if isinstance(department, dict):
            department = department.get("name", "")
        skills_list = None
        if isinstance(skills_raw, list) and skills_raw:
            skills_list = [s.get("name", s) if isinstance(s, dict) else str(s) for s in skills_raw[:20]]
        return RawJob(
            external_id=f"cutshort_{job_id}",
            source="cutshort",
            title=title,
            company=company_name,
            location=location_str or ("Remote" if is_remote else "India"),
            is_remote=is_remote,
            job_url=job_url,
            apply_url=job_url,
            description=truncate_description(description) if description else None,
            ats_type="other",
            salary_min=float(sal_min) if sal_min else None,
            salary_max=float(sal_max) if sal_max else None,
            salary_currency="INR",
            posted_at=posted_at,
            employment_type=employment_type,
            department=str(department)[:64] if department else None,
            skills_required=skills_list,
            source_query=source_role,
        )
    except Exception as e:
        console.print(f"[dim]Cutshort parse error: {e}[/dim]")
        return None


def fetch_cutshort_jobs(
    roles: Optional[List[str]] = None,
    locations: Optional[List[str]] = None,
    config: Optional[dict] = None,
    limit: int = 100,
) -> List[RawJob]:
    """
    Fetch jobs from Cutshort.io for India tech/startup roles.
    Tries public API first, falls back to Next.js __NEXT_DATA__ scraping.
    """
    if not roles:
        roles = ["Backend Engineer", "Software Engineer", "Python Developer"]
    target_location = "Bangalore"
    is_remote_search = False
    if locations:
        for loc in locations:
            if loc.lower() in ("remote", "remote india"):
                is_remote_search = True
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
        "Accept": "application/json, */*",
        "Referer": "https://cutshort.io/jobs",
    }
    jobs: List[RawJob] = []
    seen_ids: set = set()
    for role in roles[:4]:
        api_success = False
        try:
            params = {"query": role, "limit": 30, "offset": 0, "location": target_location}
            r = requests.get(CUTSHORT_API, params=params, headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                items = data.get("data", data.get("jobs", data.get("results", [])))
                if isinstance(items, list) and items:
                    api_success = True
                    for item in items[:limit]:
                        raw_job = _parse_cutshort_job(item, role)
                        if raw_job and raw_job.external_id not in seen_ids:
                            seen_ids.add(raw_job.external_id)
                            jobs.append(raw_job)
        except Exception as e:
            console.print(f"[dim]Cutshort API error for '{role}': {e}[/dim]")
        if not api_success:
            try:
                r = requests.get(CUTSHORT_SEARCH, params={"q": role, "l": target_location}, headers=headers, timeout=20)
                if r.status_code == 200:
                    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.DOTALL)
                    if match:
                        import json
                        next_data = json.loads(match.group(1))
                        page_props = next_data.get("props", {}).get("pageProps", {})
                        items = page_props.get("jobs", []) or page_props.get("jobListings", []) or []
                        for item in items[:limit]:
                            raw_job = _parse_cutshort_job(item, role)
                            if raw_job and raw_job.external_id not in seen_ids:
                                seen_ids.add(raw_job.external_id)
                                jobs.append(raw_job)
            except Exception as e:
                console.print(f"[dim]Cutshort scrape error for '{role}': {e}[/dim]")
        time.sleep(_REQUEST_DELAY)
    console.print(f"[cyan]Cutshort:[/cyan] Fetched {len(jobs)} India tech jobs")
    return jobs
