"""
Career Page ATS Auto-Prober.

Given a list of company names, automatically probes Greenhouse → Lever → Ashby →
SmartRecruiters in sequence until a working board is found. Fetches all open jobs.

This mirrors what the Apify ATS scraper does but runs 100% locally — no paid API needed.
Covered ATS: Greenhouse, Lever, Ashby, SmartRecruiters (the 4 most common in tech startups).
"""

import re
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console

from autoapply.discovery import http
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
    data = http.get_json(f"{GH_API}/{slug}/jobs", params={"content": "true"}, timeout=10)
    return data.get("jobs", []) if isinstance(data, dict) else []


def _probe_lever(slug: str) -> list[dict]:
    """Probe Lever API for company jobs."""
    data = http.get_json(f"{LEVER_API}/{slug}", params={"mode": "json"}, timeout=10)
    return data if isinstance(data, list) else []


def _probe_ashby(slug: str) -> list[dict]:
    """Probe Ashby API for company jobs. POST is 401 since 2026; GET is public."""
    data = http.get_json(
        f"{ASHBY_API}/{slug}", params={"includeCompensation": "true"}, timeout=12
    )
    if not isinstance(data, dict):
        return []
    return data.get("jobs") or data.get("jobPostings") or []


def _probe_smartrecruiters(slug: str) -> list[dict]:
    """Probe SmartRecruiters API for company jobs."""
    data = http.get_json(
        f"{SR_API}/{slug}/postings", params={"limit": 50, "status": "PUBLIC"}, timeout=10
    )
    return data.get("content", []) if isinstance(data, dict) else []


def _probe_pair(company_name: str, slug: str, ats: str,
                roles_filter: list[str] | None) -> list[RawJob]:
    """Probe one (slug, ats) combination. Used by the cache fast path."""
    return _probe_company_slugs(company_name, [slug], roles_filter, only_ats=ats)


def probe_company_jobs(company_name: str, roles_filter: list[str] | None = None,
                       use_cache: bool = True) -> list[RawJob]:
    """
    Auto-probe a company across Greenhouse → Lever → Ashby → SmartRecruiters.

    A cache hit collapses up to 5 slug variants × 4 ATS probes into one request;
    without it this function issued thousands of HTTP calls per run.
    """
    from autoapply.discovery.slug_cache import get_cached_slug, set_cached_slug

    if use_cache:
        cached = get_cached_slug(company_name)
        if cached:
            if not cached.get("active", True):
                return []
            slug, ats = cached.get("slug"), cached.get("ats_type")
            if slug and ats:
                jobs = _probe_pair(company_name, slug, ats, roles_filter)
                if jobs:
                    set_cached_slug(company_name, ats, slug, len(jobs))
                    return jobs

    jobs = _probe_company_slugs(company_name, _normalize_slug(company_name), roles_filter)
    if use_cache:
        if jobs:
            first = jobs[0]
            set_cached_slug(company_name, first.ats_type or "", first.ats_company_slug or "", len(jobs))
        else:
            set_cached_slug(company_name, "", "", 0)
    return jobs


def _probe_company_slugs(company_name: str, slugs: list[str],
                         roles_filter: list[str] | None = None,
                         only_ats: str | None = None) -> list[RawJob]:
    """Try each slug against each ATS, returning the first non-empty result."""
    jobs: list[RawJob] = []

    for slug in slugs:
        # Try Greenhouse
        gh_jobs = _probe_greenhouse(slug) if only_ats in (None, "greenhouse") else []
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
        lv_jobs = _probe_lever(slug) if only_ats in (None, "lever") else []
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
        ashby_jobs = _probe_ashby(slug) if only_ats in (None, "ashby") else []
        if ashby_jobs:
            for item in ashby_jobs:
                title = item.get("title", "").strip()
                if not title:
                    continue
                if roles_filter and not any(kw.lower() in title.lower() for kw in roles_filter):
                    continue
                location = item.get("location", "Remote")
                job_id = item.get("id", "")
                description = item.get("descriptionPlain") or clean_html(
                    item.get("descriptionHtml", "")
                    or "".join(
                        s.get("descriptionHtml", "")
                        for s in item.get("descriptionSections", [])
                    )
                )
                job = RawJob(
                    external_id=f"probe_ashby_{slug}_{job_id}",
                    source="career_probe_ashby",
                    title=title,
                    company=company_name,
                    location=location,
                    is_remote=item.get("isRemote", False),
                    job_url=item.get("jobUrl", f"https://jobs.ashbyhq.com/{slug}/{job_id}"),
                    apply_url=item.get("applyUrl") or f"https://jobs.ashbyhq.com/{slug}/{job_id}/application",
                    description=truncate_description(description),
                    ats_type="ashby",
                    ats_company_slug=slug,
                    ats_job_id=job_id,
                )
                jobs.append(job)
            if jobs:
                return jobs

        # Try SmartRecruiters (use original company name as ID)
        sr_jobs = (_probe_smartrecruiters(company_name.replace(" ", ""))
                   if only_ats in (None, "smartrecruiters") else [])
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
                sr_slug = company_name.replace(" ", "")
                from autoapply.discovery.smartrecruiters_discovery import _extract_sr_description
                try:
                    description = _extract_sr_description(posting, sr_slug, job_id)
                except Exception:
                    description = ""
                job = RawJob(
                    external_id=f"probe_sr_{company_name}_{job_id}",
                    source="career_probe_smartrecruiters",
                    title=title,
                    company=company_name,
                    location=location or "Remote",
                    is_remote=False,
                    job_url=f"https://careers.smartrecruiters.com/{sr_slug}//jobs/{job_id}",
                    apply_url=f"https://careers.smartrecruiters.com/{sr_slug}//jobs/{job_id}",
                    description=description,
                    ats_type="smartrecruiters",
                    ats_company_slug=sr_slug,
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
