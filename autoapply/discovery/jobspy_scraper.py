"""
JobSpy scraper wrapper — scrapes LinkedIn, Indeed, Glassdoor.
Uses the python-jobspy library: pip install python-jobspy
Note: Scraping LinkedIn may violate their ToS. Use responsibly.
"""

import time
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description

console = Console()


def fetch_jobspy_jobs(
    search_terms: list[str],
    locations: list[str] | None = None,
    site_names: list[str] | None = None,
    results_wanted: int = 30,
    country_indeed: str = "India",
) -> List[RawJob]:
    """
    Scrape jobs from LinkedIn, Indeed, Glassdoor via JobSpy.

    Args:
        search_terms: Job title keywords
        locations: Location strings
        site_names: Which sites to scrape ["linkedin", "indeed", "glassdoor"]
        results_wanted: Results per search term per site
        country_indeed: Country for Indeed search

    Returns:
        List of RawJob objects
    """
    try:
        from jobspy import scrape_jobs
        import pandas as pd
    except ImportError:
        console.print("[yellow]JobSpy:[/yellow] Not installed — run: pip install python-jobspy")
        return []

    jobs: List[RawJob] = []
    seen_urls: set[str] = set()

    # Remove glassdoor — it blocks India IPs (HTTP 400)
    site_names = [s for s in (site_names or ["linkedin", "indeed"]) if s != "glassdoor"]
    locations_to_try = locations or ["Bangalore, India", "Remote"]

    for term in search_terms[:3]:  # Limit to avoid rate limits
        for location in locations_to_try[:1]:  # Only primary location to save time
            try:
                console.print(f"[dim]JobSpy: Searching '{term}' in '{location}'...[/dim]")

                scraped = scrape_jobs(
                    site_name=site_names,
                    search_term=term,
                    location=location,
                    results_wanted=results_wanted,
                    country_indeed=country_indeed,
                    hours_old=72,         # Jobs posted in last 3 days
                    is_remote=False,       # Include both remote and on-site
                    linkedin_fetch_description=True,
                )

                if scraped is None or len(scraped) == 0:
                    continue

                for _, row in scraped.iterrows():
                    job_url = str(row.get("job_url", "") or "")
                    if not job_url or job_url in seen_urls:
                        continue
                    seen_urls.add(job_url)

                    # Determine ATS type from URL
                    ats_type = "other"
                    if "linkedin.com" in job_url:
                        ats_type = "linkedin"
                    elif "greenhouse.io" in job_url:
                        ats_type = "greenhouse"
                    elif "lever.co" in job_url:
                        ats_type = "lever"
                    elif "workday.com" in job_url:
                        ats_type = "workday"
                    elif "indeed.com" in job_url:
                        ats_type = "indeed"

                    # Parse salary
                    salary_min = None
                    salary_max = None
                    try:
                        sal_min = row.get("min_amount")
                        sal_max = row.get("max_amount")
                        if pd.notna(sal_min):
                            salary_min = float(sal_min)
                        if pd.notna(sal_max):
                            salary_max = float(sal_max)
                    except Exception:
                        pass

                    is_remote = bool(row.get("is_remote", False))
                    site = str(row.get("site", "jobspy")).lower()

                    job = RawJob(
                        external_id=f"jobspy_{site}_{abs(hash(job_url)) % 10**10}",
                        source=f"jobspy_{site}",
                        title=str(row.get("title", "") or "").strip(),
                        company=str(row.get("company", "") or "").strip(),
                        location=str(row.get("location", "") or ""),
                        is_remote=is_remote,
                        job_url=job_url,
                        apply_url=str(row.get("job_url_direct", job_url) or job_url),
                        description=truncate_description(str(row.get("description", "") or "")),
                        salary_min=salary_min,
                        salary_max=salary_max,
                        salary_currency=str(row.get("currency", "INR") or "INR"),
                        ats_type=ats_type,
                    )

                    if job.title and job.company and job.job_url:
                        jobs.append(job)

                # Be polite between requests
                time.sleep(2)

            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "rate" in err_msg.lower():
                    console.print(f"[yellow]JobSpy: Rate limited — waiting 30s[/yellow]")
                    time.sleep(30)
                elif "glassdoor" in err_msg.lower() or "400" in err_msg:
                    pass  # Glassdoor blocks India IPs — silently skip
                else:
                    console.print(f"[yellow]JobSpy error ({term} / {location}):[/yellow] {e}")

    console.print(f"[cyan]JobSpy:[/cyan] Fetched {len(jobs)} jobs")
    return jobs
