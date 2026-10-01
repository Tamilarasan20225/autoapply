"""
Cross-source job deduplication.
Prevents the same job from appearing multiple times when scraped from different sources.
"""

import re
from typing import List
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from autoapply.discovery.base import RawJob

_TRACKING_PARAMS = {
    "gh_src", "gh_jid", "source", "src", "ref", "referrer", "trk", "trackingid",
    "lever-source", "lever-origin", "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "utm_id",
}
_HOST_ALIASES = {
    "job-boards.greenhouse.io": "boards.greenhouse.io",
    "boards.eu.greenhouse.io": "boards.greenhouse.io",
    "jobs.eu.lever.co": "jobs.lever.co",
}


def canonical_url(url: str) -> str:
    """Strip tracking params and host variants so cross-source URL dedup works."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = _HOST_ALIASES.get(parts.netloc.lower(), parts.netloc.lower())
    query = urlencode([
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, query, ""))


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation and extra spaces."""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co|gmbh|technologies|"
    r"technology|solutions|labs|club|group|india|global|software|systems)\b"
)


def normalize_company(company: str) -> str:
    """Collapse 'Cred Club' / 'cred-club' / 'CRED Inc.' onto one key."""
    text = normalize_text((company or "").replace("-", " ").replace("_", " "))
    text = _COMPANY_SUFFIXES.sub("", text)
    return re.sub(r"\s+", "", text)


def _normalize_location(location: str) -> str:
    text = normalize_text(location or "")
    text = re.sub(r"\b(bengaluru)\b", "bangalore", text)
    # Keep only the first token group so "Bangalore, Karnataka, India" matches "Bangalore".
    return text.split(" ")[0] if text else ""


def job_fingerprint(job: RawJob) -> str:
    """
    Dedup fingerprint: company + normalized title + location + ats_job_id.

    Location and ats_job_id are part of the key because a company routinely posts
    the same title for several teams and offices; keying on title alone silently
    discarded all but one of them.
    """
    company = normalize_company(job.company)
    title = normalize_text(job.title)

    # Strip seniority symmetrically or not at all — stripping only junior made
    # "Junior Backend Engineer" collide with "Backend Engineer".
    title = re.sub(r"\b(sr|jr)\b", "", title)
    title = re.sub(r"\b(engineer|developer)\b", "eng", title)
    title = re.sub(r"\b(backend|back end|back-end)\b", "backend", title)
    title = re.sub(r"\b(software|swe|sde|sde2|sde-2)\b", "swe", title)
    title = re.sub(r"\s+", " ", title).strip()

    location = _normalize_location(job.location or "")
    ats_id = (job.ats_job_id or "").strip()

    return f"{company}::{title}::{location}::{ats_id}"


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

    def _better(candidate: RawJob, incumbent: RawJob) -> bool:
        cand_rank = ATS_PRIORITY.get(candidate.ats_type or "other", 1)
        inc_rank = ATS_PRIORITY.get(incumbent.ats_type or "other", 1)
        if cand_rank != inc_rank:
            return cand_rank > inc_rank
        return len(candidate.description or "") > len(incumbent.description or "")

    seen: dict[str, RawJob] = {}
    by_url: dict[str, str] = {}   # canonical url -> fingerprint

    for job in jobs:
        fp = job_fingerprint(job)
        url_key = canonical_url(job.job_url)

        # URL collisions resolve on quality, not arrival order. Previously the
        # winner depended on which thread finished first.
        if url_key and url_key in by_url:
            owner_fp = by_url[url_key]
            incumbent = seen.get(owner_fp)
            if incumbent is not None and _better(job, incumbent):
                seen[owner_fp] = job
            continue

        if fp not in seen:
            seen[fp] = job
            if url_key:
                by_url[url_key] = fp
        elif _better(job, seen[fp]):
            seen[fp] = job
            if url_key:
                by_url[url_key] = fp

    return list(seen.values())


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
    "calibration", "mechanical",
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
