"""
Greenhouse ATS direct application submission.
Fixed: file handles are tracked explicitly and always closed (was leaking).
"""
import requests
from pathlib import Path
from rich.console import Console

console = Console()
GREENHOUSE_APPLY_BASE = "https://boards-api.greenhouse.io/v1/boards"


def submit_greenhouse_application(
    company_slug: str,
    job_id: str,
    candidate: dict,
    resume_path: str,
    cover_letter_path: str | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    url = f"{GREENHOUSE_APPLY_BASE}/{company_slug}/jobs/{job_id}/apply"
    if dry_run:
        console.print(f"  [dim][DRY RUN] Would POST to Greenhouse: {url}[/dim]")
        return True, "dry_run"

    name_parts = candidate.get("name", "").split(" ", 1)
    form_data = {
        "first_name": name_parts[0],
        "last_name": name_parts[1] if len(name_parts) > 1 else "",
        "email": candidate.get("email", ""),
        "phone": candidate.get("phone", ""),
        "job_id": job_id,
    }
    if candidate.get("linkedin"):
        form_data["linkedin_profile_url"] = candidate["linkedin"]
    if candidate.get("github"):
        form_data["website"] = candidate["github"]

    # Track file handles to always close them (was leaking before)
    open_handles = []
    files = {}
    try:
        resume_file = Path(resume_path)
        if resume_file.exists():
            mime = "application/pdf" if str(resume_path).endswith(".pdf") else "text/html"
            fh = open(resume_path, "rb")
            open_handles.append(fh)
            files["resume"] = (resume_file.name, fh, mime)

        if cover_letter_path:
            cl_file = Path(cover_letter_path)
            if cl_file.exists():
                fh_cl = open(cover_letter_path, "rb")
                open_handles.append(fh_cl)
                files["cover_letter"] = (cl_file.name, fh_cl, "text/plain")

        response = requests.post(url, data=form_data, files=files or None, timeout=30)

        if response.status_code in (200, 201):
            console.print(f"  [green]\u2713 Greenhouse API applied:[/green] {company_slug}/{job_id}")
            return True, f"Applied via Greenhouse API (HTTP {response.status_code})"
        elif response.status_code == 404:
            return False, "custom_portal_404"
        else:
            return False, f"Greenhouse API HTTP {response.status_code}: {response.text[:200]}"
    except Exception as e:
        return False, f"Greenhouse API error: {e}"
    finally:
        for fh in open_handles:
            try:
                fh.close()
            except Exception:
                pass
