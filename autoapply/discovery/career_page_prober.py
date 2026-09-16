"""
Career Page ATS Auto-Prober.

Given a list of company names, automatically probes Greenhouse → Lever → Ashby →
SmartRecruiters in sequence until a working board is found. Fetches all open jobs.

This mirrors what the Apify ATS scraper does but runs 100% locally — no paid API needed.
Covered ATS: Greenhouse, Lever, Ashby, SmartRecruiters (the 4 most common in tech startups).
"""

import re
import requests
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description, clean_html

console = Console()

GH_API = "https://boards-api.greenhouse.io/v1/boards"
LEVER_API = "https://api.lever.co/v0/postings"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board"
SR_API = "https://api.smartrecruiters.com/v1/companies"


def _normalize_slug(company_name: str) -> list[str]:
    """
    Generate candidate slugs from a company name.
    Tries several normalizations to find the right board slug.
    """
    name = company_name.strip().lower()
    # Remove common corporate suffixes
    for suffix in [" inc", " inc.", " llc", " ltd", " corp", " corporation",
                   " technologies", " technology", " solutions", " systems",
                   " software", " pvt", " private", " limited"]:
        name = name.replace(suffix, "")
    name = name.strip()

    slugs = [
        name.replace(" ", ""),         # "zohocorporation" -> "zoho"
        name.replace(" ", "-"),        # "y combinator" -> "y-combinator"
        name.replace(" ", "_"),        # underscored
        name,                           # as-is
        name.split()[0] if name.split() else name,  # first word only
    ]
    # Also try with common brand variations
    if "&" in name:
        slugs.append(name.replace(" & ", "and").replace(" ", ""))

    return list(dict.fromkeys(slugs))  # deduplicated, order preserved


def _probe_greenhouse(slug: str) -> list[dict]:
    """Probe Greenhouse API for company jobs."""
    try:
        r = requests.get(f"{GH_API}/{slug}/jobs", params={"content": "true"}, timeout=10)
        if r.status_code == 200:
            return r.json().get("jobs", [])
    except Exception:
        pass
    return []


