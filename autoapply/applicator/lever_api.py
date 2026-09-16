"""
Lever ATS direct application submission.
Uses Lever's public postings API apply endpoint.
"""

import requests
from pathlib import Path
from rich.console import Console

console = Console()

LEVER_APPLY_BASE = "https://api.lever.co/v0/postings"


def submit_lever_application(
    company_slug: str,
    job_id: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: str | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Submit an application via Lever's public apply API.

    Args:
        company_slug: Lever company identifier (e.g. "postman")
        job_id: Lever posting ID (UUID)
        candidate: Dict with name, email, phone, linkedin, github
        resume_path: Path to resume PDF file
        cover_letter_text: Optional cover letter text
        dry_run: If True, log but don't submit

    Returns:
        (success, message) tuple
    """
    url = f"{LEVER_APPLY_BASE}/{company_slug}/{job_id}/apply"

    if dry_run:
        console.print(f"  [dim][DRY RUN] Would POST to Lever: {url}[/dim]")
        return True, "dry_run"

    name_parts = candidate.get("name", "").split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    form_data = {
        "name": candidate.get("name", ""),
        "email": candidate.get("email", ""),
        "phone": candidate.get("phone", ""),
        "org": "",  # Current company (optional)
        "urls[LinkedIn]": candidate.get("linkedin", ""),
        "urls[GitHub]": candidate.get("github", ""),
    }

    if cover_letter_text:
        form_data["comments"] = cover_letter_text[:2000]

    files = {}
    try:
        resume_file = Path(resume_path)
        if resume_file.exists():
            mime_type = "application/pdf" if resume_path.endswith(".pdf") else "text/html"
            files["resume"] = (resume_file.name, open(resume_path, "rb"), mime_type)

        response = requests.post(
            url,
            data=form_data,
            files=files if files else None,
            timeout=30,
        )

        if response.status_code in (200, 201):
            console.print(f"  [green]✓ Lever applied:[/green] {company_slug} | Job {job_id}")
            return True, f"Applied via Lever API (status {response.status_code})"
        else:
            msg = f"HTTP {response.status_code}: {response.text[:200]}"
            console.print(f"  [yellow]Lever apply failed:[/yellow] {msg}")
            return False, msg

    except Exception as e:
        msg = f"Error: {e}"
        console.print(f"  [red]Lever error:[/red] {msg}")
        return False, msg
    finally:
        for f in files.values():
            try:
                f[1].close()
            except Exception:
                pass
