"""
India-specific job boards: Naukri, Instahyre, Hirist.

These are the top India boards by volume. JobSpy's Naukri module is flaky and
returns no descriptions, so Naukri is queried directly here.
"""

from __future__ import annotations

import os
from typing import List, Optional

from rich.console import Console

from autoapply.discovery import http
from autoapply.discovery.base import RawJob, clean_html, truncate_description

console = Console()

# ── Naukri ───────────────────────────────────────────────────────────────────

# Naukri's search API now answers {"message":"recaptcha required","statusCode":406}
# for unauthenticated clients, so sources.naukri is disabled by default. The code
# stays because supplying a logged-in cookie via NAUKRI_COOKIE revives it.
NAUKRI_SEARCH = "https://www.naukri.com/jobapi/v3/search"
NAUKRI_DETAIL = "https://www.naukri.com/jobapi/v4/job/{job_id}"
_NAUKRI_HEADERS = {
    "appid": "109",
    "systemid": "Naukri",
    "Accept": "application/json",
    "Referer": "https://www.naukri.com/",
}


def _naukri_detail(job_id: str) -> Optional[str]:
    data = http.get_json(
        NAUKRI_DETAIL.format(job_id=job_id),
        headers=_NAUKRI_HEADERS, timeout=15, host_delay=0.5,
    )
    if not isinstance(data, dict):
        return None
    details = (data.get("jobDetails") or {})
    parts = [
        details.get("description", ""),
        details.get("keySkills", ""),
        " ".join(str(v) for v in (details.get("jobDetail") or {}).values() if isinstance(v, str)),
    ]
    text = clean_html("\n\n".join(p for p in parts if p))
    return text or None


def _naukri_lacs_to_inr(value) -> Optional[float]:
    try:
        return float(value) * 100_000
    except (TypeError, ValueError):
        return None


