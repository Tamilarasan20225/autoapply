"""
Ashby ATS Applicator — applies via Ashby's public candidate API.

Ashby is used by: Linear, Cal.com, Resend, Turso, Raycast, Descript, Ramp, Mercury,
Vercel, Loom, Retool, and many top startups.

Public API (no auth required for candidates):
  POST https://api.ashbyhq.com/posting-api/application/create

This is the cleanest Phase 2 integration — Ashby exposes a proper candidate API
unlike Workday/iCIMS which are purely form-based.
"""

import base64
import json
import mimetypes
import requests
from pathlib import Path
from typing import Optional
from rich.console import Console

console = Console()

ASHBY_API_BASE = "https://api.ashbyhq.com/posting-api"


def _encode_resume(resume_path: str) -> Optional[dict]:
    """
    Encode resume file as base64 for Ashby API.

    Returns dict with {content, name, mediaType} or None if file not found.
    """
    path = Path(resume_path)
    if not path.exists():
        console.print(f"  [yellow]Resume file not found: {resume_path}[/yellow]")
        return None

    mime_type, _ = mimetypes.guess_type(str(path))
    if not mime_type:
        # Default by extension
        ext = path.suffix.lower()
        mime_map = {
            ".pdf": "application/pdf",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".html": "text/html",
            ".txt": "text/plain",
        }
        mime_type = mime_map.get(ext, "application/octet-stream")

    with open(path, "rb") as f:
        content = base64.b64encode(f.read()).decode("utf-8")

    return {
        "content": content,
        "name": path.name,
        "mediaType": mime_type,
    }


