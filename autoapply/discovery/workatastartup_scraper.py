"""
Y Combinator WorkAtAStartup / HN Who Is Hiring Scraper.

Uses Hacker News Algolia API to fetch 'Who is hiring' thread comments.
Each top-level comment in the thread is one company's job posting.
API: https://hn.algolia.com/api/v1/search_by_date?query=Ask+HN+Who+is+hiring&tags=ask_hn
     https://hn.algolia.com/api/v1/items/{thread_id}
Free, no API key needed, 10,000+ req/hour limit.
"""

import re
import time
import requests
from typing import List
from rich.console import Console
from autoapply.discovery.base import RawJob, truncate_description, clean_html

console = Console()
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEMS_URL = "https://hn.algolia.com/api/v1/items/{item_id}"
HN_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0"}

TECH_KEYWORDS = [
    "engineer", "developer", "dev", "backend", "software", "python",
    "java", "data", "ml", "ai", "sde", "swe", "platform", "infrastructure",
    "devops", "sre", "fullstack", "full stack", "scientist", "nlp",
    "machine learning", "deep learning", "llm", "api", "cloud",
]


def _get_latest_hn_hiring_thread_id() -> int | None:
    """Find the latest Ask HN: Who is hiring? thread ID via Algolia."""
    try:
        r = requests.get(
            HN_SEARCH_URL,
            params={"query": "Ask HN: Who is hiring", "tags": "ask_hn", "hitsPerPage": 5},
            timeout=15,
            headers=HN_HEADERS,
        )
        if r.status_code != 200:
            return None
        hits = r.json().get("hits", [])
        for h in hits:
            if "who is hiring" in h.get("title", "").lower():
                return int(h["objectID"])
    except Exception:
        pass
    return None


def _parse_hn_job_comment(child: dict, roles_filter: list) -> RawJob | None:
    """Parse a single HN thread comment into a RawJob."""
    text_html = child.get("text", "") or ""
    if len(text_html) < 30:
        return None
    text_clean = clean_html(text_html).strip()
    first_line = re.split(r'[\n\r]', text_clean)[0][:250]

    # Role filter check (search in full text)
    if roles_filter and not any(kw.lower() in text_clean.lower() for kw in roles_filter):
        return None

    # Tech keyword check - skip clearly non-tech posts
    if not any(kw.lower() in text_clean.lower() for kw in TECH_KEYWORDS[:15]):
        return None

    # Extract company name (usually first part of first line: "Company | Role | Location")
    parts = re.split(r'\s*[|/]\s*', first_line)
    company = parts[0].strip()[:80] if parts else "HN Company"
    # Clean company name of common prefixes
    company = re.sub(r'^(we are|we\'re|join|at|at\s+|company:?)\s*', '', company, flags=re.I).strip()
    if not company or len(company) < 2:
        return None

    # Extract role from second part if available
    title = "Software Engineer"
    if len(parts) > 1:
        candidate_title = parts[1].strip()
        if any(kw.lower() in candidate_title.lower() for kw in TECH_KEYWORDS):
            title = candidate_title[:100]

    # Extract location
    is_remote = bool(re.search(r'\bremote\b', text_clean, re.I))
    location = "Remote" if is_remote else "USA"
    loc_match = re.search(
        r'(Bangalore|Bengaluru|India|San Francisco|New York|Seattle|London|Berlin|Singapore|Remote|Worldwide)',
        text_clean, re.I
    )
    if loc_match:
        location = loc_match.group(1)
        if location.lower() in ('bangalore', 'bengaluru', 'india'):
            is_remote = is_remote or True  # India-relevant!

    # Extract URL
    url_match = re.search(r'https?://[^\s<>"\)]+', text_clean)
    job_url = url_match.group(0).rstrip('.,') if url_match else f"https://news.ycombinator.com/item?id={child.get('id','')}"

    job_id = str(child.get("id", ""))
    posted_at = str(child.get("created_at", ""))[:10] or None

    return RawJob(
        external_id=f"hn_hiring_{job_id}",
        source="workatastartup",
        title=title,
        company=company,
        location=location,
        is_remote=is_remote,
        job_url=job_url,
        apply_url=job_url,
        description=truncate_description(text_clean),
        posted_at=posted_at,
        source_query="hn_who_is_hiring",
    )


def fetch_workatastartup_jobs(roles=None, max_results=150, config=None) -> List[RawJob]:
    """
    Fetch jobs from HN 'Who is hiring?' thread (YC/startup community).
    Discovers the latest monthly thread automatically via Algolia.

    Args:
        roles: Role keywords for filtering (e.g. ['engineer', 'backend'])
        max_results: Max jobs to return
        config: Config dict (unused)

    Returns:
        List of RawJob objects
    """
    roles_filter = roles or ["engineer", "developer", "backend", "python", "java", "data", "ai"]
    jobs: List[RawJob] = []
    seen_ids: set = set()

    # Find latest thread
    thread_id = _get_latest_hn_hiring_thread_id()
    if not thread_id:
        console.print("[dim]WAS/HN: Could not find latest hiring thread[/dim]")
        return []

    console.print(f"[dim]WAS/HN: Fetching thread {thread_id}...[/dim]")

    try:
        r = requests.get(
            HN_ITEMS_URL.format(item_id=thread_id),
            timeout=25,
            headers=HN_HEADERS,
        )
        if r.status_code != 200:
            console.print(f"[dim]WAS/HN: HTTP {r.status_code}[/dim]")
            return []

        data = r.json()
        children = data.get("children", [])
        console.print(f"[dim]WAS/HN: {len(children)} comments in hiring thread[/dim]")

        for child in children:
            if not isinstance(child, dict):
                continue
            job_id = str(child.get("id", ""))
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)

            job = _parse_hn_job_comment(child, roles_filter)
            if job:
                jobs.append(job)

            if len(jobs) >= max_results:
                break

    except requests.exceptions.Timeout:
        console.print("[dim]WAS/HN: Timeout fetching thread[/dim]")
    except Exception as e:
        console.print(f"[dim]WAS/HN: {type(e).__name__}: {e}[/dim]")

    console.print(f"[cyan]WorkAtAStartup:[/cyan] Found {len(jobs)} YC/HN startup jobs")
    return jobs[:max_results]
