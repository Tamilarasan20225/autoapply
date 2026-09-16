"""
SmartRecruiters ATS Applier.

SmartRecruiters is used by: Freshworks, CRED, Razorpay (some roles),
Zomato (some roles), Volvo, Bosch, LinkedIn, Visa, and many global enterprises.

Two application strategies:
  1. Public Candidate API  (preferred — SmartRecruiters exposes a clean REST API)
  2. Playwright form       (fallback — careers.smartrecruiters.com)

SmartRecruiters Candidate API (no auth needed for candidates):
  POST https://api.smartrecruiters.com/v1/companies/{companyId}/postings/{jobId}/candidates

SmartRecruiters Job Listing API (to get jobId from URL):
  GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings/{jobId}
"""

import asyncio
import json
import random
import mimetypes
import requests
from pathlib import Path
from typing import Optional
from rich.console import Console

console = Console()

SR_API_BASE = "https://api.smartrecruiters.com/v1"


def _extract_sr_ids(job_url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extract SmartRecruiters companyId and jobId from a job URL.

    URL formats:
      https://careers.smartrecruiters.com/{CompanyName}/jobs/{jobId}
      https://jobs.smartrecruiters.com/{CompanyName}/{jobId}
      https://{company}.com/careers?reqs={jobId}   (embedded widget)

    Returns:
        (company_identifier, job_id) or (None, None)
    """
    import re
    url_lower = job_url.lower()

    # careers.smartrecruiters.com/{Company}/jobs/{jobId}
    m = re.search(r"careers\.smartrecruiters\.com/([^/]+)/jobs/([A-Za-z0-9_-]+)", job_url)
    if m:
        return m.group(1), m.group(2)

    # jobs.smartrecruiters.com/{Company}/{jobId}
    m = re.search(r"smartrecruiters\.com/([^/]+)/([A-Za-z0-9_-]+)", job_url)
    if m:
        return m.group(1), m.group(2)

    return None, None


def _fetch_job_details(company_id: str, job_id: str) -> Optional[dict]:
    """Fetch job posting details from SmartRecruiters API."""
    try:
        url = f"{SR_API_BASE}/companies/{company_id}/postings/{job_id}"
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        console.print(f"  [dim]SR job details fetch failed: {e}[/dim]")
    return None


def _build_candidate_payload(candidate: dict, cover_letter_text: Optional[str]) -> dict:
    """
    Build the SmartRecruiters candidate API payload.

    SR candidate schema:
    {
      "firstName": str,
      "lastName": str,
      "email": str,
      "phoneNumber": str,
      "location": {
        "country": "IN",
        "city": "Bangalore"
      },
      "web": {
        "linkedin": str,
        "portfolio": str
      },
      "tags": {
        "public": ["Source: Job Board"]
      },
      "consent": true
    }
    """
    name_parts = candidate.get("name", "").split(" ", 1)
    first = name_parts[0]
    last = name_parts[1] if len(name_parts) > 1 else ""

    payload = {
        "firstName": first,
        "lastName": last,
        "email": candidate.get("email", ""),
        "phoneNumber": candidate.get("phone", ""),
        "location": {
            "country": "IN",
            "city": candidate.get("city", "Bangalore"),
            "regionCode": "KA",
        },
        "web": {},
        "tags": {
            "public": ["Source: Job Board"],
        },
        "consent": True,  # GDPR consent
    }

    if candidate.get("linkedin"):
        payload["web"]["linkedin"] = candidate["linkedin"]

    if candidate.get("github"):
        payload["web"]["portfolio"] = candidate["github"]

    if cover_letter_text:
        # SR supports a coverLetter field on some job boards
        payload["coverLetter"] = cover_letter_text[:5000]

    return payload


def _post_application_with_resume(
    company_id: str,
    job_id: str,
    candidate_payload: dict,
    resume_path: str,
) -> requests.Response:
    """
    Post the application with resume as multipart form data.
    SmartRecruiters API accepts multipart: candidate JSON + resume file.
    """
    resume_file = Path(resume_path)

    mime_type = "application/pdf"
    if resume_file.suffix.lower() == ".html":
        mime_type = "text/html"
    elif resume_file.suffix.lower() in (".doc", ".docx"):
        mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    url = f"{SR_API_BASE}/companies/{company_id}/postings/{job_id}/candidates"

    with open(resume_file, "rb") as f:
        response = requests.post(
            url,
            headers={
                "Accept": "application/json",
                "X-SmartToken": "",  # Public endpoint — no token needed
            },
            files={
                "candidate": (None, json.dumps(candidate_payload), "application/json"),
                "resume": (resume_file.name, f, mime_type),
            },
            timeout=30,
        )

    return response


def _post_application_json_only(
    company_id: str,
    job_id: str,
    candidate_payload: dict,
) -> requests.Response:
    """
    Post the application as JSON only (no resume attachment).
    Used as fallback when multipart fails.
    """
    url = f"{SR_API_BASE}/companies/{company_id}/postings/{job_id}/candidates"

    response = requests.post(
        url,
        json=candidate_payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30,
    )

    return response


def apply_smartrecruiters_api(
    job_url: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str] = None,
    company_id: Optional[str] = None,
    job_id: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Apply to a SmartRecruiters job via the public candidate API.

    Args:
        job_url: SmartRecruiters job URL
        candidate: Candidate profile dict
        resume_path: Path to resume file
        cover_letter_text: Cover letter body text
        company_id: Optional — extracted from URL if not provided
        job_id: Optional — extracted from URL if not provided
        dry_run: If True, validate but don't submit

    Returns:
        (success, message) tuple
    """
    # Extract IDs from URL if not provided
    if not company_id or not job_id:
        company_id, job_id = _extract_sr_ids(job_url)

    if not company_id or not job_id:
        return False, f"Could not extract SmartRecruiters IDs from URL: {job_url}"

    if dry_run:
        console.print(f"  [dim][DRY RUN] Would apply via SR API: {company_id}/{job_id}[/dim]")
        return True, "dry_run"

    console.print(f"  [dim]Applying via SmartRecruiters API: {company_id}/{job_id}...[/dim]")

    # Build candidate payload
    candidate_payload = _build_candidate_payload(candidate, cover_letter_text)

    # Try with resume first (multipart)
    resume_file = Path(resume_path)
    if resume_file.exists():
        try:
            response = _post_application_with_resume(company_id, job_id, candidate_payload, resume_path)

            if response.status_code in (200, 201):
                console.print(f"  [green]✓ Applied via SmartRecruiters API (with resume)[/green]")
                return True, "Applied via SmartRecruiters API"

            elif response.status_code == 400:
                # Try JSON-only as fallback
                console.print(f"  [dim]SR multipart failed (400), trying JSON-only...[/dim]")

            elif response.status_code == 404:
                return False, f"SmartRecruiters job not found: {company_id}/{job_id}"

            elif response.status_code == 409:
                # Duplicate application
                console.print(f"  [yellow]SR: Already applied to this job (409)[/yellow]")
                return True, "Already applied (SmartRecruiters 409 Conflict)"

            else:
                console.print(f"  [dim]SR multipart returned {response.status_code}, trying JSON-only[/dim]")

        except Exception as e:
            console.print(f"  [dim]SR multipart error: {e}, trying JSON-only[/dim]")

    # Try JSON-only (without resume file)
    try:
        response = _post_application_json_only(company_id, job_id, candidate_payload)

        if response.status_code in (200, 201):
            console.print(f"  [green]✓ Applied via SmartRecruiters API (JSON only)[/green]")
            return True, "Applied via SmartRecruiters API (no resume file)"

        elif response.status_code == 400:
            try:
                error = response.json()
                msg = error.get("message", str(error))
            except Exception:
                msg = response.text[:200]
            return False, f"SmartRecruiters API validation error: {msg}"

        elif response.status_code == 404:
            return False, f"SmartRecruiters job/company not found: {company_id}/{job_id}"

        elif response.status_code == 409:
            return True, "Already applied (SmartRecruiters 409 Conflict)"

        else:
            return False, f"SmartRecruiters API error: HTTP {response.status_code}"

    except Exception as e:
        return False, f"SmartRecruiters API failed: {e}"


async def _apply_sr_playwright(
    job_url: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str],
    headless: bool,
    dry_run: bool,
) -> tuple[bool, str]:
    """Playwright fallback for SmartRecruiters form."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "Playwright not installed"

    if dry_run:
        console.print(f"  [dim][DRY RUN] Would open SR form: {job_url}[/dim]")
        return True, "dry_run"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        try:
            async def delay(a=0.5, b=2.0):
                await asyncio.sleep(random.uniform(a, b))

            await page.goto(job_url, timeout=30000)
            await delay(2, 3)

            # Look for "Apply" button if on job listing page
            apply_btn = await page.query_selector(
                '[data-hook="sr-apply-btn"], button:has-text("Apply"), '
                'a:has-text("Apply now"), a[href*="apply"]'
            )
            if apply_btn:
                await apply_btn.click()
                await delay(2, 3)

            # Wait for application form
            try:
                await page.wait_for_selector(
                    'input[name="firstName"], input[id*="firstName"], '
                    'input[type="email"], [data-hook*="firstName"]',
                    timeout=15000,
                )
            except Exception:
                return False, "SmartRecruiters application form not found"

            # Fill fields
            name_parts = candidate.get("name", "").split(" ", 1)
            for sel in ['input[name="firstName"]', 'input[id*="firstName"]', '[placeholder*="First"]']:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(name_parts[0])
                        break
                except Exception:
                    pass

            for sel in ['input[name="lastName"]', 'input[id*="lastName"]', '[placeholder*="Last"]']:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(name_parts[1] if len(name_parts) > 1 else "")
                        break
                except Exception:
                    pass

            for sel in ['input[name="email"]', 'input[type="email"]']:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(candidate.get("email", ""))
                        break
                except Exception:
                    pass

            for sel in ['input[name="phone"]', 'input[type="tel"]', '[name*="phone"]']:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(candidate.get("phone", ""))
                        break
                except Exception:
                    pass

            # Upload resume
            resume_file = Path(resume_path)
            if resume_file.exists():
                try:
                    file_input = await page.query_selector('input[type="file"]')
                    if file_input:
                        await file_input.set_input_files(str(resume_file))
                    else:
                        upload_btn = await page.query_selector(
                            'button:has-text("Upload"), label:has-text("Upload")'
                        )
                        if upload_btn:
                            async with page.expect_file_chooser(timeout=5000) as fc_info:
                                await upload_btn.click()
                            fc = await fc_info.value
                            await fc.set_files(str(resume_file))
                    await delay(1, 2)
                except Exception as e:
                    console.print(f"  [yellow]SR resume upload failed: {e}[/yellow]")

            # Cover letter
            if cover_letter_text:
                for sel in ['textarea[name*="cover"]', 'textarea[id*="cover"]', 'textarea']:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.fill(cover_letter_text[:3000])
                            break
                    except Exception:
                        pass

            await delay(1, 2)

            # Multi-step navigation
            for step in range(6):
                await delay(0.5, 1)

                # Try Submit first
                for sel in ['button[type="submit"]', 'button:has-text("Submit")', 'button:has-text("Apply")']:
                    try:
                        btn = await page.query_selector(sel)
                        if btn:
                            disabled = await btn.get_attribute("disabled")
                            if not disabled:
                                await btn.click()
                                await delay(3, 5)
                                content = await page.content()
                                if any(p in content.lower() for p in ["thank you", "success", "submitted", "received"]):
                                    console.print("  [green]✓ SmartRecruiters Playwright applied[/green]")
                                    return True, "Applied via SmartRecruiters Playwright"
                                return True, f"Submitted via SmartRecruiters Playwright (verify at {page.url})"
                    except Exception:
                        pass

                # Try Next
                next_clicked = False
                for sel in ['button:has-text("Next")', 'button:has-text("Continue")', 'button[type="button"]']:
                    try:
                        btn = await page.query_selector(sel)
                        if btn:
                            disabled = await btn.get_attribute("disabled")
                            if not disabled:
                                await btn.click()
                                await delay(1.5, 2.5)
                                next_clicked = True
                                break
                    except Exception:
                        pass

                if not next_clicked:
                    break

            return False, "SmartRecruiters form navigation stalled"

        except Exception as e:
            return False, f"SmartRecruiters Playwright error: {e}"
        finally:
            await browser.close()


def apply_smartrecruiters(
    job_url: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str] = None,
    headless: bool = True,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Main SmartRecruiters apply function: tries API first, then Playwright.

    Returns:
        (success, message) tuple
    """
    # Try API first
    success, msg = apply_smartrecruiters_api(
        job_url=job_url,
        candidate=candidate,
        resume_path=resume_path,
        cover_letter_text=cover_letter_text,
        dry_run=dry_run,
    )

    if success:
        return success, msg

    # API failed — fallback to Playwright
    console.print(f"  [yellow]SR API failed ({msg}), trying Playwright...[/yellow]")

    coro = _apply_sr_playwright(
        job_url=job_url,
        candidate=candidate,
        resume_path=resume_path,
        cover_letter_text=cover_letter_text,
        headless=headless,
        dry_run=dry_run,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        try:
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(coro)
        except ImportError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result(timeout=120)
    else:
        return asyncio.run(coro)
