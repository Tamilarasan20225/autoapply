"""
Ashby ATS Direct API Client - Updated for new GET endpoint.

Ashby is used by: Linear, Ramp, Cursor, Resend, Cal.com, Mercury, etc.

Public API (no auth required):
  GET https://api.ashbyhq.com/posting-api/job-board/{slug}
  Response: {"jobs": [{"id", "title", "department", "location", "isRemote",
             "jobUrl", "applyUrl", "descriptionPlain", "descriptionHtml",
             "publishedAt", "employmentType", "workplaceType", ...}]}

Note: Old POST endpoint now returns 401. Use GET instead.
"""

import requests
from typing import List
from rich.console import Console
from autoapply.discovery.base import RawJob, truncate_description, clean_html

console = Console()
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
ASHBY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Accept": "application/json",
}


def fetch_ashby_jobs(company_slugs: list[str]) -> List[RawJob]:
    """
    Fetch jobs from Ashby public job board API for specified companies.
    Uses the new GET endpoint (POST deprecated as of 2026).

    Args:
        company_slugs: List of Ashby company slugs (e.g. ["linear", "ramp", "cursor"])

    Returns:
        List of RawJob objects
    """
    jobs: List[RawJob] = []

    for slug in company_slugs:
        try:
            url = ASHBY_API.format(slug=slug)
            response = requests.get(url, headers=ASHBY_HEADERS, timeout=15)

            if response.status_code == 404:
                continue  # Company not on Ashby
            if response.status_code == 401:
                continue  # Slug requires auth or no longer public
            if response.status_code != 200:
                continue

            data = response.json()
            # New API returns {"jobs": [...]}, old returned {"jobPostings": [...]}
            postings = data.get("jobs", data.get("jobPostings", []))

            for item in postings:
                if not isinstance(item, dict):
                    continue

                # New API field names
                title = item.get("title", "").strip()
                if not title:
                    continue

                # Location
                location = (
                    item.get("location", "")
                    or item.get("locationName", "")
                    or ""
                ).strip()
                is_remote = (
                    item.get("isRemote", False)
                    or item.get("workplaceType", "") in ("Remote", "remote")
                    or "remote" in location.lower()
                )
                if not location:
                    location = "Remote" if is_remote else "Worldwide"

                # URLs
                job_url = item.get("jobUrl", "").strip()
                apply_url = item.get("applyUrl", job_url).strip()
                job_id = item.get("id", "")
                if not job_url and job_id:
                    job_url = f"https://jobs.ashbyhq.com/{slug}/{job_id}"
                    apply_url = f"https://jobs.ashbyhq.com/{slug}/{job_id}/application"
                if not job_url:
                    continue

                # Description - prefer plain text, fallback to HTML
                description_plain = item.get("descriptionPlain", "").strip()
                description_html = item.get("descriptionHtml", "").strip()
                description_raw = description_plain or clean_html(description_html)
                description = truncate_description(description_raw) if description_raw else None

                # Salary
                salary_min = salary_max = salary_currency = None
                comp = item.get("compensation", {}) or {}
                if comp:
                    salary_min = comp.get("minValue") or comp.get("min")
                    salary_max = comp.get("maxValue") or comp.get("max")
                    salary_currency = comp.get("currencyCode", "USD")

                # Employment type
                emp_type_raw = item.get("employmentType", "") or ""
                emp_type = emp_type_raw.lower().replace("fulltime", "full-time").replace("parttime", "part-time") or None

                # Posted date
                posted_at = None
                pub_date = item.get("publishedAt", "") or item.get("createdAt", "")
                if pub_date:
                    posted_at = str(pub_date)[:10]

                department = item.get("department", "") or item.get("team", "") or None
                seniority = None  # Not directly in new API

                job = RawJob(
                    external_id=f"ashby_{slug}_{job_id}",
                    source="ashby",
                    title=title,
                    company=slug.replace("-", " ").title(),
                    location=location,
                    is_remote=is_remote,
                    job_url=job_url,
                    apply_url=apply_url,
                    description=description,
                    ats_type="ashby",
                    ats_company_slug=slug,
                    ats_job_id=str(job_id),
                    salary_min=float(salary_min) if salary_min else None,
                    salary_max=float(salary_max) if salary_max else None,
                    salary_currency=salary_currency,
                    employment_type=emp_type,
                    department=department,
                    posted_at=posted_at,
                    seniority=seniority,
                )
                if job.title and job.job_url:
                    jobs.append(job)

        except requests.exceptions.ConnectionError:
            console.print(f"[yellow]Ashby {slug}:[/yellow] Connection failed")
        except requests.exceptions.Timeout:
            console.print(f"[yellow]Ashby {slug}:[/yellow] Timeout")
        except Exception as e:
            if "404" not in str(e) and "401" not in str(e):
                console.print(f"[yellow]Ashby {slug}:[/yellow] {type(e).__name__}: {str(e)[:60]}")

    console.print(f"[cyan]Ashby:[/cyan] Fetched {len(jobs)} jobs from {len(company_slugs)} companies")
    return jobs
