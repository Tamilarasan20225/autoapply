#!/usr/bin/env python3
"""
AutoAppy — Automated Job Application System
Main entry point and pipeline orchestrator.

Usage:
  python run.py                    # Full pipeline run
  python run.py --dry-run          # Fill forms but don't submit
  python run.py --discover-only    # Only discover jobs, no scoring/apply
  python run.py --score-only       # Discover + score, no apply
  python run.py --apply-only       # Apply to already-scored jobs (skip discovery)
  python run.py --dashboard        # Launch Streamlit dashboard
  python run.py --schedule         # Run on schedule (daily at configured time)
"""

import sys
import json
import os
import argparse
from pathlib import Path
from datetime import datetime

import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ── Bootstrap ─────────────────────────────────────────────────────────────────
load_dotenv()
console = Console()

BANNER = """
[bold blue]
  ╔═══════════════════════════════════════╗
  ║         🤖  A U T O A P P Y         ║
  ║    Automated Job Application System   ║
  ╚═══════════════════════════════════════╝
[/bold blue]"""


def load_config(config_path: str = "config.yaml") -> dict:
    """Load and return config.yaml."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def load_master_resume(resume_path: str = "master_resume.json") -> dict:
    """Load master_resume.json."""
    with open(resume_path, "r") as f:
        return json.load(f)


def ensure_dirs(config: dict):
    """Create output directories if they don't exist."""
    paths = config.get("paths", {})
    for key in ["resumes_dir", "cover_letters_dir", "logs_dir"]:
        d = paths.get(key, f"outputs/{key.replace('_dir', 's')}")
        Path(d).mkdir(parents=True, exist_ok=True)
    Path(paths.get("database", "data/autoapply.db")).parent.mkdir(parents=True, exist_ok=True)


def get_blacklisted_companies(config: dict) -> set[str]:
    """Return normalized set of blacklisted company names from config."""
    raw = config.get("search", {}).get("exclude_companies", [])
    return {c.strip().lower() for c in raw if c.strip()}


# ── Pipeline Stages ───────────────────────────────────────────────────────────

def run_discovery(config: dict) -> list:
    """Stage 1: Discover jobs from all sources."""
    from autoapply.discovery import discover_jobs, populate_metadata_batch, resolve_aggregator_urls
    from autoapply.tracker.db import upsert_job, init_db
    from autoapply.utils.logger import log_discovery

    init_db(config.get("paths", {}).get("database", "data/autoapply.db"))

    console.print(Panel("[bold]Stage 1: Job Discovery[/bold]", style="blue"))
    raw_jobs = discover_jobs(config)

    # Apply company blacklist
    blacklist = get_blacklisted_companies(config)
    if blacklist:
        before = len(raw_jobs)
        raw_jobs = [j for j in raw_jobs if j.company.strip().lower() not in blacklist]
        removed = before - len(raw_jobs)
        if removed:
            console.print(f"[dim]Blacklist: removed {removed} jobs from excluded companies[/dim]")

    # Save to DB
    new_count = 0
    new_jobs = []
    for raw_job in raw_jobs:
        job_obj, is_new = upsert_job(raw_job.to_db_dict())
        if is_new:
            new_count += 1
            new_jobs.append(raw_job)

    log_discovery("all_sources", len(raw_jobs))
    console.print(
        f"[green]Discovery complete:[/green] {new_count} new jobs saved "
        f"({len(raw_jobs) - new_count} already known)\n"
    )

    # ── Auto-populate metadata for newly discovered jobs ──────────────────────
    discovery_cfg = config.get("discovery", {})
    if new_jobs and discovery_cfg.get("populate_metadata_after_discovery", True):
        try:
            updated = populate_metadata_batch(new_jobs)
            if updated:
                console.print(f"[dim]Metadata populated for {updated} new jobs[/dim]")
        except Exception as e:
            console.print(f"[dim]Metadata population error (non-fatal): {e}[/dim]")

    # ── Resolve aggregator redirect URLs for new jobs ─────────────────────────
    if discovery_cfg.get("resolve_urls_after_discovery", True):
        try:
            resolved = resolve_aggregator_urls(raw_jobs, config)
            if resolved:
                console.print(f"[dim]URL resolution: {resolved} aggregator URLs resolved[/dim]")
        except Exception as e:
            console.print(f"[dim]URL resolution error (non-fatal): {e}[/dim]")

    return raw_jobs


