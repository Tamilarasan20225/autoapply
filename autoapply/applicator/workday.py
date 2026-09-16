"""
Workday ATS Applier — Playwright-based multi-step wizard automation.

Workday is used by: Amazon, Flipkart, Zomato, Swiggy, PhonePe, Juspay, Meesho,
CRED, Nykaa, Ola, PayU, Navi, Jio, Byju's, Walmart Global Tech India, and hundreds
more Indian and global tech companies.

Workday's application flow (typically 4-8 steps):
  Step 1: My Information    — name, email, phone, address, source
  Step 2: My Experience     — resume upload, work history, education
  Step 3: Application Qs   — company-specific screening questions
  Step 4: Voluntary Discl.  — EEO, veteran, disability (optional)
  Step 5: Review            — final review before submit
  Step 6: Submit

Key challenges:
  - All rendered via React/custom web components with data-automation-id attributes
  - File upload requires clicking a button to reveal the hidden input
  - Dynamic fields — form changes based on previous answers
  - Rate limiting / bot detection on some instances
  - Sometimes gated behind LinkedIn SSO or email verification
"""

import asyncio
import random
import time
from pathlib import Path
from typing import Optional
from rich.console import Console

console = Console()

# Workday data-automation-id selectors (stable across Workday versions)
WD = {
    # Form container
    "form": '[data-automation-id="formContainer"]',
    "page_title": '[data-automation-id="pageTitle"], [data-automation-id="stepTitle"]',
    
    # Step 1: My Information
    "first_name": '[data-automation-id="firstName"], input[id*="firstName"]',
    "last_name": '[data-automation-id="lastName"], input[id*="lastName"]',
    "email": '[data-automation-id="email"], input[type="email"]',
    "phone": '[data-automation-id="phone"], input[type="tel"]',
    "address_line1": '[data-automation-id="addressLine1"]',
    "city": '[data-automation-id="city"]',
    "country": '[data-automation-id="countryDropdown"]',
    "state": '[data-automation-id="stateDropdown"]',
    "source": '[data-automation-id="sourcePrompt"]',  # How did you hear about us
    
    # Step 2: My Experience
    "resume_upload_btn": '[data-automation-id="file-upload-drop-zone"] button, '
                         '[data-automation-id="resumeUpload"] button, '
                         'button[aria-label*="upload"], button[aria-label*="Upload resume"]',
    "file_input": 'input[type="file"]',
    "linkedin_url": '[data-automation-id="linkedinUrl"], input[placeholder*="LinkedIn"]',
    "website_url": '[data-automation-id="website"], input[placeholder*="website"]',
    
    # Navigation
    "next_btn": '[data-automation-id="bottom-navigation-next-button"], '
                'button[data-automation-id="nextButton"], '
                'button:has-text("Next"), button:has-text("Save and Continue")',
    "save_btn": '[data-automation-id="bottom-navigation-save-button"], '
                'button:has-text("Save")',
    "submit_btn": '[data-automation-id="bottom-navigation-next-button"]:has-text("Submit"), '
                  'button[data-automation-id="submitButton"], '
                  'button:has-text("Submit"), button:has-text("Apply")',
    "review_btn": 'button:has-text("Review")',
    
    # Common question types
    "radio_yes": 'input[type="radio"][value="Yes"], input[type="radio"][id*="Yes"]',
    "radio_no": 'input[type="radio"][value="No"], input[type="radio"][id*="No"]',
    "dropdown": 'select, [data-automation-id*="dropdown"]',
    "checkbox": 'input[type="checkbox"]',
}


