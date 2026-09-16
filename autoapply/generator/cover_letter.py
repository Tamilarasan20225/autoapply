"""
Cover letter generator.
Produces a concise, targeted 3-paragraph cover letter for each matched job.
"""

from pathlib import Path
from rich.console import Console

from autoapply.scoring.llm_client import LLMClient
from autoapply.scoring.prompts import COVER_LETTER_SYSTEM, build_cover_letter_prompt

console = Console()


def generate_cover_letter(
    master_resume: dict,
    job_title: str,
    company: str,
    job_description: str,
    tailoring_variant: str,
    score_hint: str,
    llm_client: LLMClient,
) -> dict | None:
    """
    Generate a targeted cover letter using LLM.

    Returns dict with:
    - subject: Email subject line
    - body: Full cover letter text
    - word_count: Word count
    """
    personal = master_resume["personal"]
    candidate = {
        "name": personal["name"],
        "email": personal["email"],
        "linkedin": personal.get("linkedin", ""),
    }

    # Improved: gather top 6 bullets from the most relevant experiences
    # (was [:2] per project, [:4] total — too thin for LLM context)
    key_bullets = []
    for exp in master_resume.get("experiences", []):
        for proj in exp.get("projects", []):
            key_bullets.extend(proj.get("bullets", [])[:3])
    key_experience = " | ".join(key_bullets[:6])[:700]

    prompt = build_cover_letter_prompt(
        candidate=candidate,
        company=company,
        role=job_title,
        jd=job_description,
        key_experience=key_experience,
        tailoring_hint=score_hint,
    )

    result = llm_client.chat_json(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=COVER_LETTER_SYSTEM,
        max_tokens=1024,
        temperature=0.4,
    )

    if not result:
        console.print(f"[yellow]  Cover letter generation failed for {company}[/yellow]")
        return _fallback_cover_letter(master_resume, company, job_title)

    # Ensure word_count is present
    body = result.get("body", "")
    if "word_count" not in result:
        result["word_count"] = len(body.split())

    return result


def _fallback_cover_letter(master_resume: dict, company: str, job_title: str) -> dict:
    """Minimal fallback cover letter if LLM fails."""
    personal = master_resume["personal"]
    name = personal["name"]
    summary = master_resume.get("summary_variants", {}).get("balanced", "")

    body = f"""Dear Hiring Manager,

I am writing to express my interest in the {job_title} role at {company}. {summary[:200]}

During my time at Zoho Corporation, I architected distributed backend pipelines processing 1M+ web pages daily, engineered multilingual search infrastructure scaling to 200M+ records, and built LLM-powered extraction systems improving data accuracy by 30%. These experiences have given me a strong foundation in building reliable, high-throughput systems at scale.

I would welcome the opportunity to discuss how my background aligns with your team's goals. Thank you for considering my application.

Best regards,
{name}"""

    return {
        "subject": f"Application for {job_title} — {name}",
        "body": body,
        "word_count": len(body.split()),
    }


def save_cover_letter(
    cover_letter: dict,
    company: str,
    job_title: str,
    output_dir: str = "outputs/cover_letters",
) -> str:
    """
    Save cover letter as both plain text (.txt) and formatted HTML (.html).
    Returns the plain text file path (for ATS compatibility).
    """
    safe_company = "".join(c for c in company if c.isalnum() or c in "-_")[:30]
    safe_title = "".join(c for c in job_title if c.isalnum() or c in "-_")[:20]
    base_name = f"{safe_company}_{safe_title}_cover_letter"

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    subject = cover_letter.get("subject", "")
    body = cover_letter.get("body", "")

    # ── Plain text (for direct copy-paste / ATS upload) ───────────────────
    txt_path = output_dir / f"{base_name}.txt"
    content = f"Subject: {subject}\n\n{body}"
    txt_path.write_text(content, encoding="utf-8")

    # ── HTML version (for email / formatted view) ─────────────────────────
    html_body = body.replace("\n\n", "</p><p>").replace("\n", "<br>")
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: Georgia, serif; font-size: 13px; color: #222;
         line-height: 1.7; max-width: 700px; margin: 40px auto; padding: 0 20px; }}
  .subject {{ font-size: 11px; color: #666; margin-bottom: 20px; }}
  p {{ margin-bottom: 12px; text-align: justify; }}
</style>
</head>
<body>
<div class="subject"><strong>Subject:</strong> {subject}</div>
<p>{html_body}</p>
</body>
</html>"""

    html_path = output_dir / f"{base_name}.html"
    html_path.write_text(html_content, encoding="utf-8")

    console.print(f"[green]  Cover letter (TXT + HTML):[/green] {base_name}")
    return str(txt_path)