# ── Pre-filter keywords (applied after discovery, before LLM scoring) ────────
_PREFILTER_TECH_KEEP = [
    "engineer", "developer", "dev", "backend", "software", "python",
    "java", "data", "ml", "ai", "sde", "swe", "platform", "infrastructure",
    "devops", "site reliability", "sre", "fullstack", "full stack",
    "full-stack", "scientist", "research", "nlp", "machine learning",
    "deep learning", "llm", "api", "cloud", "security", "mobile",
    "android", "ios", "automation engineer", "architect", "analyst",
]
_PREFILTER_HARD_REJECT = [
    "account executive", "accountant", "accounting", "financial controller",
    "bdr", "sdr", "salesperson", "marketing manager", "marketing specialist",
    "social media", "content creator", "brand manager", "copywriter",
    "recruiter", "recruiting", "talent acquisition", "sourcing specialist",
    "human resources", "hr manager",
    "legal ", "lawyer", "attorney", "paralegal",
    "customer success", "customer support", "support jedi",
    "operations manager", "supply chain", "logistics manager", "procurement",
    "office manager", "executive assistant", "personal assistant",
    "calibration", "fahrzeugtechniker", "kfz",
    "strategischer einkäufer", "einkäufer",
    "ausbildung", "azubi", "praktikum", "werkstud",
    "gtm strategy", "scrum master", "agile coach",
    "investigator", "abuse investigator",
    "growth manager",
]


def run_prefilter(config: dict):
    """Pre-filter: mark clearly non-tech jobs as 'skipped' before LLM scoring.
    Saves LLM quota by rejecting irrelevant jobs using title-based rules only."""
    from autoapply.tracker.db import get_session
    from autoapply.tracker.models import Job

    # Also apply blacklist to already-discovered jobs in DB
    blacklist = get_blacklisted_companies(config)

    session = get_session()
    try:
        pending = session.query(Job).filter(Job.status == "discovered").all()
        skipped = 0
        for job in pending:
            title_lower = job.title.lower()

            # Blacklist check
            if blacklist and job.company.strip().lower() in blacklist:
                job.status = "skipped"
                skipped += 1
                continue

            if any(kw in title_lower for kw in _PREFILTER_HARD_REJECT):
                job.status = "skipped"
                skipped += 1
            elif not any(kw in title_lower for kw in _PREFILTER_TECH_KEEP):
                job.status = "skipped"
                skipped += 1
        session.commit()
        if skipped:
            console.print(f"[dim]Pre-filter: skipped {skipped} non-tech/blacklisted jobs[/dim]")
    finally:
        session.close()


def run_scoring(config: dict, master_resume: dict, llm_client) -> dict:
    """Stage 2: Score unscored jobs with LLM."""
    from autoapply.tracker.db import get_session, get_jobs_pending_scoring
    from autoapply.tracker.models import Job
    from autoapply.scoring.scorer import score_jobs_batch

    # Apply hard title-based pre-filter first (saves LLM calls)
    run_prefilter(config)

    console.print(Panel("[bold]Stage 2: LLM Scoring[/bold]", style="blue"))

    # Get unscored jobs from DB using improved helper
    jobs_to_score = get_jobs_pending_scoring(limit=200)

    if not jobs_to_score:
        console.print("[dim]No new jobs to score[/dim]\n")
        return {}

    console.print(f"[dim]Found {len(jobs_to_score)} unscored jobs[/dim]")

    if not llm_client.available:
        console.print("[red]No LLM providers configured — skipping scoring[/red]\n")
        return {}

    score_results = score_jobs_batch(
        jobs=jobs_to_score,
        master_resume=master_resume,
        llm_client=llm_client,
        config=config,
    )
    return score_results


