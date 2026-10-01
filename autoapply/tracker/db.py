"""
Database operations for AutoAppy.
Provides session management and CRUD helpers.

Fixes:
- SQLAlchemy 2.x: replaced deprecated .get() with session.get()
- Thread-safety: uses scoped_session for concurrent access
- UTC-aware timestamps throughout
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List

from sqlalchemy import create_engine, and_
from sqlalchemy.orm import sessionmaker, scoped_session, Session

from autoapply.tracker.models import Base, Job, RunLog, Resume, JobScore


_engine = None
_session_factory = None
_ScopedSession = None


def _utcnow() -> datetime:
    """Return current UTC datetime (timezone-naive for SQLite compat)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _migrate_db(engine) -> None:
    """
    Apply schema migrations for new columns added after initial DB creation.
    SQLAlchemy's create_all() only creates missing tables — it doesn't add
    new columns to existing tables. This function handles that gap.

    Safe to run repeatedly — uses ALTER TABLE IF NOT EXISTS pattern via
    sqlite_master inspection.
    """
    new_columns = [
        # (table, column_name, column_type_sql)
        ("jobs", "interview_stage",         "VARCHAR(64)"),
        ("jobs", "interview_date",           "DATETIME"),
        ("jobs", "offer_amount",             "TEXT"),
        ("jobs", "screenshot_path",          "TEXT"),
        # Phase 2 columns
        ("jobs", "ats_detected_at_apply",   "TEXT"),
        ("jobs", "apply_attempts",           "INTEGER DEFAULT 0"),
        ("jobs", "last_attempt_error",       "TEXT"),
        ("jobs", "session_key",             "TEXT"),
        ("jobs", "company_portal_url",      "TEXT"),
        # Phase 3: two-stage scoring + extended metadata
        ("jobs", "tfidf_score",             "FLOAT"),
        ("jobs", "skill_match_score",       "FLOAT"),
        ("jobs", "posted_at",               "VARCHAR(32)"),
        ("jobs", "employment_type",         "VARCHAR(32)"),
        ("jobs", "department",              "VARCHAR(64)"),
        ("jobs", "seniority_level",         "VARCHAR(32)"),
        ("jobs", "skills_required",         "TEXT"),
        ("jobs", "redirect_resolved",       "BOOLEAN DEFAULT 0"),
        ("jobs", "source_query",            "VARCHAR(256)"),
        # Phase 4: scoring v2 audit trail
        ("jobs", "score_version",           "INTEGER"),
        ("jobs", "raw_llm_score",           "FLOAT"),
        ("jobs", "verdict",                 "VARCHAR(32)"),
        ("jobs", "score_model",             "VARCHAR(64)"),
        ("jobs", "score_prompt_version",    "VARCHAR(16)"),
        ("jobs", "score_breakdown",         "TEXT"),
        ("jobs", "scored_at",               "DATETIME"),
        ("jobs", "hard_gate_failures",      "TEXT"),
        ("jobs", "jd_required_years",       "FLOAT"),
        ("jobs", "jd_geo_scope",            "VARCHAR(32)"),
        ("jobs", "description_quality",     "VARCHAR(16)"),
        # Resume profile columns
        ("resumes", "schedule_enabled",       "BOOLEAN DEFAULT 1"),
        ("resumes", "domain",                "VARCHAR(64)"),
        ("resumes", "years_experience",      "FLOAT"),
        ("resumes", "auto_apply_threshold",  "FLOAT"),
        ("resumes", "review_threshold",      "FLOAT"),
        ("resumes", "tfidf_threshold",       "FLOAT"),
        # JobScore extended columns
        ("job_scores", "verdict",                      "VARCHAR(32)"),
        ("job_scores", "adjusted_score",               "FLOAT"),
        ("job_scores", "recency_bonus_applied",        "FLOAT"),
        ("job_scores", "seniority_multiplier_applied", "FLOAT"),
        ("job_scores", "score_version",                "INTEGER"),
        ("job_scores", "raw_llm_score",                "FLOAT"),
        ("job_scores", "score_model",                  "VARCHAR(64)"),
        ("job_scores", "score_prompt_version",         "VARCHAR(16)"),
        ("job_scores", "score_breakdown",              "TEXT"),
        ("job_scores", "scored_at",                    "DATETIME"),
        ("job_scores", "hard_gate_failures",           "TEXT"),
    ]

    from sqlalchemy import text as _sql

    failures = []
    with engine.connect() as conn:
        for table, col, col_type in new_columns:
            try:
                existing_cols = {
                    row[1] for row in conn.execute(_sql(f"PRAGMA table_info({table})"))
                }
                if not existing_cols:
                    continue  # table not created yet — create_all will handle it
                if col not in existing_cols:
                    conn.execute(_sql(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                    conn.commit()
            except Exception as e:
                failures.append(f"{table}.{col}: {e}")

        # Freeze every pre-v2 score: existing rows keep their v1 numbers forever.
        try:
            conn.execute(
                _sql(
                    "UPDATE jobs SET score_version = 1 "
                    "WHERE score_version IS NULL AND match_score IS NOT NULL"
                )
            )
            conn.commit()
        except Exception as e:
            failures.append(f"jobs.score_version backfill: {e}")

    if failures:
        import sys
        print("[db migration] FAILED:\n  " + "\n  ".join(failures), file=sys.stderr)


def init_db(db_path: str = "data/autoapply.db") -> None:
    """Initialize database, run migrations, create tables. Thread-safe via scoped_session."""
    global _engine, _session_factory, _ScopedSession
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},  # Allow cross-thread SQLite use
    )
    Base.metadata.create_all(_engine)
    _migrate_db(_engine)  # Add any new columns to existing tables
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    _ScopedSession = scoped_session(_session_factory)


