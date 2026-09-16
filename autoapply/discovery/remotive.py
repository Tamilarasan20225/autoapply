"""
Remotive API client — remote tech job listings, no API key needed.
Endpoint: https://remotive.com/api/remote-jobs
"""

import requests
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description

console = Console()

REMOTIVE_API = "https://remotive.com/api/remote-jobs"

CATEGORY_MAP = {
    "software-dev": "Software Development",
    "data": "Data",
    "devops": "DevOps / Sysadmin",
    "design": "Design",
    "product": "Product",
}


def fetch_remotive_jobs(
    category: str = "software-dev",
    search_terms: list[str] | None = None,
    limit: int = 100,
) -> List[RawJob]:
    """
    Fetch remote jobs from Remotive.

    Args:
        category: Job category slug (software-dev, data, devops, etc.)
        search_terms: Optional keywords to filter by
        limit: Max results to return

    Returns:
        List of RawJob objects
    """
    jobs: List[RawJob] = []

    try:
        params = {"category": category, "limit": limit}
        if search_terms:
            params["search"] = " ".join(search_terms[:3])  # API supports one search string

        response = requests.get(REMOTIVE_API, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        raw_jobs = data.get("jobs", [])
        console.print(f"[cyan]Remotive:[/cyan] Fetched {len(raw_jobs)} jobs")

        for item in raw_jobs:
            job = RawJob(
                external_id=f"remotive_{item.get('id', '')}",
                source="remotive",
                title=item.get("title", "").strip(),
                company=item.get("company_name", "").strip(),
                location=item.get("candidate_required_location", "Worldwide"),
                is_remote=True,
                job_url=item.get("url", ""),
                apply_url=item.get("url", ""),
                description=truncate_description(item.get("description", "")),
                ats_type="other",
            )
            if job.title and job.company and job.job_url:
                jobs.append(job)

    except requests.exceptions.ConnectionError:
        console.print("[yellow]Remotive:[/yellow] Connection failed — skipping")
    except requests.exceptions.Timeout:
        console.print("[yellow]Remotive:[/yellow] Timeout — skipping")
    except Exception as e:
        console.print(f"[red]Remotive error:[/red] {e}")

    return jobs
