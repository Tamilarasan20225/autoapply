"""
Resume ingestion for multi-user support.

Accepts a raw upload (.json matching master_resume.json's schema, or a
.pdf/.docx which is parsed to text and then structured into the same schema
via one LLM call) and returns BOTH the structured resume AND an auto-derived
UserProfile (domain, target roles, keywords, scoring thresholds).

Zero manual configuration: upload a resume → everything is derived automatically.
"""

import json
from typing import Optional

from rich.console import Console

from autoapply.scoring.llm_client import LLMClient

console = Console()

REQUIRED_TOP_LEVEL_KEYS = {"personal", "summary_variants", "experiences", "skills"}

RESUME_SCHEMA_HINT = """
{
  "personal": {"name": "", "email": "", "phone": "", "linkedin": "", "github": "", "location": ""},
  "summary_variants": {"backend": "", "ai_ml": "", "balanced": "", "data_infra": ""},
  "experiences": [
    {
      "company": "", "role": "", "start": "", "end": "", "location": "",
      "projects": [{"name": "", "bullets": ["..."], "tags": ["..."]}]
    }
  ],
  "skills": {
    "languages": [], "frameworks": [], "ai_ml": [], "databases": [],
    "search": [], "apis_protocols": [], "devops_tools": [], "other": []
  },
  "education": [{"degree": "", "institution": "", "cgpa": "", "start": "", "end": ""}],
  "certifications": [{"name": "", "issuer": "", "date": ""}],
  "meta": {"target_roles": [], "domain": "", "total_experience_years": 0}
}
""".strip()

PARSE_SYSTEM_PROMPT = (
    "You are a resume parser. Convert the given resume text into a single compact "
    "JSON object matching the exact schema provided. Infer missing fields conservatively "
    "(do not invent employers, dates, or metrics). Respond with ONLY the JSON object, "
    "no markdown, no commentary."
)

# ── User Profile derivation ───────────────────────────────────────────────────

PROFILE_SCHEMA_HINT = """
{
  "domain": "backend",
  "years_experience": 3.0,
  "target_roles": ["Software Engineer", "Backend Engineer"],
  "strong_keywords": ["python", "java", "rest api"],
  "locations": ["Bangalore", "Remote", "India"],
  "exclude_companies": [],
  "auto_apply_threshold": 68,
  "review_threshold": 52,
  "tfidf_threshold": 0.10,
  "seniority": "mid"
}
"""

PROFILE_SYSTEM_PROMPT = (
    "You are a career advisor and technical recruiter. Analyse the candidate's resume and derive "
    "their job search profile. Respond ONLY with a JSON object — no commentary, no markdown, no code block."
)


class ResumeParseError(ValueError):
    """Raised when an uploaded resume can't be parsed or is missing required fields."""


def _extract_text_from_pdf(file_bytes: bytes) -> str:
    from pypdf import PdfReader
    import io
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_text_from_docx(file_bytes: bytes) -> str:
    import docx
    import io
    doc = docx.Document(io.BytesIO(file_bytes))
    return "\n".join(p.text for p in doc.paragraphs)


def _validate_schema(data: dict) -> None:
    missing = REQUIRED_TOP_LEVEL_KEYS - set(data.keys())
    if missing:
        raise ResumeParseError(f"Resume JSON is missing required keys: {sorted(missing)}")


