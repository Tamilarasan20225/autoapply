"""
SQLAlchemy models for AutoAppy tracking database.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Text, Boolean, create_engine
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Job(Base):
    """Represents a discovered job listing."""
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    external_id = Column(String(256), unique=True, nullable=True)
    source = Column(String(64), nullable=False)
    title = Column(String(256), nullable=False)
    company = Column(String(256), nullable=False)
    location = Column(String(256), nullable=True)
    is_remote = Column(Boolean, default=False)
    job_url = Column(Text, nullable=False)
    apply_url = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    ats_type = Column(String(64), nullable=True)
    ats_company_slug = Column(String(128), nullable=True)
    ats_job_id = Column(String(256), nullable=True)
    salary_min = Column(Float, nullable=True)
    salary_max = Column(Float, nullable=True)
    salary_currency = Column(String(8), nullable=True)
    match_score = Column(Float, nullable=True)
    score_reasoning = Column(Text, nullable=True)
    skill_gaps = Column(Text, nullable=True)
    tailoring_variant = Column(String(32), nullable=True)
    red_flags = Column(Text, nullable=True)
    tfidf_score = Column(Float, nullable=True)
    skill_match_score = Column(Float, nullable=True)
    status = Column(String(32), default="discovered")
    applied_at = Column(DateTime, nullable=True)
    resume_path = Column(Text, nullable=True)
    cover_letter_path = Column(Text, nullable=True)
    application_method = Column(String(32), nullable=True)
    follow_up_date = Column(DateTime, nullable=True)
    response_received = Column(Boolean, default=False)
    response_type = Column(String(32), nullable=True)
    notes = Column(Text, nullable=True)
    interview_stage = Column(String(64), nullable=True)
    interview_date = Column(DateTime, nullable=True)
    offer_amount = Column(Text, nullable=True)
    screenshot_path = Column(Text, nullable=True)
    ats_detected_at_apply = Column(String(64), nullable=True)
    apply_attempts = Column(Integer, default=0)
    last_attempt_error = Column(Text, nullable=True)
    session_key = Column(String(256), nullable=True)
    company_portal_url = Column(Text, nullable=True)
    posted_at = Column(String(32), nullable=True)
    employment_type = Column(String(32), nullable=True)
    department = Column(String(64), nullable=True)
    seniority_level = Column(String(32), nullable=True)
    skills_required = Column(Text, nullable=True)
    redirect_resolved = Column(Boolean, default=False)
    source_query = Column(String(256), nullable=True)
    discovered_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    def __repr__(self):
        return f"<Job {self.company} | {self.title} | score={self.match_score} | status={self.status}>"


class RunLog(Base):
    """Tracks each pipeline run for diagnostics."""
    __tablename__ = "run_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)
    status = Column(String(32), default="running")
    jobs_discovered = Column(Integer, default=0)
    jobs_scored = Column(Integer, default=0)
    jobs_applied = Column(Integer, default=0)
    jobs_skipped = Column(Integer, default=0)
    llm_calls_made = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