def run_applications(config: dict, master_resume: dict, llm_client, score_results: dict, dry_run: bool = False):
    """Stage 3: Apply to jobs that scored above threshold."""
    from autoapply.tracker.db import get_session, update_job_status, get_jobs_ready_to_apply
    from autoapply.tracker.models import Job
    from autoapply.applicator import apply_to_job
    from autoapply.scoring.scorer import ScoreResult
    from autoapply.utils.logger import log_application

    console.print(Panel("[bold]Stage 3: Auto-Apply[/bold]", style="blue"))

    app_cfg = config.get("application", {})
    max_per_day = app_cfg.get("max_applications_per_day", 20)
    auto_apply_threshold = config.get("scoring", {}).get("auto_apply_threshold", 75)

    # Override dry_run from CLI
    if dry_run:
        config.setdefault("application", {})["dry_run"] = True

    # Get jobs ready to apply using DB helper (includes jobs from previous runs)
    apply_jobs = get_jobs_ready_to_apply(
        auto_apply_threshold=auto_apply_threshold,
        limit=max_per_day,
    )

    if not apply_jobs:
        console.print("[dim]No jobs meet auto-apply threshold[/dim]")

        # Show review-threshold jobs
        session = get_session()
        review_threshold = config.get("scoring", {}).get("review_threshold", 60)
        review_jobs = session.query(Job).filter(
            Job.status == "scored",
            Job.match_score >= review_threshold,
            Job.match_score < auto_apply_threshold,
        ).order_by(Job.match_score.desc()).limit(20).all()
        session.close()

        if review_jobs:
            console.print(f"\n[yellow]{len(review_jobs)} jobs flagged for manual review (score {review_threshold}-{auto_apply_threshold-1}):[/yellow]")
            table = Table(show_header=True, header_style="bold")
            table.add_column("Company", style="magenta")
            table.add_column("Title", style="cyan")
            table.add_column("Score")
            table.add_column("URL")
            for j in review_jobs[:10]:
                table.add_row(
                    j.company, j.title[:40],
                    f"{j.match_score:.0f}",
                    (j.job_url or "")[:60],
                )
            console.print(table)
        return

    console.print(f"[green]{len(apply_jobs)} jobs to apply (score ≥ {auto_apply_threshold})[/green]")

    applied = 0
    failed = 0

    for job in apply_jobs:
        # Get score result (from this run or reconstruct from DB)
        score_result = score_results.get(job.id)
        if not score_result:
            score_result = ScoreResult(
                score=job.match_score or 75,
                verdict="auto_apply",
                tailoring_variant=job.tailoring_variant or "balanced",
                summary_hint=job.score_reasoning or "",
                auto_apply_threshold=auto_apply_threshold,
            )

        success, method = apply_to_job(
            job=job,
            score_result=score_result,
            master_resume=master_resume,
            llm_client=llm_client,
            config=config,
        )

        log_application(
            job_id=job.id,
            company=job.company,
            title=job.title,
            method=method,
            success=success,
        )

        if success or method == "dry_run":
            applied += 1
        else:
            failed += 1

    console.print(f"\n[bold]Applications complete:[/bold] {applied} applied, {failed} failed/manual\n")