def _fetch_job_posting(company_slug: str, job_id: str) -> Optional[dict]:
    """
    Fetch a specific Ashby job posting to get its application form schema.
    This tells us what custom questions the form has.
    """
    try:
        url = f"{ASHBY_API_BASE}/job-board/{company_slug}"
        response = requests.post(
            url,
            json={"limit": 200, "includeCompensation": True},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if response.status_code != 200:
            return None

        data = response.json()
        for posting in data.get("jobPostings", []):
            if posting.get("id") == job_id:
                return posting

    except Exception as e:
        console.print(f"  [dim]Could not fetch Ashby posting: {e}[/dim]")

    return None


def _fetch_application_form(company_slug: str, job_id: str) -> Optional[dict]:
    """
    Fetch the application form schema for a specific Ashby job posting.
    This gives us the list of required/optional fields.
    """
    try:
        url = f"{ASHBY_API_BASE}/job-board/{company_slug}/application-form"
        response = requests.post(
            url,
            json={"jobPostingId": job_id},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        console.print(f"  [dim]Could not fetch Ashby form schema: {e}[/dim]")

    return None


def _build_application_form_data(
    form_schema: Optional[dict],
    candidate: dict,
    cover_letter_text: Optional[str] = None,
) -> list:
    """
    Build applicationForm list from form schema and candidate data.

    Handles required fields: name, email, phone, linkedin, github,
    location, work_authorization, salary_expectation, etc.
    """
    form_data = []

    # If no schema, build basic required fields
    if not form_schema:
        return [
            {"path": "name.firstName", "value": candidate.get("name", "").split()[0]},
            {"path": "name.lastName", "value": candidate.get("name", "").split()[-1]},
            {"path": "email", "value": candidate.get("email", "")},
            {"path": "phone", "value": candidate.get("phone", "")},
        ]

    fields = form_schema.get("applicationFormDefinition", {}).get("sections", [])

    for section in fields:
        for field in section.get("fields", []):
            field_path = field.get("path", "")
            field_type = field.get("type", "")
            field_label = field.get("title", "").lower()
            is_required = field.get("isRequired", False)

            value = None

            # Map common field paths to candidate data
            if "firstName" in field_path or "first_name" in field_path:
                name_parts = candidate.get("name", "").split()
                value = name_parts[0] if name_parts else ""

            elif "lastName" in field_path or "last_name" in field_path:
                name_parts = candidate.get("name", "").split()
                value = name_parts[-1] if len(name_parts) > 1 else ""

            elif "email" in field_path:
                value = candidate.get("email", "")

            elif "phone" in field_path:
                value = candidate.get("phone", "")

            elif "linkedin" in field_label or "linkedIn" in field_path:
                value = candidate.get("linkedin", "")

            elif "github" in field_label or "portfolio" in field_label:
                value = candidate.get("github", candidate.get("portfolio", ""))

            elif "website" in field_label or "url" in field_label:
                value = candidate.get("website", candidate.get("github", ""))

            elif "cover" in field_label and cover_letter_text:
                value = cover_letter_text[:3000]  # Ashby has char limit

            elif "location" in field_label or "city" in field_label:
                value = candidate.get("location", "Bangalore, India")

            elif "authorized" in field_label or "work auth" in field_label:
                # Work authorization
                if field_type in ("Boolean", "YesNo"):
                    value = True
                else:
                    value = "Yes"

            elif "salary" in field_label or "compensation" in field_label:
                value = candidate.get("expected_salary", "")

            elif "sponsor" in field_label or "visa" in field_label:
                if field_type in ("Boolean", "YesNo"):
                    value = False  # Don't need sponsorship
                else:
                    value = "No"

            elif "experience" in field_label and "year" in field_label:
                value = str(candidate.get("years_of_experience", "3"))

            elif "hear" in field_label or "source" in field_label:
                value = "Job Board"

            # Only add if we have a value and field is required or we have data
            if value is not None and (is_required or value):
                form_data.append({"path": field_path, "value": value})

    return form_data


def apply_ashby_api(
    company_slug: str,
    job_id: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str] = None,
    cover_letter_path: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Apply to an Ashby job via the public candidate API.

    Args:
        company_slug: Ashby company slug (e.g. "linear", "cal")
        job_id: Ashby job posting ID (UUID)
        candidate: Candidate profile dict
        resume_path: Path to resume file (PDF preferred)
        cover_letter_text: Cover letter text content
        cover_letter_path: Path to cover letter file (optional)
        dry_run: If True, validate but don't submit

    Returns:
        (success, message) tuple
    """
    if dry_run:
        console.print(f"  [dim][DRY RUN] Would apply to Ashby: {company_slug}/{job_id}[/dim]")
        return True, "dry_run"

    # ── Step 1: Fetch form schema ─────────────────────────────────────────────
    console.print(f"  [dim]Fetching Ashby application form schema...[/dim]")
    form_schema = _fetch_application_form(company_slug, job_id)

    # ── Step 2: Build application form data ───────────────────────────────────
    form_data = _build_application_form_data(form_schema, candidate, cover_letter_text)
    console.print(f"  [dim]Prepared {len(form_data)} form fields[/dim]")

    # ── Step 3: Encode resume ─────────────────────────────────────────────────
    resume_file = _encode_resume(resume_path)
    if not resume_file:
        # Try alternate paths
        for ext in [".pdf", ".html", ".txt"]:
            alt_path = str(resume_path).rsplit(".", 1)[0] + ext
            resume_file = _encode_resume(alt_path)
            if resume_file:
                break

    # ── Step 4: Build request payload ────────────────────────────────────────
    payload: dict = {
        "jobPostingId": job_id,
        "applicationForm": form_data,
    }

    # Add resume if available
    if resume_file:
        payload["resume"] = resume_file
        console.print(f"  [dim]Resume encoded: {resume_file['name']} ({resume_file['mediaType']})[/dim]")

    # Add cover letter as a separate file if provided
    if cover_letter_path:
        cl_file = _encode_resume(cover_letter_path)
        if cl_file:
            payload["coverLetter"] = cl_file

    # ── Step 5: Submit application ────────────────────────────────────────────
    apply_url = f"{ASHBY_API_BASE}/application/create"

    console.print(f"  [dim]Submitting to Ashby API: {company_slug}/{job_id}...[/dim]")

    try:
        response = requests.post(
            apply_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; JobApplicant/1.0)",
            },
            timeout=30,
        )

        # ── Parse response ─────────────────────────────────────────────────
        if response.status_code in (200, 201):
            data = response.json()

            # Ashby returns {"success": true, "applicationId": "..."}
            if data.get("success") or data.get("applicationId"):
                app_id = data.get("applicationId", "unknown")
                console.print(f"  [green]✓ Applied via Ashby API (applicationId: {app_id})[/green]")
                return True, f"Applied via Ashby API (applicationId: {app_id})"

            # Check for errors in 200 response
            errors = data.get("errors", [])
            if errors:
                error_msg = "; ".join(str(e) for e in errors[:3])
                console.print(f"  [yellow]Ashby API errors: {error_msg}[/yellow]")
                return False, f"Ashby API validation errors: {error_msg}"

            # Success without explicit confirmation field
            console.print(f"  [green]✓ Applied via Ashby API[/green]")
            return True, "Applied via Ashby API"

        elif response.status_code == 400:
            # Validation error — try to parse the error
            try:
                error_data = response.json()
                error_msg = error_data.get("message", error_data.get("error", str(error_data)))
                console.print(f"  [yellow]Ashby validation error: {error_msg}[/yellow]")
                return False, f"Ashby validation error: {error_msg}"
            except Exception:
                return False, f"Ashby API returned 400: {response.text[:200]}"

        elif response.status_code == 404:
            return False, f"Ashby job posting not found: {company_slug}/{job_id}"

        elif response.status_code == 422:
            # Unprocessable entity — usually a form field issue
            try:
                error_data = response.json()
                console.print(f"  [yellow]Ashby form error (422): {error_data}[/yellow]")
                return False, f"Ashby form error: {error_data.get('message', 'Unprocessable entity')}"
            except Exception:
                return False, f"Ashby API returned 422"

        else:
            console.print(f"  [yellow]Ashby API returned {response.status_code}[/yellow]")
            return False, f"Ashby API error: HTTP {response.status_code}"

    except requests.exceptions.Timeout:
        return False, "Ashby API request timed out (30s)"
    except requests.exceptions.ConnectionError as e:
        return False, f"Ashby API connection failed: {e}"
    except Exception as e:
        return False, f"Ashby API unexpected error: {e}"


def apply_ashby_playwright(
    company_slug: str,
    job_id: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str] = None,
    headless: bool = True,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Fallback: Apply via Ashby's web form using Playwright.
    Used when the API returns errors or custom questions can't be answered via API.

    Ashby form URL: https://jobs.ashbyhq.com/{slug}/{jobId}/application
    """
    import asyncio

    async def _apply():
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return False, "Playwright not installed"

        apply_url = f"https://jobs.ashbyhq.com/{company_slug}/{job_id}/application"

        if dry_run:
            console.print(f"  [dim][DRY RUN] Would open Ashby form: {apply_url}[/dim]")
            return True, "dry_run"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            try:
                import random

                async def delay(a=1.0, b=3.0):
                    await asyncio.sleep(random.uniform(a, b))

                console.print(f"  [dim]Opening Ashby form: {apply_url}[/dim]")
                await page.goto(apply_url, timeout=30000)
                await delay(2, 4)

                # Wait for form to load
                try:
                    await page.wait_for_selector(
                        'input[name="email"], input[type="email"], '
                        '[data-testid*="email"], [placeholder*="email"]',
                        timeout=15000,
                    )
                except Exception:
                    content = await page.content()
                    if "not found" in content.lower() or "404" in content:
                        return False, f"Ashby form not found: {apply_url}"
                    return False, "Ashby form fields not detected"

                # Fill name
                name_parts = candidate.get("name", "").split(" ", 1)
                first = name_parts[0]
                last = name_parts[1] if len(name_parts) > 1 else ""

                for sel in ['input[name="firstName"]', '[placeholder*="First"]', 'input[autocomplete*="given"]']:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.fill(first)
                            break
                    except Exception:
                        pass

                for sel in ['input[name="lastName"]', '[placeholder*="Last"]', 'input[autocomplete*="family"]']:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.fill(last)
                            break
                    except Exception:
                        pass

                # Fill email
                for sel in ['input[name="email"]', 'input[type="email"]', '[placeholder*="email"]']:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.fill(candidate.get("email", ""))
                            break
                    except Exception:
                        pass

                # Fill phone
                for sel in ['input[name="phone"]', 'input[type="tel"]', '[placeholder*="phone"]']:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.fill(candidate.get("phone", ""))
                            break
                    except Exception:
                        pass

                # Fill LinkedIn
                if candidate.get("linkedin"):
                    for sel in ['input[name="linkedin"]', '[placeholder*="LinkedIn"]', 'input[name*="linkedin"]']:
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                await el.fill(candidate["linkedin"])
                                break
                        except Exception:
                            pass

                # Fill GitHub / Portfolio
                if candidate.get("github"):
                    for sel in ['input[name="github"]', '[placeholder*="GitHub"]', '[placeholder*="Portfolio"]']:
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                await el.fill(candidate["github"])
                                break
                        except Exception:
                            pass

                # Cover letter textarea
                if cover_letter_text:
                    for sel in ['textarea[name*="cover"]', 'textarea[placeholder*="cover"]', 'textarea']:
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                await el.fill(cover_letter_text[:3000])
                                break
                        except Exception:
                            pass

                await delay(0.5, 1)

                # Upload resume
                resume_file = Path(resume_path)
                if resume_file.exists():
                    # Ashby may have a drag-drop zone or an upload button
                    try:
                        # Try direct file input first
                        file_input = await page.query_selector('input[type="file"]')
                        if file_input:
                            await file_input.set_input_files(str(resume_file))
                        else:
                            # Try clicking upload button to reveal input
                            upload_btn = await page.query_selector(
                                'button:has-text("Upload"), button:has-text("Attach"), '
                                'label:has-text("Upload"), [role="button"]:has-text("Upload")'
                            )
                            if upload_btn:
                                async with page.expect_file_chooser() as fc_info:
                                    await upload_btn.click()
                                fc = await fc_info.value
                                await fc.set_files(str(resume_file))

                        await delay(1, 2)
                        console.print(f"  [dim]Resume uploaded: {resume_file.name}[/dim]")
                    except Exception as e:
                        console.print(f"  [yellow]Resume upload failed: {e}[/yellow]")

                await delay(1, 2)

                # Handle dropdowns / select fields (Yes/No, location)
                try:
                    selects = await page.query_selector_all("select")
                    for sel in selects:
                        options = await sel.query_selector_all("option")
                        opt_texts = []
                        for opt in options:
                            txt = await opt.text_content()
                            opt_texts.append((txt or "").strip())

                        for txt in opt_texts:
                            if txt.lower() in ("yes", "india", "bangalore", "remote"):
                                await sel.select_option(label=txt)
                                break
                except Exception:
                    pass

                # Handle Yes/No radio buttons
                try:
                    radios = await page.query_selector_all('input[type="radio"][value="Yes"], input[type="radio"][value="yes"], input[type="radio"][value="true"]')
                    for radio in radios:
                        await radio.check()
                        await delay(0.2, 0.4)
                except Exception:
                    pass

                # Multi-step navigation: click through steps
                max_steps = 6
                for step in range(max_steps):
                    await delay(0.5, 1)

                    # Check for Submit button
                    submit_btn = None
                    for sub_sel in [
                        'button[type="submit"]:has-text("Submit")',
                        'button:has-text("Submit Application")',
                        'button:has-text("Submit")',
                        '[data-testid="submit-application-button"]',
                    ]:
                        try:
                            btn = await page.query_selector(sub_sel)
                            if btn:
                                is_disabled = await btn.get_attribute("disabled")
                                if not is_disabled:
                                    submit_btn = btn
                                    break
                        except Exception:
                            pass

                    if submit_btn:
                        console.print(f"  [dim]Submitting application...[/dim]")
                        await submit_btn.click()
                        await delay(3, 5)

                        # Check success
                        content = await page.content()
                        if any(p in content.lower() for p in [
                            "thank you", "application submitted", "successfully", "received your application"
                        ]):
                            console.print(f"  [green]✓ Ashby Playwright applied[/green]")
                            return True, "Applied via Ashby Playwright"
                        else:
                            return True, f"Submitted via Ashby Playwright (verify at {page.url})"

                    # Look for Next button
                    next_btn = None
                    for next_sel in [
                        'button:has-text("Next")', 'button:has-text("Continue")',
                        '[data-testid="next-button"]', 'button[type="button"]:has-text("Next")',
                    ]:
                        try:
                            btn = await page.query_selector(next_sel)
                            if btn:
                                is_disabled = await btn.get_attribute("disabled")
                                if not is_disabled:
                                    next_btn = btn
                                    break
                        except Exception:
                            pass

                    if next_btn:
                        await next_btn.click()
                        await delay(1.5, 2.5)
                    else:
                        break  # No next or submit found — stop

                return False, "Form navigation stalled — could not find Submit button"

            except Exception as e:
                return False, f"Ashby Playwright error: {e}"
            finally:
                await browser.close()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        try:
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(_apply())
        except ImportError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, _apply())
                return future.result(timeout=120)
    else:
        return asyncio.run(_apply())


def apply_ashby(
    company_slug: str,
    job_id: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str] = None,
    cover_letter_path: Optional[str] = None,
    headless: bool = True,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Main Ashby apply function: tries API first, falls back to Playwright form.

    Args:
        company_slug: Ashby company slug (e.g. "linear")
        job_id: Ashby job posting ID
        candidate: Candidate profile dict from config
        resume_path: Path to tailored resume file
        cover_letter_text: Cover letter body text
        cover_letter_path: Path to cover letter file (optional)
        headless: Run browser headlessly
        dry_run: Fill/prepare but don't submit

    Returns:
        (success, method_message) tuple
    """
    console.print(f"  [dim]Attempting Ashby API apply for {company_slug}/{job_id}...[/dim]")

    # Try API first (preferred — faster, more reliable)
    success, msg = apply_ashby_api(
        company_slug=company_slug,
        job_id=job_id,
        candidate=candidate,
        resume_path=resume_path,
        cover_letter_text=cover_letter_text,
        cover_letter_path=cover_letter_path,
        dry_run=dry_run,
    )

    if success:
        return success, msg

    # API failed — try Playwright form
    console.print(f"  [yellow]Ashby API failed ({msg}), trying Playwright form...[/yellow]")

    success, msg = apply_ashby_playwright(
        company_slug=company_slug,
        job_id=job_id,
        candidate=candidate,
        resume_path=resume_path,
        cover_letter_text=cover_letter_text,
        headless=headless,
        dry_run=dry_run,
    )

    return success, msg
