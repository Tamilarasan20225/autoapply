"""
Description back-fill.

Several sources store no description at all (Workable, CareerProber-SmartRecruiters)
or only a ~150-char SERP snippet (Adzuna, Jooble, Serper-Ashby). Those jobs can
never be scored. This pass re-fetches the real description from the ATS detail
endpoint the job already points at, using the fetchers the repo already has.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from rich.console import Console

from autoapply.discovery import http
from autoapply.discovery.base import clean_html, truncate_description

console = Console()

MIN_USEFUL_CHARS = 300


# ── Per-ATS fetchers ─────────────────────────────────────────────────────────

def _greenhouse(slug: str, job_id: str) -> Optional[str]:
    data = http.get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}",
        params={"content": "true"},
    )
    if not isinstance(data, dict):
        return None
    return clean_html(data.get("content", "") or "")


def _lever(slug: str, job_id: str) -> Optional[str]:
    from autoapply.discovery.ats_direct import _fetch_lever_description
    return _fetch_lever_description(slug, job_id)


def _ashby(slug: str, job_id: str) -> Optional[str]:
    # The POST posting-api is 401 now; GET on the board returns every posting
    # with its full description, so pick the one we want out of the payload.
    data = http.get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        params={"includeCompensation": "true"},
        timeout=20,
    )
    if not isinstance(data, dict):
        return None
    for item in data.get("jobs") or data.get("jobPostings") or []:
        if str(item.get("id")) == str(job_id):
            return item.get("descriptionPlain") or clean_html(item.get("descriptionHtml", "") or "")
    return None


def _smartrecruiters(slug: str, job_id: str) -> Optional[str]:
    from autoapply.discovery.smartrecruiters_discovery import _extract_sr_description
    detail = http.get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}")
    if not isinstance(detail, dict):
        return None
    return _extract_sr_description(detail, slug, job_id)


def _workable(slug: str, job_id: str) -> Optional[str]:
    data = http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}/jobs/{job_id}")
    if not isinstance(data, dict):
        data = http.get_json(f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{job_id}")
    if not isinstance(data, dict):
        return None
    parts = [
        data.get("description", ""),
        data.get("requirements", ""),
        data.get("benefits", ""),
    ]
    return clean_html("\n\n".join(p for p in parts if p))


_ATS_FETCHERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "smartrecruiters": _smartrecruiters,
    "workable": _workable,
}


# ── Generic HTML fallback ────────────────────────────────────────────────────

_STRIP_BLOCKS = re.compile(r"<(script|style|nav|footer|header|svg)\b[^>]*>.*?</\1>", re.I | re.S)
_MAIN_BLOCK = re.compile(
    r"<(?:main|article|section)\b[^>]*>(.*?)</(?:main|article|section)>"
    r"|<div[^>]+(?:class|id)=\"[^\"]*(?:job-?description|posting|content|description)[^\"]*\"[^>]*>(.*?)</div>",
    re.I | re.S,
)


def _generic_html(url: str) -> Optional[str]:
    response = http.get(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=20)
    if response is None or response.status_code != 200:
        return None
    html = _STRIP_BLOCKS.sub(" ", response.text)

    # Pick the richest candidate block, not the first — the first match is often
    # a small wrapper div that yields less text than the page body.
    candidates = [clean_html(html)]
    for match in _MAIN_BLOCK.finditer(html):
        for group in match.groups():
            if group:
                candidates.append(clean_html(group))
    text = max(candidates, key=len)
    return text if len(text) >= MIN_USEFUL_CHARS else None


# ── Orchestration ────────────────────────────────────────────────────────────

def fetch_description_for_job(job) -> Optional[str]:
    """Best description obtainable for one job, or None."""
    ats = (getattr(job, "ats_type", None) or "").lower()
    slug = getattr(job, "ats_company_slug", None)
    job_id = getattr(job, "ats_job_id", None)

    fetcher = _ATS_FETCHERS.get(ats)
    if fetcher and slug and job_id:
        try:
            text = fetcher(slug, str(job_id))
            if text and len(text.strip()) >= MIN_USEFUL_CHARS:
                return truncate_description(text)
        except Exception as e:
            console.print(f"[dim]enrich {ats}/{slug}: {type(e).__name__}: {e}[/dim]")

    for url in (getattr(job, "apply_url", None), getattr(job, "job_url", None)):
        if not url:
            continue
        try:
            text = _generic_html(url)
            if text:
                return truncate_description(text)
        except Exception:
            continue
    return None


def enrich_descriptions(limit: int = 300, max_workers: int = 8,
                        min_chars: int = MIN_USEFUL_CHARS) -> int:
    """Back-fill thin/missing descriptions. Returns the number of jobs improved."""
    from autoapply.tracker.db import get_jobs_needing_enrichment, set_job_description

    jobs = get_jobs_needing_enrichment(limit=limit, min_chars=min_chars)
    if not jobs:
        console.print("[dim]Enrichment: nothing to back-fill[/dim]")
        return 0

    console.print(f"[bold blue]Enriching {len(jobs)} thin/missing descriptions...[/bold blue]")

    # (id, before_len) is captured up front so the DB write happens on the main thread.
    payloads = [(j.id, len(j.description or ""), j) for j in jobs]
    improved = 0

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {
            executor.submit(fetch_description_for_job, job): (job_id, before)
            for job_id, before, job in payloads
        }
        for future in as_completed(futures):
            job_id, before = futures[future]
            try:
                text = future.result()
            except Exception:
                text = None
            if text and len(text) > max(before, min_chars - 1):
                set_job_description(job_id, text)
                improved += 1

    console.print(f"[green]Enrichment:[/green] {improved}/{len(jobs)} descriptions back-filled")
    return improved