def fetch_naukri_jobs(
    roles: list[str],
    locations: list[str] | None = None,
    max_results: int = 150,
    fetch_descriptions: bool = True,
) -> List[RawJob]:
    locations = locations or ["bangalore"]
    jobs: List[RawJob] = []
    seen: set[str] = set()

    headers = dict(_NAUKRI_HEADERS)
    cookie = os.environ.get("NAUKRI_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie

    for role in roles[:5]:
        for location in locations[:2]:
            data = http.get_json(
                NAUKRI_SEARCH,
                headers=headers,
                params={
                    "noOfResults": 40,
                    "urlType": "search_by_key_loc",
                    "searchType": "adv",
                    "keyword": role,
                    "location": location,
                    "pageNo": 1,
                    "seoKey": f"{role}-jobs-in-{location}".lower().replace(" ", "-"),
                    "src": "jobsearchDesk",
                    "latLong": "",
                },
                timeout=20,
                host_delay=0.8,
            )
            if not isinstance(data, dict):
                continue
            for item in data.get("jobDetails", []) or []:
                job_id = str(item.get("jobId", "")).strip()
                title = (item.get("title") or "").strip()
                company = (item.get("companyName") or "").strip()
                if not (job_id and title and company) or job_id in seen:
                    continue
                seen.add(job_id)

                url = item.get("jdURL") or ""
                if url and not url.startswith("http"):
                    url = f"https://www.naukri.com{url}"
                if not url:
                    url = f"https://www.naukri.com/job-listings-{job_id}"

                description = _naukri_detail(job_id) if fetch_descriptions else None
                if not description:
                    description = clean_html(item.get("jobDescription", "") or "")

                placeholders = item.get("placeholders") or []
                location_text = next(
                    (p.get("label", "") for p in placeholders if p.get("type") == "location"),
                    location,
                )
                salary_text = next(
                    (p.get("label", "") for p in placeholders if p.get("type") == "salary"), ""
                )

                jobs.append(RawJob(
                    external_id=f"naukri_{job_id}",
                    source="naukri",
                    title=title,
                    company=company,
                    location=location_text,
                    is_remote="remote" in f"{title} {location_text}".lower(),
                    job_url=url,
                    apply_url=url,
                    description=truncate_description(description) if description else None,
                    salary_min=_naukri_lacs_to_inr(item.get("minimumSalary")),
                    salary_max=_naukri_lacs_to_inr(item.get("maximumSalary")),
                    salary_currency="INR" if salary_text else None,
                    posted_at=(item.get("createdDate") or "")[:10] or None,
                    ats_type="naukri",
                    ats_job_id=job_id,
                    source_query=f"{role}/{location}",
                ))
                if len(jobs) >= max_results:
                    console.print(f"[cyan]Naukri:[/cyan] {len(jobs)} jobs")
                    return jobs

    console.print(f"[cyan]Naukri:[/cyan] {len(jobs)} jobs")
    return jobs


# ── Instahyre ────────────────────────────────────────────────────────────────

INSTAHYRE_SEARCH = "https://www.instahyre.com/api/v1/job_search"


def fetch_instahyre_jobs(
    roles: list[str],
    max_results: int = 100,
) -> List[RawJob]:
    jobs: List[RawJob] = []
    seen: set[str] = set()

    for role in roles[:4]:
        data = http.get_json(
            INSTAHYRE_SEARCH,
            params={"limit": 40, "offset": 0, "q": role, "job_type": 0},
            headers={"Referer": "https://www.instahyre.com/search-jobs/"},
            timeout=20,
            host_delay=0.8,
        )
        if not isinstance(data, dict):
            continue
        for item in data.get("objects", []) or []:
            if not isinstance(item, dict):
                continue
            employer = item.get("employer") or {}
            title = (item.get("title") or "").strip()
            company = (employer.get("company_name") or "").strip()
            slug = item.get("public_url") or item.get("id")
            if not (title and company and slug) or str(slug) in seen:
                continue
            seen.add(str(slug))

            url = slug if str(slug).startswith("http") else f"https://www.instahyre.com/job/{slug}"
            # The search API carries no JD body; the enricher back-fills it from public_url.
            keywords = item.get("keywords") or ""
            if isinstance(keywords, list):
                keywords = ", ".join(str(k) for k in keywords)
            description = clean_html(str(keywords))
            jobs.append(RawJob(
                external_id=f"instahyre_{item.get('id', slug)}",
                source="instahyre",
                title=title,
                company=company,
                location=item.get("locations") or "India",
                is_remote="remote" in f"{title}".lower(),
                job_url=url,
                apply_url=url,
                description=truncate_description(description) if description else None,
                source_query=role,
            ))
            if len(jobs) >= max_results:
                break

    console.print(f"[cyan]Instahyre:[/cyan] {len(jobs)} jobs")
    return jobs[:max_results]


# ── Hirist ───────────────────────────────────────────────────────────────────

HIRIST_SEARCH = "https://www.hirist.tech/api/v1/search/jobs"


def fetch_hirist_jobs(roles: list[str], max_results: int = 100) -> List[RawJob]:
    jobs: List[RawJob] = []
    seen: set[str] = set()

    for role in roles[:4]:
        data = http.get_json(
            HIRIST_SEARCH,
            params={"q": role, "page": 1, "size": 40, "location": "bangalore"},
            headers={"Referer": "https://www.hirist.tech/"},
            timeout=20,
            host_delay=0.8,
        )
        if not isinstance(data, dict):
            continue
        for item in (data.get("jobs") or data.get("data") or []):
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("jobId") or item.get("id") or "").strip()
            title = (item.get("title") or item.get("jobTitle") or "").strip()
            company = (item.get("companyName") or item.get("company") or "").strip()
            if not (job_id and title and company) or job_id in seen:
                continue
            seen.add(job_id)

            url = item.get("jobUrl") or f"https://www.hirist.tech/j/{job_id}"
            description = clean_html(item.get("jobDescription") or item.get("description") or "")
            jobs.append(RawJob(
                external_id=f"hirist_{job_id}",
                source="hirist",
                title=title,
                company=company,
                location=item.get("location") or "India",
                is_remote="remote" in f"{title}".lower(),
                job_url=url,
                apply_url=url,
                description=truncate_description(description) if description else None,
                posted_at=(item.get("postedDate") or "")[:10] or None,
                ats_job_id=job_id,
                source_query=role,
            ))
            if len(jobs) >= max_results:
                break

    console.print(f"[cyan]Hirist:[/cyan] {len(jobs)} jobs")
    return jobs[:max_results]
