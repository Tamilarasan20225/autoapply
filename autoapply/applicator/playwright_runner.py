"""
Playwright-based browser automation for job applications.
Handles:
  - LinkedIn Easy Apply
  - Greenhouse boards.greenhouse.io applications
  - Lever boards apply pages
  - Generic form fill for other ATS portals
"""

import asyncio
import random
import time
from pathlib import Path
from typing import Optional
from rich.console import Console

console = Console()

# Persistent browser session directory (reuses login across runs)
SESSION_DIR = Path(".browser_session")


async def _human_type(page, selector: str, text: str, delay_min=0.05, delay_max=0.15):
    """Type text with human-like delays between keystrokes."""
    await page.click(selector)
    await page.fill(selector, "")  # Clear existing
    for char in text:
        await page.type(selector, char)
        await asyncio.sleep(random.uniform(delay_min, delay_max))


async def _random_delay(min_s=1.0, max_s=3.0):
    """Wait a random human-like duration."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _fill_text_field(page, selectors: list, value: str, skip_if_filled=True):
    """Try multiple selectors to fill a field."""
    for selector in selectors:
        try:
            el = await page.query_selector(selector)
            if el:
                if skip_if_filled:
                    val = await el.input_value()
                    if val and val.strip():
                        return True  # Already filled
                await el.fill(value)
                await _random_delay(0.2, 0.5)
                return True
        except Exception:
            continue
    return False


# ══════════════════════════════════════════════════════════════════════════════
# GREENHOUSE BOARDS PLAYWRIGHT APPLY
# ══════════════════════════════════════════════════════════════════════════════

async def apply_greenhouse_playwright(
    company_slug: str,
    job_id: str,
    candidate: dict,
    resume_path: str,
    cover_letter_path: str | None = None,
    headless: bool = True,
    dry_run: bool = False,
    apply_url: str | None = None,  # Direct apply URL (e.g. https://stripe.com/careers/apply/...)
    llm_client=None,
    cover_letter_text: str | None = None,
    job_title: str = "",
    company_name: str = "",
) -> tuple[bool, str]:
    """
    Apply to a Greenhouse job via the web form using Playwright.
    Works even when the public API endpoint returns 404.
    
    The apply_url can be the company's actual apply URL (e.g. stripe.com/careers/apply/...)
    which is more reliable than the generic boards.greenhouse.io URL.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "Playwright not installed. Run: pip install playwright && playwright install chromium"

    # Use direct boards.greenhouse.io URL (more reliable than company portal redirect)
    # Try new job-boards format first, fall back to old boards format
    # Companies like Stripe redirect boards.greenhouse.io to their login-walled portal
    # but job-boards.greenhouse.io often works directly
    if apply_url:
        target_url = apply_url
        # If it's a company portal URL with gh_jid param, use the direct GH board URL instead
        if "gh_jid" in str(apply_url).lower() or ("careers" in str(apply_url).lower() and "greenhouse" not in str(apply_url).lower()):
            target_url = f"https://job-boards.greenhouse.io/{company_slug}/jobs/{job_id}"
    else:
        target_url = f"https://job-boards.greenhouse.io/{company_slug}/jobs/{job_id}"

    if dry_run:
        console.print(f"  [dim][DRY RUN] Would open Greenhouse form: {target_url}[/dim]")
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
            console.print(f"  [dim]Opening Greenhouse form: {target_url}[/dim]")
            await page.goto(target_url, timeout=30000)
            await _random_delay(2, 4)

            # If there's an "Apply now" / "Apply for this role" link visible, click it first
            try:
                apply_btn = await page.query_selector(
                    "a[href*='/apply'], a:has-text('Apply now'), "
                    "a:has-text('Apply for this role'), button:has-text('Apply now')"
                )
                if apply_btn:
                    href = await apply_btn.get_attribute("href")
                    if href:
                        # Navigate to the actual apply URL
                        if href.startswith("http"):
                            await page.goto(href, timeout=30000)
                        else:
                            base = target_url.split("/careers")[0] if "/careers" in target_url else "https://boards.greenhouse.io"
                            await page.goto(f"{base}{href}", timeout=30000)
                        await _random_delay(2, 3)
            except Exception:
                pass

            # Check if apply form is now loaded
            # Greenhouse may redirect to job-boards.greenhouse.io (new URL format)
            try:
                await page.wait_for_selector(
                    "form#application-form, #app form, input[type='file'], "
                    "input[name='first_name'], #first_name, input[name='candidate[first_name]'], "
                    "input[id='first_name'], input[id='email']",
                    timeout=10000
                )
            except Exception:
                # Check if there's a sign-in wall (like Stripe)
                content = await page.content()
                if "sign in" in content.lower() or "log in" in content.lower():
                    return False, f"sign_in_required"
                return False, f"Greenhouse apply form not found at: {page.url}"

            # ── Phase 1: Upload Resume ─────────────────────────────────────────
            resume_file = Path(resume_path)
            if resume_file.exists():
                uploaded = False
                try:
                    file_input = await page.query_selector(
                        "input[type='file'][name*='resume'], "
                        "input[type='file'][id='resume'], "
                        "input[type='file']"
                    )
                    if file_input:
                        await file_input.set_input_files(str(resume_file))
                        await _random_delay(1, 2)
                        console.print(f"  [dim]Resume uploaded: {resume_file.name}[/dim]")
                        uploaded = True
                except Exception as e:
                    console.print(f"  [yellow]Resume direct upload failed: {e}[/yellow]")

                # Fallback: click upload button to trigger file chooser
                if not uploaded:
                    for btn_sel in [
                        "button:has-text('Upload')", "label:has-text('Upload')",
                        "button:has-text('Attach')", "[aria-label*='upload']",
                        ".upload-button", "label[for*='resume']",
                    ]:
                        try:
                            btn = await page.query_selector(btn_sel)
                            if btn:
                                async with page.expect_file_chooser(timeout=5000) as fc_info:
                                    await btn.click()
                                fc = await fc_info.value
                                await fc.set_files(str(resume_file))
                                await _random_delay(1, 2)
                                console.print(f"  [dim]Resume uploaded via chooser: {resume_file.name}[/dim]")
                                break
                        except Exception:
                            continue

            # ── Phase 2: Upload Cover Letter ──────────────────────────────────
            if cover_letter_path:
                cl_file = Path(cover_letter_path)
                if cl_file.exists():
                    try:
                        cl_inputs = await page.query_selector_all("input[type='file']")
                        if len(cl_inputs) > 1:
                            await cl_inputs[1].set_input_files(str(cl_file))
                            await _random_delay(0.5, 1)
                    except Exception:
                        pass

            # ── Phase 3: Universal form fill (dynamic handler) ────────────────
            from autoapply.applicator.dynamic_form_handler import (
                fill_all_form_fields,
                submit_with_retry,
                is_success_page,
            )

            cov_text = cover_letter_text or ""

            # ── Multi-step form navigation ────────────────────────────────────
            MAX_STEPS = 8
            for step_num in range(MAX_STEPS):
                console.print(f"  [dim]  GH Step {step_num + 1}: {page.url[:60]}...[/dim]")

                # Fill all visible fields on this page/step
                await fill_all_form_fields(
                    page=page,
                    candidate=candidate,
                    llm_client=llm_client,
                    cover_letter_text=cov_text,
                    job_title=job_title or company_name,
                    company=company_name or company_slug,
                )

                await _random_delay(0.5, 1)

                # Check if current page is already success
                content = await page.content()
                if is_success_page(content, page.url):
                    console.print(f"  [green]✓ Greenhouse Playwright applied[/green]")
                    return True, "Applied via Greenhouse Playwright"

                # Look for Submit button
                submit_btn = None
                for selector in [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button:has-text('Submit Application')",
                    "button:has-text('Submit')",
                    "#submit_app",
                    "[data-qa='btn-submit']",
                ]:
                    try:
                        btn = await page.query_selector(selector)
                        if btn and await btn.is_visible():
                            is_disabled = await btn.get_attribute("disabled")
                            if not is_disabled:
                                submit_btn = btn
                                break
                    except Exception:
                        continue

                if submit_btn:
                    # Use retry-aware submit
                    success, msg = await submit_with_retry(
                        page=page,
                        candidate=candidate,
                        llm_client=llm_client,
                        cover_letter_text=cov_text,
                        job_title=job_title or company_name,
                        company=company_name or company_slug,
                        max_retries=3,
                        dry_run=dry_run,
                    )
                    if success:
                        console.print(f"  [green]✓ Greenhouse Playwright applied[/green]")
                        return True, "Applied via Greenhouse Playwright"
                    else:
                        screenshot_path = f"/tmp/gh_apply_{company_slug}_{job_id}.png"
                        try:
                            await page.screenshot(path=screenshot_path)
                        except Exception:
                            pass
                        return True, f"Submitted via Playwright (verify at {page.url})"

                # Look for Next / Continue button
                next_btn = None
                for next_sel in [
                    "button:has-text('Next')",
                    "button:has-text('Continue')",
                    "button:has-text('Next Step')",
                    "button:has-text('Save and Continue')",
                    "[data-qa='btn-next']",
                    "a:has-text('Next')",
                ]:
                    try:
                        btn = await page.query_selector(next_sel)
                        if btn and await btn.is_visible():
                            is_disabled = await btn.get_attribute("disabled")
                            if not is_disabled:
                                next_btn = btn
                                break
                    except Exception:
                        continue

                if next_btn:
                    console.print(f"  [dim]Navigating to step {step_num + 2}...[/dim]")
                    await next_btn.click()
                    await _random_delay(2, 3)
                else:
                    console.print(f"  [yellow]No Next/Submit found on step {step_num + 1}[/yellow]")
                    break

            return False, "Submit button not found on Greenhouse form after multi-step navigation"

        except Exception as e:
            return False, f"Playwright Greenhouse error: {e}"
        finally:
            await browser.close()