def get_session() -> Session:
    """Get a thread-local scoped session."""
    if _ScopedSession is None:
        init_db()
    return _ScopedSession()


def close_session():
    """Remove the scoped session for the current thread."""
    if _ScopedSession is not None:
        _ScopedSession.remove()


def upsert_job(job_data: dict) -> tuple[Job, bool]:
    """
    Insert or update a job by external_id+source.
    Returns (job, is_new).
    """
    session = get_session()
    try:
        external_id = job_data.get("external_id")
        source = job_data.get("source", "unknown")

        existing = None
        if external_id:
            existing = session.query(Job).filter_by(
                external_id=external_id, source=source
            ).first()

        if not existing:
            # Also check by URL to avoid dupes from different sources
            url = job_data.get("job_url", "")
            if url:
                existing = session.query(Job).filter_by(job_url=url).first()

        if existing:
            # Update all discoverable fields without overwriting status/score
            refreshable = [
                "title", "company", "location", "description", "apply_url",
                "posted_at", "employment_type", "seniority_level", "is_remote",
                "skills_required", "ats_type", "ats_job_id", "ats_company_slug",
            ]
            for field in refreshable:
                val = job_data.get(field)
                if val is not None and val != "":
                    setattr(existing, field, val)
            existing.updated_at = _utcnow()
            session.commit()
            return existing, False

        # New job
        allowed_fields = {c.key for c in Job.__table__.columns}
        filtered_data = {k: v for k, v in job_data.items() if k in allowed_fields}
        job = Job(**filtered_data)
        session.add(job)
        session.commit()
        return job, True
    except Exception:
        session.rollback()
        raise


def update_job_score(job_id: int, score: float, reasoning: str,
                     skill_gaps: list, tailoring_variant: str,
                     red_flags: list,
                     tfidf_score: float = 0.0,
                     skill_match_score: float = 0.0,
                     *,
                     status: str = "scored",
                     score_version: int = 2,
                     raw_llm_score: Optional[float] = None,
                     verdict: Optional[str] = None,
                     score_model: Optional[str] = None,
                     score_prompt_version: Optional[str] = None,
                     score_breakdown: Optional[dict] = None,
                     hard_gate_failures: Optional[list] = None,
                     jd_required_years: Optional[float] = None,
                     jd_geo_scope: Optional[str] = None,
                     description_quality: Optional[str] = None) -> None:
    """Save scoring results plus the full v2 audit trail to a job record."""
    session = get_session()
    try:
        job = session.get(Job, job_id)
        if job:
            job.match_score = score
            job.score_reasoning = reasoning
            job.skill_gaps = json.dumps(skill_gaps)
            job.tailoring_variant = tailoring_variant
            job.red_flags = json.dumps(red_flags)
            job.tfidf_score = tfidf_score
            job.skill_match_score = skill_match_score
            job.status = status
            job.score_version = score_version
            job.raw_llm_score = raw_llm_score
            job.verdict = verdict
            job.score_model = score_model
            job.score_prompt_version = score_prompt_version
            job.score_breakdown = json.dumps(score_breakdown) if score_breakdown else None
            job.hard_gate_failures = json.dumps(hard_gate_failures) if hard_gate_failures else None
            job.jd_required_years = jd_required_years
            job.jd_geo_scope = jd_geo_scope
            job.description_quality = description_quality
            job.scored_at = _utcnow()
            job.updated_at = _utcnow()
            session.commit()
    except Exception:
        session.rollback()
        raise


