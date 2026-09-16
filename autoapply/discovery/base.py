"""
Base classes and data models for job discovery sources.
"""

from dataclasses import dataclass, field
from typing import Optional
import hashlib
import re

# ── Employment type patterns for regex detection ─────────────────────────────
_EMPLOYMENT_TYPE_PATTERNS = [
    (re.compile(r'\b(full[\s\-]?time|permanent|fte)\b', re.I), 'full-time'),
    (re.compile(r'\b(part[\s\-]?time)\b', re.I), 'part-time'),
    (re.compile(r'\b(contract|contractor|c2c|corp[\s\-]?to[\s\-]?corp|freelance|consulting)\b', re.I), 'contract'),
    (re.compile(r'\b(intern(ship)?|trainee|apprentice)\b', re.I), 'internship'),
]


@dataclass
class RawJob:
    """Normalized job listing from any source."""
    # Required
    title: str
    company: str
    job_url: str
    source: str  # remotive, arbeitnow, adzuna, linkedin, greenhouse, lever, etc.

    # Optional
    external_id: Optional[str] = None
    location: Optional[str] = None
    is_remote: bool = False
    description: Optional[str] = None
    apply_url: Optional[str] = None

    # ATS info (for direct submission)
    ats_type: Optional[str] = None          # greenhouse, lever, ashby, linkedin, workday, other
    ats_company_slug: Optional[str] = None  # e.g. "stripe"
    ats_job_id: Optional[str] = None

    # Salary
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None

    # Extended metadata (Phase 3)
    posted_at: Optional[str] = None          # ISO date of posting
    employment_type: Optional[str] = None    # full-time/contract/internship
    department: Optional[str] = None         # Engineering/Product/Data
    seniority: Optional[str] = None          # junior/mid/senior/staff/principal
    skills_required: Optional[list] = None   # extracted required skills
    redirect_resolved: bool = False          # apply_url has been redirect-resolved
    source_query: Optional[str] = None       # search query that found this job

    def to_db_dict(self) -> dict:
        """Convert to dict suitable for db.upsert_job.
        
        Note: self.seniority field → "seniority_level" DB column key.
        """
        import json as _json
        return {
            "external_id": self.external_id or self._generate_id(),
            "source": self.source,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "is_remote": self.is_remote,
            "job_url": self.job_url,
            "apply_url": self.apply_url or self.job_url,
            "description": self.description,
            "ats_type": self.ats_type,
            "ats_company_slug": self.ats_company_slug,
            "ats_job_id": self.ats_job_id,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_currency": self.salary_currency,
            "posted_at": self.posted_at,
            "employment_type": self.employment_type,
            "department": self.department,
            "seniority_level": self.seniority,   # RawJob.seniority → DB seniority_level
            "skills_required": _json.dumps(self.skills_required) if self.skills_required else None,
            "redirect_resolved": self.redirect_resolved,
            "source_query": self.source_query,
            "status": "discovered",
        }

    def _generate_id(self) -> str:
        """Generate a stable ID from company+title+url."""
        key = f"{self.company}|{self.title}|{self.job_url}"
        return hashlib.md5(key.encode()).hexdigest()[:16]

    def populate_metadata(self, skill_extractor=None) -> None:
        """
        Inplace metadata population:
        1. seniority from title (using extract_seniority_from_title)
        2. employment_type from description regex (if not already set)
        3. skills_required via SkillExtractor (if provided and description available)
        
        Args:
            skill_extractor: Optional SkillExtractor instance for skills extraction
        """
        # 1. Seniority from title
        if not self.seniority and self.title:
            try:
                from autoapply.scoring.skill_extractor import extract_seniority_from_title
                self.seniority = extract_seniority_from_title(self.title)
            except Exception:
                pass

        # 2. Employment type from description regex
        if not self.employment_type:
            text = f"{self.title} {self.description or ''}"
            for pattern, etype in _EMPLOYMENT_TYPE_PATTERNS:
                if pattern.search(text):
                    self.employment_type = etype
                    break
            else:
                self.employment_type = 'full-time'  # default assumption

        # 3. Skills extraction from description
        if skill_extractor and self.description and not self.skills_required:
            try:
                extracted = skill_extractor.extract(self.description)
                if extracted:
                    self.skills_required = extracted
            except Exception:
                pass

    def is_relevant(self, keywords: list[str], locations: list[str]) -> bool:
        """Quick pre-filter before expensive LLM scoring."""
        text = f"{self.title} {self.company} {self.description or ''}".lower()

        # Always pass if it's remote
        if self.is_remote:
            return True

        # Location check
        if locations:
            loc_text = (self.location or "").lower()
            if any(loc.lower() in loc_text for loc in locations):
                return True

        return True  # Default pass — let LLM score decide


def clean_html(text: str) -> str:
    """Strip HTML tags from job descriptions."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def truncate_description(text: str, max_chars: int = 3000) -> str:
    """Truncate job description to fit in LLM context."""
    if not text:
        return ""
    text = clean_html(text)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text