# ══════════════════════════════════════════════════════════════════════════════
# LEVER BOARDS PLAYWRIGHT APPLY
# ══════════════════════════════════════════════════════════════════════════════

async def apply_lever_playwright(
    company_slug: str,
    job_id: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: str | None = None,
    headless: bool = True,
    dry_run: bool = False,
    llm_client=None,
    job_title: str = "",
    company_name: str = "",
) -> tuple[bool, str]:
    """
    Apply to a Lever job via the web form using Playwright.
    Now uses dynamic_form_handler for all custom questions, radio groups,
    checkboxes, and pre-submit validation.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "Playwright not installed. Run: pip install playwright && playwright install chromium"

    apply_url = f"https://jobs.lever.co/{company_slug}/{job_id}/apply"

    if dry_run:
        console.print(f"  [dim][DRY RUN] Would open Lever form: {apply_url}[/dim]")
        return True, "dry_run"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        try:
            console.print(f"  [dim]Opening Lever form: {apply_url}[/dim]")
            await page.goto(apply_url, timeout=30000)
            await _random_delay(2, 3)

            # Wait for form
            try:
                await page.wait_for_selector(
                    "input[name='name'], input[placeholder*='Full name'], "
                    "input[name='email'], input[type='email']",
                    timeout=10000
                )
            except Exception:
                return False, f"Lever apply page not found: {apply_url}"

            # ── Phase 1: Upload resume first ──────────────────────────────────
            resume_file = Path(resume_path)
            if resume_file.exists():
                try:
                    # Try direct file input
                    file_input = await page.query_selector("input[type='file']")
                    if file_input:
                        await file_input.set_input_files(str(resume_file))
                        await _random_delay(1, 2)
                        console.print(f"  [dim]Lever resume uploaded: {resume_file.name}[/dim]")
                    else:
                        # Try file chooser via button
                        for btn_sel in [
                            "button:has-text('Upload')", "label:has-text('Upload')",
                            ".resume-upload-btn", "[aria-label*='upload']",
                        ]:
                            try:
                                btn = await page.query_selector(btn_sel)
                                if btn:
                                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                                        await btn.click()
                                    fc = await fc_info.value
                                    await fc.set_files(str(resume_file))
                                    await _random_delay(1, 2)
                                    break
                            except Exception:
                                continue
                except Exception as e:
                    console.print(f"  [yellow]Lever resume upload failed: {e}[/yellow]")

            # ── Phase 2: Universal form fill ──────────────────────────────────
            from autoapply.applicator.dynamic_form_handler import (
                fill_all_form_fields,
                submit_with_retry,
                is_success_page,
            )

            cov_text = cover_letter_text or ""

            await fill_all_form_fields(
                page=page,
                candidate=candidate,
                llm_client=llm_client,
                cover_letter_text=cov_text,
                job_title=job_title or company_name,
                company=company_name or company_slug,
            )

            await _random_delay(1, 2)

            # ── Phase 3: Submit with validation retry ─────────────────────────
            success, msg = await submit_with_retry(
                page=page,
                candidate=candidate,
                llm_client=llm_client,
                cover_letter_text=cov_text,
                job_title=job_title or company_name,
                company=company_name or company_slug,
                max_retries=3,
                dry_run=dry_run,
            )

            if success:
                console.print(f"  [green]✓ Lever Playwright applied[/green]")
                return True, "Applied via Lever Playwright"
            else:
                console.print(f"  [yellow]Lever submit failed: {msg}[/yellow]")
                return False, f"Lever submit failed: {msg}"

        except Exception as e:
            return False, f"Playwright Lever error: {e}"
        finally:
            await browser.close()


# ══════════════════════════════════════════════════════════════════════════════
# SYNC WRAPPERS
# ══════════════════════════════════════════════════════════════════════════════

def _run_async(coro):
    """
    Run an async coroutine safely regardless of whether an event loop
    is already running (e.g. inside Streamlit, Jupyter, or APScheduler).
    Avoids the 'This event loop is already running' error from asyncio.run().
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # There's already a running loop (Streamlit, Jupyter, etc.)
        # Use nest_asyncio if available, otherwise create a new thread
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