def update_job_status(job_id: int, status: str, **kwargs) -> None:
    """Update job status and optional fields."""
    session = get_session()
    try:
        job = session.get(Job, job_id)  # SQLAlchemy 2.x compatible
        if job:
            job.status = status
            allowed = {c.key for c in Job.__table__.columns}
            for k, v in kwargs.items():
                if k in allowed:
                    setattr(job, k, v)
            job.updated_at = _utcnow()
            session.commit()
    except Exception:
        session.rollback()
        raise


def mark_applied(job_id: int, method: str, resume_path: str = None,
                 cover_letter_path: str = None) -> None:
    """Mark a job as applied."""
    follow_up = _utcnow() + timedelta(days=7)
    update_job_status(
        job_id,
        "applied",
        applied_at=_utcnow(),
        application_method=method,
        resume_path=resume_path,
        cover_letter_path=cover_letter_path,
        follow_up_date=follow_up,
    )


def get_jobs_for_review(
    limit: int = 50,
    min_score: float = 50,
    max_score: float = 65,
) -> List[Job]:
    """
    Return jobs flagged for manual review (score between min_score and max_score).

    Defaults match config.yaml: review_threshold=50, auto_apply_threshold=65.
    Pass thresholds from config to keep behaviour in sync with scoring.
    """
    session = get_session()
    try:
        return session.query(Job).filter(
            Job.status == "scored",
            Job.match_score >= min_score,
            Job.match_score < max_score,
        ).order_by(Job.match_score.desc()).limit(limit).all()
    finally:
        session.close()


def get_recent_applications(days: int = 30) -> List[Job]:
    """Return recent applied jobs."""
    since = _utcnow() - timedelta(days=days)
    session = get_session()
    try:
        return session.query(Job).filter(
            Job.status == "applied",
            Job.applied_at >= since,
        ).order_by(Job.applied_at.desc()).all()
    finally:
        session.close()


def get_all_jobs(limit: int = 500) -> List[Job]:
    """Return all jobs for dashboard."""
    session = get_session()
    try:
        return session.query(Job).order_by(
            Job.discovered_at.desc()
        ).limit(limit).all()
    finally:
        session.close()


def get_jobs_pending_scoring(limit: int = 200, oldest_first: bool = False) -> List[Job]:
    """
    Return jobs that need scoring — discovered status with a description.

    With a fixed LIMIT, always ordering newest-first starves any backlog.
    Pass oldest_first=True to drain it.
    """
    session = get_session()
    try:
        order = Job.discovered_at.asc() if oldest_first else Job.discovered_at.desc()
        return session.query(Job).filter(
            Job.status == "discovered",
            Job.description.isnot(None),
            Job.description != "",
        ).order_by(order).limit(limit).all()
    finally:
        session.close()


