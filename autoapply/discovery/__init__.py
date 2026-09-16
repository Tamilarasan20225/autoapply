"""
Job discovery engine — aggregates jobs from all configured sources concurrently,
deduplicates them, and saves to the database.

Sprint 2 improvement: All sources now run in parallel using ThreadPoolExecutor
for dramatically faster discovery (was sequential, now ~5x faster).
"""

import time
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.panel import Panel

from autoapply.discovery.base import RawJob
from autoapply.discovery.deduplicator import deduplicate_jobs, filter_by_keywords

console = Console()


def _safe_fetch(name: str, fn, *args, **kwargs) -> tuple[str, List[RawJob], str | None]:
    """
    Wrapper to run a discovery function safely.
    Returns (source_name, jobs, error_or_None).
    """
    try:
        start = time.monotonic()
        jobs = fn(*args, **kwargs)
        elapsed = int((time.monotonic() - start) * 1000)
        console.print(f"  [green]✓[/green] [cyan]{name}[/cyan]: {len(jobs)} jobs ({elapsed}ms)")
        return name, jobs, None
    except Exception as e:
        console.print(f"  [red]✗[/red] [cyan]{name}[/cyan]: {type(e).__name__}: {e}")
        return name, [], str(e)


def populate_metadata_batch(raw_jobs: List[RawJob]) -> int:
    """
    Populate metadata (seniority, employment_type, skills_required) for a batch of RawJobs.
    Updates the DB records for each job after calling populate_metadata().
    Returns count of jobs updated in DB.
    """
    if not raw_jobs:
        return 0
    try:
        from autoapply.scoring.skill_extractor import SkillExtractor
        from autoapply.tracker.db import get_session
        from autoapply.tracker.models import Job
        import json

        skill_extractor = None
        try:
            skill_extractor = SkillExtractor()
            if not skill_extractor._available:
                skill_extractor = None
        except Exception:
            pass

        updated = 0
        session = get_session()
        try:
            for raw_job in raw_jobs:
                raw_job.populate_metadata(skill_extractor=skill_extractor)
                ext_id = raw_job.external_id or raw_job._generate_id()
                db_job = session.query(Job).filter_by(external_id=ext_id).first()
                if not db_job:
                    db_job = session.query(Job).filter_by(job_url=raw_job.job_url).first()
                if db_job:
                    if raw_job.seniority and not db_job.seniority_level:
                        db_job.seniority_level = raw_job.seniority
                    if raw_job.employment_type and not db_job.employment_type:
                        db_job.employment_type = raw_job.employment_type
                    if raw_job.skills_required and not db_job.skills_required:
                        db_job.skills_required = json.dumps(raw_job.skills_required)
                    updated += 1
            session.commit()
        finally:
            session.close()
        console.print(f"  [dim]Metadata: populated seniority/type/skills for {updated} jobs[/dim]")
        return updated
    except Exception as e:
        console.print(f"[dim]populate_metadata_batch error (non-fatal): {e}[/dim]")
        return 0


def backfill_metadata_from_db(limit: int = 2000) -> int:
    """
    Back-fill metadata for existing jobs in DB that are missing seniority/employment_type/skills.
    Returns count of jobs updated.
    """
    try:
        from autoapply.tracker.db import get_session
        from autoapply.tracker.models import Job
        from autoapply.scoring.skill_extractor import SkillExtractor
        import json

        skill_extractor = None
        try:
            skill_extractor = SkillExtractor()
            if not skill_extractor._available:
                skill_extractor = None
        except Exception:
            pass

        session = get_session()
        try:
            jobs_to_update = session.query(Job).filter(
                (Job.seniority_level.is_(None)) | (Job.employment_type.is_(None))
            ).limit(limit).all()

            updated = 0
            for db_job in jobs_to_update:
                stub = RawJob(
                    title=db_job.title or "",
                    company=db_job.company or "",
                    job_url=db_job.job_url or "",
                    source=db_job.source or "",
                    description=db_job.description,
                    employment_type=db_job.employment_type,
                    seniority=db_job.seniority_level,
                )
                stub.populate_metadata(skill_extractor=skill_extractor)
                changed = False
                if stub.seniority and not db_job.seniority_level:
                    db_job.seniority_level = stub.seniority
                    changed = True
                if stub.employment_type and not db_job.employment_type:
                    db_job.employment_type = stub.employment_type
                    changed = True
                if stub.skills_required and not db_job.skills_required:
                    db_job.skills_required = json.dumps(stub.skills_required)
                    changed = True
                if changed:
                    updated += 1
            session.commit()
            console.print(f"[green]Metadata backfill:[/green] Updated {updated}/{len(jobs_to_update)} jobs")
            return updated
        finally:
            session.close()
    except Exception as e:
        console.print(f"[red]Metadata backfill error:[/red] {e}")
        return 0


