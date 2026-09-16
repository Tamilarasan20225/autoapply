"""
Direct ATS API clients for Greenhouse and Lever.
These are free, public, unauthenticated endpoints — no key required.

Greenhouse: https://boards-api.greenhouse.io/v1/boards/{company}/jobs
Lever:      https://api.lever.co/v0/postings/{company}
"""

import requests
from typing import List, Optional
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description, clean_html

console = Console()

GREENHOUSE_BASE = "https://boards-api.greenhouse.io/v1/boards"
LEVER_BASE = "https://api.lever.co/v0/postings"


def _fetch_lever_description(slug: str, job_id: str) -> Optional[str]:
    """
    Fetch full job description from Lever's individual posting endpoint.
    Used to fill empty descriptions from the listing API.
    
    Args:
        slug: Lever company slug (e.g. "paytm")
        job_id: Lever job UUID
        
    Returns:
        Cleaned plain-text description or None if fetch fails
    """
    try:
        url = f"{LEVER_BASE}/{slug}/{job_id}"
        r = requests.get(url, params={"mode": "json"}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        
        parts = []
        
        # Primary: descriptionPlain (already clean)
        dp = data.get("descriptionPlain", "").strip()
        if dp:
            parts.append(dp)
        
        # Fallback: description HTML
        dh = data.get("description", "").strip()
        if dh and not dp:
            parts.append(clean_html(dh))
        
        # Lists sections (requirements, responsibilities, etc.)
        for section in (data.get("lists") or []):
            heading = section.get("text", "")
            if heading:
                parts.append(f"\n{heading}:")
            content = section.get("content", "")
            if content:
                parts.append(clean_html(content))
        
        # Additional info
        additional = data.get("additionalPlain", "").strip()
        if additional:
            parts.append(f"\nAdditional:\n{additional}")
        
        combined = "\n".join(parts).strip()
        return truncate_description(combined) if combined else None
    except Exception:
        return None


def fetch_greenhouse_jobs(company_slugs: list[str]) -> List[RawJob]:
    """
    Fetch jobs from Greenhouse public API for specified companies.

    Args:
        company_slugs: List of Greenhouse company slugs (e.g. ["stripe", "notion"])

    Returns:
        List of RawJob objects
    """
    jobs: List[RawJob] = []

    for slug in company_slugs:
        try:
            url = f"{GREENHOUSE_BASE}/{slug}/jobs"
            params = {"content": "true"}  # Include full job description
            response = requests.get(url, params=params, timeout=15)

            if response.status_code == 404:
                # Company not on Greenhouse
                continue
            response.raise_for_status()
            data = response.json()

            raw_jobs = data.get("jobs", [])

            for item in raw_jobs:
                # Extract location
                offices = item.get("offices", [])
                location = ", ".join(o.get("name", "") for o in offices) if offices else ""
                is_remote = "remote" in location.lower() or "remote" in item.get("title", "").lower()

                job = RawJob(
                    external_id=f"greenhouse_{slug}_{item.get('id', '')}",
                    source="greenhouse",
                    title=item.get("title", "").strip(),
                    company=slug.capitalize(),
                    location=location or "Remote",
                    is_remote=is_remote,
                    job_url=item.get("absolute_url", ""),
                    apply_url=item.get("absolute_url", ""),
                    description=truncate_description(
                        item.get("content", "") or item.get("description", "")
                    ),
                    ats_type="greenhouse",
                    ats_company_slug=slug,
                    ats_job_id=str(item.get("id", "")),
                )
                if job.title and job.job_url:
                    jobs.append(job)

        except requests.exceptions.ConnectionError:
            console.print(f"[yellow]Greenhouse {slug}:[/yellow] Connection failed")
        except Exception as e:
            if "404" not in str(e):
                console.print(f"[yellow]Greenhouse {slug}:[/yellow] {type(e).__name__}: {e}")

    console.print(f"[cyan]Greenhouse:[/cyan] Fetched {len(jobs)} jobs from {len(company_slugs)} companies")
    return jobs


def fetch_lever_jobs(company_slugs: list[str]) -> List[RawJob]:
    """
    Fetch jobs from Lever public API for specified companies.
    
    Improvement: When the listing API returns an empty description,
    falls back to fetching the full posting from the individual endpoint.

    Args:
        company_slugs: List of Lever company slugs (e.g. ["postman", "freshworks"])

    Returns:
        List of RawJob objects
    """
    jobs: List[RawJob] = []
    filled_desc_count = 0

    for slug in company_slugs:
        try:
            url = f"{LEVER_BASE}/{slug}"
            params = {"mode": "json"}
            response = requests.get(url, params=params, timeout=15)

            if response.status_code == 404:
                continue
            response.raise_for_status()
            raw_jobs = response.json()

            if not isinstance(raw_jobs, list):
                continue

            for item in raw_jobs:
                categories = item.get("categories", {})
                location = categories.get("location", "")
                is_remote = "remote" in location.lower() or "remote" in item.get("text", "").lower()

                # Build description from lists — strip HTML tags from content
                description_parts = []
                # Try plain text first (more reliable)
                plain = item.get("descriptionPlain", "").strip()
                if plain:
                    description_parts.append(plain)
                else:
                    desc_html = item.get("description", "").strip()
                    if desc_html:
                        description_parts.append(clean_html(desc_html))

                for section in (item.get("lists") or []):
                    description_parts.append(section.get("text", ""))
                    content = section.get("content", "")
                    if content:
                        description_parts.append(clean_html(content))
                
                description = "\n".join(p for p in description_parts if p).strip()
                
                job_id = item.get("id", "")
                
                # If still empty, fetch from the individual posting endpoint
                if (not description or len(description) < 50) and job_id:
                    fetched = _fetch_lever_description(slug, job_id)
                    if fetched:
                        description = fetched
                        filled_desc_count += 1

                job = RawJob(
                    external_id=f"lever_{slug}_{job_id}",
                    source="lever",
                    title=item.get("text", "").strip(),
                    company=slug.capitalize(),
                    location=location or "Remote",
                    is_remote=is_remote,
                    job_url=item.get("hostedUrl", ""),
                    apply_url=item.get("applyUrl", item.get("hostedUrl", "")),
                    description=truncate_description(description) if description else None,
                    ats_type="lever",
                    ats_company_slug=slug,
                    ats_job_id=job_id,
                )
                if job.title and job.job_url:
                    jobs.append(job)

        except requests.exceptions.ConnectionError:
            console.print(f"[yellow]Lever {slug}:[/yellow] Connection failed")
        except Exception as e:
            if "404" not in str(e):
                console.print(f"[yellow]Lever {slug}:[/yellow] {type(e).__name__}: {e}")

    if filled_desc_count:
        console.print(f"  [dim]Lever: filled {filled_desc_count} empty descriptions via detail API[/dim]")
    console.print(f"[cyan]Lever:[/cyan] Fetched {len(jobs)} jobs from {len(company_slugs)} companies")
    return jobs
