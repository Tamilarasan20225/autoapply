"""
Resume tailoring engine.
Takes master_resume.json + job description → tailored resume (JSON + HTML + PDF).
"""

import json
from pathlib import Path
from typing import Optional
from rich.console import Console

from autoapply.scoring.llm_client import LLMClient
from autoapply.scoring.prompts import RESUME_TAILOR_SYSTEM, build_resume_tailor_prompt

console = Console()


def tailor_resume(
    master_resume: dict,
    job_title: str,
    company: str,
    job_description: str,
    tailoring_variant: str,
    score_hint: str,
    llm_client: LLMClient,
    max_tokens: int = 2048,
) -> dict | None:
    """
    Use LLM to tailor resume for a specific job.

    Returns dict with:
    - chosen_summary: tailored summary text
    - experience_bullets: list of {project, bullets}
    - top_skills: most relevant skills (ordered)
    - additional_skills: other relevant skills
    """
    # Compact master resume for prompt (avoid token overflow)
    compact_resume = {
        "name": master_resume["personal"]["name"],
        "summary_variants": master_resume.get("summary_variants", {}),
        "experiences": [
            {
                "company": exp["company"],
                "role": exp["role"],
                "projects": [
                    {
                        "name": p["name"],
                        "bullets": p["bullets"]
                    }
                    for p in exp.get("projects", [])
                ]
            }
            for exp in master_resume.get("experiences", [])
        ],
        "skills": master_resume.get("skills", {}),
    }

    # Fixed: pass compact_resume as dict — build_resume_tailor_prompt handles serialization.
    # Previously: json.dumps(compact_resume)[:3000] was passed, causing double-serialization.
    prompt = build_resume_tailor_prompt(
        master_resume=compact_resume,   # Pass dict, NOT pre-serialized string
        jd=job_description,
        variant=tailoring_variant,
        score_hints=score_hint,
    )

    result = llm_client.chat_json(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=RESUME_TAILOR_SYSTEM,
        max_tokens=max_tokens,
        temperature=0.3,
    )

    if not result:
        console.print(f"[yellow]  Resume tailoring failed for {company}[/yellow]")
        return _fallback_resume(master_resume, tailoring_variant)

    # Validate structure
    if "chosen_summary" not in result:
        result["chosen_summary"] = master_resume.get("summary_variants", {}).get(tailoring_variant, "")
    if "experience_bullets" not in result:
        result["experience_bullets"] = _default_bullets(master_resume)
    if "top_skills" not in result:
        result["top_skills"] = list(master_resume.get("skills", {}).values())[:5]

    return result


def _fallback_resume(master_resume: dict, variant: str) -> dict:
    """Fallback to original resume data if LLM fails."""
    summary = master_resume.get("summary_variants", {}).get(
        variant,
        master_resume.get("summary_variants", {}).get("balanced", "")
    )
    return {
        "chosen_summary": summary,
        "experience_bullets": _default_bullets(master_resume),
        "top_skills": (
            master_resume.get("skills", {}).get("languages", []) +
            master_resume.get("skills", {}).get("frameworks", [])
        )[:6],
        "additional_skills": (
            master_resume.get("skills", {}).get("databases", []) +
            master_resume.get("skills", {}).get("ai_ml", [])
        )[:4],
    }


def _default_bullets(master_resume: dict) -> list[dict]:
    """Extract default bullets from master resume."""
    result = []
    for exp in master_resume.get("experiences", []):
        for proj in exp.get("projects", []):
            result.append({
                "project": proj["name"],
                "bullets": proj["bullets"][:4],
            })
    return result


