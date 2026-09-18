"""
Resume ingestion for multi-resume support.

Accepts a raw upload (.json matching master_resume.json's schema, or a
.pdf/.docx which is parsed to text and then structured into the same schema
via one LLM call) and returns a dict ready to store as Resume.resume_json.
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
  "meta": {"target_roles": []}
}
""".strip()

PARSE_SYSTEM_PROMPT = (
    "You are a resume parser. Convert the given resume text into a single compact "
    "JSON object matching the exact schema provided. Infer missing fields conservatively "
    "(do not invent employers, dates, or metrics). Respond with ONLY the JSON object, "
    "no markdown, no commentary."
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


def parse_resume_file(
    file_bytes: bytes,
    filename: str,
    llm_client: Optional[LLMClient] = None,
) -> dict:
    """
    Parse an uploaded resume file into the master_resume.json schema.

    - .json: loaded directly and schema-validated (no LLM call).
    - .pdf/.docx: text extracted, then structured via one LLM call.

    Raises ResumeParseError on invalid/unsupported input.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext == "json":
        try:
            data = json.loads(file_bytes.decode("utf-8"))
        except Exception as e:
            raise ResumeParseError(f"Invalid JSON file: {e}") from e
        _validate_schema(data)
        return data

    if ext == "pdf":
        text = _extract_text_from_pdf(file_bytes)
    elif ext == "docx":
        text = _extract_text_from_docx(file_bytes)
    else:
        raise ResumeParseError(f"Unsupported file type: .{ext} (use .json, .pdf, or .docx)")

    text = text.strip()
    if len(text) < 100:
        raise ResumeParseError("Could not extract enough text from the file to parse a resume")

    client = llm_client or LLMClient()
    if not client.available:
        raise ResumeParseError("No LLM provider configured — cannot parse PDF/DOCX resumes without an LLM call")

    prompt = (
        f"Resume text:\n---\n{text[:8000]}\n---\n\n"
        f"Target JSON schema:\n{RESUME_SCHEMA_HINT}\n\n"
        "Fill the schema from the resume text above. For summary_variants, write four short "
        "(2-3 sentence) professional summaries tailored to: backend, ai_ml, balanced, data_infra "
        "roles, grounded only in the candidate's actual experience."
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
    return result