def _probe_lever(slug: str) -> list[dict]:
    """Probe Lever API for company jobs."""
    try:
        r = requests.get(f"{LEVER_API}/{slug}", params={"mode": "json"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _probe_ashby(slug: str) -> list[dict]:
    """Probe Ashby API for company jobs."""
    try:
        r = requests.post(
            f"{ASHBY_API}/{slug}",
            json={"limit": 100, "includeCompensation": True},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("jobPostings", [])
    except Exception:
        pass
    return []


def _probe_smartrecruiters(slug: str) -> list[dict]:
    """Probe SmartRecruiters API for company jobs."""
    try:
        r = requests.get(f"{SR_API}/{slug}/postings", params={"limit": 50, "status": "PUBLIC"}, timeout=10)
        if r.status_code == 200:
            return r.json().get("content", [])
    except Exception:
        pass
    return []


def probe_company_jobs(company_name: str, roles_filter: list[str] | None = None) -> list[RawJob]:
    """
    Auto-probe a company across Greenhouse → Lever → Ashby → SmartRecruiters.
    Returns jobs from the first successful probe.
    """
    slugs = _normalize_slug(company_name)
    jobs: list[RawJob] = []

    for slug in slugs:
        # Try Greenhouse
        gh_jobs = _probe_greenhouse(slug)
        if gh_jobs:
            for item in gh_jobs:
                offices = item.get("offices", [])
                location = ", ".join(o.get("name", "") for o in offices) or "Remote"
                title = item.get("title", "").strip()
                if not title:
                    continue
                if roles_filter and not any(kw.lower() in title.lower() for kw in roles_filter):
                    continue
                job = RawJob(
                    external_id=f"probe_gh_{slug}_{item.get('id', '')}",
                    source="career_probe_greenhouse",
                    title=title,
                    company=company_name,
                    location=location,
                    is_remote="remote" in location.lower(),
                    job_url=item.get("absolute_url", f"https://boards.greenhouse.io/{slug}/jobs/{item.get('id','')}"),
                    apply_url=f"https://job-boards.greenhouse.io/{slug}/jobs/{item.get('id','')}",
                    description=truncate_description(item.get("content", "")),
                    ats_type="greenhouse",
                    ats_company_slug=slug,
                    ats_job_id=str(item.get("id", "")),
                )
                jobs.append(job)
            if jobs:
                return jobs

        # Try Lever
        lv_jobs = _probe_lever(slug)
        if lv_jobs:
            for item in lv_jobs:
                cats = item.get("categories", {})
                location = cats.get("location", "Remote")
                title = item.get("text", "").strip()
                if not title:
                    continue
                if roles_filter and not any(kw.lower() in title.lower() for kw in roles_filter):
                    continue
                desc_parts = []
                for section in (item.get("lists") or []):
                    desc_parts.append(section.get("text", ""))
                    content_html = section.get("content", "")
                    if content_html:
                        desc_parts.append(clean_html(content_html))
                job = RawJob(
                    external_id=f"probe_lv_{slug}_{item.get('id', '')}",
                    source="career_probe_lever",
                    title=title,
                    company=company_name,
                    location=location or "Remote",
                    is_remote="remote" in location.lower(),
                    job_url=item.get("hostedUrl", f"https://jobs.lever.co/{slug}/{item.get('id','')}"),
                    apply_url=item.get("applyUrl", f"https://jobs.lever.co/{slug}/{item.get('id','')}/apply"),
                    description=truncate_description("\n".join(desc_parts)),
                    ats_type="lever",
                    ats_company_slug=slug,
                    ats_job_id=item.get("id", ""),
                )
                jobs.append(job)
            if jobs:
                return jobs

        # Try Ashby
        ashby_jobs = _probe_ashby(slug)
        if ashby_jobs:
            for item in ashby_jobs:
                title = item.get("title", "").strip()
                if not title:
                    continue
                if roles_filter and not any(kw.lower() in title.lower() for kw in roles_filter):
                    continue
                location = item.get("location", "Remote")
                job_id = item.get("id", "")
                desc_html = "".join(
                    s.get("descriptionHtml", "") or s.get("title", "")
                    for s in item.get("descriptionSections", [])
                )
                job = RawJob(
                    external_id=f"probe_ashby_{slug}_{job_id}",
                    source="career_probe_ashby",
                    title=title,
                    company=company_name,
                    location=location,
                    is_remote=item.get("isRemote", False),
                    job_url=item.get("jobUrl", f"https://jobs.ashbyhq.com/{slug}/{job_id}"),
                    apply_url=f"https://jobs.ashbyhq.com/{slug}/{job_id}/application",
                    description=truncate_description(clean_html(desc_html)),
                    ats_type="ashby",
                    ats_company_slug=slug,
                    ats_job_id=job_id,
                )
                jobs.append(job)
            if jobs:
                return jobs

        # Try SmartRecruiters (use original company name as ID)
        sr_jobs = _probe_smartrecruiters(company_name.replace(" ", ""))
        if sr_jobs:
            for posting in sr_jobs:
                title = posting.get("name", "").strip()
                if not title:
                    continue
                if roles_filter and not any(kw.lower() in title.lower() for kw in roles_filter):
                    continue
                loc = posting.get("location", {})
                location = ", ".join(filter(None, [loc.get("city", ""), loc.get("country", "")])) if loc else ""
                job_id = posting.get("id", "")
                job = RawJob(
                    external_id=f"probe_sr_{company_name}_{job_id}",
                    source="career_probe_smartrecruiters",
                    title=title,
                    company=company_name,
                    location=location or "Remote",
                    is_remote=False,
                    job_url=f"https://careers.smartrecruiters.com/{company_name.replace(' ','')}//jobs/{job_id}",
                    apply_url=f"https://careers.smartrecruiters.com/{company_name.replace(' ','')}//jobs/{job_id}",
                    description="",
                    ats_type="smartrecruiters",
                    ats_company_slug=company_name.replace(" ", ""),
                    ats_job_id=job_id,
                )
                jobs.append(job)
            if jobs:
                return jobs

    return []  # No ATS found for this company


def probe_companies_batch(
    company_names: list[str],
    roles_filter: list[str] | None = None,
    max_workers: int = 8,
) -> List[RawJob]:
    """
    Probe multiple companies concurrently.
    Returns all jobs found across all companies.
    """
    all_jobs: List[RawJob] = []
    found_companies = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(probe_company_jobs, company, roles_filter): company
            for company in company_names
        }
        for future in as_completed(futures):
            company = futures[future]
            try:
                jobs = future.result()
                if jobs:
                    found_companies += 1
                    all_jobs.extend(jobs)
            except Exception as e:
                console.print(f"[dim]Career probe {company}: {e}[/dim]")

    console.print(
        f"[cyan]Career Page Prober:[/cyan] {len(all_jobs)} jobs from "
        f"{found_companies}/{len(company_names)} companies"
    )
    return all_jobs
