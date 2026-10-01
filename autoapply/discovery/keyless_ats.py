"""
Keyless per-company ATS endpoints: Recruitee, Teamtailor, Personio, Breezy.

No API key required. Each probe is a single request against `{company}.{vendor}`,
so a company list is the only input needed. Breezy's list endpoint omits
descriptions; those jobs are back-filled later by the enricher.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional

from rich.console import Console

from autoapply.discovery import http
from autoapply.discovery.base import RawJob, clean_html, truncate_description

console = Console()

DEFAULT_TECH_FILTER = [
    "engineer", "developer", "software", "backend", "python", "java",
    "data", "ai", "ml", "api", "platform", "infrastructure", "devops",
    "sre", "fullstack", "full-stack", "architect", "scientist",
]


def _matches(title: str, roles_filter: Optional[list[str]]) -> bool:
    keywords = roles_filter or DEFAULT_TECH_FILTER
    lowered = title.lower()
    return any(kw.lower() in lowered for kw in keywords)


def _mk(source: str, ats: str, slug: str, job_id: str, title: str, company: str,
        location: str, url: str, description: str, posted_at: Optional[str] = None,
        is_remote: bool = False) -> RawJob:
    return RawJob(
        external_id=f"{source}_{slug}_{job_id}",
        source=source,
        title=title,
        company=company,
        location=location or "Not specified",
        is_remote=is_remote or "remote" in f"{title} {location}".lower(),
        job_url=url,
        apply_url=url,
        description=truncate_description(description) if description else None,
        posted_at=posted_at,
        ats_type=ats,
        ats_company_slug=slug,
        ats_job_id=str(job_id),
    )


# ── Vendors ──────────────────────────────────────────────────────────────────

def _recruitee(slug: str, roles_filter) -> List[RawJob]:
    data = http.get_json(f"https://{slug}.recruitee.com/api/offers/", timeout=12)
    if not isinstance(data, dict):
        return []
    company = slug.replace("-", " ").title()
    out = []
    for offer in data.get("offers", []) or []:
        title = (offer.get("title") or "").strip()
        if not title or not _matches(title, roles_filter):
            continue
        description = clean_html(
            (offer.get("description") or "") + "\n\n" + (offer.get("requirements") or "")
        )
        out.append(_mk(
            "recruitee", "recruitee", slug, str(offer.get("id", "")), title,
            offer.get("company_name") or company,
            ", ".join(filter(None, [offer.get("city"), offer.get("country")])),
            offer.get("careers_url") or offer.get("careers_apply_url") or "",
            description,
            (offer.get("published_at") or "")[:10] or None,
            bool(offer.get("remote")),
        ))
    return [j for j in out if j.job_url]


def _teamtailor(slug: str, roles_filter) -> List[RawJob]:
    # jobs.json is a JSON Feed: {"items": [{id,title,url,content_html,_jobposting}]}
    data = http.get_json(f"https://{slug}.teamtailor.com/jobs.json", timeout=12)
    if not isinstance(data, (dict, list)):
        return []
    items = (data.get("items") or data.get("jobs") or []) if isinstance(data, dict) else data
    company = slug.replace("-", " ").title()
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title or not _matches(title, roles_filter):
            continue
        posting = item.get("_jobposting") or {}
        location = ""
        job_loc = posting.get("jobLocation") or {}
        if isinstance(job_loc, dict):
            addr = job_loc.get("address") or {}
            location = ", ".join(filter(None, [
                addr.get("addressLocality"), addr.get("addressCountry"),
            ])) if isinstance(addr, dict) else ""
        out.append(_mk(
            "teamtailor", "teamtailor", slug, str(item.get("id", "")), title,
            (posting.get("hiringOrganization") or {}).get("name") or company
            if isinstance(posting.get("hiringOrganization"), dict) else company,
            location,
            item.get("url") or "",
            clean_html(item.get("content_html") or posting.get("description") or ""),
            (item.get("date_published") or "")[:10] or None,
        ))
    return [j for j in out if j.job_url]


_PERSONIO_ITEM = re.compile(r"<position>(.*?)</position>", re.S | re.I)
_PERSONIO_FIELD = re.compile(r"<(\w+)>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</\1>", re.S)


def _personio(slug: str, roles_filter) -> List[RawJob]:
    response = http.get(
        f"https://{slug}.jobs.personio.de/xml",
        headers={"Accept": "application/xml"}, timeout=12,
    )
    if response is None or response.status_code != 200:
        return []
    company = slug.replace("-", " ").title()
    out = []
    for block in _PERSONIO_ITEM.findall(response.text):
        fields = {k.lower(): v.strip() for k, v in _PERSONIO_FIELD.findall(block)}
        title = fields.get("name", "")
        job_id = fields.get("id", "")
        if not (title and job_id) or not _matches(title, roles_filter):
            continue
        out.append(_mk(
            "personio", "personio", slug, job_id, title, company,
            fields.get("office", ""),
            f"https://{slug}.jobs.personio.de/job/{job_id}",
            clean_html(block),
            (fields.get("createdat") or "")[:10] or None,
        ))
    return out


def _breezy(slug: str, roles_filter) -> List[RawJob]:
    data = http.get_json(f"https://{slug}.breezy.hr/json", timeout=12)
    if not isinstance(data, list):
        return []
    company = slug.replace("-", " ").title()
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = (item.get("name") or "").strip()
        job_id = str(item.get("id", ""))
        if not (title and job_id) or not _matches(title, roles_filter):
            continue
        location = item.get("location") or {}
        loc_text = ""
        if isinstance(location, dict):
            city = (location.get("city") or "")
            country = (location.get("country") or {}).get("name", "") \
                if isinstance(location.get("country"), dict) else str(location.get("country") or "")
            loc_text = ", ".join(filter(None, [city, country]))
        out.append(_mk(
            "breezy", "breezy", slug, job_id, title, company, loc_text,
            item.get("url") or f"https://{slug}.breezy.hr/p/{job_id}",
            clean_html(item.get("description") or ""),
            (item.get("published_date") or "")[:10] or None,
            bool((location or {}).get("is_remote")) if isinstance(location, dict) else False,
        ))
    return out


# BambooHR is deliberately absent: /careers/list now serves an HTML SPA shell
# rather than JSON, so there is no keyless endpoint left to scrape.
VENDORS: dict[str, Callable[[str, Optional[list]], List[RawJob]]] = {
    "recruitee": _recruitee,
    "teamtailor": _teamtailor,
    "personio": _personio,
    "breezy": _breezy,
}


def fetch_keyless_ats_jobs(
    companies: list[str],
    vendors: list[str] | None = None,
    roles_filter: list[str] | None = None,
    max_workers: int = 8,
) -> List[RawJob]:
    """Probe each company against each keyless vendor."""
    if not companies:
        return []
    active = [v for v in (vendors or list(VENDORS)) if v in VENDORS]
    tasks = [(vendor, slug) for slug in companies for vendor in active]

    all_jobs: List[RawJob] = []
    hits = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(VENDORS[vendor], slug, roles_filter): (vendor, slug)
            for vendor, slug in tasks
        }
        for future in as_completed(futures):
            vendor, slug = futures[future]
            try:
                result = future.result()
            except Exception as e:
                console.print(f"[dim]{vendor}/{slug}: {type(e).__name__}: {e}[/dim]")
                continue
            if result:
                hits += 1
                all_jobs.extend(result)

    console.print(
        f"[cyan]Keyless ATS:[/cyan] {len(all_jobs)} jobs from {hits}/{len(tasks)} probes"
    )
    return all_jobs