def build_resume_html(
    master_resume: dict,
    tailored_data: dict,
    job_title: str,
    company: str,
) -> str:
    """
    Render the tailored resume as clean HTML for PDF export.
    """
    personal = master_resume["personal"]
    name = personal["name"]
    email = personal["email"]
    phone = personal["phone"]
    linkedin = personal.get("linkedin", "")
    github = personal.get("github", "")
    education = master_resume.get("education", [])
    certifications = master_resume.get("certifications", [])
    experiences_meta = master_resume.get("experiences", [])

    summary = tailored_data.get("chosen_summary", "")
    top_skills = tailored_data.get("top_skills", [])
    additional_skills = tailored_data.get("additional_skills", [])
    all_skills = top_skills + [s for s in additional_skills if s not in top_skills]

    # Build experience HTML
    exp_html = ""
    bullets_map = {
        item["project"]: item["bullets"]
        for item in tailored_data.get("experience_bullets", [])
    }

    for exp in experiences_meta:
        exp_html += f"""
        <div class="experience-item">
          <div class="exp-header">
            <span class="exp-role">{exp['role']}</span>
            <span class="exp-date">{exp.get('start', '')} – {exp.get('end', '')}</span>
          </div>
          <div class="exp-company">{exp['company']}"""
        if exp.get("division"):
            exp_html += f" &mdash; {exp['division']}"
        exp_html += f" &nbsp;|&nbsp; {exp.get('location', '')}</div>"

        for proj in exp.get("projects", []):
            proj_name = proj["name"]
            bullets = bullets_map.get(proj_name, proj.get("bullets", []))
            exp_html += f"<div class='project-name'>{proj_name}</div><ul>"
            for bullet in bullets[:5]:
                exp_html += f"<li>{bullet}</li>"
            exp_html += "</ul>"

        exp_html += "</div>"

    # Education HTML
    edu_html = ""
    for edu in education:
        edu_html += f"""
        <div class="edu-item">
          <div class="edu-header">
            <span class="edu-degree">{edu['degree']}</span>
            <span class="edu-date">{edu.get('start', '')} – {edu.get('end', '')}</span>
          </div>
          <div class="edu-institution">{edu['institution']} &nbsp;|&nbsp; CGPA: {edu.get('cgpa', '')}</div>
        </div>"""

    # Certifications
    cert_html = ""
    if certifications:
        cert_html = "<ul>"
        for cert in certifications:
            cert_html += f"<li><strong>{cert['name']}</strong> — {cert['issuer']} ({cert.get('date', '')})</li>"
        cert_html += "</ul>"

    # Skills
    skills_html = " &bull; ".join(all_skills[:14])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Arial', sans-serif; font-size: 11px; color: #222; line-height: 1.4; padding: 20px 30px; }}
  h1 {{ font-size: 20px; font-weight: bold; text-align: center; margin-bottom: 3px; }}
  .contact {{ text-align: center; font-size: 10px; color: #555; margin-bottom: 12px; }}
  .contact a {{ color: #555; text-decoration: none; }}
  h2 {{ font-size: 12px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.5px;
        border-bottom: 1px solid #333; margin: 10px 0 6px; padding-bottom: 2px; }}
  .summary {{ margin-bottom: 8px; text-align: justify; }}
  .skills-row {{ margin-bottom: 4px; }}
  .experience-item {{ margin-bottom: 10px; }}
  .exp-header {{ display: flex; justify-content: space-between; font-weight: bold; }}
  .exp-role {{ font-size: 11px; }}
  .exp-date {{ font-size: 10px; color: #555; }}
  .exp-company {{ font-size: 10px; color: #444; margin-bottom: 3px; }}
  .project-name {{ font-weight: bold; font-size: 10px; margin: 4px 0 2px; }}
  ul {{ padding-left: 16px; margin-bottom: 4px; }}
  li {{ margin-bottom: 2px; text-align: justify; }}
  .edu-item {{ margin-bottom: 6px; }}
  .edu-header {{ display: flex; justify-content: space-between; font-weight: bold; }}
  .edu-degree {{ font-size: 11px; }}
  .edu-date {{ font-size: 10px; color: #555; }}
  .edu-institution {{ font-size: 10px; color: #444; }}
</style>
</head>
<body>

<h1>{name}</h1>
<div class="contact">
  {email} &bull; {phone} &bull;
  <a href="{linkedin}">LinkedIn</a> &bull;
  <a href="{github}">GitHub</a>
</div>

<h2>Professional Summary</h2>
<div class="summary">{summary}</div>

<h2>Technical Skills</h2>
<div class="skills-row">{skills_html}</div>

<h2>Experience</h2>
{exp_html}

<h2>Education</h2>
{edu_html}

{"<h2>Certifications</h2>" + cert_html if cert_html else ""}

</body>
</html>"""

    return html


def build_plain_text_resume(
    master_resume: dict,
    tailored_data: dict,
) -> str:
    """
    Generate an ATS-friendly plain-text version of the resume.
    No formatting, no HTML — maximizes ATS parse-ability.
    """
    personal = master_resume["personal"]
    education = master_resume.get("education", [])
    certifications = master_resume.get("certifications", [])
    experiences_meta = master_resume.get("experiences", [])

    summary = tailored_data.get("chosen_summary", "")
    top_skills = tailored_data.get("top_skills", [])
    additional_skills = tailored_data.get("additional_skills", [])
    all_skills = top_skills + [s for s in additional_skills if s not in top_skills]

    bullets_map = {
        item["project"]: item["bullets"]
        for item in tailored_data.get("experience_bullets", [])
    }

    lines = []

    # Header
    lines.append(personal["name"].upper())
    lines.append(f"{personal['email']} | {personal['phone']}")
    if personal.get("linkedin"):
        lines.append(f"LinkedIn: {personal['linkedin']}")
    if personal.get("github"):
        lines.append(f"GitHub: {personal['github']}")
    lines.append("")

    # Summary
    lines.append("PROFESSIONAL SUMMARY")
    lines.append("-" * 40)
    lines.append(summary)
    lines.append("")

    # Skills
    lines.append("TECHNICAL SKILLS")
    lines.append("-" * 40)
    lines.append(", ".join(all_skills[:16]))
    lines.append("")

    # Experience
    lines.append("EXPERIENCE")
    lines.append("-" * 40)
    for exp in experiences_meta:
        lines.append(f"{exp['role']} | {exp['company']} | {exp.get('start', '')} – {exp.get('end', '')}")
        if exp.get("division"):
            lines.append(f"  {exp['division']}")
        for proj in exp.get("projects", []):
            proj_name = proj["name"]
            bullets = bullets_map.get(proj_name, proj.get("bullets", []))
            lines.append(f"  {proj_name}:")
            for bullet in bullets[:5]:
                lines.append(f"  • {bullet}")
        lines.append("")

    # Education
    lines.append("EDUCATION")
    lines.append("-" * 40)
    for edu in education:
        lines.append(f"{edu['degree']} | {edu['institution']}")
        lines.append(f"  CGPA: {edu.get('cgpa', '')} | {edu.get('start', '')} – {edu.get('end', '')}")
    lines.append("")

    # Certifications
    if certifications:
        lines.append("CERTIFICATIONS")
        lines.append("-" * 40)
        for cert in certifications:
            lines.append(f"• {cert['name']} — {cert['issuer']} ({cert.get('date', '')})")

    return "\n".join(lines)


def save_resume(
    html: str,
    company: str,
    job_title: str,
    output_dir: str = "outputs/resumes",
    plain_text: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """
    Save resume as HTML, plain text (.txt), and optionally as PDF.
    Returns (html_path, pdf_path).
    """
    safe_company = "".join(c for c in company if c.isalnum() or c in "-_")[:30]
    safe_title = "".join(c for c in job_title if c.isalnum() or c in "-_")[:20]
    filename_base = f"{safe_company}_{safe_title}"

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    html_path = output_dir / f"{filename_base}.html"
    html_path.write_text(html, encoding="utf-8")

    # Save ATS-friendly plain text version
    if plain_text:
        txt_path = output_dir / f"{filename_base}_ats.txt"
        txt_path.write_text(plain_text, encoding="utf-8")
        console.print(f"[green]  Resume TXT (ATS):[/green] {txt_path}")

    # Try PDF generation
    pdf_path = None
    try:
        from weasyprint import HTML as WP_HTML
        pdf_path = output_dir / f"{filename_base}.pdf"
        WP_HTML(string=html).write_pdf(str(pdf_path))
        console.print(f"[green]  Resume PDF:[/green] {pdf_path}")
    except ImportError:
        console.print("[yellow]  WeasyPrint not available — HTML only[/yellow]")
    except Exception as e:
        console.print(f"[yellow]  PDF generation failed:[/yellow] {e}")

    return str(html_path), str(pdf_path) if pdf_path else None
