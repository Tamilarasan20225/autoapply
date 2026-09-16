"""
RemoteOK API client — remote job board, no key needed.
Endpoint: https://remoteok.com/api
Note: Attribution required per their terms — jobs link back to remoteok.com
"""

import requests
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description

console = Console()

REMOTEOK_API = "https://remoteok.com/api"


def fetch_remoteok_jobs(tags: list[str] | None = None, limit: int = 60) -> List[RawJob]:
    """
    Fetch remote jobs from RemoteOK.

    Args:
        tags: Filter by tags e.g. ["backend", "python", "java"]
        limit: Max results to return

    Returns:
        List of RawJob objects
    """
    jobs: List[RawJob] = []

    try:
        # Build URL — RemoteOK supports tag-based endpoints
        # Fixed: 'tag' variable was computed but never used in URL (was using tags join directly)
        if tags:
            tags_str = ",".join(t.lower().replace(" ", "-") for t in tags[:3])
            url = f"{REMOTEOK_API}?tags={tags_str}"
        else:
            url = REMOTEOK_API

        headers = {
            "User-Agent": "AutoAppy Job Bot (personal use) - github.com/Tamilarasan20225"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        # First item is metadata, rest are jobs
        raw_jobs = [j for j in data if isinstance(j, dict) and j.get("position")][:limit]
        console.print(f"[cyan]RemoteOK:[/cyan] Fetched {len(raw_jobs)} jobs")

        for item in raw_jobs:
            # Parse salary if available
            salary_min = None
            salary_max = None
            if item.get("salary_min"):
                try:
                    salary_min = float(str(item["salary_min"]).replace(",", ""))
                    salary_max = float(str(item.get("salary_max", salary_min)).replace(",", ""))
                except Exception:
                    pass

            job = RawJob(
                external_id=f"remoteok_{item.get('id', '')}",
                source="remoteok",
                title=item.get("position", "").strip(),
                company=item.get("company", "").strip(),
                location="Remote",
                is_remote=True,
                job_url=item.get("url", f"https://remoteok.com/l/{item.get('id', '')}"),
                apply_url=item.get("apply_url", item.get("url", "")),
                description=truncate_description(item.get("description", "")),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_currency="USD",
                ats_type="other",
            )
            if job.title and job.company and job.job_url:
                jobs.append(job)

    except requests.exceptions.ConnectionError:
        console.print("[yellow]RemoteOK:[/yellow] Connection failed — skipping")
    except requests.exceptions.Timeout:
        console.print("[yellow]RemoteOK:[/yellow] Timeout — skipping")
    except Exception as e:
        console.print(f"[red]RemoteOK error:[/red] {e}")

    return jobs
