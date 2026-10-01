"""
Workday tenant discovery.

Workday powers Flipkart, Walmart, Salesforce, Adobe, SAP, Infosys and most large
enterprises. The CXS search endpoint is keyless:

    POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    body: {"appliedFacets":{},"limit":20,"offset":0,"searchText":"..."}

Detail (full JD) is a GET on the same base + the posting path.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from rich.console import Console

from autoapply.discovery import http
from autoapply.discovery.base import RawJob, clean_html, truncate_description

console = Console()

_PAGE_LIMIT = 20


def _cxs_base(tenant: str, site: str, dc: str) -> str:
    return f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def _fetch_detail(base: str, external_path: str) -> Optional[str]:
    data = http.get_json(f"{base}{external_path}", timeout=20, host_delay=0.4)
    if not isinstance(data, dict):
        return None
    info = data.get("jobPostingInfo") or {}
    text = clean_html(info.get("jobDescription", "") or "")
    return text or None


def _fetch_tenant(
    tenant: str,
    site: str,
    dc: str,
    search_terms: list[str],
    max_per_tenant: int,
    fetch_descriptions: bool,
) -> List[RawJob]:
    base = _cxs_base(tenant, site, dc)
    jobs: List[RawJob] = []
    seen: set[str] = set()

    for term in search_terms:
        offset = 0
        while offset < max_per_tenant:
            data = http.post_json(
                f"{base}/jobs",
                json={"appliedFacets": {}, "limit": _PAGE_LIMIT, "offset": offset, "searchText": term},
                headers={"Content-Type": "application/json"},
                timeout=20,
                host_delay=0.4,
            )
            if not isinstance(data, dict):
                break
            postings = data.get("jobPostings") or []
            if not postings:
                break

            for item in postings:
                external_path = item.get("externalPath", "")
                title = (item.get("title") or "").strip()
                if not (external_path and title) or external_path in seen:
                    continue
                seen.add(external_path)

                public_url = f"https://{tenant}.{dc}.myworkdayjobs.com/en-US/{site}{external_path}"
                description = _fetch_detail(base, external_path) if fetch_descriptions else None

                jobs.append(RawJob(
                    external_id=f"workday_{tenant}_{external_path.strip('/').replace('/', '_')}",
                    source="workday",
                    title=title,
                    company=tenant.replace("-", " ").title(),
                    location=item.get("locationsText") or "Not specified",
                    is_remote="remote" in (item.get("locationsText") or "").lower(),
                    job_url=public_url,
                    apply_url=public_url,
                    description=truncate_description(description) if description else None,
                    posted_at=None,
                    ats_type="workday",
                    ats_company_slug=tenant,
                    ats_job_id=external_path,
                    source_query=term,
                ))
                if len(jobs) >= max_per_tenant:
                    return jobs

            offset += _PAGE_LIMIT

    return jobs


def fetch_workday_jobs(
    tenants: list[dict],
    search_terms: list[str] | None = None,
    max_per_tenant: int = 40,
    max_workers: int = 6,
    fetch_descriptions: bool = True,
) -> List[RawJob]:
    """
    Args:
        tenants: [{"tenant": "flipkart", "site": "External", "dc": "wd3"}, ...]
    """
    if not tenants:
        return []
    search_terms = search_terms or ["software engineer", "backend engineer"]
    all_jobs: List[RawJob] = []
    ok = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _fetch_tenant,
                t.get("tenant", ""),
                t.get("site", "External"),
                t.get("dc", "wd3"),
                search_terms[:2],
                max_per_tenant,
                fetch_descriptions,
            ): t
            for t in tenants if t.get("tenant")
        }
        for future in as_completed(futures):
            tenant = futures[future]
            try:
                result = future.result()
            except Exception as e:
                console.print(f"[dim]Workday {tenant.get('tenant')}: {type(e).__name__}: {e}[/dim]")
                continue
            if result:
                ok += 1
                all_jobs.extend(result)

    console.print(f"[cyan]Workday:[/cyan] {len(all_jobs)} jobs from {ok}/{len(tenants)} tenants")
    return all_jobs