def discover_jobs(config: dict) -> List[RawJob]:
    """
    Run all enabled job discovery sources concurrently and return deduplicated jobs.

    Args:
        config: Parsed config.yaml dict

    Returns:
        List of deduplicated RawJob objects, pre-filtered by relevance
    """
    sources_cfg = config.get("sources", {})
    search_cfg = config.get("search", {})
    roles = search_cfg.get("roles", ["Software Engineer"])
    locations = search_cfg.get("locations", ["Bangalore", "Remote"])
    keywords = search_cfg.get("strong_keywords", [])
    include_remote = search_cfg.get("include_remote", True)

    # Build list of (name, fn, args, kwargs) tasks to run concurrently
    tasks = []

    # ── Remotive ──────────────────────────────────────────────────────────────
    if sources_cfg.get("remotive", {}).get("enabled", True):
        from autoapply.discovery.remotive import fetch_remotive_jobs
        category = sources_cfg.get("remotive", {}).get("category", "software-dev")
        tasks.append(("Remotive", fetch_remotive_jobs, [], {
            "category": category,
            "search_terms": roles[:3],
        }))

    # ── Arbeitnow ─────────────────────────────────────────────────────────────
    if sources_cfg.get("arbeitnow", {}).get("enabled", False):
        from autoapply.discovery.arbeitnow import fetch_arbeitnow_jobs
        tasks.append(("Arbeitnow", fetch_arbeitnow_jobs, [], {"limit": 100}))

    # ── RemoteOK ──────────────────────────────────────────────────────────────
    if sources_cfg.get("remoteok", {}).get("enabled", True):
        from autoapply.discovery.remoteok import fetch_remoteok_jobs
        tags = sources_cfg.get("remoteok", {}).get("tags", ["backend", "python"])
        tasks.append(("RemoteOK", fetch_remoteok_jobs, [], {
            "tags": tags,
            "limit": 60,
        }))

    # ── Adzuna ────────────────────────────────────────────────────────────────
    if sources_cfg.get("adzuna", {}).get("enabled", True):
        from autoapply.discovery.adzuna import fetch_adzuna_jobs
        adzuna_cfg = sources_cfg.get("adzuna", {})
        tasks.append(("Adzuna", fetch_adzuna_jobs, [], {
            "roles": roles,
            "locations": locations,
            "country": adzuna_cfg.get("country", "in"),
            "max_results": adzuna_cfg.get("max_results", 50),
        }))

    # ── Greenhouse (direct ATS API) ───────────────────────────────────────────
    gh_cfg = sources_cfg.get("greenhouse_targets", {})
    if gh_cfg.get("enabled", True):
        companies = gh_cfg.get("companies", [])
        if companies:
            from autoapply.discovery.ats_direct import fetch_greenhouse_jobs
            tasks.append(("Greenhouse", fetch_greenhouse_jobs, [companies], {}))

    # ── Lever (direct ATS API) ────────────────────────────────────────────────
    lv_cfg = sources_cfg.get("lever_targets", {})
    if lv_cfg.get("enabled", True):
        companies = lv_cfg.get("companies", [])
        if companies:
            from autoapply.discovery.ats_direct import fetch_lever_jobs
            tasks.append(("Lever", fetch_lever_jobs, [companies], {}))

    # ── Ashby (direct ATS API) ────────────────────────────────────────────────
    ashby_cfg = sources_cfg.get("ashby_targets", {})
    if ashby_cfg.get("enabled", False):
        companies = ashby_cfg.get("companies", [])
        if companies:
            from autoapply.discovery.ashby import fetch_ashby_jobs
            tasks.append(("Ashby", fetch_ashby_jobs, [companies], {}))

    # ── JobSpy (LinkedIn, Indeed scraper) — Bangalore priority ────────────────
    if sources_cfg.get("jobspy", {}).get("enabled", True):
        from autoapply.discovery.jobspy_scraper import fetch_jobspy_jobs
        jspy_cfg = sources_cfg.get("jobspy", {})
        site_names_cfg = [
            s for s in jspy_cfg.get("site_names", ["linkedin", "indeed"])
            if s != "glassdoor"  # blocks India IPs
        ]
        tasks.append(("JobSpy", fetch_jobspy_jobs, [], {
            "search_terms": roles[:4],
            "locations": ["Bangalore, India"],
            "site_names": site_names_cfg,
            "results_wanted": jspy_cfg.get("results_wanted", 50),
            "country_indeed": jspy_cfg.get("country_indeed", "India"),
        }))

    # ── Google SERP Discovery (Serper API) ────────────────────────────────────
    import os as _os
    serper_key = _os.environ.get("SERPER_API_KEY", "").strip()
    if sources_cfg.get("serper_discovery", {}).get("enabled", True) and serper_key:
        from autoapply.discovery.serper_discovery import fetch_serper_jobs
        tasks.append(("SerperSERP", fetch_serper_jobs, [], {
            "roles": roles, "locations": locations, "serper_key": serper_key, "config": config,
        }))

    # ── SmartRecruiters targets ────────────────────────────────────────────────
    sr_cfg = sources_cfg.get("smartrecruiters_targets", {})
    if sr_cfg.get("enabled", True):
        sr_companies = sr_cfg.get("companies", [])
        if sr_companies:
            from autoapply.discovery.smartrecruiters_discovery import fetch_smartrecruiters_jobs
            tasks.append(("SmartRecruiters", fetch_smartrecruiters_jobs, [], {
                "company_ids": sr_companies, "roles_filter": roles, "config": config,
            }))

    # ── Career Page Prober (auto-probe any company's ATS) ─────────────────────
    probe_cfg = sources_cfg.get("career_page_prober", {})
    if probe_cfg.get("enabled", True):
        probe_companies = probe_cfg.get("companies", [])
        if probe_companies:
            from autoapply.discovery.career_page_prober import probe_companies_batch
            tasks.append(("CareerProber", probe_companies_batch, [], {
                "company_names": probe_companies, "roles_filter": roles,
            }))

    # ── Wellfound (startup jobs) ───────────────────────────────────────────────
    if sources_cfg.get("wellfound", {}).get("enabled", False):
        from autoapply.discovery.wellfound_scraper import fetch_wellfound_jobs
        tasks.append(("Wellfound", fetch_wellfound_jobs, [], {
            "roles": roles, "locations": locations, "config": config,
        }))

    # ── Cutshort (India tech/startup jobs) ────────────────────────────────────
    if sources_cfg.get("cutshort", {}).get("enabled", False):
        from autoapply.discovery.cutshort_scraper import fetch_cutshort_jobs
        cutshort_limit = sources_cfg.get("cutshort", {}).get("limit", 100)
        tasks.append(("Cutshort", fetch_cutshort_jobs, [], {
            "roles": roles, "locations": locations, "config": config, "limit": cutshort_limit,
        }))

    # ── Run all sources concurrently ──────────────────────────────────────────
    console.print(Panel(
        f"[bold]Stage 1: Job Discovery[/bold]\n[dim]Running {len(tasks)} sources in parallel...[/dim]",
        style="blue"
    ) if len(tasks) > 1 else Panel("[bold]Stage 1: Job Discovery[/bold]", style="blue"))

    all_jobs: List[RawJob] = []

    # Use up to 12 threads — most tasks are I/O bound (HTTP requests)
    max_workers = min(len(tasks), 12)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_name = {}
        for name, fn, args, kwargs in tasks:
            future = executor.submit(_safe_fetch, name, fn, *args, **kwargs)
            future_to_name[future] = name

        for future in as_completed(future_to_name):
            _, jobs, error = future.result()
            all_jobs.extend(jobs)

    console.print(f"\n[bold]Discovery total:[/bold] {len(all_jobs)} raw jobs from all sources")

    # ── Deduplication ─────────────────────────────────────────────────────────
    deduped = deduplicate_jobs(all_jobs)
    console.print(f"[bold]After dedup:[/bold] {len(deduped)} unique jobs")

    # ── Keyword pre-filter ────────────────────────────────────────────────────
    filtered = filter_by_keywords(deduped, keywords)
    console.print(f"[bold]After keyword filter:[/bold] {len(filtered)} jobs to score\n")

    return filtered


def resolve_aggregator_urls(raw_jobs: list, config: dict) -> int:
    """
    Post-discovery: resolve aggregator redirect URLs for all newly discovered jobs.
    Called after upsert_job() so we can query the DB for unresolved jobs.
    Returns number of URLs resolved.
    """
    try:
        from autoapply.discovery.url_resolver_async import get_jobs_pending_url_resolution, resolve_jobs_urls_batch
        pending = get_jobs_pending_url_resolution(limit=100)
        if pending:
            return resolve_jobs_urls_batch(pending, max_workers=6)
    except Exception as e:
        console.print(f"[dim]URL resolution step error (non-fatal): {e}[/dim]")
    return 0