def requeue_failed_scores(limit: int = 500) -> int:
    """Re-queue score_error / needs_enrichment jobs so they stop being a black hole."""
    session = get_session()
    try:
        rows = session.query(Job).filter(
            Job.status.in_(["score_error", "needs_enrichment"])
        ).limit(limit).all()
        for job in rows:
            job.status = "discovered"
        session.commit()
        return len(rows)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_jobs_for_rescore_v2(limit: int = 500, since_days: Optional[int] = None) -> int:
    """
    Reset v2-eligible jobs back to 'discovered'. v1 rows are frozen and excluded
    unless they were never scored at all.
    """
    session = get_session()
    try:
        q = session.query(Job).filter(
            Job.status.in_(["scored", "gated", "score_error", "needs_enrichment"]),
            (Job.score_version.is_(None)) | (Job.score_version >= 2),
        )
        if since_days:
            q = q.filter(Job.discovered_at >= _utcnow() - timedelta(days=since_days))
        rows = q.limit(limit).all()
        for job in rows:
            job.status = "discovered"
        session.commit()
        return len(rows)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def log_source_run(source: str, jobs_returned: int, jobs_new: int = 0,
                   elapsed_ms: int = 0, error: Optional[str] = None,
                   run_id: Optional[int] = None) -> None:
    """Record per-source discovery telemetry."""
    from autoapply.tracker.models import SourceRun
    session = get_session()
    try:
        session.add(SourceRun(
            run_id=run_id, source=source, jobs_returned=jobs_returned,
            jobs_new=jobs_new, elapsed_ms=elapsed_ms, error=error,
        ))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def get_source_run_stats(limit: int = 200) -> List:
    """Return recent SourceRun rows for the dashboard."""
    from autoapply.tracker.models import SourceRun
    session = get_session()
    try:
        return session.query(SourceRun).order_by(
            SourceRun.created_at.desc()
        ).limit(limit).all()
    finally:
        session.close()


def get_jobs_needing_enrichment(limit: int = 300, min_chars: int = 300) -> List[Job]:
    """Jobs whose description is missing or too thin to score reliably."""
    from sqlalchemy import func, or_
    session = get_session()
    try:
        return session.query(Job).filter(
            Job.status.in_(["discovered", "needs_enrichment"]),
            or_(
                Job.description.is_(None),
                Job.description == "",
                func.length(Job.description) < min_chars,
            ),
        ).order_by(Job.discovered_at.desc()).limit(limit).all()
    finally:
        session.close()


def set_job_description(job_id: int, description: str) -> None:
    """Write a back-filled description and clear the needs_enrichment state."""
    session = get_session()
    try:
        job = session.get(Job, job_id)
        if job:
            job.description = description
            job.description_quality = None
            if job.status == "needs_enrichment":
                job.status = "discovered"
            job.updated_at = _utcnow()
            session.commit()
    except Exception:
        session.rollback()
        raise


def get_jobs_ready_to_apply(auto_apply_threshold: float = 75.0, limit: int = 20) -> List[Job]:
    """
    Return scored jobs above auto-apply threshold.
    Ordered by discovered_at DESC first, then match_score DESC as tiebreaker.
    Also includes previously failed jobs for retry.
    """
    session = get_session()
    try:
        return session.query(Job).filter(
            Job.status.in_(["scored", "failed"]),
            Job.match_score >= auto_apply_threshold,
        ).order_by(Job.discovered_at.desc(), Job.match_score.desc()).limit(limit).all()
    finally:
        session.close()


def get_latest_jobs(hours: int = 48, limit: int = 100) -> List[Job]:
    """Return jobs discovered in the last N hours, ordered newest first."""
    since = _utcnow() - timedelta(hours=hours)
    session = get_session()
    try:
        return session.query(Job).filter(
            Job.discovered_at >= since,
        ).order_by(Job.discovered_at.desc()).limit(limit).all()
    finally:
        session.close()


def get_latest_unscored_jobs(hours: int = 48, limit: int = 200) -> List[Job]:
    """
    Return unscored jobs discovered in the last N hours, newest first.
    Focused scoring helper — scores only fresh discoveries rather than the full backlog.

    Args:
        hours: Look-back window in hours (default 48h)
        limit: Max jobs to return
    """
    since = _utcnow() - timedelta(hours=hours)
    session = get_session()
    try:
        return session.query(Job).filter(
            Job.status == "discovered",
            Job.description.isnot(None),
            Job.description != "",
            Job.discovered_at >= since,
        ).order_by(Job.discovered_at.desc()).limit(limit).all()
    finally:
        session.close()


def start_run() -> int:
    """Create a run log entry and return its ID."""
    session = get_session()
    try:
        run = RunLog(started_at=_utcnow())
        session.add(run)
        session.commit()
        return run.id
    except Exception:
        session.rollback()
        raise