def run_pipeline(
    config: dict,
    discover_only: bool = False,
    score_only: bool = False,
    apply_only: bool = False,
    dry_run: bool = False,
):
    """Run the full AutoAppy pipeline."""
    from autoapply.scoring.llm_client import LLMClient
    from autoapply.tracker.db import start_run, finish_run
    from autoapply.utils.logger import log_pipeline_start, log_pipeline_end, get_logger

    # Initialize logger with configured log dir
    log_dir = config.get("paths", {}).get("logs_dir", "logs")
    get_logger(log_dir=log_dir)

    console.print(BANNER)
    console.print(f"[dim]Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")

    if dry_run:
        console.print("[bold yellow]🔄 DRY RUN MODE — forms will be filled but not submitted[/bold yellow]\n")
    if apply_only:
        console.print("[bold cyan]⚡ APPLY-ONLY MODE — skipping discovery and scoring[/bold cyan]\n")

    ensure_dirs(config)
    master_resume = load_master_resume(
        config.get("paths", {}).get("master_resume", "master_resume.json")
    )

    # Initialize LLM client
    llm_client = LLMClient(config=config)
    if llm_client.available:
        provider_names = [p["name"] for p in llm_client.providers]
        console.print(f"[green]LLM providers ready:[/green] {', '.join(provider_names)}\n")
    else:
        console.print("[yellow]Warning: No LLM API keys configured — scoring/generation disabled[/yellow]")
        console.print("[dim]Set GEMINI_API_KEY or GROQ_API_KEY in your .env file[/dim]\n")

    # Start run log
    run_id = start_run()
    log_pipeline_start(run_id, dry_run=dry_run)
    jobs_discovered = 0
    jobs_scored = 0
    jobs_applied = 0

    try:
        # ── apply-only: skip discovery + scoring ──────────────────────────────
        if apply_only:
            from autoapply.tracker.db import init_db
            init_db(config.get("paths", {}).get("database", "data/autoapply.db"))
            run_applications(config, master_resume, llm_client, score_results={}, dry_run=dry_run)
            finish_run(run_id, jobs_applied=jobs_applied, status="completed")
            return

        # Stage 1: Discovery
        raw_jobs = run_discovery(config)
        jobs_discovered = len(raw_jobs)

        if discover_only:
            console.print("[yellow]--discover-only: stopping after discovery[/yellow]")
            finish_run(run_id, jobs_discovered=jobs_discovered, status="completed")
            return

        # Stage 2: Scoring
        score_results = run_scoring(config, master_resume, llm_client)
        jobs_scored = len(score_results)

        if score_only:
            console.print("[yellow]--score-only: stopping after scoring[/yellow]")
            finish_run(run_id, jobs_discovered=jobs_discovered,
                       jobs_scored=jobs_scored, status="completed")
            return

        # Stage 3: Apply
        run_applications(config, master_resume, llm_client, score_results, dry_run=dry_run)
        jobs_applied = sum(
            1 for r in score_results.values() if r.verdict == "auto_apply"
        )

        finish_run(
            run_id,
            jobs_discovered=jobs_discovered,
            jobs_scored=jobs_scored,
            jobs_applied=jobs_applied,
            llm_calls_made=llm_client._call_count,
            status="completed",
        )

    except KeyboardInterrupt:
        console.print("\n[yellow]Pipeline interrupted by user[/yellow]")
        finish_run(run_id, status="interrupted", jobs_discovered=jobs_discovered)
    except Exception as e:
        console.print(f"\n[red]Pipeline error:[/red] {e}")
        import traceback
        traceback.print_exc()
        finish_run(run_id, status="failed", error_message=str(e),
                   jobs_discovered=jobs_discovered)

    log_pipeline_end(run_id, jobs_discovered, jobs_scored, jobs_applied, "completed")

    # ── Send daily digest notification ────────────────────────────────────────
    try:
        from autoapply.notifier.digest import send_daily_digest, collect_digest_data
        digest_data = collect_digest_data(
            db_path=config.get("paths", {}).get("database", "data/autoapply.db")
        )
        send_daily_digest(**digest_data)
    except Exception:
        pass  # Digest is non-critical

    # Final summary
    console.print(Panel(
        f"[bold green]Pipeline Complete[/bold green]\n"
        f"  Discovered: {jobs_discovered}\n"
        f"  Scored:     {jobs_scored}\n"
        f"  Applied:    {jobs_applied}\n"
        f"  LLM Calls:  {llm_client._call_count}\n\n"
        f"[dim]Run dashboard: streamlit run autoapply/dashboard/app.py[/dim]",
        title="AutoAppy Summary",
        style="green",
    ))


