"""
iCIMS ATS Applier — Playwright-based automation for iCIMS career portals.

iCIMS is used by: Infosys, Cognizant, HCL Technologies, Accenture India,
Capgemini, Dell, HP, Lenovo, IBM India, and many large enterprises.

iCIMS portal URL formats:
  https://careers.icims.com/jobs/{jobId}/job
  https://{company}.icims.com/jobs/{jobId}/job
  https://icims.com/jobs/{jobId}/job

Key challenges:
  - Requires creating an iCIMS account before applying (per portal)
  - Sessions can be saved and reused across applications to the same company
  - Multi-step form: Create Profile → Upload Resume → Fill Questions → Submit
  - Strong anti-bot detection in newer iCIMS versions
  - Some iCIMS portals use CAPTCHAs
"""

import asyncio
import json
import random
from pathlib import Path
from typing import Optional
from rich.console import Console

console = Console()

# iCIMS-specific CSS/ID selectors
ICIMS = {
    # Login / Account
    "email_field": '#iCIMS_EmailField input, input[id*="Email"], #login_email',
    "password_field": '#iCIMS_PasswordField input, input[type="password"], #login_password',
    "login_btn": '#iCIMS_LoginButton, button:has-text("Sign In"), button:has-text("Login")',
    "create_account_link": 'a:has-text("Create Account"), a:has-text("Register"), a:has-text("Sign Up")',
    "create_account_btn": 'button:has-text("Create Account"), button:has-text("Register")',
    
    # Registration
    "first_name": '#iCIMS_FirstNameField input, input[id*="FirstName"], input[name*="first"]',
    "last_name": '#iCIMS_LastNameField input, input[id*="LastName"], input[name*="last"]',
    "reg_email": '#iCIMS_EmailField input, input[id*="Email"], input[type="email"]',
    "reg_password": 'input[id*="Password"], input[type="password"]',
    "reg_confirm_password": 'input[id*="Confirm"], input[id*="confirm"], input[id*="repassword"]',
    "reg_phone": 'input[id*="Phone"], input[type="tel"]',
    "reg_submit": 'button[type="submit"], button:has-text("Create Account"), button:has-text("Register")',
    
    # Application form
    "resume_upload": 'input[type="file"], #iCIMS_ResumeUpload',
    "upload_btn": 'button:has-text("Upload"), label:has-text("Upload resume"), #iCIMS_UploadResume',
    "apply_btn": '#iCIMS_JobHeaderApplyButtonElement, button:has-text("Apply"), a:has-text("Apply Now")',
    
    # Navigation
    "next_btn": 'button:has-text("Next"), button:has-text("Continue"), input[value="Next"]',
    "submit_btn": 'button:has-text("Submit"), input[type="submit"], button[type="submit"]',
    
    # Success indicators
    "success_msg": '.iCIMS_SuccessText, .iCIMS_ThankYou, [class*="success"]',
}


class ICIMSSession:
    """
    Manages iCIMS account creation and login sessions.
    Sessions are stored per company portal to avoid repeated registrations.
    """

    def __init__(self, session_dir: str = ".browser_session/icims"):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, portal_host: str) -> Path:
        safe_host = portal_host.replace(".", "_").replace("/", "_")
        return self.session_dir / f"{safe_host}.json"

    def _creds_path(self, portal_host: str) -> Path:
        safe_host = portal_host.replace(".", "_").replace("/", "_")
        return self.session_dir / f"{safe_host}_creds.json"

    def has_session(self, portal_host: str) -> bool:
        return self._session_path(portal_host).exists()

    def get_session_path(self, portal_host: str) -> Optional[str]:
        p = self._session_path(portal_host)
        return str(p) if p.exists() else None

    def save_session(self, portal_host: str, storage_state: dict):
        with open(self._session_path(portal_host), "w") as f:
            json.dump(storage_state, f)

    def save_creds(self, portal_host: str, email: str, password: str):
        with open(self._creds_path(portal_host), "w") as f:
            json.dump({"email": email, "password": password}, f)

    def get_creds(self, portal_host: str) -> Optional[tuple[str, str]]:
        p = self._creds_path(portal_host)
        if p.exists():
            data = json.loads(p.read_text())
            return data.get("email"), data.get("password")
        return None