async def _random_delay(min_s: float = 0.5, max_s: float = 2.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _fill_field(page, selector: str, value: str, clear_first: bool = True):
    """Fill a single field, trying multiple selectors."""
    for sel in selector.split(", "):
        sel = sel.strip()
        try:
            el = await page.query_selector(sel)
            if el:
                if clear_first:
                    await el.click()
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Delete")
                await el.fill(value)
                await _random_delay(0.2, 0.5)
                return True
        except Exception:
            continue
    return False


async def _wait_for_workday_form(page, timeout: int = 20000) -> bool:
    """Wait for Workday form to be ready."""
    try:
        await page.wait_for_selector(WD["form"], timeout=timeout)
        return True
    except Exception:
        # Try alternative indicators
        try:
            await page.wait_for_selector(
                '[data-automation-id="firstName"], '
                '[data-automation-id="email"], '
                '.WGUL, [class*="workday"]',
                timeout=timeout,
            )
            return True
        except Exception:
            return False


async def _get_current_step(page) -> str:
    """
    Detect which step of the Workday wizard we're on.
    Returns: "my_information" | "my_experience" | "application_questions" 
             | "voluntary" | "review" | "submit" | "success" | "unknown"
    """
    try:
        # Check page/step title
        title_el = await page.query_selector(WD["page_title"])
        if title_el:
            title = (await title_el.text_content() or "").lower().strip()
            if "my information" in title or "personal" in title:
                return "my_information"
            elif "my experience" in title or "experience" in title or "resume" in title:
                return "my_experience"
            elif "application questions" in title or "screening" in title:
                return "application_questions"
            elif "voluntary" in title or "disclosure" in title or "eeoc" in title:
                return "voluntary"
            elif "review" in title:
                return "review"
            elif "submit" in title:
                return "submit"

        # Check URL for step hints — Workday uses both query params and hash routing
        url = page.url
        if ("step=My_Information" in url or "step=1" in url or
                "#step=My_Information" in url or "/step/1" in url):
            return "my_information"
        elif ("step=My_Experience" in url or "step=2" in url or
              "#step=My_Experience" in url or "/step/2" in url):
            return "my_experience"
        elif ("step=Application_Questions" in url or "step=3" in url or
              "#step=Application_Questions" in url or "/step/3" in url):
            return "application_questions"

        # Check for success indicators
        content = await page.content()
        if any(p in content.lower() for p in [
            "thank you for applying", "application submitted",
            "successfully submitted", "application received",
        ]):
            return "success"

    except Exception:
        pass

    return "unknown"


async def _fill_my_information(page, candidate: dict):
    """Fill Step 1: My Information."""
    console.print("  [dim]  Filling: My Information...[/dim]")

    name_parts = candidate.get("name", "").split(" ", 1)
    first = name_parts[0]
    last = name_parts[1] if len(name_parts) > 1 else ""

    await _fill_field(page, WD["first_name"], first)
    await _fill_field(page, WD["last_name"], last)
    await _fill_field(page, WD["email"], candidate.get("email", ""))
    await _fill_field(page, WD["phone"], candidate.get("phone", ""))

    # Address (Workday often asks for this)
    address = candidate.get("address", "Bangalore, Karnataka, India")
    await _fill_field(page, WD["address_line1"], address.split(",")[0].strip())

    # City
    city = candidate.get("city", "Bangalore")
    await _fill_field(page, WD["city"], city)

    # Country dropdown
    try:
        country_el = await page.query_selector(WD["country"])
        if country_el:
            # Try to select India
            await country_el.click()
            await _random_delay(0.5, 1)
            # Type to search
            await page.keyboard.type("India")
            await _random_delay(0.3, 0.5)
            # Select from dropdown list
            india_option = await page.query_selector(
                '[role="option"]:has-text("India"), option:has-text("India")'
            )
            if india_option:
                await india_option.click()
            else:
                await page.keyboard.press("Enter")
    except Exception:
        pass

    # Source / "How did you hear about us"
    try:
        source_el = await page.query_selector(WD["source"])
        if source_el:
            await source_el.click()
            await _random_delay(0.3, 0.6)
            # Try to select "Job Board" or "Internet" option
            for option_text in ["Job Board", "Internet", "Online Job Board", "LinkedIn", "Job Site"]:
                option = await page.query_selector(f'[role="option"]:has-text("{option_text}")')
                if option:
                    await option.click()
                    break
    except Exception:
        pass

    await _random_delay(0.5, 1)


async def _fill_my_experience(page, candidate: dict, resume_path: str):
    """Fill Step 2: My Experience — resume upload and profile links."""
    console.print("  [dim]  Filling: My Experience...[/dim]")

    resume_file = Path(resume_path)

    # Try to upload resume
    if resume_file.exists():
        uploaded = False

        # Method 1: Direct file input (sometimes visible)
        try:
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(str(resume_file))
                await _random_delay(2, 4)
                console.print(f"  [dim]  Resume uploaded (direct): {resume_file.name}[/dim]")
                uploaded = True
        except Exception:
            pass

        # Method 2: Click "Select files" / "Upload Resume" button
        if not uploaded:
            try:
                for btn_sel in [
                    WD["resume_upload_btn"],
                    'button:has-text("Select files")',
                    'button:has-text("Upload resume")',
                    'button:has-text("Upload Resume")',
                    'button:has-text("Browse")',
                    '[aria-label*="Upload"]',
                    'label:has-text("Upload")',
                ]:
                    try:
                        btn = await page.query_selector(btn_sel)
                        if btn:
                            async with page.expect_file_chooser(timeout=5000) as fc_info:
                                await btn.click()
                            fc = await fc_info.value
                            await fc.set_files(str(resume_file))
                            await _random_delay(2, 4)
                            console.print(f"  [dim]  Resume uploaded (btn): {resume_file.name}[/dim]")
                            uploaded = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        if not uploaded:
            console.print(f"  [yellow]  Could not upload resume to Workday form[/yellow]")

    # LinkedIn URL
    if candidate.get("linkedin"):
        await _fill_field(page, WD["linkedin_url"], candidate["linkedin"])

    # Website / Portfolio
    if candidate.get("github"):
        await _fill_field(page, WD["website_url"], candidate.get("github", ""))

    await _random_delay(0.5, 1)


async def _handle_application_questions(page, candidate: dict, llm_client=None):
    """
    Handle Step 3: Application Questions.

    Strategies:
      1. For Yes/No questions → select Yes for work auth, No for sponsorship
      2. For dropdowns → select India/Bangalore/Yes where appropriate
      3. For text questions → use LLM to generate answers if available
      4. For number fields → fill years of experience
    """
    console.print("  [dim]  Filling: Application Questions...[/dim]")

    try:
        # ── Handle Yes/No radio buttons ───────────────────────────────────────
        # First, try to understand what each question is asking
        question_groups = await page.query_selector_all(
            '[data-automation-id*="formField"], '
            '.WGUG, [class*="formField"], '
            '[role="radiogroup"]'
        )

        for group in question_groups:
            try:
                label_el = await group.query_selector('label, [role="heading"], legend')
                label = (await label_el.text_content() if label_el else "").lower()

                # Work authorization
                if any(kw in label for kw in ["authorized", "authorization", "eligible", "legally", "work in"]):
                    yes_radio = await group.query_selector('input[type="radio"][value="Yes"], input[type="radio"][id*="Yes"]')
                    if yes_radio:
                        await yes_radio.check()
                        await _random_delay(0.2, 0.4)

                # Sponsorship / Visa
                elif any(kw in label for kw in ["sponsor", "visa", "sponsorship", "require"]):
                    no_radio = await group.query_selector('input[type="radio"][value="No"], input[type="radio"][id*="No"]')
                    if no_radio:
                        await no_radio.check()
                        await _random_delay(0.2, 0.4)

                # Currently employed / available to start
                elif any(kw in label for kw in ["currently employed", "available", "notice"]):
                    yes_radio = await group.query_selector('input[type="radio"]')
                    if yes_radio:
                        await yes_radio.check()
                        await _random_delay(0.2, 0.4)

            except Exception:
                continue

        # ── Handle dropdowns ──────────────────────────────────────────────────
        selects = await page.query_selector_all("select")
        for sel_el in selects:
            try:
                # Get associated label
                sel_id = await sel_el.get_attribute("id")
                label = ""
                if sel_id:
                    label_el = await page.query_selector(f'label[for="{sel_id}"]')
                    if label_el:
                        label = (await label_el.text_content() or "").lower()

                options = await sel_el.query_selector_all("option")
                opt_texts = [(await o.text_content() or "").strip() for o in options]

                # Country selection
                if any(kw in label for kw in ["country", "nation"]):
                    for opt in opt_texts:
                        if "india" in opt.lower():
                            await sel_el.select_option(label=opt)
                            break

                # State selection
                elif any(kw in label for kw in ["state", "province"]):
                    for opt in opt_texts:
                        if "karnataka" in opt.lower() or "bangalore" in opt.lower():
                            await sel_el.select_option(label=opt)
                            break

                # Yes/No or authorization
                elif any(kw in label for kw in ["authorized", "eligible", "sponsor"]):
                    prefer_yes = "sponsor" not in label
                    for opt in opt_texts:
                        if prefer_yes and opt.lower() in ("yes", "y"):
                            await sel_el.select_option(label=opt)
                            break
                        elif not prefer_yes and opt.lower() in ("no", "n"):
                            await sel_el.select_option(label=opt)
                            break

                # Source / "How did you hear"
                elif any(kw in label for kw in ["hear", "source", "find"]):
                    for preferred in ["Job Board", "LinkedIn", "Internet", "Online"]:
                        for opt in opt_texts:
                            if preferred.lower() in opt.lower():
                                await sel_el.select_option(label=opt)
                                break
                        else:
                            continue
                        break

                await _random_delay(0.2, 0.4)
            except Exception:
                continue

        # ── Handle number inputs (years of experience) ────────────────────────
        number_inputs = await page.query_selector_all('input[type="number"]')
        for inp in number_inputs:
            try:
                val = await inp.input_value()
                if not val:
                    # Check label for context
                    inp_id = await inp.get_attribute("id")
                    label = ""
                    if inp_id:
                        label_el = await page.query_selector(f'label[for="{inp_id}"]')
                        if label_el:
                            label = (await label_el.text_content() or "").lower()

                    if "year" in label or "experience" in label:
                        await inp.fill(str(candidate.get("years_of_experience", "3")))
                    else:
                        await inp.fill("0")
                    await _random_delay(0.2, 0.3)
            except Exception:
                continue

        # ── Handle text areas (optional but helpful) ──────────────────────────
        textareas = await page.query_selector_all("textarea")
        for ta in textareas:
            try:
                val = await ta.input_value()
                if not val:
                    ta_id = await ta.get_attribute("id")
                    label = ""
                    if ta_id:
                        label_el = await page.query_selector(f'label[for="{ta_id}"]')
                        if label_el:
                            label = (await label_el.text_content() or "").lower()

                    # Cover letter or additional info
                    if any(kw in label for kw in ["cover", "letter", "additional", "info", "statement"]):
                        default_text = candidate.get(
                            "default_cover_letter",
                            "I am excited about this opportunity and believe my experience aligns well with the role's requirements."
                        )
                        await ta.fill(default_text[:1000])
                        await _random_delay(0.3, 0.5)
            except Exception:
                continue

    except Exception as e:
        console.print(f"  [yellow]  Application questions error: {e}[/yellow]")


async def _handle_voluntary_disclosures(page):
    """
    Handle voluntary disclosure section (EEO, veteran status, disability).
    Select "Decline to Answer" / "I don't wish to answer" for all.
    """
    console.print("  [dim]  Filling: Voluntary Disclosures...[/dim]")

    try:
        # EEO / Disability / Veteran status — prefer "Decline to identify" options
        decline_phrases = [
            "decline", "prefer not", "i don't wish", "no answer",
            "choose not", "rather not",
        ]

        selects = await page.query_selector_all("select")
        for sel_el in selects:
            try:
                options = await sel_el.query_selector_all("option")
                for opt in options:
                    opt_text = (await opt.text_content() or "").lower()
                    if any(phrase in opt_text for phrase in decline_phrases):
                        val = await opt.get_attribute("value")
                        if val:
                            await sel_el.select_option(value=val)
                        break
                await _random_delay(0.2, 0.4)
            except Exception:
                continue

        # Radio buttons for voluntary section
        radios = await page.query_selector_all('input[type="radio"]')
        for radio in radios:
            try:
                radio_id = await radio.get_attribute("id")
                label = ""
                if radio_id:
                    label_el = await page.query_selector(f'label[for="{radio_id}"]')
                    if label_el:
                        label = (await label_el.text_content() or "").lower()

                if any(phrase in label for phrase in decline_phrases):
                    await radio.check()
                    await _random_delay(0.2, 0.3)
            except Exception:
                continue

    except Exception as e:
        console.print(f"  [dim]  Voluntary disclosures: {e}[/dim]")


async def _click_next(page) -> bool:
    """
    Click the Next / Save and Continue button.
    Returns True if clicked successfully.
    """
    for selector in [
        '[data-automation-id="bottom-navigation-next-button"]',
        'button[data-automation-id="nextButton"]',
        'button:has-text("Save and Continue")',
        'button:has-text("Next")',
        'button:has-text("Continue")',
    ]:
        try:
            btn = await page.query_selector(selector)
            if btn:
                is_disabled = await btn.get_attribute("disabled")
                is_aria_disabled = await btn.get_attribute("aria-disabled")
                if not is_disabled and is_aria_disabled != "true":
                    await btn.scroll_into_view_if_needed()
                    await _random_delay(0.5, 1)
                    await btn.click()
                    await _random_delay(2, 4)
                    return True
        except Exception:
            continue
    return False


async def _click_submit(page) -> bool:
    """
    Click the final Submit button.
    Returns True if clicked successfully.
    """
    for selector in [
        '[data-automation-id="bottom-navigation-next-button"]',
        'button[data-automation-id="submitButton"]',
        'button:has-text("Submit")',
        'button:has-text("Apply")',
        'button:has-text("Submit Application")',
    ]:
        try:
            btn = await page.query_selector(selector)
            if btn:
                text = (await btn.text_content() or "").strip().lower()
                if any(kw in text for kw in ["submit", "apply"]):
                    is_disabled = await btn.get_attribute("disabled")
                    if not is_disabled:
                        await btn.scroll_into_view_if_needed()
                        await _random_delay(0.5, 1)
                        await btn.click()
                        await _random_delay(4, 6)
                        return True
        except Exception:
            continue
    return False


async def apply_workday_async(
    apply_url: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str] = None,
    headless: bool = True,
    dry_run: bool = False,
    llm_client=None,
) -> tuple[bool, str]:
    """
    Apply to a Workday job application form.

    Args:
        apply_url: Direct Workday application URL
        candidate: Candidate profile dict
        resume_path: Path to tailored resume
        cover_letter_text: Cover letter body text
        headless: Run browser headlessly
        dry_run: Prepare but don't submit
        llm_client: Optional LLM for answering custom questions

    Returns:
        (success, message) tuple
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "Playwright not installed. Run: pip install playwright && playwright install chromium"

    if dry_run:
        console.print(f"  [dim][DRY RUN] Would open Workday form: {apply_url}[/dim]")
        return True, "dry_run"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,900",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            # Remove automation fingerprints
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        # Mask automation signals
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        page = await context.new_page()

        try:
            console.print(f"  [dim]Opening Workday form: {apply_url[:80]}...[/dim]")
            await page.goto(apply_url, timeout=45000, wait_until="networkidle")
            await _random_delay(3, 5)

            # Wait for Workday form to load
            form_loaded = await _wait_for_workday_form(page, timeout=20000)
            if not form_loaded:
                # Check if there's a sign-in wall
                content = await page.content()
                if "sign in" in content.lower() or "create account" in content.lower():
                    return False, "workday_signin_required"
                if "job is no longer available" in content.lower() or "position has been filled" in content.lower():
                    return False, "Job no longer available"
                return False, f"Workday form not detected at: {page.url}"

            console.print("  [dim]  Workday form loaded ✓[/dim]")

            # ── Multi-step wizard navigation ───────────────────────────────────
            max_steps = 10
            steps_completed = []

            for step_num in range(max_steps):
                await _random_delay(1, 2)

                current_step = await _get_current_step(page)
                console.print(f"  [dim]  Step {step_num + 1}: {current_step}[/dim]")

                if current_step == "success":
                    console.print(f"  [green]✓ Workday application submitted![/green]")
                    return True, "Applied via Workday Playwright"

                if current_step in steps_completed and step_num > 0:
                    # We're stuck on the same step — something failed
                    screenshot_path = f"/tmp/workday_stuck_{step_num}.png"
                    try:
                        await page.screenshot(path=screenshot_path)
                    except Exception:
                        pass
                    return False, f"Stuck on step: {current_step} — check screenshot at {screenshot_path}"

                steps_completed.append(current_step)

                # Fill based on current step
                if current_step == "my_information":
                    await _fill_my_information(page, candidate)

                elif current_step == "my_experience":
                    await _fill_my_experience(page, candidate, resume_path)

                elif current_step == "application_questions":
                    await _handle_application_questions(page, candidate, llm_client)

                elif current_step == "voluntary":
                    await _handle_voluntary_disclosures(page)

                elif current_step == "review":
                    # On review page — look for the final Submit button
                    console.print("  [dim]  On review page — submitting...[/dim]")
                    submitted = await _click_submit(page)
                    if submitted:
                        await _random_delay(4, 6)
                        # Verify success
                        content = await page.content()
                        if any(p in content.lower() for p in [
                            "thank you", "application submitted", "successfully", "received"
                        ]):
                            console.print(f"  [green]✓ Workday applied successfully[/green]")
                            return True, "Applied via Workday Playwright"
                        else:
                            # Take screenshot to verify
                            screenshot_path = "/tmp/workday_submitted.png"
                            try:
                                await page.screenshot(path=screenshot_path)
                                console.print(f"  [yellow]Submitted — verify at: {page.url}[/yellow]")
                                console.print(f"  [dim]Screenshot: {screenshot_path}[/dim]")
                            except Exception:
                                pass
                            return True, f"Submitted via Workday Playwright (verify at {page.url})"
                    else:
                        return False, "Could not find Submit button on Workday review page"

                elif current_step == "unknown":
                    # Try to handle unknown step generically
                    console.print("  [dim]  Unknown step — attempting generic form fill...[/dim]")
                    await _handle_application_questions(page, candidate, llm_client)

                # Navigate to next step
                await _random_delay(1, 2)
                clicked = await _click_next(page)

                if not clicked:
                    # Maybe we're on the final step already
                    submitted = await _click_submit(page)
                    if submitted:
                        await _random_delay(4, 6)
                        content = await page.content()
                        if any(p in content.lower() for p in [
                            "thank you", "application submitted", "successfully", "received"
                        ]):
                            console.print(f"  [green]✓ Workday applied successfully[/green]")
                            return True, "Applied via Workday Playwright"
                        return True, f"Submitted via Workday Playwright (verify at {page.url})"
                    else:
                        # Take screenshot for debugging
                        screenshot_path = f"/tmp/workday_stuck_step{step_num}.png"
                        try:
                            await page.screenshot(path=screenshot_path)
                            console.print(f"  [yellow]Could not navigate forward. Screenshot: {screenshot_path}[/yellow]")
                        except Exception:
                            pass
                        return False, f"Navigation stalled at step {step_num + 1} ({current_step})"

                await _random_delay(2, 3)

            return False, "Exceeded maximum Workday wizard steps (10)"

        except Exception as e:
            import traceback
            console.print(f"  [red]Workday error: {e}[/red]")
            console.print(f"  [dim]{traceback.format_exc()[:500]}[/dim]")
            return False, f"Workday Playwright error: {e}"
        finally:
            await browser.close()


def apply_workday(
    apply_url: str,
    candidate: dict,
    resume_path: str,
    cover_letter_text: Optional[str] = None,
    headless: bool = True,
    dry_run: bool = False,
    llm_client=None,
) -> tuple[bool, str]:
    """
    Synchronous wrapper for Workday apply. Event-loop safe.

    Args:
        apply_url: Direct Workday application URL (e.g. wd3.myworkdayjobs.com/...)
        candidate: Candidate profile dict
        resume_path: Path to tailored resume
        cover_letter_text: Cover letter text
        headless: Run browser headlessly
        dry_run: Prepare but don't submit
        llm_client: Optional LLM for custom question answering

    Returns:
        (success, message) tuple
    """
    coro = apply_workday_async(
        apply_url=apply_url,
        candidate=candidate,
        resume_path=resume_path,
        cover_letter_text=cover_letter_text,
        headless=headless,
        dry_run=dry_run,
        llm_client=llm_client,
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
