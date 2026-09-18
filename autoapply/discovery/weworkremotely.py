"""
We Work Remotely — free RSS feeds, no API key needed.
Category feeds: https://weworkremotely.com/categories/<slug>.rss
"""

import re
import requests
import xml.etree.ElementTree as ET
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description, clean_html

console = Console()

WWR_BASE = "https://weworkremotely.com/categories/{slug}.rss"

DEFAULT_CATEGORIES = [
    "remote-programming-jobs",
    "remote-back-end-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-devops-sysadmin-jobs",
]


def _parse_title(raw_title: str) -> tuple[str, str]:
    """WWR titles are formatted 'Company: Role' — split them, falling back gracefully."""
    if ":" in raw_title:
        company, role = raw_title.split(":", 1)
        return company.strip(), role.strip()
    return "Unknown", raw_title.strip()


def fetch_weworkremotely_jobs(categories: list[str] | None = None, limit: int = 100) -> List[RawJob]:
    """
    Fetch jobs from We Work Remotely's public RSS feeds (no key required).

    Args:
        categories: WWR category slugs to pull (defaults to backend/full-stack/devops)
        limit: Max jobs to return total

    Returns:
        List of RawJob objects
    """
    jobs: List[RawJob] = []
    seen_urls: set[str] = set()

    for slug in (categories or DEFAULT_CATEGORIES):
        try:
            url = WWR_BASE.format(slug=slug)
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            root = ET.fromstring(response.content)

            for item in root.findall(".//item"):
                link = (item.findtext("link") or "").strip()
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)

                raw_title = (item.findtext("title") or "").strip()
                company, role = _parse_title(raw_title)
                region = (item.findtext("region") or "").strip()
                description = clean_html(item.findtext("description") or "")

                job = RawJob(
                    external_id=f"wwr_{abs(hash(link)) % 10**12}",
                    source="weworkremotely",
                    title=role,
                    company=company,
                    location=region or "Remote",
                    is_remote=True,
                    job_url=link,
                    apply_url=link,
                    description=truncate_description(description) if description else None,
                    ats_type="other",
                )
                if job.title and job.company and job.job_url:
                    jobs.append(job)

                if len(jobs) >= limit:
                    break

        except requests.exceptions.ConnectionError:
            console.print(f"[yellow]WeWorkRemotely:[/yellow] Connection failed for {slug} — skipping")
        except requests.exceptions.Timeout:
            console.print(f"[yellow]WeWorkRemotely:[/yellow] Timeout for {slug} — skipping")
        except ET.ParseError as e:
            console.print(f"[yellow]WeWorkRemotely:[/yellow] Feed parse error for {slug}: {e}")
        except Exception as e:
            console.print(f"[red]WeWorkRemotely error ({slug}):[/red] {e}")

        if len(jobs) >= limit:
            break

    console.print(f"[cyan]WeWorkRemotely:[/cyan] Fetched {len(jobs)} jobs")
    return jobs[:limit]
