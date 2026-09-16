"""
Async URL Resolver — Resolves aggregator redirect URLs at DISCOVERY time.

Instead of resolving redirects at apply time (slow, blocks application pipeline),
this resolves them right after discovery and stores the canonical ATS URL in the DB.

This means:
- Adzuna/Indeed/Naukri/Remotive tracking URLs → resolved to Greenhouse/Lever/Workday URLs
- ATS type correctly identified for all jobs before scoring
- Apply phase skips URL resolution (job.redirect_resolved=True check)

Expected improvement: saves 3-10s per application, more accurate ATS routing.
"""

import asyncio
import re
import time
from typing import Optional
from urllib.parse import urlparse
from rich.console import Console

console = Console()

AGGREGATOR_DOMAINS = [
    "adzuna.", "adzuna.in", "adzuna.com",
    "indeed.com", "indeed.co.in",
    "naukri.com",
    "remotive.com", "remotive.io",
    "remoteok.com", "remoteok.io",
    "shine.com", "foundit.in", "monster.in",
    "glassdoor.com", "glassdoor.co.in",
    "arbeitnow.com",
    "ziprecruiter.com",
]

DIRECT_ATS_DOMAINS = [
    "greenhouse.io", "lever.co", "ashbyhq.com",
    "myworkdayjobs.com", "workday.com",
    "icims.com", "taleo.net", "smartrecruiters.com",
    "bamboohr.com", "jobvite.com",
]


def is_aggregator_url(url: str) -> bool:
    """Check if URL is from a job aggregator (needs redirect resolution)."""
    if not url:
        return False
    url_lower = url.lower()
    return any(agg in url_lower for agg in AGGREGATOR_DOMAINS)


def is_direct_ats_url(url: str) -> bool:
    """Check if URL is already a direct ATS URL (no resolution needed)."""
    if not url:
        return False
    url_lower = url.lower()
    return any(ats in url_lower for ats in DIRECT_ATS_DOMAINS)


def detect_ats_from_resolved_url(url: str) -> str:
    """Detect ATS type from a resolved URL."""
    if not url:
        return "other"
    url_lower = url.lower()

    ats_patterns = [
        ("greenhouse.io", "greenhouse"),
        ("lever.co", "lever"),
        ("ashbyhq.com", "ashby"),
        ("myworkdayjobs.com", "workday"),
        ("workday.com", "workday"),
        ("icims.com", "icims"),
        ("taleo.net", "taleo"),
        ("smartrecruiters.com", "smartrecruiters"),
        ("bamboohr.com", "bamboohr"),
        ("jobvite.com", "jobvite"),
        ("linkedin.com", "linkedin"),
        ("naukri.com", "naukri"),
        ("wellfound.com", "wellfound"),
        ("recruitee.com", "recruitee"),
        ("personio", "personio"),
        ("workable.com", "workable"),
        ("rippling.com", "rippling"),
        ("breezy.hr", "breezy"),
        ("jazzhr.com", "jazzhr"),
    ]
    for pattern, ats in ats_patterns:
        if pattern in url_lower:
            return ats
    return "other"


def resolve_url_sync(url: str, timeout: int = 8) -> tuple[str, str]:
    """
    Follow redirects synchronously. Returns (resolved_url, detected_ats).
    """
    if not url or is_direct_ats_url(url):
        return url, detect_ats_from_resolved_url(url)

    try:
        import requests
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*",
        }
        r = requests.head(url, allow_redirects=True, timeout=timeout, headers=headers)
        resolved = r.url

        # Some servers don't redirect on HEAD, try GET for known aggregators
        if resolved == url and is_aggregator_url(url):
            r2 = requests.get(url, allow_redirects=True, timeout=timeout, headers=headers)
            resolved = r2.url

        detected = detect_ats_from_resolved_url(resolved)
        return resolved, detected

    except Exception:
        return url, detect_ats_from_resolved_url(url)


def resolve_jobs_urls_batch(
    jobs: list,   # List of DB Job objects
    max_workers: int = 6,
    delay: float = 0.1,
) -> int:
    """
    Resolve apply_url for all aggregator-sourced jobs.
    Updates job.apply_url, job.ats_type, job.redirect_resolved in DB.

    Returns:
        Number of jobs where URL was successfully resolved (changed).
    """
    from autoapply.tracker.db import get_session
    from autoapply.tracker.models import Job as JobModel

    jobs_to_resolve = [
        j for j in jobs
        if not j.redirect_resolved
        and is_aggregator_url(str(j.apply_url or j.job_url or ""))
    ]

    if not jobs_to_resolve:
        return 0

    console.print(f"  [dim]Resolving {len(jobs_to_resolve)} aggregator URLs...[/dim]")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    resolved_count = 0
    session = get_session()

    try:
        futures = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for job in jobs_to_resolve:
                url = str(job.apply_url or job.job_url or "")
                future = executor.submit(resolve_url_sync, url)
                futures[future] = job

            for future in as_completed(futures):
                job = futures[future]
                try:
                    resolved_url, detected_ats = future.result()
                    db_job = session.get(JobModel, job.id)
                    if db_job:
                        db_job.redirect_resolved = True
                        if resolved_url != str(job.apply_url or job.job_url or ""):
                            db_job.apply_url = resolved_url
                            resolved_count += 1
                        if detected_ats and detected_ats != "other" and db_job.ats_type in ("other", "unknown", None, ""):
                            db_job.ats_type = detected_ats
                        session.commit()
                    time.sleep(delay)
                except Exception:
                    pass
    finally:
        session.close()

    console.print(f"  [dim]URL resolution: {resolved_count} URLs resolved to canonical ATS[/dim]")
    return resolved_count


def get_jobs_pending_url_resolution(limit: int = 100) -> list:
    """Get jobs from DB that need URL resolution."""
    from autoapply.tracker.db import get_session
    from autoapply.tracker.models import Job

    session = get_session()
    try:
        return session.query(Job).filter(
            Job.redirect_resolved == False,
            Job.status.in_(["discovered", "scored"]),
        ).limit(limit).all()
    finally:
        session.close()