def finish_run(run_id: int, **stats) -> None:
    """Mark a run as complete with stats."""
    session = get_session()
    try:
        run = session.get(RunLog, run_id)  # SQLAlchemy 2.x compatible
        if run:
            run.completed_at = _utcnow()
            run.status = stats.pop("status", "completed")
            allowed = {c.key for c in RunLog.__table__.columns}
            for k, v in stats.items():
                if k in allowed:
                    setattr(run, k, v)
            session.commit()
    except Exception:
        session.rollback()
        raise


# ── Multi-resume support (Resume + JobScore) ────────────────────────────────
# The default/original candidate keeps using Job's own scoring columns above.
# These helpers back additional uploaded resumes, scored via JobScore rows
# joined against the shared `jobs` table so discovery is never duplicated.

def create_resume(label: str, resume_json: str, raw_file_path: str = None,
                   search_config: str = None, domain: str = None,
                   years_experience: float = None,
                   auto_apply_threshold: float = None,
                   review_threshold: float = None,
                   tfidf_threshold: float = None) -> Resume:
    """Create a new resume profile with auto-derived user profile fields."""
    session = get_session()
    try:
        resume = Resume(
            label=label,
            resume_json=resume_json,
            raw_file_path=raw_file_path,
            search_config=search_config,
            domain=domain,
            years_experience=years_experience,
            auto_apply_threshold=auto_apply_threshold,
            review_threshold=review_threshold,
            tfidf_threshold=tfidf_threshold,
        )
        session.add(resume)
        session.commit()
        return resume
    except Exception:
        session.rollback()
        raise


def get_active_resumes() -> List[Resume]:
    """Return all active (non-deactivated) uploaded resumes."""
    session = get_session()
    try:
        return session.query(Resume).filter(Resume.is_active.is_(True)).order_by(Resume.created_at).all()
    finally:
        session.close()


def get_scheduled_resumes() -> List[Resume]:
    """
    Return resumes opted-in to daily auto-scoring (is_active + schedule_enabled).
    """
    session = get_session()
    try:
        return session.query(Resume).filter(
            Resume.is_active.is_(True),
            Resume.schedule_enabled.is_(True),
        ).order_by(Resume.created_at).all()
    finally:
        session.close()


def set_resume_schedule(resume_id: int, enabled: bool) -> None:
    """Toggle the per-profile daily schedule on or off."""
    session = get_session()
    try:
        resume = session.get(Resume, resume_id)
        if resume:
            resume.schedule_enabled = enabled
            resume.updated_at = _utcnow()
            session.commit()
    except Exception:
        session.rollback()
        raise


def get_all_resumes() -> List[Resume]:
    """Return every resume, active or not (for dashboard management)."""
    session = get_session()
    try:
        return session.query(Resume).order_by(Resume.created_at).all()
    finally:
        session.close()


def get_resume(resume_id: int) -> Optional[Resume]:
    session = get_session()
    try:
        return session.get(Resume, resume_id)
    finally:
        session.close()


def set_resume_active(resume_id: int, is_active: bool) -> None:
    session = get_session()
    try:
        resume = session.get(Resume, resume_id)
        if resume:
            resume.is_active = is_active
            resume.updated_at = _utcnow()
            session.commit()
    except Exception:
        session.rollback()
        raise


def update_resume_profile(resume_id: int, domain: str = None,
                           years_experience: float = None,
                           auto_apply_threshold: float = None,
                           review_threshold: float = None,
                           tfidf_threshold: float = None,
                           search_config: str = None) -> None:
    """Update the derived profile fields for an existing resume (e.g. after backfill)."""
    session = get_session()
    try:
        resume = session.get(Resume, resume_id)
        if resume:
            if domain is not None:
                resume.domain = domain
            if years_experience is not None:
                resume.years_experience = years_experience
            if auto_apply_threshold is not None:
                resume.auto_apply_threshold = auto_apply_threshold
            if review_threshold is not None:
                resume.review_threshold = review_threshold
            if tfidf_threshold is not None:
                resume.tfidf_threshold = tfidf_threshold
            if search_config is not None:
                resume.search_config = search_config
            resume.updated_at = _utcnow()
            session.commit()
    except Exception:
        session.rollback()
        raise


