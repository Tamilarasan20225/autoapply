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

from autoapply.tracker.models import Base, Job, RunLog


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
    ]

    with engine.connect() as conn:
        for table, col, col_type in new_columns:
            try:
                # Check if column already exists via pragma
                result = conn.execute(
                    __import__("sqlalchemy").text(f"PRAGMA table_info({table})")
                )
                existing_cols = {row[1] for row in result}
                if col not in existing_cols:
                    conn.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                        )
                    )
                    conn.commit()
            except Exception:
                pass  # Non-fatal — column may already exist or table may not exist yet


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
            # Update mutable fields without overwriting status/score
            for field in ["title", "company", "location", "description", "apply_url"]:
                if job_data.get(field):
                    setattr(existing, field, job_data[field])
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
                     skill_match_score: float = 0.0) -> None:
    """Save LLM scoring results to a job record."""
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
            job.status = "scored"
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


def get_jobs_for_review(limit: int = 50) -> List[Job]:
    """Return jobs flagged for manual review (score 60-74)."""
    session = get_session()
    return session.query(Job).filter(
        Job.status == "scored",
        Job.match_score >= 60,
        Job.match_score < 75,
    ).order_by(Job.match_score.desc()).limit(limit).all()


def get_recent_applications(days: int = 30) -> List[Job]:
    """Return recent applied jobs."""
    since = _utcnow() - timedelta(days=days)
    session = get_session()
    return session.query(Job).filter(
        Job.status == "applied",
        Job.applied_at >= since,
    ).order_by(Job.applied_at.desc()).all()


def get_all_jobs(limit: int = 500) -> List[Job]:
    """Return all jobs for dashboard."""
    session = get_session()
    return session.query(Job).order_by(
        Job.discovered_at.desc()
    ).limit(limit).all()


def get_jobs_pending_scoring(limit: int = 200) -> List[Job]:
    """
    Return jobs that need scoring — discovered status with a description.
    Includes retry of previously errored jobs.
    """
    session = get_session()
    return session.query(Job).filter(
        Job.status == "discovered",
        Job.description.isnot(None),
        Job.description != "",
    ).order_by(Job.discovered_at.desc()).limit(limit).all()


def get_jobs_ready_to_apply(auto_apply_threshold: float = 75.0, limit: int = 20) -> List[Job]:
    """
    Return scored jobs above auto-apply threshold.
    Ordered by discovered_at DESC first (latest jobs first for higher conversion),
    then match_score DESC as tiebreaker.
    Also includes previously failed jobs for retry (they may succeed with a different method).
    """
    session = get_session()
    return session.query(Job).filter(
        Job.status.in_(["scored", "failed"]),  # Retry failed jobs too
        Job.match_score >= auto_apply_threshold,
    ).order_by(Job.discovered_at.desc(), Job.match_score.desc()).limit(limit).all()


def get_latest_jobs(hours: int = 48, limit: int = 100) -> List[Job]:
    """
    Return jobs discovered in the last N hours, ordered newest first.
    Used to surface the freshest listings for priority scoring and applying.

    Args:
        hours: Look-back window in hours (default 48h)
        limit: Max jobs to return
    """
    since = _utcnow() - timedelta(hours=hours)
    session = get_session()
    return session.query(Job).filter(
        Job.discovered_at >= since,
    ).order_by(Job.discovered_at.desc()).limit(limit).all()


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
    return session.query(Job).filter(
        Job.status == "discovered",
        Job.description.isnot(None),
        Job.description != "",
        Job.discovered_at >= since,
    ).order_by(Job.discovered_at.desc()).limit(limit).all()


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