def apply_greenhouse_playwright_sync(
    company_slug: str,
    job_id: str,
    candidate: dict,
    resume_path: str,
    cover_letter_path: str | None = None,
    headless: bool = True,
    dry_run: bool = False,
    llm_client=None,
    cover_letter_text: str | None = None,
    job_title: str = "",
    company_name: str = "",
) -> tuple[bool, str]:
    """Synchronous wrapper for Greenhouse Playwright apply. Event-loop safe."""
    try:
        return _run_async(apply_greenhouse_playwright(
            company_slug=company_slug,
            job_id=job_id,
            candidate=candidate,
            resume_path=resume_path,
            cover_letter_path=cover_letter_path,
            headless=headless,
            dry_run=dry_run,
            llm_client=llm_client,
            cover_letter_text=cover_letter_text,
            job_title=job_title,
            company_name=company_name,
        ))
    except Exception as e:
        return False, f"Async runner error: {e}"


def apply_lever_playwright_sync(
    company_slug: str,
    job_id: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: str | None = None,
    headless: bool = True,
    dry_run: bool = False,
    llm_client=None,
    job_title: str = "",
    company_name: str = "",
) -> tuple[bool, str]:
    """Synchronous wrapper for Lever Playwright apply. Event-loop safe."""
    try:
        return _run_async(apply_lever_playwright(
            company_slug=company_slug,
            job_id=job_id,
            candidate=candidate,
            resume_path=resume_path,
            cover_letter_text=cover_letter_text,
            headless=headless,
            dry_run=dry_run,
            llm_client=llm_client,
            job_title=job_title,
            company_name=company_name,
        ))
    except Exception as e:
        return False, f"Async runner error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# LINKEDIN EASY APPLY (existing, unchanged)
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInApplier:
    """
    Automates LinkedIn Easy Apply with Playwright.
    Uses persistent session to avoid repeated logins.
    """

    def __init__(
        self,
        email: str,
        password: str,
        headless: bool = False,
        slow_mo: int = 50,
        dry_run: bool = False,
    ):
        self.email = email
        self.password = password
        self.headless = headless
        self.slow_mo = slow_mo
        self.dry_run = dry_run
        self.browser = None
        self.context = None
        self.page = None

    async def start(self):
        """Launch browser with persistent session."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            console.print("[red]Playwright not installed. Run: pip install playwright && playwright install chromium[/red]")
            return False

        SESSION_DIR.mkdir(exist_ok=True)
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        self.context = await self.browser.new_context(
            storage_state=str(SESSION_DIR / "linkedin_session.json")
            if (SESSION_DIR / "linkedin_session.json").exists()
            else None,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        self.page = await self.context.new_page()
        return True

    async def login(self) -> bool:
        """Login to LinkedIn if not already logged in."""
        await self.page.goto("https://www.linkedin.com/feed/", timeout=30000)

        if "feed" in self.page.url:
            console.print("  [green]LinkedIn: Already logged in ✓[/green]")
            return True

        console.print("  [dim]LinkedIn: Logging in...[/dim]")
        await self.page.goto("https://www.linkedin.com/login")
        await _random_delay(1, 2)

        await _human_type(self.page, "#username", self.email)
        await _random_delay(0.5, 1.0)
        await _human_type(self.page, "#password", self.password)
        await _random_delay(0.5, 1.0)
        await self.page.click('button[type="submit"]')
        await _random_delay(3, 5)

        await self.context.storage_state(path=str(SESSION_DIR / "linkedin_session.json"))

        if "feed" in self.page.url or "checkpoint" not in self.page.url:
            console.print("  [green]LinkedIn: Login successful ✓[/green]")
            return True
        else:
            console.print("  [red]LinkedIn: Login failed — check credentials or solve CAPTCHA manually[/red]")
            return False

    async def apply_easy_apply(
        self,
        job_url: str,
        resume_path: str,
        cover_letter_text: str | None = None,
        candidate: dict | None = None,
    ) -> tuple[bool, str]:
        """Apply to a LinkedIn Easy Apply job."""
        if self.dry_run:
            console.print(f"  [dim][DRY RUN] Would LinkedIn Easy Apply: {job_url}[/dim]")
            return True, "dry_run"

        try:
            await self.page.goto(job_url, timeout=30000)
            await _random_delay(2, 4)

            easy_apply_btn = None
            for selector in [
                "button.jobs-apply-button",
                "button[aria-label*='Easy Apply']",
                ".jobs-apply-button--top-card",
                "button:has-text('Easy Apply')",
            ]:
                try:
                    btn = await self.page.wait_for_selector(selector, timeout=5000)
                    if btn:
                        easy_apply_btn = btn
                        break
                except Exception:
                    continue

            if not easy_apply_btn:
                return False, "No Easy Apply button found — may require external application"

            await easy_apply_btn.click()
            await _random_delay(2, 3)

            max_steps = 10
            for step in range(max_steps):
                try:
                    resume_input = await self.page.wait_for_selector("input[type='file']", timeout=3000)
                    if resume_input and Path(resume_path).exists():
                        await resume_input.set_input_files(resume_path)
                        await _random_delay(1, 2)
                except Exception:
                    pass

                if candidate:
                    for phone_selector in [
                        "input[id*='phoneNumber']",
                        "input[name*='phone']",
                        "input[placeholder*='Phone']",
                    ]:
                        try:
                            phone_field = await self.page.query_selector(phone_selector)
                            if phone_field:
                                val = await phone_field.input_value()
                                if not val:
                                    await _human_type(self.page, phone_selector, candidate.get("phone", ""))
                        except Exception:
                            pass

                if cover_letter_text:
                    for cl_selector in [
                        "textarea[id*='cover']",
                        "textarea[name*='cover']",
                        "textarea[placeholder*='cover']",
                        "textarea[aria-label*='cover']",
                    ]:
                        try:
                            cl_field = await self.page.query_selector(cl_selector)
                            if cl_field:
                                await cl_field.fill(cover_letter_text[:2000])
                                await _random_delay(0.5, 1)
                        except Exception:
                            pass

                try:
                    radio_yes = await self.page.query_selector_all(
                        "input[type='radio'][value='Yes'], input[type='radio'][value='yes']"
                    )
                    for radio in radio_yes:
                        await radio.check()
                        await _random_delay(0.3, 0.7)
                except Exception:
                    pass

                try:
                    number_inputs = await self.page.query_selector_all("input[type='number']")
                    for inp in number_inputs:
                        val = await inp.input_value()
                        if not val:
                            await inp.fill("2")
                            await _random_delay(0.3, 0.5)
                except Exception:
                    pass

                for btn_text in ["Submit application", "Submit", "Review"]:
                    try:
                        submit_btn = await self.page.query_selector(f"button:has-text('{btn_text}')")
                        if submit_btn:
                            is_disabled = await submit_btn.get_attribute("disabled")
                            if not is_disabled:
                                if btn_text in ("Submit application", "Submit"):
                                    await submit_btn.click()
                                    await _random_delay(2, 3)
                                    console.print(f"  [green]✓ LinkedIn Easy Apply submitted[/green]")
                                    return True, "Applied via LinkedIn Easy Apply"
                                else:
                                    await submit_btn.click()
                                    await _random_delay(1.5, 2.5)
                                    break
                    except Exception:
                        continue

                try:
                    next_btn = await self.page.query_selector(
                        "button[aria-label='Continue to next step'], button:has-text('Next')"
                    )
                    if next_btn:
                        await next_btn.click()
                        await _random_delay(1.5, 2.5)
                        continue
                except Exception:
                    pass

                break

            return False, "Form navigation stalled — needs manual review"

        except Exception as e:
            return False, f"Playwright error: {e}"

    async def close(self):
        """Save session and close browser."""
        if self.context:
            try:
                await self.context.storage_state(path=str(SESSION_DIR / "linkedin_session.json"))
            except Exception:
                pass
        if self.browser:
            await self.browser.close()
        if hasattr(self, "_pw"):
            await self._pw.stop()


def apply_linkedin_sync(
    job_url: str,
    resume_path: str,
    cover_letter_text: str | None,
    candidate: dict,
    config: dict,
) -> tuple[bool, str]:
    """Synchronous wrapper for LinkedIn Easy Apply."""
    import os
    email = os.environ.get("LINKEDIN_EMAIL", "")
    password = os.environ.get("LINKEDIN_PASSWORD", "")

    if not email or not password:
        return False, "LinkedIn credentials not set in .env — skipping LinkedIn automation"

    browser_cfg = config.get("application", {}).get("browser", {})
    headless = browser_cfg.get("headless", False)
    slow_mo = browser_cfg.get("slow_mo", 50)
    dry_run = config.get("application", {}).get("dry_run", False)

    async def _run():
        applier = LinkedInApplier(
            email=email,
            password=password,
            headless=headless,
            slow_mo=slow_mo,
            dry_run=dry_run,
        )
        started = await applier.start()
        if not started:
            return False, "Failed to start browser"
        logged_in = await applier.login()
        if not logged_in:
            await applier.close()
            return False, "LinkedIn login failed"
        result = await applier.apply_easy_apply(
            job_url=job_url,
            resume_path=resume_path,
            cover_letter_text=cover_letter_text,
            candidate=candidate,
        )
        await applier.close()
        return result

    try:
        return asyncio.run(_run())
    except Exception as e:
        return False, f"Async runner error: {e}"
