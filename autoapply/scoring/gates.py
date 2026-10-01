"""
Deterministic pre-LLM gates for scoring v2.

Everything that can be decided from text with rules lives here, so the LLM is
only asked to judge genuine fit. Each signal is produced exactly once and is
consumed exactly once by the scorer — no signal appears in both the prompt
rubric and a code-side multiplier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ── Description quality ──────────────────────────────────────────────────────

QUALITY_FULL = "full"
QUALITY_PARTIAL = "partial"
QUALITY_SNIPPET = "snippet"
QUALITY_MISSING = "missing"

_REQUIREMENT_MARKERS = (
    "requirement", "qualification", "you will", "you'll", "responsibilit",
    "what you", "about the role", "skills", "experience with", "must have",
    "nice to have", "we're looking for", "we are looking for",
)
_TRUNCATION_SENTINEL = "...[truncated]"


def classify_description_quality(description: Optional[str]) -> str:
    """Grade a job description so snippet-only rows never get an LLM score."""
    if not description:
        return QUALITY_MISSING
    text = description.strip()
    if len(text) < 50:
        return QUALITY_MISSING
    if len(text) < 400:
        return QUALITY_SNIPPET
    lowered = text.lower()
    marker_hits = sum(1 for m in _REQUIREMENT_MARKERS if m in lowered)
    if len(text) >= 1200 and marker_hits >= 2:
        return QUALITY_FULL
    if marker_hits >= 1:
        return QUALITY_PARTIAL
    return QUALITY_SNIPPET


def is_truncated(description: Optional[str]) -> bool:
    return bool(description) and _TRUNCATION_SENTINEL in description


# ── Years of experience ──────────────────────────────────────────────────────

_YOE_PATTERNS = [
    # "5-8 years", "5 to 8 years"
    re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I),
    # "minimum 5 years", "at least 5 years", "5+ years"
    re.compile(r"(?:minimum|min\.?|at least|least)\s*(?:of\s*)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)", re.I),
    re.compile(r"(\d{1,2})\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+|professional\s+|hands[- ]on\s+)?experience", re.I),
]
_INTERNSHIP_RE = re.compile(r"\b(intern|internship|fresher|graduate programme|new grad)\b", re.I)


def extract_required_years(description: Optional[str]) -> Optional[float]:
    """
    Lowest credible minimum-YOE requirement in the JD, or None if unstated.
    Takes the minimum across matches — JDs often list a strict overall minimum
    plus higher per-technology asks.
    """
    if not description:
        return None
    candidates: list[float] = []
    for pattern in _YOE_PATTERNS:
        for match in pattern.finditer(description):
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if 0 < value <= 25:
                candidates.append(value)
    if not candidates:
        return None
    return min(candidates)


# ── Geographic eligibility ───────────────────────────────────────────────────

GEO_INDIA_OK = "india_ok"
GEO_US_ONLY = "us_only"
GEO_EU_ONLY = "eu_only"
GEO_OTHER_ONLY = "other_only"
GEO_UNKNOWN = "unknown"

_US_ONLY_RE = re.compile(
    r"(authorized to work in the (united states|u\.?s\.?)"
    r"|must (be |reside )?(located|based) in the (united states|u\.?s\.?|usa)"
    r"|us[- ]only|u\.?s\.?[- ]based only|united states only|usa only"
    r"|work authorization in the (united states|u\.?s\.?)"
    r"|(must be a )?us (citizen|permanent resident)"
    r"|green card|security clearance"
    r"|remote \(us\)|remote[, -]+(united states|usa|us only))",
    re.I,
)
# A location field naming a non-India country is itself the eligibility signal,
# e.g. "United States - Remote" never appears as prose inside the description.
_US_LOCATION_RE = re.compile(
    r"\b(united states|u\.?s\.?a\.?|usa|canada|new york|san francisco|seattle|"
    r"austin|boston|chicago|denver|atlanta|california|texas|washington|"
    r"\b(ny|ca|tx|wa|ma|il|co|ga)\b)\b",
    re.I,
)
_EU_LOCATION_RE = re.compile(
    r"\b(united kingdom|london|berlin|munich|amsterdam|paris|dublin|madrid|"
    r"barcelona|lisbon|warsaw|stockholm|zurich|germany|france|spain|poland|"
    r"netherlands|sweden|switzerland|ireland|portugal)\b",
    re.I,
)
_EU_ONLY_RE = re.compile(
    r"(eu[- ]only|europe only|emea only|within the (eu|european union)"
    r"|must (be |reside )?(located|based) in (the )?(eu|european union|europe|uk|germany)"
    r"|eligible to work in the (eu|european union|uk))",
    re.I,
)
_NO_SPONSOR_RE = re.compile(
    r"(visa sponsorship (is )?(not|un)available"
    r"|(we )?(do not|don't|cannot|can't) (offer|provide) (visa )?sponsorship"
    r"|no (visa )?sponsorship"
    r"|without (the need for )?sponsorship)",
    re.I,
)
_INDIA_RE = re.compile(
    r"\b(india|bangalore|bengaluru|hyderabad|chennai|pune|mumbai|delhi|gurgaon|"
    r"gurugram|noida|kolkata|ahmedabad|coimbatore|kochi|trivandrum|indore|jaipur)\b",
    re.I,
)
_GLOBAL_REMOTE_RE = re.compile(
    r"(remote\s*[,/ -]+\s*(global|worldwide|anywhere)"
    r"|(global|worldwide|anywhere)\s*[,/ -]+\s*remote"
    r"|work from anywhere|globally remote|remote \(global\)|anywhere in the world)",
    re.I,
)


def extract_geo_scope(
    description: Optional[str],
    location: Optional[str] = None,
    is_remote: bool = False,
) -> str:
    """
    Classify India eligibility. `is_remote` alone is NOT evidence of eligibility —
    most "remote" postings are region-locked.
    """
    location_text = location or ""
    haystack = f"{location_text}\n{description or ''}"

    if _INDIA_RE.search(location_text):
        return GEO_INDIA_OK
    if _GLOBAL_REMOTE_RE.search(haystack):
        return GEO_INDIA_OK

    if _US_ONLY_RE.search(haystack) or _US_LOCATION_RE.search(location_text):
        return GEO_US_ONLY
    if _EU_ONLY_RE.search(haystack) or _EU_LOCATION_RE.search(location_text):
        return GEO_EU_ONLY
    if _NO_SPONSOR_RE.search(haystack) and not _INDIA_RE.search(haystack):
        return GEO_OTHER_ONLY

    if _INDIA_RE.search(haystack):
        return GEO_INDIA_OK
    return GEO_UNKNOWN


# ── Compensation ─────────────────────────────────────────────────────────────

# Rough static rates; comp gating only needs order-of-magnitude accuracy.
_FX_TO_INR = {
    "INR": 1.0, "USD": 84.0, "EUR": 91.0, "GBP": 107.0,
    "CAD": 61.0, "AUD": 55.0, "SGD": 63.0, "AED": 23.0,
}
_LPA_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:-|–|to)?\s*(\d{1,3}(?:\.\d+)?)?\s*(?:lpa|lakhs? per annum|lacs? per annum)", re.I)


def normalize_salary_to_inr(
    salary_min: Optional[float],
    salary_max: Optional[float],
    currency: Optional[str],
    description: Optional[str] = None,
) -> Optional[float]:
    """Best-effort annual INR figure, preferring the max of a posted range."""
    value = salary_max or salary_min
    if value:
        rate = _FX_TO_INR.get((currency or "INR").upper())
        if rate:
            return value * rate
    if description:
        match = _LPA_RE.search(description)
        if match:
            raw = match.group(2) or match.group(1)
            try:
                return float(raw) * 100_000
            except ValueError:
                pass
    return None


# ── Seniority ────────────────────────────────────────────────────────────────

SENIORITY_ORDER = ["intern", "junior", "mid", "senior", "staff", "principal", "director"]

_SENIORITY_PATTERNS = [
    ("director", re.compile(r"\b(director|vp|vice president|head of|cto|chief)\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow|architect)\b", re.I)),
    ("staff", re.compile(r"\b(staff|lead|team lead|tech lead|manager|sde[ -]?(iii|3|4))\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|sde[ -]?(ii|2)|software engineer (ii|2)|level (ii|2)|l[45])\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|associate|entry[- ]level|graduate|trainee|sde[ -]?(i|1)\b|l[12])\b", re.I)),
    ("intern", re.compile(r"\b(intern|internship|apprentice|co[- ]?op)\b", re.I)),
]

# Target YOE band per level. Used to compute a fit multiplier from the
# candidate's actual years rather than a static table.
_SENIORITY_YOE_BAND = {
    "intern": (0.0, 1.0),
    "junior": (0.0, 2.0),
    "mid": (1.5, 5.0),
    "senior": (4.0, 9.0),
    "staff": (7.0, 14.0),
    "principal": (9.0, 20.0),
    "director": (10.0, 25.0),
}


def classify_seniority(title: Optional[str]) -> str:
    """Seniority from the title. Word-boundary matched — bare 'ii'/'2' hit Hawaii/req-IDs."""
    if not title:
        return "mid"
    for level, pattern in _SENIORITY_PATTERNS:
        if pattern.search(title):
            return level
    return "mid"


def seniority_fit_multiplier(level: str, candidate_years: Optional[float]) -> float:
    """
    Penalty for YOE distance from the role's band. Unlike the old static table,
    this actually uses candidate_years, so a 2-yr and a 10-yr candidate are not
    penalised identically for a staff role.
    """
    if candidate_years is None:
        return 1.0
    low, high = _SENIORITY_YOE_BAND.get(level, (1.5, 5.0))
    if low <= candidate_years <= high:
        return 1.0
    gap = low - candidate_years if candidate_years < low else candidate_years - high
    # 0.12 per year outside the band, floored so a near-miss is not fatal.
    return max(0.45, 1.0 - 0.12 * gap)


# ── Employment type ──────────────────────────────────────────────────────────

_NON_FULLTIME_RE = re.compile(
    r"\b(contract|contractor|c2c|corp[- ]to[- ]corp|freelance|part[- ]time|"
    r"temporary|temp|internship|consultant)\b", re.I,
)


def is_full_time(employment_type: Optional[str], title: Optional[str] = None) -> bool:
    blob = f"{employment_type or ''} {title or ''}"
    return not _NON_FULLTIME_RE.search(blob)


# ── Recency ──────────────────────────────────────────────────────────────────

def recency_bonus(posted_at: Optional[str], max_bonus: float = 8.0) -> float:
    """
    Freshness bonus. Unknown posting date yields 0.0 — the old 3.0 default
    silently inflated nearly every job, since most sources never set posted_at.
    """
    if not posted_at:
        return 0.0
    from datetime import datetime, timezone

    text = str(posted_at).strip()
    parsed = None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(text[:len(fmt) + 2].rstrip("Z"), fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)

    age_days = (datetime.utcnow() - parsed).days
    if age_days < 0:
        return 0.0
    if age_days <= 3:
        return max_bonus
    if age_days <= 7:
        return max_bonus * 0.65
    if age_days <= 14:
        return max_bonus * 0.35
    if age_days <= 30:
        return max_bonus * 0.1
    return 0.0


# ── Aggregate ────────────────────────────────────────────────────────────────

@dataclass
class GateResult:
    """Outcome of all deterministic checks for one job."""
    passed: bool = True
    failures: list[str] = field(default_factory=list)
    description_quality: str = QUALITY_FULL
    required_years: Optional[float] = None
    geo_scope: str = GEO_UNKNOWN
    salary_inr: Optional[float] = None
    seniority_level: str = "mid"
    seniority_multiplier: float = 1.0
    full_time: bool = True
    recency_bonus: float = 0.0
    truncated: bool = False
    needs_enrichment: bool = False

    def as_dict(self) -> dict:
        return {
            "description_quality": self.description_quality,
            "required_years": self.required_years,
            "geo_scope": self.geo_scope,
            "salary_inr": self.salary_inr,
            "seniority_level": self.seniority_level,
            "seniority_multiplier": round(self.seniority_multiplier, 3),
            "full_time": self.full_time,
            "recency_bonus": round(self.recency_bonus, 2),
            "truncated": self.truncated,
            "failures": self.failures,
        }


def evaluate_gates(
    *,
    title: Optional[str],
    description: Optional[str],
    location: Optional[str],
    is_remote: bool,
    posted_at: Optional[str],
    employment_type: Optional[str],
    salary_min: Optional[float],
    salary_max: Optional[float],
    salary_currency: Optional[str],
    candidate_years: Optional[float],
    yoe_tolerance: float = 2.0,
    min_salary_inr: Optional[float] = None,
    allow_unknown_geo: bool = True,
) -> GateResult:
    """Run every deterministic check. Failures are recorded, never silently zeroed."""
    result = GateResult()

    result.description_quality = classify_description_quality(description)
    result.truncated = is_truncated(description)
    if result.description_quality in (QUALITY_MISSING, QUALITY_SNIPPET):
        result.passed = False
        result.needs_enrichment = True
        result.failures.append(f"description_quality={result.description_quality}")

    result.required_years = extract_required_years(description)
    if (
        result.required_years is not None
        and candidate_years is not None
        and result.required_years > candidate_years + yoe_tolerance
    ):
        result.passed = False
        result.failures.append(
            f"requires_{result.required_years:g}y_have_{candidate_years:g}y"
        )

    result.geo_scope = extract_geo_scope(description, location, is_remote)
    if result.geo_scope in (GEO_US_ONLY, GEO_EU_ONLY, GEO_OTHER_ONLY):
        result.passed = False
        result.failures.append(f"geo={result.geo_scope}")
    elif result.geo_scope == GEO_UNKNOWN and not allow_unknown_geo:
        result.passed = False
        result.failures.append("geo=unknown")

    result.salary_inr = normalize_salary_to_inr(
        salary_min, salary_max, salary_currency, description
    )
    if min_salary_inr and result.salary_inr and result.salary_inr < min_salary_inr:
        result.passed = False
        result.failures.append(f"salary_{int(result.salary_inr)}_below_{int(min_salary_inr)}")

    result.seniority_level = classify_seniority(title)
    result.seniority_multiplier = seniority_fit_multiplier(result.seniority_level, candidate_years)

    result.full_time = is_full_time(employment_type, title)
    if _INTERNSHIP_RE.search(title or ""):
        result.full_time = False

    result.recency_bonus = recency_bonus(posted_at)
    return result
