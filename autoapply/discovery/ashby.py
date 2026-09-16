"""
Ashby ATS Direct API Client.

Ashby is now used by many top companies:
  Linear, Vercel, Loom, Notion, Descript, Cal.com, Resend, Turso, etc.

Public API (no auth required):
  POST https://api.ashbyhq.com/posting-api/job-board/{slug}
  Body: {"limit": 100, "includeCompensation": true}

Find a company's Ashby slug:
  Visit their careers page → look for ashbyhq.com/{slug} in iframes/links
"""

import requests
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description

console = Console()

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch_ashby_jobs(company_slugs: list[str]) -> List[RawJob]:
    """
    Fetch jobs from Ashby public job board API for specified companies.

    Args:
        company_slugs: List of Ashby company slugs (e.g. ["linear", "cal"])

    Returns:
        List of RawJob objects
    """
    jobs: List[RawJob] = []

    for slug in company_slugs:
        try:
            url = ASHBY_API.format(slug=slug)
            response = requests.post(
                url,
                json={"limit": 100, "includeCompensation": True},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )

            if response.status_code == 404:
                continue
            if response.status_code != 200:
                continue

            data = response.json()
            postings = data.get("jobPostings", [])

            for item in postings:
                location_parts = []
                if item.get("isRemote"):
                    location_parts.append("Remote")
                for loc in item.get("locationIds", []):
                    pass  # Ashby v2 returns locationIds not names

                # Try to extract location from publishedDepartments or other fields
                location = item.get("location", "") or "Remote"
                is_remote = item.get("isRemote", False) or "remote" in location.lower()

                # Build description from sections
                desc_parts = []
                for section in item.get("descriptionSections", []):
                    if section.get("descriptionHtml"):
                        desc_parts.append(section["descriptionHtml"])
                    elif section.get("title"):
                        desc_parts.append(section["title"])
                description = "\n".join(desc_parts) or item.get("description", "")

                # Salary
                salary_min = salary_max = salary_currency = None
                comp = item.get("compensation", {})
                if comp:
                    salary_min = comp.get("minValue")
                    salary_max = comp.get("maxValue")
                    salary_currency = comp.get("currencyCode", "USD")

                job_id = item.get("id", "")
                job_url = item.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{job_id}"
                apply_url = f"https://jobs.ashbyhq.com/{slug}/{job_id}/application"

                job = RawJob(
                    external_id=f"ashby_{slug}_{job_id}",
                    source="ashby",
                    title=item.get("title", "").strip(),
                    company=item.get("organizationName", slug.capitalize()),
                    location=location,
                    is_remote=is_remote,
                    job_url=job_url,
                    apply_url=apply_url,
                    description=truncate_description(description),
                    ats_type="ashby",
                    ats_company_slug=slug,
                    ats_job_id=job_id,
                    salary_min=salary_min,
                    salary_max=salary_max,
                    salary_currency=salary_currency,
                )

                if job.title and job.job_url:
                    jobs.append(job)

        except requests.exceptions.ConnectionError:
            console.print(f"[yellow]Ashby {slug}:[/yellow] Connection failed")
        except Exception as e:
            if "404" not in str(e):
                console.print(f"[yellow]Ashby {slug}:[/yellow] {type(e).__name__}: {e}")

    console.print(f"[cyan]Ashby:[/cyan] Fetched {len(jobs)} jobs from {len(company_slugs)} companies")
    return jobs