def backfill_resume_profiles() -> int:
    """
    For resumes uploaded before the domain/years_experience columns existed,
    derive their profile from the stored resume_json using heuristics and
    update the DB columns. Returns count of resumes updated.
    """
    from autoapply.generator.resume_parser import _heuristic_profile
    session = get_session()
    updated = 0
    try:
        resumes = session.query(Resume).all()
        for r in resumes:
            # Only backfill if domain column is empty
            if r.domain:
                continue
            try:
                resume_json = json.loads(r.resume_json) if r.resume_json else {}
            except Exception:
                continue
            profile = _heuristic_profile(resume_json)

            # Also merge profile into search_config if search_config lacks domain/roles
            sc = {}
            try:
                sc = json.loads(r.search_config) if r.search_config else {}
            except Exception:
                pass

            # Merge: keep existing roles/locations if present, add missing fields
            merged_sc = {
                "domain": profile["domain"],
                "years_experience": profile["years_experience"],
                "seniority": profile["seniority"],
                "roles": sc.get("roles") or profile["roles"],
                "locations": sc.get("locations") or profile["locations"],
                "strong_keywords": sc.get("strong_keywords") or profile["strong_keywords"],
                "exclude_companies": sc.get("exclude_companies", []),
                "auto_apply_threshold": profile["auto_apply_threshold"],
                "review_threshold": profile["review_threshold"],
                "tfidf_threshold": profile["tfidf_threshold"],
            }

            r.domain = profile["domain"]
            r.years_experience = profile["years_experience"]
            r.auto_apply_threshold = profile["auto_apply_threshold"]
            r.review_threshold = profile["review_threshold"]
            r.tfidf_threshold = profile["tfidf_threshold"]
            r.search_config = json.dumps(merged_sc)
            r.updated_at = _utcnow()
            updated += 1
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()
    return updated


def get_jobs_pending_scoring_for_resume(resume_id: int, limit: int = 200,
                                         title_keywords: list | None = None) -> List[Job]:
    """
    Jobs with a description that don't yet have a JobScore row for this resume,
    newest first. Optionally filtered by title/description keywords for domain
    pre-filtering (saves LLM quota on clearly irrelevant jobs).
    """
    from sqlalchemy import or_ as _or
    session = get_session()
    already_scored_ids = session.query(JobScore.job_id).filter(JobScore.resume_id == resume_id).scalar_subquery()
    q = session.query(Job).filter(
        Job.description.isnot(None),
        Job.description != "",
        Job.id.notin_(already_scored_ids),
    )
    if title_keywords:
        # OR-match across title and first portion of description for speed
        title_filters = [Job.title.ilike(f"%{kw}%") for kw in title_keywords[:20]]
        desc_filters = [Job.description.ilike(f"%{kw}%") for kw in title_keywords[:8]]
        q = q.filter(_or(*title_filters, *desc_filters))
    return q.order_by(Job.discovered_at.desc()).limit(limit).all()


def get_user_profile(resume_id: int) -> dict:
    """
    Return the scoring profile for an uploaded resume — thresholds, domain, keywords.
    Used to configure per-user scoring without touching config.yaml.
    """
    session = get_session()
    r = session.get(Resume, resume_id)
    if not r:
        return {}
    sc: dict = {}
    try:
        sc = json.loads(r.search_config) if r.search_config else {}
    except Exception:
        pass
    return {
        "domain": r.domain or sc.get("domain", "general"),
        "years_experience": r.years_experience or sc.get("years_experience", 3.0),
        "auto_apply_threshold": r.auto_apply_threshold or sc.get("auto_apply_threshold", 68.0),
        "review_threshold": r.review_threshold or sc.get("review_threshold", 52.0),
        "tfidf_threshold": r.tfidf_threshold or sc.get("tfidf_threshold", 0.10),
        "strong_keywords": sc.get("strong_keywords", []),
        "roles": sc.get("roles", []),
        "locations": sc.get("locations", []),
        "exclude_companies": sc.get("exclude_companies", []),
        "seniority": sc.get("seniority", "mid"),
    }


