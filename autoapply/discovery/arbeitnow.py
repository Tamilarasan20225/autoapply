"""
Arbeitnow API client — free job board, Europe/Remote focused, no key needed.
Endpoint: https://www.arbeitnow.com/api/job-board-api
"""

import requests
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description

console = Console()

ARBEITNOW_API = "https://www.arbeitnow.com/api/job-board-api"


def fetch_arbeitnow_jobs(limit: int = 100) -> List[RawJob]:
    """
    Fetch jobs from Arbeitnow (Europe/Remote focused, free).

    Returns:
        List of RawJob objects
    """
    jobs: List[RawJob] = []

    try:
        response = requests.get(ARBEITNOW_API, timeout=15)
        response.raise_for_status()
        data = response.json()

        raw_jobs = data.get("data", [])[:limit]
        console.print(f"[cyan]Arbeitnow:[/cyan] Fetched {len(raw_jobs)} jobs")

        for item in raw_jobs:
            tags = item.get("tags", [])
            is_remote = item.get("remote", False)

            job = RawJob(
                external_id=f"arbeitnow_{item.get('slug', '')}",
                source="arbeitnow",
                title=item.get("title", "").strip(),
                company=item.get("company_name", "").strip(),
                location=item.get("location", "Remote" if is_remote else ""),
                is_remote=is_remote,
                job_url=item.get("url", ""),
                apply_url=item.get("url", ""),
                description=truncate_description(item.get("description", "")),
                ats_type="other",
            )
            if job.title and job.company and job.job_url:
                jobs.append(job)

    except requests.exceptions.ConnectionError:
        console.print("[yellow]Arbeitnow:[/yellow] Connection failed — skipping")
    except requests.exceptions.Timeout:
        console.print("[yellow]Arbeitnow:[/yellow] Timeout — skipping")
    except Exception as e:
        console.print(f"[red]Arbeitnow error:[/red] {e}")

    return jobs