def run_scheduler(config: dict):
    """Run pipeline on a daily schedule."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        console.print("[red]APScheduler not installed. Run: pip install apscheduler[/red]")
        return

    sched_cfg = config.get("scheduler", {})
    run_time = sched_cfg.get("run_time", "09:00")
    timezone = sched_cfg.get("timezone", "Asia/Kolkata")
    hour, minute = run_time.split(":")

    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(
        lambda: run_pipeline(config),
        trigger=CronTrigger(hour=int(hour), minute=int(minute)),
        id="daily_pipeline",
        name="AutoAppy Daily Run",
    )

    console.print(f"[green]Scheduler started:[/green] Will run daily at {run_time} {timezone}")
    console.print("[dim]Press Ctrl+C to stop[/dim]")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped[/yellow]")


# ── CLI Entry Point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AutoAppy — Automated Job Application System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fill forms but don't submit applications",
    )
    parser.add_argument(
        "--discover-only", action="store_true",
        help="Only discover jobs, skip scoring and applying",
    )
    parser.add_argument(
        "--score-only", action="store_true",
        help="Discover and score, skip applying",
    )
    parser.add_argument(
        "--apply-only", action="store_true",
        help="Apply to already-scored jobs — skip discovery and scoring entirely",
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Launch the Streamlit dashboard",
    )
    parser.add_argument(
        "--schedule", action="store_true",
        help="Run on configured daily schedule",
    )
    parser.add_argument(
        "--resolve-urls", action="store_true",
        help="Resolve aggregator redirect URLs for all unresolved jobs in DB",
    )
    parser.add_argument(
        "--populate-metadata", action="store_true",
        help="Back-fill seniority/employment_type/skills for all existing jobs in DB",
    )
    parser.add_argument(
        "--rescore", action="store_true",
        help="Re-score all 'scored' jobs using updated scoring pipeline",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Pull latest jobs from all sources and score only the newest unscored ones (latest-first priority)",
    )

    args = parser.parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        console.print(f"[red]Config not found:[/red] {args.config}")
        console.print("Run from the AutoAppy project directory")
        sys.exit(1)

    if args.dashboard:
        import subprocess
        subprocess.run([
            sys.executable, "-m", "streamlit", "run",
            "autoapply/dashboard/app.py",
            "--server.port", "8501",
        ])
        return

    if args.schedule:
        run_scheduler(config)
        return

    # ── Standalone utility commands ───────────────────────────────────────────
    if args.resolve_urls:
        from autoapply.tracker.db import init_db
        from autoapply.discovery import resolve_aggregator_urls
        init_db(config.get("paths", {}).get("database", "data/autoapply.db"))
        console.print("[bold]Resolving aggregator redirect URLs...[/bold]")
        resolved = resolve_aggregator_urls([], config)
        console.print(f"[green]Resolved {resolved} URLs[/green]")
        return

    if args.populate_metadata:
        from autoapply.tracker.db import init_db
        from autoapply.discovery import backfill_metadata_from_db
        init_db(config.get("paths", {}).get("database", "data/autoapply.db"))
        console.print("[bold]Back-filling metadata for all jobs...[/bold]")
        updated = backfill_metadata_from_db(limit=5000)
        console.print(f"[green]Updated metadata for {updated} jobs[/green]")
        return

    if args.rescore:
        from autoapply.tracker.db import init_db, get_session
        from autoapply.tracker.models import Job
        import json
        init_db(config.get("paths", {}).get("database", "data/autoapply.db"))
        master_resume = load_master_resume(
            config.get("paths", {}).get("master_resume", "master_resume.json")
        )
        from autoapply.scoring.llm_client import LLMClient
        llm_client = LLMClient(config=config)
        # Reset scored jobs back to discovered so they get re-scored
        session = get_session()
        try:
            jobs = session.query(Job).filter(Job.status == "scored").all()
            for j in jobs:
                j.status = "discovered"
            session.commit()
            console.print(f"[yellow]Reset {len(jobs)} jobs to 'discovered' for re-scoring[/yellow]")
        finally:
            session.close()
        score_results = run_scoring(config, master_resume, llm_client)
        console.print(f"[green]Re-scored {len(score_results)} jobs[/green]")
        return

    if args.refresh:
        from autoapply.tracker.db import init_db, get_latest_unscored_jobs, get_latest_jobs
        from autoapply.scoring.llm_client import LLMClient
        from autoapply.scoring.scorer import score_jobs_batch

        init_db(config.get("paths", {}).get("database", "data/autoapply.db"))
        ensure_dirs(config)
        master_resume = load_master_resume(
            config.get("paths", {}).get("master_resume", "master_resume.json")
        )
        llm_client = LLMClient(config=config)

        console.print(BANNER)
        console.print(Panel(
            "[bold cyan]⚡ REFRESH MODE — Pull latest jobs + score newest first[/bold cyan]",
            style="cyan"
        ))

        # Step 1: Discover fresh jobs from all sources
        console.print(Panel("[bold]Step 1: Discovering Latest Jobs[/bold]", style="blue"))
        raw_jobs = run_discovery(config)
        console.print(f"[green]Discovered {len(raw_jobs)} jobs from all sources[/green]\n")

        # Step 2: Pre-filter non-tech jobs
        run_prefilter(config)

        # Step 3: Score only the freshest unscored jobs (last 48h), newest first
        console.print(Panel("[bold]Step 2: Scoring Latest Unscored Jobs (newest first)[/bold]", style="blue"))
        fresh_jobs = get_latest_unscored_jobs(hours=48, limit=200)

        if not fresh_jobs:
            console.print("[dim]No new unscored jobs in the last 48 hours. All caught up![/dim]")
        else:
            console.print(f"[cyan]Found {len(fresh_jobs)} fresh unscored jobs to score (ordered newest → oldest)[/cyan]")
            if llm_client.available:
                score_results = score_jobs_batch(
                    jobs=fresh_jobs,
                    master_resume=master_resume,
                    llm_client=llm_client,
                    config=config,
                )
                auto = sum(1 for r in score_results.values() if r.verdict == "auto_apply")
                review = sum(1 for r in score_results.values() if r.verdict == "review")
                console.print(f"\n[bold green]Refresh scoring complete:[/bold green] {auto} auto-apply, {review} review")
            else:
                console.print("[yellow]No LLM configured — skipping scoring[/yellow]")

        # Step 4: Show latest scored jobs summary
        latest_all = get_latest_jobs(hours=48, limit=20)
        if latest_all:
            from rich.table import Table as RichTable
            console.print(Panel("[bold]Latest 20 Jobs (last 48h, newest first)[/bold]", style="green"))
            tbl = RichTable(show_header=True, header_style="bold green")
            tbl.add_column("Discovered", style="dim", width=18)
            tbl.add_column("Score", width=7)
            tbl.add_column("Status", width=12)
            tbl.add_column("Company", style="magenta", width=22)
            tbl.add_column("Title", style="cyan", width=40)
            for j in latest_all:
                disc = j.discovered_at.strftime("%b %d %H:%M") if j.discovered_at else "—"
                score = f"{j.match_score:.0f}" if j.match_score is not None else "—"
                tbl.add_row(disc, score, j.status or "—", j.company or "—", (j.title or "—")[:40])
            console.print(tbl)
        return

    # Full pipeline
    run_pipeline(
        config=config,
        discover_only=args.discover_only,
        score_only=args.score_only,
        apply_only=args.apply_only,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