def _heuristic_profile(resume_json: dict) -> dict:
    """
    Derive a user profile from the resume without an LLM call.
    Used as fallback when LLM is unavailable.
    """
    skills_flat = []
    for cat_items in resume_json.get("skills", {}).values():
        if isinstance(cat_items, list):
            skills_flat.extend(str(s).lower() for s in cat_items)
    skills_text = " ".join(skills_flat)

    # Detect domain from skills keywords
    if any(kw in skills_text for kw in ["embedded", "rtos", "firmware", "autosar", "can bus", "microcontroller", "fpga"]):
        domain = "embedded_testing"
    elif any(kw in skills_text for kw in ["selenium", "appium", "testng", "test automation", "istqb", "sdet"]):
        domain = "embedded_testing"  # testing falls under same bucket
    elif any(kw in skills_text for kw in ["pytorch", "tensorflow", "llm", "nlp", "deep learning", "rag", "hugging"]):
        domain = "ai_ml"
    elif any(kw in skills_text for kw in ["spark", "airflow", "dbt", "bigquery", "snowflake", "etl", "databricks"]):
        domain = "data"
    elif any(kw in skills_text for kw in ["react", "vue", "angular", "frontend", "next.js", "svelte"]):
        domain = "frontend"
    else:
        domain = "backend"

    target_roles = resume_json.get("meta", {}).get("target_roles", ["Software Engineer"])
    location = resume_json.get("personal", {}).get("location", "India")
    locations = ["Remote", "India"]
    if "bangalore" in location.lower() or "bengaluru" in location.lower():
        locations = ["Bangalore", "Remote", "India"]

    years = resume_json.get("meta", {}).get("total_experience_years", 3.0)
    try:
        years = float(str(years).replace("+", "").strip())
    except (ValueError, TypeError):
        years = 3.0

    # Seniority from years
    if years < 2:
        seniority = "junior"
    elif years < 5:
        seniority = "mid"
    else:
        seniority = "senior"

    return {
        "domain": domain,
        "years_experience": years,
        "roles": target_roles[:8],
        "strong_keywords": [s for s in skills_flat[:20] if len(s) > 2],
        "locations": locations,
        "exclude_companies": [],
        "auto_apply_threshold": 65.0 if domain == "embedded_testing" else 68.0,
        "review_threshold": 50.0,
        "tfidf_threshold": 0.08 if domain == "embedded_testing" else 0.10,
        "seniority": seniority,
    }


def derive_user_profile(resume_json: dict, llm_client: LLMClient) -> dict:
    """
    Auto-derive job search profile from a parsed resume via LLM.

    Returns a search_config dict with:
    - domain: embedded_testing | backend | data | ai_ml | frontend | general
    - years_experience: float
    - target_roles: list of job titles to search for
    - strong_keywords: technical skills for discovery queries
    - locations: preferred job locations
    - exclude_companies: blocklist
    - auto_apply_threshold: float (per-user)
    - review_threshold: float (per-user)
    - tfidf_threshold: float (per-user)
    - seniority: junior | mid | senior
    """
    personal = resume_json.get("personal", {})
    experiences = resume_json.get("experiences", [])
    skills = resume_json.get("skills", {})
    meta = resume_json.get("meta", {})

    # Build concise experience summary for the prompt
    exp_text = []
    for exp in experiences[:4]:
        role = exp.get("role", "")
        company = exp.get("company", "")
        start = exp.get("start", "")
        end = exp.get("end", "Present")
        exp_text.append(f"- {role} at {company} ({start}–{end})")
        for proj in exp.get("projects", [])[:2]:
            bullets = proj.get("bullets", [])
            if bullets:
                exp_text.append(f"  → {bullets[0][:120]}")

    skills_flat = []
    for cat_items in skills.values():
        if isinstance(cat_items, list):
            skills_flat.extend(str(s) for s in cat_items)

    summary_text = resume_json.get("summary_variants", {}).get("balanced", "")[:300]
    location = personal.get("location", "")

    prompt = (
        f"Candidate resume:\n"
        f"Name: {personal.get('name', 'Unknown')}\n"
        f"Location: {location}\n"
        f"Summary: {summary_text}\n"
        f"Skills: {', '.join(skills_flat[:30])}\n"
        f"Experience:\n" + "\n".join(exp_text[:10]) + "\n\n"
        f"Target roles stated (if any): {', '.join(meta.get('target_roles', []))}\n\n"
        f"Derive the job search profile. Rules:\n"
        f"- domain: choose EXACTLY one of: backend, embedded_testing, data, ai_ml, frontend, general\n"
        f"- embedded_testing covers BOTH embedded systems engineering AND software QA/testing roles\n"
        f"- years_experience: calculate total from all experience dates\n"
        f"- target_roles: 5-8 specific job titles this candidate should apply to\n"
        f"- strong_keywords: 10-15 technical skills/tools from their resume (for search queries)\n"
        f"- locations: derive from candidate location (include 'Remote' and 'India' always)\n"
        f"- tfidf_threshold: 0.07 for embedded_testing, 0.10 for backend/data, 0.12 for frontend\n"
        f"- auto_apply_threshold: 65 for embedded_testing (harder to match), 68 for others\n"
        f"- review_threshold: 50 for embedded_testing, 53 for others\n"
        f"- seniority: junior (<2 yrs), mid (2-5 yrs), senior (5+ yrs)\n\n"
        f"Schema:\n{PROFILE_SCHEMA_HINT}\n\n"
        f"Respond with ONLY the JSON object."
    )

    result = llm_client.chat_json(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=PROFILE_SYSTEM_PROMPT,
        max_tokens=800,
        temperature=0.1,
    )

    if not result:
        console.print("[yellow]Profile derivation LLM call failed — using heuristic fallback[/yellow]")
        return _heuristic_profile(resume_json)

    # Validate and sanitise the result
    valid_domains = {"backend", "embedded_testing", "data", "ai_ml", "frontend", "general"}
    domain = result.get("domain", "general")
    if domain not in valid_domains:
        domain = "general"

    # Ensure India/remote always in locations
    locations = result.get("locations", [])
    if not locations:
        lc = location.lower()
        locations = ["Bangalore", "Remote", "India"] if ("bangalore" in lc or "bengaluru" in lc) else ["Remote", "India"]
    if "India" not in locations:
        locations.append("India")
    if "Remote" not in locations:
        locations.append("Remote")

    profile = {
        "domain": domain,
        "years_experience": float(result.get("years_experience", 3.0)),
        "roles": result.get("target_roles", [])[:10],
        "strong_keywords": result.get("strong_keywords", [])[:20],
        "locations": locations,
        "exclude_companies": result.get("exclude_companies", []),
        "auto_apply_threshold": float(result.get("auto_apply_threshold", 68)),
        "review_threshold": float(result.get("review_threshold", 52)),
        "tfidf_threshold": float(result.get("tfidf_threshold", 0.10)),
        "seniority": result.get("seniority", "mid"),
    }
    console.print(
        f"[green]Profile derived:[/green] domain={profile['domain']}, "
        f"{profile['years_experience']:.0f} yrs, seniority={profile['seniority']}, "
        f"auto_apply≥{profile['auto_apply_threshold']}"
    )
    return profile


