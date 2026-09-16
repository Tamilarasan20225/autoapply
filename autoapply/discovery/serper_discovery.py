"""
Google SERP-based Job Discovery using Serper API.

Strategy: Use Google's index to find direct ATS URLs (Greenhouse, Lever, Ashby,
SmartRecruiters) that match the candidate's target roles. This bypasses aggregator
redirect chains and finds jobs directly from source ATS boards.

Free tier: 100 queries/month on Serper. Budget: ~20 queries per daily run.

Query patterns:
  site:boards.greenhouse.io "{role}" "bangalore" OR "india" OR "remote"
  site:jobs.lever.co "{role}" bangalore OR india
  site:jobs.ashbyhq.com "{role}" remote OR india
"""

import os
import re
import time
import requests
from typing import List, Optional
from urllib.parse import urlparse
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description
from autoapply.discovery.ats_direct import fetch_greenhouse_jobs, fetch_lever_jobs

console = Console()

SERPER_API = "https://google.serper.dev/search"


# ── ATS URL parsers ──────────────────────────────────────────────────────────────

def _parse_greenhouse_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (slug, job_id) from greenhouse.io URL."""
    m = re.search(r'greenhouse\.io/([^/]+)/jobs/(\d+)', url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def _parse_lever_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (slug, job_id) from jobs.lever.co URL."""
    m = re.search(r'jobs\.lever\.co/([^/?]+)/([a-f0-9-]{36})', url, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    # Check if it's a company listing URL (no specific job)
    m2 = re.search(r'jobs\.lever\.co/([^/?]+)', url)
    if m2:
        return m2.group(1), None  # slug only, no specific job
    return None, None


def _parse_ashby_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (slug, job_id) from jobs.ashbyhq.com URL."""
    m = re.search(r'jobs\.ashbyhq\.com/([^/?]+)/([a-f0-9-]{36})', url, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    m2 = re.search(r'jobs\.ashbyhq\.com/([^/?]+)', url)
    if m2:
        return m2.group(1), None
    return None, None


def _parse_smartrecruiters_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (company_id, job_id) from SmartRecruiters URL."""
    m = re.search(r'careers\.smartrecruiters\.com/([^/]+)/jobs/([A-Za-z0-9_-]+)', url)
    if m:
        return m.group(1), m.group(2)
    m2 = re.search(r'careers\.smartrecruiters\.com/([^/?]+)', url)
    if m2:
        return m2.group(1), None
    return None, None


# ── Serper query builder ───────────────────────────────────────────────────────

def _build_serper_queries(roles: list[str], locations: list[str]) -> list[dict]:
    """
    Build targeted SERP queries for each ATS.
    Returns list of {query, ats_type} dicts.
    Budget: cap at 20 queries to stay within free tier.
    """
    location_str = " OR ".join(
        f'"{loc}"' for loc in locations[:3] if loc.lower() not in ("remote",)
    )
    remote_clause = "OR remote OR \"work from home\""

    queries = []

    # Use top 3 roles only to conserve API budget
    top_roles = roles[:3]

    for role in top_roles:
        role_q = f'"{role}"'

        # Greenhouse boards (both old and new URL formats)
        queries.append({
            "q": f"site:boards.greenhouse.io {role_q} ({location_str} {remote_clause})",
            "ats": "greenhouse",
        })
        queries.append({
            "q": f"site:job-boards.greenhouse.io {role_q}",
            "ats": "greenhouse",
        })

        # Lever
        queries.append({
            "q": f"site:jobs.lever.co {role_q} ({location_str} {remote_clause})",
            "ats": "lever",
        })

        # Ashby (startup-heavy, remote-friendly)
        queries.append({
            "q": f"site:jobs.ashbyhq.com {role_q}",
            "ats": "ashby",
        })

    # One broad query for SmartRecruiters India
    queries.append({
        "q": f"site:careers.smartrecruiters.com ({' OR '.join(top_roles)}) ({location_str})",
        "ats": "smartrecruiters",
    })

    return queries[:20]  # Hard cap at 20 queries per run


def _search_serper(query: str, api_key: str, num: int = 10) -> list[dict]:
    """Execute a single Serper API search. Returns organic results."""
    try:
        response = requests.post(
            SERPER_API,
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": num},
            timeout=15,
        )
        if response.status_code == 200:
            return response.json().get("organic", [])
        elif response.status_code == 429:
            console.print("[yellow]Serper: Rate limited — pausing 10s[/yellow]")
            time.sleep(10)
        else:
            console.print(f"[dim]Serper HTTP {response.status_code} for query: {query[:60]}[/dim]")
    except Exception as e:
        console.print(f"[dim]Serper error: {e}[/dim]")
    return []


def _fetch_greenhouse_job_details(slug: str, job_id: str) -> Optional[dict]:
    """Fetch full job details from Greenhouse API."""
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?content=true"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _fetch_lever_job_details(slug: str, job_id: str) -> Optional[dict]:
    """Fetch full job details from Lever API."""
    try:
        url = f"https://api.lever.co/v0/postings/{slug}/{job_id}"
        r = requests.get(url, params={"mode": "json"}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# ── Main discovery function ────────────────────────────────────────────────────────────

def fetch_serper_jobs(
    roles: list[str],
    locations: list[str],
    serper_key: str,
    config: dict | None = None,
) -> List[RawJob]:
    """
    Discover jobs via Google SERP using site: operators for direct ATS URLs.

    This finds jobs on Greenhouse, Lever, Ashby, and SmartRecruiters boards
    that are indexed by Google, bypassing aggregator redirect chains.

    Returns:
        List[RawJob] with direct ATS URLs, slugs, and job IDs populated.
    """
    if not serper_key:
        console.print("[dim]Serper: No API key configured — skipping SERP discovery[/dim]")
        return []

    jobs: List[RawJob] = []
    seen_urls: set[str] = set()
    seen_job_ids: set[str] = set()

    queries = _build_serper_queries(roles, locations)
    console.print(f"[dim]Serper SERP: Running {len(queries)} queries...[/dim]")

    for query_info in queries:
        query = query_info["q"]
        ats_hint = query_info["ats"]

        results = _search_serper(query, serper_key, num=10)
        time.sleep(0.3)  # Polite rate limit

        for result in results:
            url = result.get("link", "")
            title_text = result.get("title", "")
            snippet = result.get("snippet", "")

            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            # Route to appropriate parser based on ATS
            if "greenhouse.io" in url:
                slug, job_id = _parse_greenhouse_url(url)
                if not slug or not job_id:
                    continue
                dedup_key = f"gh_{slug}_{job_id}"
                if dedup_key in seen_job_ids:
                    continue
                seen_job_ids.add(dedup_key)

                # Fetch full job from GH API
                job_data = _fetch_greenhouse_job_details(slug, job_id)
                if not job_data:
                    # Fallback: create job from SERP snippet
                    job = RawJob(
                        external_id=f"serper_gh_{slug}_{job_id}",
                        source="serper_greenhouse",
                        title=title_text.replace(" - Greenhouse", "").strip(),
                        company=slug.replace("-", " ").title(),
                        location="Remote / Global",
                        is_remote="remote" in title_text.lower() or "remote" in snippet.lower(),
                        job_url=f"https://boards.greenhouse.io/{slug}/jobs/{job_id}",
                        apply_url=f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}",
                        description=truncate_description(snippet),
                        ats_type="greenhouse",
                        ats_company_slug=slug,
                        ats_job_id=job_id,
                        source_query=query[:100],
                    )
                else:
                    offices = job_data.get("offices", [])
                    location = ", ".join(o.get("name", "") for o in offices) or "Remote"
                    job = RawJob(
                        external_id=f"serper_gh_{slug}_{job_id}",
                        source="serper_greenhouse",
                        title=job_data.get("title", title_text).strip(),
                        company=slug.replace("-", " ").title(),
                        location=location,
                        is_remote="remote" in location.lower(),
                        job_url=job_data.get("absolute_url", url),
                        apply_url=f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}",
                        description=truncate_description(job_data.get("content", snippet)),
                        ats_type="greenhouse",
                        ats_company_slug=slug,
                        ats_job_id=job_id,
                        source_query=query[:100],
                    )

                if job.title and job.company:
                    jobs.append(job)

            elif "lever.co" in url:
                slug, job_id = _parse_lever_url(url)
                if not slug:
                    continue
                if not job_id:  # Company listing page, not a specific job
                    continue
                dedup_key = f"lv_{slug}_{job_id}"
                if dedup_key in seen_job_ids:
                    continue
                seen_job_ids.add(dedup_key)

                job_data = _fetch_lever_job_details(slug, job_id)
                if not job_data:
                    job = RawJob(
                        external_id=f"serper_lv_{slug}_{job_id}",
                        source="serper_lever",
                        title=title_text.replace(" - Lever", "").strip(),
                        company=slug.replace("-", " ").title(),
                        location="Remote / Global",
                        is_remote=True,
                        job_url=url,
                        apply_url=f"https://jobs.lever.co/{slug}/{job_id}/apply",
                        description=truncate_description(snippet),
                        ats_type="lever",
                        ats_company_slug=slug,
                        ats_job_id=job_id,
                        source_query=query[:100],
                    )
                else:
                    cats = job_data.get("categories", {})
                    location = cats.get("location", "Remote")
                    # Build description from lists
                    desc_parts = []
                    for section in (job_data.get("lists") or []):
                        desc_parts.append(section.get("text", ""))
                        content_html = section.get("content", "")
                        if content_html:
                            from autoapply.discovery.base import clean_html
                            desc_parts.append(clean_html(content_html))
                    job = RawJob(
                        external_id=f"serper_lv_{slug}_{job_id}",
                        source="serper_lever",
                        title=job_data.get("text", title_text).strip(),
                        company=slug.replace("-", " ").title(),
                        location=location or "Remote",
                        is_remote="remote" in location.lower(),
                        job_url=job_data.get("hostedUrl", url),
                        apply_url=job_data.get("applyUrl", f"https://jobs.lever.co/{slug}/{job_id}/apply"),
                        description=truncate_description("\n".join(desc_parts)),
                        ats_type="lever",
                        ats_company_slug=slug,
                        ats_job_id=job_id,
                        source_query=query[:100],
                    )

                if job.title and job.company:
                    jobs.append(job)

            elif "ashbyhq.com" in url:
                slug, job_id = _parse_ashby_url(url)
                if not slug or not job_id:
                    continue
                dedup_key = f"ashby_{slug}_{job_id}"
                if dedup_key in seen_job_ids:
                    continue
                seen_job_ids.add(dedup_key)

                job = RawJob(
                    external_id=f"serper_ashby_{slug}_{job_id}",
                    source="serper_ashby",
                    title=title_text.strip(),
                    company=slug.replace("-", " ").title(),
                    location=snippet[:60],
                    is_remote="remote" in snippet.lower(),
                    job_url=f"https://jobs.ashbyhq.com/{slug}/{job_id}",
                    apply_url=f"https://jobs.ashbyhq.com/{slug}/{job_id}/application",
                    description=truncate_description(snippet),
                    ats_type="ashby",
                    ats_company_slug=slug,
                    ats_job_id=job_id,
                    source_query=query[:100],
                )
                if job.title and job.company:
                    jobs.append(job)

    console.print(f"[cyan]Serper SERP:[/cyan] Found {len(jobs)} direct ATS jobs")
    return jobs
