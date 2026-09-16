"""
Cross-source job deduplication.
Prevents the same job from appearing multiple times when scraped from different sources.
"""

import re
from typing import List
from autoapply.discovery.base import RawJob


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation and extra spaces."""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def job_fingerprint(job: RawJob) -> str:
    """
    Generate a deduplication fingerprint from company + normalized title.
    Same job posted on multiple boards will have the same fingerprint.

    Fixed: removed "staff" from the strip list — "Staff Backend Engineer" and
    "Junior Backend Engineer" are different seniority levels and should NOT dedup.
    Only strip truly interchangeable abbreviations (sr/jr, swe/sde).
    """
    company = normalize_text(job.company)
    title = normalize_text(job.title)

    # Only normalize truly interchangeable abbreviations — NOT seniority levels
    title = re.sub(r"\b(sr|junior|jr)\b", "", title)          # sr/jr are interchangeable abbrevs
    title = re.sub(r"\b(engineer|developer|dev)\b", "eng", title)
    title = re.sub(r"\b(backend|back end|back-end)\b", "backend", title)
    title = re.sub(r"\b(software|swe|sde|sde2|sde-2)\b", "swe", title)
    # Note: "senior", "lead", "staff", "principal" are intentionally kept —
    # they represent different seniority levels and should NOT be deduplicated
    title = re.sub(r"\s+", " ", title).strip()

    return f"{company}::{title}"


def deduplicate_jobs(jobs: List[RawJob]) -> List[RawJob]:
    """
    Remove duplicate jobs across sources.
    Prefers jobs with more description content and better ATS types.

    ATS priority: greenhouse > lever > linkedin > remotive > remoteok > other
    """
    ATS_PRIORITY = {
        "greenhouse": 10,
        "lever": 9,
        "ashby": 8,
        "linkedin": 7,
        "remotive": 6,
        "remoteok": 5,
        "adzuna": 4,
        "arbeitnow": 3,
        "indeed": 2,
        "other": 1,
    }

    seen: dict[str, RawJob] = {}
    url_seen: set[str] = set()

    for job in jobs:
        # First check URL dedup (exact same URL from two sources)
        if job.job_url in url_seen:
            continue
        url_seen.add(job.job_url)

        fp = job_fingerprint(job)
        if fp not in seen:
            seen[fp] = job
        else:
            existing = seen[fp]
            # Keep the one from a higher-priority ATS
            existing_priority = ATS_PRIORITY.get(existing.ats_type or "other", 1)
            new_priority = ATS_PRIORITY.get(job.ats_type or "other", 1)

            if new_priority > existing_priority:
                seen[fp] = job
            elif new_priority == existing_priority:
                # Keep the one with more description
                existing_desc_len = len(existing.description or "")
                new_desc_len = len(job.description or "")
                if new_desc_len > existing_desc_len:
                    seen[fp] = job

    deduped = list(seen.values())
    return deduped


# Job roles that are clearly not relevant for a Backend/AI Engineer
_IRRELEVANT_TITLE_KEYWORDS = [
    # Business / non-tech
    "accountant", "accounting", "financial", "finance", "controller",
    "sales", "salesperson", "bdr", "sdr", "account executive", "account manager",
    "marketing", "social media", "content creator", "brand", "copywriter",
    "hr ", "human resources", "talent acquisition", "recruiter", "recruiting",
    "sourcing specialist",
    "consultant business", "management consultant",
    "legal", "lawyer", "attorney", "paralegal",
    "customer success", "customer support", "support jedi",
    "operations manager", "supply chain", "logistics", "procurement",
    "office manager", "executive assistant", "personal assistant",
    # Non-backend tech
    "calibration", "electrical engineer", "hardware engineer", "mechanical",
    "embedded systems",
    # German-only apprenticeships / internships
    "ausbildung", "azubi", "praktikum", "werkstudent",
    # Specific non-relevant roles
    "gtm strategy", "product owner", "scrum master", "agile coach",
    "cloud consultant", "it architect",
]

# Must match at least one of these to be a real tech role
_TECH_TITLE_KEYWORDS = [
    "engineer", "developer", "dev", "backend", "software", "python",
    "java", "data", "ml", "ai", "sde", "swe", "platform", "infrastructure",
    "devops", "site reliability", "sre", "fullstack", "full stack",
    "full-stack", "architect", "data scientist", "research", "nlp",
    "machine learning", "deep learning", "llm", "api", "cloud", "security",
    "mobile", "android", "ios", "embedded", "firmware", "qa engineer",
    "test engineer", "automation engineer",
]


def filter_by_keywords(jobs: List[RawJob], keywords: list[str]) -> List[RawJob]:
    """
    Two-stage pre-filter before LLM scoring:
    1. Hard reject: clearly non-tech or irrelevant roles (saves LLM calls)
    2. Soft accept: must have at least one tech keyword or strong domain keyword
    """
    keywords_lower = [k.lower() for k in keywords] if keywords else []

    filtered = []
    skipped = 0
    for job in jobs:
        title_lower = job.title.lower()
        desc_lower = (job.description or "").lower()[:500]  # Only check first 500 chars

        # Stage 1: Hard reject if title contains irrelevant keywords
        if any(kw in title_lower for kw in _IRRELEVANT_TITLE_KEYWORDS):
            skipped += 1
            continue

        # Stage 2: Must contain at least one tech keyword (in title or description)
        text = f"{title_lower} {desc_lower}"
        has_tech = any(kw in text for kw in _TECH_TITLE_KEYWORDS)
        has_strong_kw = keywords_lower and any(kw in text for kw in keywords_lower)

        if has_tech or has_strong_kw:
            filtered.append(job)
        else:
            skipped += 1

    if skipped > 0:
        print(f"  Pre-filter: skipped {skipped} clearly irrelevant jobs")

    return filtered