def update_job_score_for_resume(job_id: int, resume_id: int, score: float, reasoning: str,
                                 skill_gaps: list, tailoring_variant: str,
                                 red_flags: list,
                                 tfidf_score: float = 0.0,
                                 skill_match_score: float = 0.0,
                                 verdict: str = "skip",
                                 adjusted_score: float = None,
                                 recency_bonus_applied: float = 0.0,
                                 seniority_multiplier_applied: float = 1.0,
                                 *,
                                 status: str = "scored",
                                 score_version: int = 2,
                                 raw_llm_score: Optional[float] = None,
                                 score_model: Optional[str] = None,
                                 score_prompt_version: Optional[str] = None,
                                 score_breakdown: Optional[dict] = None,
                                 hard_gate_failures: Optional[list] = None) -> None:
    """Insert or update the JobScore row for a (job, resume) pair."""
    session = get_session()
    try:
        existing = session.query(JobScore).filter_by(job_id=job_id, resume_id=resume_id).first()
        if not existing:
            existing = JobScore(job_id=job_id, resume_id=resume_id)
            session.add(existing)
        existing.match_score = score
        existing.score_reasoning = reasoning
        existing.skill_gaps = json.dumps(skill_gaps)
        existing.tailoring_variant = tailoring_variant
        existing.red_flags = json.dumps(red_flags)
        existing.tfidf_score = tfidf_score
        existing.skill_match_score = skill_match_score
        existing.verdict = verdict
        existing.adjusted_score = adjusted_score if adjusted_score is not None else score
        existing.recency_bonus_applied = recency_bonus_applied
        existing.seniority_multiplier_applied = seniority_multiplier_applied
        existing.status = status
        existing.score_version = score_version
        existing.raw_llm_score = raw_llm_score
        existing.score_model = score_model
        existing.score_prompt_version = score_prompt_version
        existing.score_breakdown = json.dumps(score_breakdown) if score_breakdown else None
        existing.hard_gate_failures = json.dumps(hard_gate_failures) if hard_gate_failures else None
        existing.scored_at = _utcnow()
        existing.updated_at = _utcnow()
        session.commit()
    except Exception:
        session.rollback()
        raise


def get_scored_jobs_for_resume(resume_id: int, limit: int = 500) -> List[tuple]:
    """Return (Job, JobScore) pairs for a resume, highest score first — for dashboard display."""
    session = get_session()
    return session.query(Job, JobScore).join(
        JobScore, JobScore.job_id == Job.id
    ).filter(
        JobScore.resume_id == resume_id,
    ).order_by(JobScore.match_score.desc()).limit(limit).all()


def delete_resume_scores(resume_id: int) -> int:
    """
    Delete ALL JobScore rows for a given resume.
    Used to purge stale/buggy scores so the profile can be cleanly re-scored.
    Returns the number of rows deleted.
    """
    session = get_session()
    try:
        deleted = session.query(JobScore).filter_by(resume_id=resume_id).delete()
        session.commit()
        return deleted
    except Exception:
        session.rollback()
        raise


def get_resume_score_stats(resume_id: int) -> dict:
    """
    Return quick stats about the JobScore rows for a resume.
    Used in the dashboard to diagnose whether scores look healthy.
    Returns dict with total, zero_score, no_verdict, max_score, mean_score counts.
    """
    session = get_session()
    scores = session.query(JobScore).filter_by(resume_id=resume_id).all()
    if not scores:
        return {"total": 0, "zero_score": 0, "no_verdict": 0,
                "max_score": None, "mean_score": None, "healthy": True}
    score_vals = [s.match_score for s in scores if s.match_score is not None]
    zero_score = sum(1 for s in scores if s.match_score == 0.0 or s.match_score is None)
    no_verdict = sum(1 for s in scores if s.verdict is None)
    # Heuristic: unhealthy if >50% have no verdict (pre-fix bug) or >40% are zero
    total = len(scores)
    healthy = (no_verdict / total < 0.5) and (zero_score / total < 0.4) if total else True
    return {
        "total": total,
        "zero_score": zero_score,
        "no_verdict": no_verdict,
        "max_score": max(score_vals) if score_vals else None,
        "mean_score": round(sum(score_vals) / len(score_vals), 1) if score_vals else None,
        "healthy": healthy,
    }
