"""
Remotive API client — remote tech job listings, no API key needed.
Endpoint: https://remotive.com/api/remote-jobs

Supports multiple categories: software-dev, data, devops-sysadmin, backend.
Runs one query per category to maximize coverage.
"""

import requests
import time
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description

console = Console()

REMOTIVE_API = "https://remotive.com/api/remote-jobs"

CATEGORY_MAP = {
    "software-dev": "Software Development",
    "data": "Data",
    "devops-sysadmin": "DevOps / Sysadmin",
    "backend": "Backend",
    "design": "Design",
    "product": "Product",
}


def fetch_remotive_jobs(
    category: str = "software-dev",
    categories: list[str] | None = None,
    search_terms: list[str] | None = None,
    limit: int = 100,
) -> List[RawJob]:
    """
    Fetch remote jobs from Remotive, optionally across multiple categories.

    Args:
        category: Single category slug (legacy param, used if categories not set)
        categories: List of category slugs to fetch from (multi-category support)
        search_terms: Optional keywords to filter by
        limit: Max results per category

    Returns:
        List of RawJob objects (deduplicated by external_id)
    """
    # Multi-category support: use categories list if provided
    cats_to_fetch = categories or [category]
    jobs: List[RawJob] = []
    seen_ids: set[str] = set()

    for cat in cats_to_fetch:
        try:
            params = {"category": cat, "limit": limit}
            if search_terms:
                params["search"] = " ".join(search_terms[:3])

            response = requests.get(REMOTIVE_API, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            raw_jobs = data.get("jobs", [])

            for item in raw_jobs:
                ext_id = f"remotive_{item.get('id', '')}"
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                posted_at = None
                pub_date = item.get("publication_date", "")
                if pub_date:
                    posted_at = str(pub_date)[:10]

                job = RawJob(
                    external_id=ext_id,
                    source="remotive",
                    title=item.get("title", "").strip(),
                    company=item.get("company_name", "").strip(),
                    location=item.get("candidate_required_location", "Worldwide"),
                    is_remote=True,
                    job_url=item.get("url", ""),
                    apply_url=item.get("url", ""),
                    description=truncate_description(item.get("description", "")),
                    ats_type="other",
                    posted_at=posted_at,
                    department=CATEGORY_MAP.get(cat, cat),
                )
                if job.title and job.company and job.job_url:
                    jobs.append(job)

            time.sleep(0.3)  # Polite between categories

        except requests.exceptions.ConnectionError:
            console.print(f"[yellow]Remotive ({cat}):[/yellow] Connection failed — skipping")
        except requests.exceptions.Timeout:
            console.print(f"[yellow]Remotive ({cat}):[/yellow] Timeout — skipping")
        except Exception as e:
            console.print(f"[red]Remotive ({cat}) error:[/red] {e}")

    console.print(f"[cyan]Remotive:[/cyan] Fetched {len(jobs)} jobs across {len(cats_to_fetch)} categories")
    return jobs

