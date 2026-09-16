"""
URL Resolver — follows HTTP redirects to find the true job application URL.

Problem: Aggregators like Adzuna, Indeed, Remotive give their own tracking URLs
(e.g. https://www.adzuna.in/details/5876720335?utm_source=...) that redirect to
the actual company/ATS URL.

This module resolves those redirect chains so we can:
  1. Detect the real ATS (Workday, Greenhouse, iCIMS, etc.)
  2. Pass the correct direct URL to the right applier

Also handles LinkedIn job URLs which have job_id embedded but may be short-form.
"""

import re
import time
import requests
from typing import Optional
from urllib.parse import urlparse, parse_qs
from rich.console import Console

from autoapply.applicator.ats_detector import detect_ats_from_url

console = Console()


def follow_redirects(url: str, max_hops: int = 5, timeout: int = 10) -> str:
    """
    Follow HTTP redirects and return the final landing URL.

    Args:
        url: Starting URL (e.g. Adzuna redirect URL)
        max_hops: Max redirect hops to follow
        timeout: Request timeout in seconds

    Returns:
        Final resolved URL (or original URL if resolution fails)
    """
    if not url or url == "nan":
        return url

    # Skip URLs that are already direct ATS URLs
    direct_ats_patterns = [
        "greenhouse.io", "lever.co", "ashbyhq.com",
        "myworkdayjobs.com", "workday.com",
        "icims.com", "taleo.net", "smartrecruiters.com",
        "bamboohr.com", "jobvite.com",
    ]
    for pattern in direct_ats_patterns:
        if pattern in url.lower():
            return url

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        response = requests.head(
            url,
            allow_redirects=True,
            timeout=timeout,
            headers=headers,
        )
        final_url = response.url

        if final_url and final_url != url:
            return final_url

        # HEAD didn't redirect — try GET (some servers require GET)
        if "adzuna" in url.lower() or "indeed" in url.lower() or "naukri" in url.lower():
            response = requests.get(
                url,
                allow_redirects=True,
                timeout=timeout,
                headers=headers,
            )
            if response.url != url:
                return response.url

        return final_url or url

    except Exception as e:
        console.print(f"  [dim]URL resolution failed for {url[:60]}: {e}[/dim]")
        return url


def extract_linkedin_job_id(url: str) -> Optional[str]:
    """Extract LinkedIn job ID from various LinkedIn URL formats."""
    if not url:
        return None

    # linkedin.com/jobs/view/{job_id}
    m = re.search(r"linkedin\.com/jobs/view/(\d+)", url)
    if m:
        return m.group(1)

    # linkedin.com/jobs/collections/...?currentJobId={id}
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "currentJobId" in params:
        return params["currentJobId"][0]

    # linkedin.com/comm/jobs/view/{id}
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", url)
    if m:
        return m.group(1)

    return None


def resolve_job_url(job_url: str, apply_url: str, ats_type: str = "") -> dict:
    """
    Resolve a job's true apply URL and detect/confirm its ATS type.

    This is called at apply-time for jobs whose URLs are aggregator redirects.

    Args:
        job_url: The job listing URL (may be aggregator redirect)
        apply_url: The stored apply URL (may be same as job_url)
        ats_type: Currently stored ATS type (may be "other" or wrong)

    Returns:
        dict with keys:
          - resolved_url: The true apply/job URL after following redirects
          - detected_ats: ATS type detected from resolved URL
          - changed: Whether the URL changed from the input
    """
    # Use apply_url if available and different from job_url, else job_url
    start_url = apply_url if apply_url and apply_url != "nan" else job_url
    if not start_url or start_url == "nan":
        return {
            "resolved_url": None,
            "detected_ats": "unknown",
            "changed": False,
        }

    # Quick ATS detection from the URL as-is
    detected = detect_ats_from_url(start_url)
    if detected and detected != "unknown":
        # Already a direct ATS URL — no need to follow redirects
        return {
            "resolved_url": start_url,
            "detected_ats": detected,
            "changed": False,
        }

    # Need to follow redirects (Adzuna, Indeed, Naukri, etc.)
    console.print(f"  [dim]Resolving redirect: {start_url[:70]}...[/dim]")
    resolved = follow_redirects(start_url)

    # Detect ATS from resolved URL
    detected = detect_ats_from_url(resolved) or "unknown"

    changed = resolved != start_url
    if changed:
        console.print(f"  [dim]Resolved to: {resolved[:70]}...[/dim]")
        console.print(f"  [dim]ATS detected: {detected}[/dim]")

    return {
        "resolved_url": resolved,
        "detected_ats": detected,
        "changed": changed,
    }


def resolve_linkedin_apply_url(job_url: str) -> str:
    """
    For LinkedIn jobs, return the proper linkedin.com/jobs/view/{id} URL.
    LinkedIn Easy Apply works on the standard job view URL.
    """
    if not job_url or "linkedin.com" not in job_url.lower():
        return job_url

    job_id = extract_linkedin_job_id(job_url)
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}"

    return job_url
