"""
SmartRecruiters Public API Job Discovery.

SmartRecruiters is used by: Freshworks, Zomato, Bosch, LinkedIn, Visa, and many global enterprises.
Public API endpoint (no auth needed for job listings):
  GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings
  GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings/{jobId}  (full details)
"""

import requests
from typing import List, Optional
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description, clean_html

console = Console()

SR_API_BASE = "https://api.smartrecruiters.com/v1"


def _fetch_sr_posting_detail(company_id: str, job_id: str) -> Optional[dict]:
    """Fetch full job posting detail from SmartRecruiters public API."""
    try:
        url = f"{SR_API_BASE}/companies/{company_id}/postings/{job_id}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _extract_sr_description(posting: dict, company_id: str, job_id: str) -> str:
    """Extract description from a SR posting dict. Falls back to detail API if empty."""
    desc = posting.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", "")
    if not desc:
        for cf in (posting.get("customField") or []):
            v = cf.get("value", "")
            if v and len(v) > len(desc):
                desc = v

    if not desc or len(clean_html(desc).strip()) < 50:
        detail = _fetch_sr_posting_detail(company_id, job_id)
        if detail:
            sections = detail.get("jobAd", {}).get("sections", {})
            parts = []
            for section_key in ["jobDescription", "qualifications", "additionalInformation"]:
                sec_text = sections.get(section_key, {}).get("text", "")
                if sec_text:
                    parts.append(clean_html(sec_text))
            desc = "\n\n".join(parts)

    return clean_html(desc).strip()


def _fetch_sr_postings(
    company_id: str,
    roles_filter: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """Fetch job postings from SmartRecruiters public API for a company."""
    try:
        url = f"{SR_API_BASE}/companies/{company_id}/postings"
        params = {"limit": limit, "status": "PUBLIC"}
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            postings = r.json().get("content", [])
            if roles_filter:
                roles_lower = [r.lower() for r in roles_filter]
                postings = [
                    p for p in postings
                    if any(kw in p.get("name", "").lower() for kw in roles_lower)
                ]
            return postings
    except Exception as e:
        console.print(f"[dim]SmartRecruiters {company_id} error: {e}[/dim]")
    return []


def fetch_smartrecruiters_jobs(
    company_ids: list[str],
    roles_filter: list[str] | None = None,
    config: dict | None = None,
) -> List[RawJob]:
    """
    Fetch jobs from SmartRecruiters public API for configured companies.

    Args:
        company_ids: SmartRecruiters company identifiers (e.g. ["Freshworks", "Zomato"])
        roles_filter: Optional keyword list to filter by title
        config: App config dict

    Returns:
        List[RawJob]
    """
    jobs: List[RawJob] = []
    seen_ids: set[str] = set()
    filled_desc_count = 0

    for company_id in company_ids:
        try:
            postings = _fetch_sr_postings(company_id, roles_filter, limit=50)
            for posting in postings:
                job_id = posting.get("id", "")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # Location
                loc = posting.get("location", {})
                location = ", ".join(filter(None, [
                    loc.get("city", ""),
                    loc.get("country", ""),
                ])) if loc else ""
                is_remote = posting.get("workplace", {}).get("wfhPolicy", "") in ("remote", "REMOTE")

                # URLs
                job_url = f"https://careers.smartrecruiters.com/{company_id}/jobs/{job_id}"
                apply_url = job_url

                # Employment type from typeOfEmployment
                emp_type_raw = posting.get("typeOfEmployment", {}).get("id", "")
                emp_map = {
                    "permanent": "full-time", "temporary": "contract",
                    "contract": "contract", "internship": "internship", "part_time": "part-time",
                }
                employment_type = emp_map.get(emp_type_raw, None)

                # Seniority from experienceLevel
                exp_level = posting.get("experienceLevel", {}).get("id", "")
                seniority_map = {
                    "entry_level": "junior", "mid_senior_level": "mid",
                    "senior_level": "senior", "internship": "intern",
                    "director": "staff", "executive": "staff",
                }
                seniority = seniority_map.get(exp_level, None)

                # Extract description with detail API fallback
                desc_before = posting.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", "")
                desc = _extract_sr_description(posting, company_id, str(job_id))
                if desc and (not desc_before or len(clean_html(desc_before).strip()) < 50):
                    filled_desc_count += 1

                job = RawJob(
                    external_id=f"sr_{company_id}_{job_id}",
                    source="smartrecruiters",
                    title=posting.get("name", "").strip(),
                    company=posting.get("company", {}).get("name", company_id),
                    location=location or "Remote",
                    is_remote=is_remote,
                    job_url=job_url,
                    apply_url=apply_url,
                    description=truncate_description(desc) if desc else None,
                    ats_type="smartrecruiters",
                    ats_company_slug=company_id,
                    ats_job_id=str(job_id),
                    posted_at=posting.get("releasedDate", "")[:10] if posting.get("releasedDate") else None,
                    department=posting.get("department", {}).get("label", ""),
                    employment_type=employment_type,
                    seniority=seniority,
                )

                if job.title:
                    jobs.append(job)

        except Exception as e:
            console.print(f"[dim]SmartRecruiters {company_id}: {type(e).__name__}: {e}[/dim]")

    if filled_desc_count:
        console.print(f"  [dim]SmartRecruiters: filled {filled_desc_count} empty descriptions via detail API[/dim]")
    console.print(f"[cyan]SmartRecruiters:[/cyan] Fetched {len(jobs)} jobs from {len(company_ids)} companies")
    return jobs