def _get_portal_host(url: str) -> str:
    """Extract the portal hostname from an iCIMS URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.netloc or "icims.com"


def _generate_portal_password(email: str, host: str, seed: str = "AutoAppy") -> str:
    """
    Generate a deterministic password for a given portal.
    Uses hash of email + host so it's reproducible but site-specific.
    """
    import hashlib
    key = f"{seed}_{email}_{host}"
    h = hashlib.sha256(key.encode()).hexdigest()[:12]
    # Make sure it meets typical password requirements
    return f"Aa1!{h[:8]}"


async def _random_delay(min_s: float = 0.5, max_s: float = 2.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _try_fill(page, selectors: list[str], value: str) -> bool:
    """Try multiple selectors to fill a field."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(value)
                await _random_delay(0.2, 0.4)
                return True
        except Exception:
            continue
    return False


async def _try_create_account(
    page, candidate: dict, email: str, password: str
) -> bool:
    """Attempt to create an iCIMS account."""
    console.print("  [dim]  Creating iCIMS account...[/dim]")

    name_parts = candidate.get("name", "").split(" ", 1)
    first = name_parts[0]
    last = name_parts[1] if len(name_parts) > 1 else ""

    # Fill registration form
    await _try_fill(page, [
        '#iCIMS_FirstNameField input', 'input[name*="first"]',
        'input[id*="FirstName"]', '#firstName',
    ], first)

    await _try_fill(page, [
        '#iCIMS_LastNameField input', 'input[name*="last"]',
        'input[id*="LastName"]', '#lastName',
    ], last)

    await _try_fill(page, [
        'input[type="email"]', 'input[id*="Email"]',
        '#iCIMS_EmailField input', '#email',
    ], email)

    # Phone
    await _try_fill(page, [
        'input[type="tel"]', 'input[id*="Phone"]', '#phone',
    ], candidate.get("phone", ""))

    # Password fields
    password_inputs = await page.query_selector_all('input[type="password"]')
    if len(password_inputs) >= 1:
        await password_inputs[0].fill(password)
        await _random_delay(0.3, 0.5)
    if len(password_inputs) >= 2:
        await password_inputs[1].fill(password)
        await _random_delay(0.3, 0.5)

    await _random_delay(1, 2)

    # Submit registration
    for sel in [
        'button[type="submit"]',
        'button:has-text("Create Account")',
        'button:has-text("Register")',
        'input[type="submit"]',
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await _random_delay(3, 5)
                return True
        except Exception:
            pass

    return False


async def _try_login(page, email: str, password: str) -> bool:
    """Attempt to login to an iCIMS portal."""
    console.print("  [dim]  Logging into iCIMS portal...[/dim]")

    await _try_fill(page, [
        '#login_email', 'input[type="email"]', 'input[id*="Email"]',
    ], email)

    await _try_fill(page, [
        '#login_password', 'input[type="password"]',
    ], password)

    await _random_delay(0.5, 1)

    for sel in [
        '#iCIMS_LoginButton', 'button[type="submit"]',
        'button:has-text("Sign In")', 'button:has-text("Login")',
        'input[value="Sign In"]',
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await _random_delay(3, 5)
                return True
        except Exception:
            pass

    return False


async def apply_icims_async(
    job_url: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str] = None,
    headless: bool = True,
    dry_run: bool = False,
    password_seed: str = "AutoAppy",
) -> tuple[bool, str]:
    """
    Apply to an iCIMS job via Playwright.

    Strategy:
      1. Navigate to job listing
      2. Check if already logged in (session)
      3. If not: try login, if login fails: create account
      4. Click Apply Now
      5. Fill multi-step form
      6. Upload resume
      7. Submit

    Args:
        job_url: iCIMS job listing URL
        candidate: Candidate profile dict
        resume_path: Path to tailored resume
        cover_letter_text: Optional cover letter text
        headless: Run browser headlessly
        dry_run: Prepare but don't submit
        password_seed: Seed for generating portal-specific password

    Returns:
        (success, message) tuple
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "Playwright not installed"

    if dry_run:
        console.print(f"  [dim][DRY RUN] Would open iCIMS form: {job_url}[/dim]")
        return True, "dry_run"

    portal_host = _get_portal_host(job_url)
    email = candidate.get("email", "")
    password = _generate_portal_password(email, portal_host, password_seed)

    session_mgr = ICIMSSession()
    session_path = session_mgr.get_session_path(portal_host)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        # Use existing session if available
        context_args = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        }
        if session_path:
            context_args["storage_state"] = session_path
            console.print(f"  [dim]  Using saved iCIMS session for {portal_host}[/dim]")

        context = await browser.new_context(**context_args)
        page = await context.new_page()

        try:
            # Navigate to job listing
            console.print(f"  [dim]Opening iCIMS job: {job_url[:80]}...[/dim]")
            await page.goto(job_url, timeout=30000)
            await _random_delay(2, 3)

            # Check if we need to login
            content = await page.content()
            needs_auth = any(kw in content.lower() for kw in [
                "sign in", "login", "log in", "create account", "register"
            ])

            if needs_auth and not session_path:
                console.print("  [dim]  iCIMS auth required...[/dim]")

                # Try login with saved credentials
                saved_creds = session_mgr.get_creds(portal_host)
                if saved_creds:
                    email_to_use, pwd_to_use = saved_creds
                    login_ok = await _try_login(page, email_to_use, pwd_to_use)
                    if not login_ok:
                        console.print("  [yellow]  Saved iCIMS credentials failed — trying registration[/yellow]")
                        saved_creds = None

                if not saved_creds:
                    # Look for "Create Account" link
                    create_link = await page.query_selector(ICIMS["create_account_link"])
                    if create_link:
                        await create_link.click()
                        await _random_delay(2, 3)
                        created = await _try_create_account(page, candidate, email, password)
                        if created:
                            session_mgr.save_creds(portal_host, email, password)
                            await _random_delay(2, 3)
                        else:
                            console.print("  [yellow]  Could not create iCIMS account[/yellow]")
                    else:
                        # Try direct login
                        login_ok = await _try_login(page, email, password)
                        if not login_ok:
                            return False, "iCIMS login/registration failed"

                # Save session after auth
                try:
                    storage = await context.storage_state()
                    session_mgr.save_session(portal_host, storage)
                except Exception:
                    pass

            # Look for Apply button
            apply_btn = await page.query_selector(ICIMS["apply_btn"])
            if apply_btn:
                console.print("  [dim]  Clicking Apply Now...[/dim]")
                await apply_btn.click()
                await _random_delay(2, 4)

            # Wait for application form
            try:
                await page.wait_for_selector(
                    'input[type="file"], input[type="email"], '
                    '.iCIMS_ApplicationForm, [class*="application"]',
                    timeout=15000,
                )
            except Exception:
                console.print("  [yellow]  iCIMS application form not detected[/yellow]")

            # ── Multi-step form navigation ─────────────────────────────────────
            resume_file = Path(resume_path)
            resume_uploaded = False

            for step in range(8):
                await _random_delay(0.5, 1)

                # Upload resume if file input visible
                if not resume_uploaded and resume_file.exists():
                    try:
                        file_input = await page.query_selector('input[type="file"]')
                        if file_input and await file_input.is_visible():
                            await file_input.set_input_files(str(resume_file))
                            await _random_delay(2, 3)
                            resume_uploaded = True
                            console.print(f"  [dim]  Resume uploaded: {resume_file.name}[/dim]")
                        else:
                            # Try upload button
                            upload_btn = await page.query_selector(
                                'button:has-text("Upload"), button:has-text("Browse"), '
                                'label[for*="resume"], label[for*="file"]'
                            )
                            if upload_btn:
                                try:
                                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                                        await upload_btn.click()
                                    fc = await fc_info.value
                                    await fc.set_files(str(resume_file))
                                    await _random_delay(2, 3)
                                    resume_uploaded = True
                                except Exception:
                                    pass
                    except Exception:
                        pass

                # Fill visible text fields
                await _try_fill(page, [
                    'input[id*="firstName"]', 'input[name*="first"]',
                ], candidate.get("name", "").split()[0])

                await _try_fill(page, [
                    'input[id*="lastName"]', 'input[name*="last"]',
                ], candidate.get("name", "").split()[-1])

                await _try_fill(page, [
                    'input[type="email"]', 'input[id*="email"]',
                ], candidate.get("email", ""))

                await _try_fill(page, [
                    'input[type="tel"]', 'input[id*="phone"]',
                ], candidate.get("phone", ""))

                # Handle Yes/No questions
                try:
                    yes_radios = await page.query_selector_all(
                        'input[type="radio"][value="Yes"], input[type="radio"][value="yes"]'
                    )
                    for radio in yes_radios:
                        await radio.check()
                        await _random_delay(0.2, 0.3)
                except Exception:
                    pass

                # Handle dropdowns
                try:
                    selects = await page.query_selector_all("select")
                    for sel_el in selects:
                        opts = await sel_el.query_selector_all("option")
                        for opt in opts:
                            txt = (await opt.text_content() or "").strip().lower()
                            if txt in ("yes", "india", "bangalore"):
                                val = await opt.get_attribute("value")
                                if val:
                                    await sel_el.select_option(value=val)
                                break
                except Exception:
                    pass

                await _random_delay(0.5, 1)

                # Try Submit
                submitted = False
                for sel in [
                    'button:has-text("Submit")', 'button[type="submit"]',
                    'input[type="submit"]', 'input[value="Submit"]',
                ]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            disabled = await btn.get_attribute("disabled")
                            if not disabled:
                                await btn.click()
                                await _random_delay(3, 5)
                                # Check for success
                                content = await page.content()
                                if any(p in content.lower() for p in [
                                    "thank you", "application submitted",
                                    "successfully", "received", "complete"
                                ]):
                                    # Save updated session
                                    try:
                                        storage = await context.storage_state()
                                        session_mgr.save_session(portal_host, storage)
                                    except Exception:
                                        pass
                                    console.print("  [green]✓ iCIMS application submitted[/green]")
                                    return True, "Applied via iCIMS Playwright"
                                submitted = True
                                break
                    except Exception:
                        pass

                if submitted:
                    # Check post-submit page
                    content = await page.content()
                    if any(p in content.lower() for p in ["thank", "success", "complete"]):
                        return True, "Applied via iCIMS Playwright"
                    console.print(f"  [yellow]Submitted — verify at: {page.url}[/yellow]")
                    return True, f"Submitted via iCIMS Playwright (verify at {page.url})"

                # Try Next
                next_clicked = False
                for sel in [
                    'button:has-text("Next")', 'button:has-text("Continue")',
                    'input[value="Next"]', 'a:has-text("Next")',
                ]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            await _random_delay(2, 3)
                            next_clicked = True
                            break
                    except Exception:
                        pass

                if not next_clicked:
                    break

            return False, "iCIMS form navigation stalled"

        except Exception as e:
            return False, f"iCIMS Playwright error: {e}"
        finally:
            await browser.close()


def apply_icims(
    job_url: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str] = None,
    headless: bool = True,
    dry_run: bool = False,
    password_seed: str = "AutoAppy",
) -> tuple[bool, str]:
    """
    Synchronous wrapper for iCIMS apply. Event-loop safe.

    Args:
        job_url: iCIMS job listing URL
        candidate: Candidate profile dict
        resume_path: Path to resume
        cover_letter_text: Optional cover letter
        headless: Headless browser mode
        dry_run: Don't submit, just fill
        password_seed: Seed for account password generation

    Returns:
        (success, message) tuple
    """
    coro = apply_icims_async(
        job_url=job_url,
        candidate=candidate,
        resume_path=resume_path,
        cover_letter_text=cover_letter_text,
        headless=headless,
        dry_run=dry_run,
        password_seed=password_seed,
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
                return future.result(timeout=180)
    else:
        return asyncio.run(coro)