def parse_resume_file(
    file_bytes: bytes,
    filename: str,
    llm_client: Optional[LLMClient] = None,
) -> tuple[dict, dict]:
    """
    Parse an uploaded resume file into (resume_json, user_profile).

    - .json: loaded directly and schema-validated; profile derived via LLM
    - .pdf/.docx: text extracted, structured via LLM, then profile derived

    Returns:
        (resume_json, user_profile) where:
        - resume_json follows master_resume.json schema
        - user_profile contains domain, roles, keywords, thresholds (auto-derived)

    Raises ResumeParseError on invalid/unsupported input.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    client = llm_client or LLMClient()

    if ext == "json":
        try:
            data = json.loads(file_bytes.decode("utf-8"))
        except Exception as e:
            raise ResumeParseError(f"Invalid JSON file: {e}") from e
        _validate_schema(data)
        profile = derive_user_profile(data, client) if client.available else _heuristic_profile(data)
        return data, profile

    if ext == "pdf":
        text = _extract_text_from_pdf(file_bytes)
    elif ext == "docx":
        text = _extract_text_from_docx(file_bytes)
    else:
        raise ResumeParseError(f"Unsupported file type: .{ext} (use .json, .pdf, or .docx)")

    text = text.strip()
    if len(text) < 100:
        raise ResumeParseError("Could not extract enough text from the file to parse a resume")

    if not client.available:
        raise ResumeParseError("No LLM provider configured — cannot parse PDF/DOCX resumes without an LLM call")

    # Step 1: Parse resume text into structured schema
    prompt = (
        f"Resume text:\n---\n{text[:8000]}\n---\n\n"
        f"Target JSON schema:\n{RESUME_SCHEMA_HINT}\n\n"
        "Fill the schema from the resume text above. For summary_variants, write four short "
        "(2-3 sentence) professional summaries tailored to the candidate's ACTUAL domain "
        "(e.g. if they are an embedded engineer, write embedded-focused summaries). "
        "Use the keys: backend, ai_ml, balanced, data_infra — but make each relevant to what "
        "the candidate actually does. For meta.target_roles, list the job titles they should apply to. "
        "For meta.domain, set to: backend | embedded_testing | data | ai_ml | frontend | general."
    )
    result = client.chat_json(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=PARSE_SYSTEM_PROMPT,
        max_tokens=2048,
        temperature=0.2,
    )
    if not result:
        raise ResumeParseError("LLM failed to parse the resume — try again or upload a JSON resume instead")

    _validate_schema(result)

    # Step 2: Derive user profile (second LLM call)
    profile = derive_user_profile(result, client)

    return result, profile
