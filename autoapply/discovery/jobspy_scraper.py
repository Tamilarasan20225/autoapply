"""
JobSpy scraper wrapper - scrapes LinkedIn, Indeed, Naukri, and more.
Uses the python-jobspy library: pip install python-jobspy

Supported sites: linkedin, indeed, naukri, glassdoor, google, zip_recruiter
Naukri is India #1 job board - added as primary source for India jobs.
Note: Scraping may violate some sites ToS. Use responsibly.
"""

import time
from typing import List
from rich.console import Console

from autoapply.discovery.base import RawJob, truncate_description

console = Console()


def _handle_jobspy_error(e, term, location):
    err_msg = str(e)
    if "429" in err_msg or "rate" in err_msg.lower():
        console.print(f"[yellow]JobSpy: Rate limited on '{term}' - waiting 30s[/yellow]")
        time.sleep(30)
    elif "glassdoor" in err_msg.lower() or "400" in err_msg:
        pass
    elif "naukri" in err_msg.lower():
        console.print(f"[dim]JobSpy Naukri: {err_msg[:80]}[/dim]")
    else:
        console.print(f"[yellow]JobSpy error ({term} / {location}):[/yellow] {err_msg[:100]}")


def _process_scraped(scraped, jobs, seen_urls, source_prefix=""):
    try:
        import pandas as pd
    except ImportError:
        return
    if scraped is None or len(scraped) == 0:
        return
    for _, row in scraped.iterrows():
        try:
            job_url = str(row.get("job_url", "") or "").strip()
            if not job_url or job_url in seen_urls:
                continue
            seen_urls.add(job_url)
            title = str(row.get("title", "") or "").strip()
            company = str(row.get("company", "") or "").strip()
            if not title or not company:
                continue
            ats_type = "other"
            if "linkedin.com" in job_url: ats_type = "linkedin"
            elif "greenhouse.io" in job_url: ats_type = "greenhouse"
            elif "lever.co" in job_url: ats_type = "lever"
            elif "workday.com" in job_url: ats_type = "workday"
            elif "indeed.com" in job_url: ats_type = "indeed"
            elif "naukri.com" in job_url: ats_type = "naukri"
            elif "ashbyhq.com" in job_url: ats_type = "ashby"
            salary_min = salary_max = None
            try:
                sal_min = row.get("min_amount")
                sal_max = row.get("max_amount")
                if pd.notna(sal_min): salary_min = float(sal_min)
                if pd.notna(sal_max): salary_max = float(sal_max)
            except Exception:
                pass
            posted_at = None
            try:
                date_val = row.get("date_posted")
                if date_val and pd.notna(date_val):
                    posted_at = str(date_val)[:10]
            except Exception:
                pass
            site = source_prefix or str(row.get("site", "jobspy")).lower()
            is_remote = bool(row.get("is_remote", False))
            job = RawJob(
                external_id=f"jobspy_{site}_{abs(hash(job_url)) % 10**10}",
                source=f"jobspy_{site}",
                title=title,
                company=company,
                location=str(row.get("location", "") or ""),
                is_remote=is_remote,
                job_url=job_url,
                apply_url=str(row.get("job_url_direct", job_url) or job_url),
                description=truncate_description(str(row.get("description", "") or "")),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_currency=str(row.get("currency", "INR") or "INR"),
                ats_type=ats_type,
                posted_at=posted_at,
            )
            if job.title and job.company and job.job_url:
                jobs.append(job)
        except Exception:
            continue


def fetch_jobspy_jobs(
    search_terms,
    locations=None,
    site_names=None,
    results_wanted=50,
    country_indeed="India",
    hours_old=72,
):
    try:
        from jobspy import scrape_jobs
    except ImportError:
        console.print("[yellow]JobSpy:[/yellow] Not installed - run: pip install python-jobspy")
        return []

    jobs = []
    seen_urls = set()
    default_sites = ["linkedin", "indeed", "naukri"]
    site_names = [s for s in (site_names or default_sites) if s != "glassdoor"]
    locations_to_try = locations or ["Bangalore, India"]
    naukri_only_sites = [s for s in site_names if s == "naukri"]
    non_naukri_sites = [s for s in site_names if s != "naukri"]

    for term in search_terms[:4]:
        for location in locations_to_try[:1]:
            if non_naukri_sites:
                try:
                    console.print(f"[dim]JobSpy ({','.join(non_naukri_sites)}): Searching '{term}' in '{location}'...[/dim]")
                    scraped = scrape_jobs(
                        site_name=non_naukri_sites,
                        search_term=term,
                        location=location,
                        results_wanted=results_wanted,
                        country_indeed=country_indeed,
                        hours_old=hours_old,
                        is_remote=False,
                        linkedin_fetch_description=True,
                    )
                    _process_scraped(scraped, jobs, seen_urls)
                    time.sleep(2)
                except Exception as e:
                    _handle_jobspy_error(e, term, location)

            if naukri_only_sites:
                try:
                    console.print(f"[dim]JobSpy (naukri): Searching '{term}' India...[/dim]")
                    scraped_naukri = scrape_jobs(
                        site_name=["naukri"],
                        search_term=term,
                        location="India",
                        results_wanted=results_wanted,
                        country_indeed="India",
                        hours_old=hours_old * 2,
                    )
                    _process_scraped(scraped_naukri, jobs, seen_urls, source_prefix="naukri")
                    time.sleep(2)
                except Exception as e:
                    _handle_jobspy_error(e, term, "Naukri-India")

    console.print(f"[cyan]JobSpy:[/cyan] Fetched {len(jobs)} jobs")
    return jobs
