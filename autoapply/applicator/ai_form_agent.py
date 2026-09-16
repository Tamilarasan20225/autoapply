"""
AI Form Agent — LLM-powered universal job application form filler.

This is the "last resort" fallback for any job application URL that doesn't match
a known ATS (Greenhouse, Lever, Ashby, Workday, iCIMS, SmartRecruiters).

Strategy:
  1. Load the page in a real browser
  2. Extract all visible form fields and their labels/context from the HTML
  3. Send a structured prompt to the LLM with:
       - Candidate profile data
       - List of form fields with labels, types, and selectors
  4. LLM returns a "fill plan" — {selector: value} mapping
  5. Execute the fill plan (click, type, select, upload)
  6. Look for a "Next" or "Submit" button and navigate forward
  7. Repeat for multi-step forms (up to max_steps)

Works for:
  - Custom company career portals (stripe.com/careers, notion.so/jobs, etc.)
  - Generic ATS platforms not specifically handled
  - Any one-off career page form

Uses Gemini Flash (supports text-only extraction from HTML — no vision needed)
"""

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Optional
from rich.console import Console

console = Console()


def _extract_form_fields_from_html(html: str) -> list[dict]:
    """
    Extract all form fields and their labels from raw HTML.
    Returns a list of field descriptors:
    [
      {
        "type": "text",
        "selector": "input#first_name",
        "name": "first_name",
        "label": "First Name",
        "placeholder": "Enter your first name",
        "required": true
      },
      ...
    ]
    """
    fields = []

    # Extract input fields
    input_pattern = re.compile(
        r'<input[^>]*>',
        re.IGNORECASE | re.DOTALL,
    )
    label_pattern = re.compile(
        r'<label[^>]*(?:for=["\']([^"\']*)["\'])?[^>]*>([^<]*)</label>',
        re.IGNORECASE,
    )

    # Build label map: id → label text
    label_map: dict[str, str] = {}
    for m in label_pattern.finditer(html):
        field_id = m.group(1) or ""
        label_text = re.sub(r'\s+', ' ', m.group(2) or "").strip()
        if field_id and label_text:
            label_map[field_id] = label_text

    # Parse input elements
    for m in input_pattern.finditer(html):
        tag = m.group(0)

        # Skip hidden, submit buttons that aren't useful
        input_type = re.search(r'type=["\']([^"\']*)["\']', tag)
        type_val = (input_type.group(1) if input_type else "text").lower()

        if type_val in ("hidden", "reset", "button", "image"):
            continue

        # Get attributes
        name_m = re.search(r'name=["\']([^"\']*)["\']', tag)
        id_m = re.search(r'id=["\']([^"\']*)["\']', tag)
        placeholder_m = re.search(r'placeholder=["\']([^"\']*)["\']', tag)
        required_m = re.search(r'\brequired\b', tag)

        name = name_m.group(1) if name_m else ""
        field_id = id_m.group(1) if id_m else ""
        placeholder = placeholder_m.group(1) if placeholder_m else ""

        # Build label from multiple sources
        label = label_map.get(field_id, "") or placeholder or name

        # Build CSS selector
        if field_id:
            selector = f"#{field_id}"
        elif name:
            selector = f"input[name='{name}']"
        else:
            selector = f"input[type='{type_val}']"

        if type_val in ("text", "email", "tel", "number", "url", "search", "file", "checkbox", "radio"):
            fields.append({
                "type": type_val,
                "selector": selector,
                "name": name,
                "label": label,
                "placeholder": placeholder,
                "required": bool(required_m),
            })

    # Parse textarea elements
    textarea_pattern = re.compile(
        r'<textarea[^>]*>(.*?)</textarea>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in textarea_pattern.finditer(html):
        tag_open = m.group(0).split(">")[0]
        name_m = re.search(r'name=["\']([^"\']*)["\']', tag_open)
        id_m = re.search(r'id=["\']([^"\']*)["\']', tag_open)
        placeholder_m = re.search(r'placeholder=["\']([^"\']*)["\']', tag_open)
        required_m = re.search(r'\brequired\b', tag_open)

        name = name_m.group(1) if name_m else ""
        field_id = id_m.group(1) if id_m else ""
        placeholder = placeholder_m.group(1) if placeholder_m else ""
        label = label_map.get(field_id, "") or placeholder or name

        selector = f"#{field_id}" if field_id else (f"textarea[name='{name}']" if name else "textarea")

        fields.append({
            "type": "textarea",
            "selector": selector,
            "name": name,
            "label": label,
            "placeholder": placeholder,
            "required": bool(required_m),
        })

    # Parse select elements
    select_pattern = re.compile(r'<select[^>]*>.*?</select>', re.IGNORECASE | re.DOTALL)
    for m in select_pattern.finditer(html):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']*)["\']', tag)
        id_m = re.search(r'id=["\']([^"\']*)["\']', tag)
        required_m = re.search(r'\brequired\b', tag)

        name = name_m.group(1) if name_m else ""
        field_id = id_m.group(1) if id_m else ""
        label = label_map.get(field_id, "") or name

        # Extract options
        options = re.findall(r'<option[^>]*>([^<]*)</option>', tag, re.IGNORECASE)
        options = [o.strip() for o in options if o.strip() and o.strip().lower() not in ("", "select", "choose")]

        selector = f"#{field_id}" if field_id else (f"select[name='{name}']" if name else "select")

        fields.append({
            "type": "select",
            "selector": selector,
            "name": name,
            "label": label,
            "options": options[:10],  # Cap options list
            "required": bool(required_m),
        })

    return fields[:40]  # Cap total fields to avoid huge LLM prompts


def _build_fill_prompt(candidate: dict, fields: list[dict], cover_letter_text: Optional[str] = None) -> str:
    """
    Build LLM prompt to generate field fill plan.
    """
    # Candidate summary
    name_parts = candidate.get("name", "").split(" ", 1)
    first = name_parts[0]
    last = name_parts[1] if len(name_parts) > 1 else ""

    cand_summary = f"""
Candidate Information:
- Full Name: {candidate.get("name", "")}
- First Name: {first}
- Last Name: {last}
- Email: {candidate.get("email", "")}
- Phone: {candidate.get("phone", "")}
- LinkedIn: {candidate.get("linkedin", "")}
- GitHub/Portfolio: {candidate.get("github", candidate.get("portfolio", ""))}
- Location: {candidate.get("location", "Bangalore, Karnataka, India")}
- City: {candidate.get("city", "Bangalore")}
- Country: India
- Years of Experience: {candidate.get("years_of_experience", "3")}
- Work Authorization: Authorized to work (no sponsorship needed)
- Expected Salary: {candidate.get("expected_salary", "Open to discussion")}
- Hear About Source: Job Board / LinkedIn
"""

    if cover_letter_text:
        cand_summary += f"\nCover Letter (truncated):\n{cover_letter_text[:500]}\n"

    fields_json = json.dumps(fields, indent=2)

    prompt = f"""You are an automated job application assistant. 

{cand_summary}

The following form fields have been found on a job application page:
{fields_json}

Your task: Generate a JSON fill plan for all relevant fields.

Rules:
1. For text/email/tel/url fields: provide the appropriate string value from candidate info
2. For select fields: pick the most appropriate option from the options list
   - For country: select "India" or similar
   - For work authorization: select "Yes" / "Authorized"
   - For sponsorship needed: select "No" / "Not required"
   - For source/referral: select "Job Board", "LinkedIn", or "Internet"
3. For checkbox fields (work auth, terms): value should be true
4. For textarea fields: provide appropriate text (cover letter text if cover-related, else empty string)
5. For file inputs: skip (value: null — handled separately)
6. Skip fields you don't have data for (value: null)
7. For radio buttons: skip (handled separately)

Return ONLY valid JSON with this structure:
{{
  "fill_plan": [
    {{"selector": "#first_name", "value": "Tamil", "action": "fill"}},
    {{"selector": "select[name='country']", "value": "India", "action": "select"}},
    {{"selector": "input[type='checkbox']", "value": true, "action": "check"}},
    ...
  ],
  "confidence": 0.85,
  "notes": "Brief note about any uncertain fields"
}}

Action types: "fill" (for text/email/tel/url/textarea), "select" (for dropdowns), "check" (for checkboxes)
"""
    return prompt


async def _execute_fill_plan(page, fill_plan: list[dict], resume_path: Optional[str] = None):
    """Execute the LLM-generated fill plan on the page."""
    for action in fill_plan:
        selector = action.get("selector", "")
        value = action.get("value")
        action_type = action.get("action", "fill")

        if value is None:
            continue

        try:
            el = await page.query_selector(selector)
            if not el:
                # Try broader selector
                continue

            if action_type == "fill":
                await el.fill(str(value))
                await asyncio.sleep(random.uniform(0.1, 0.3))

            elif action_type == "select":
                try:
                    await el.select_option(label=str(value))
                except Exception:
                    # Try by value
                    try:
                        await el.select_option(value=str(value))
                    except Exception:
                        pass

            elif action_type == "check":
                if value:
                    await el.check()
                else:
                    await el.uncheck()
                await asyncio.sleep(random.uniform(0.1, 0.2))

        except Exception:
            continue


async def _handle_file_uploads(page, resume_path: Optional[str], cover_letter_path: Optional[str]):
    """Handle file upload fields (resume, cover letter)."""
    if not resume_path:
        return

    resume_file = Path(resume_path)
    if not resume_file.exists():
        return

    try:
        # Direct file input
        file_inputs = await page.query_selector_all('input[type="file"]')

        for i, file_input in enumerate(file_inputs):
            try:
                if i == 0:
                    await file_input.set_input_files(str(resume_file))
                    await asyncio.sleep(2)
                elif i == 1 and cover_letter_path and Path(cover_letter_path).exists():
                    await file_input.set_input_files(cover_letter_path)
                    await asyncio.sleep(1)
            except Exception:
                pass

        # If no direct inputs, try button-triggered file chooser
        if not file_inputs:
            for btn_sel in [
                'button:has-text("Upload")', 'button:has-text("Attach")',
                'label:has-text("Upload")', '[role="button"]:has-text("Upload")',
            ]:
                try:
                    btn = await page.query_selector(btn_sel)
                    if btn:
                        async with page.expect_file_chooser(timeout=5000) as fc_info:
                            await btn.click()
                        fc = await fc_info.value
                        await fc.set_files(str(resume_file))
                        await asyncio.sleep(2)
                        break
                except Exception:
                    continue

    except Exception as e:
        console.print(f"  [dim]AI agent file upload error: {e}[/dim]")


async def _click_navigation_button(page, prefer_submit: bool = False) -> tuple[str, bool]:
    """
    Click Next or Submit button.
    Returns (action_taken, success): ("next" | "submit" | "none", bool)
    """
    # Submit buttons
    submit_selectors = [
        'button[type="submit"]:not([disabled])',
        'input[type="submit"]:not([disabled])',
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
        'button:has-text("Apply Now")',
        'button:has-text("Apply")',
        '#submit_app',
        '[data-testid*="submit"]',
    ]

    # Next/Continue buttons
    next_selectors = [
        'button:has-text("Next")',
        'button:has-text("Continue")',
        'button:has-text("Save and Continue")',
        'button:has-text("Proceed")',
        'a:has-text("Next")',
        '[data-testid*="next"]',
    ]

    # Try submit first if preferred
    if prefer_submit:
        for sel in submit_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    if not disabled and aria_disabled != "true":
                        await btn.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.5, 1))
                        await btn.click()
                        await asyncio.sleep(random.uniform(3, 5))
                        return "submit", True
            except Exception:
                continue

    # Try Next
    for sel in next_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                disabled = await btn.get_attribute("disabled")
                if not disabled:
                    await btn.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.5, 1))
                    await btn.click()
                    await asyncio.sleep(random.uniform(2, 3))
                    return "next", True
        except Exception:
            continue

    # Try submit if not already tried
    if not prefer_submit:
        for sel in submit_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    if not disabled and aria_disabled != "true":
                        await btn.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.5, 1))
                        await btn.click()
                        await asyncio.sleep(random.uniform(3, 5))
                        return "submit", True
            except Exception:
                continue

    return "none", False


def _check_success(content: str) -> bool:
    """Check page content for application success indicators."""
    success_phrases = [
        "thank you for applying",
        "application has been submitted",
        "successfully submitted",
        "application received",
        "your application",
        "we've received your application",
        "application complete",
        "thank you for your interest",
        "you have applied",
    ]
    content_lower = content.lower()
    return any(phrase in content_lower for phrase in success_phrases)


async def apply_ai_form_agent_async(
    apply_url: str,
    candidate: dict,
    resume_path: str,
    llm_client,
    cover_letter_text: Optional[str] = None,
    cover_letter_path: Optional[str] = None,
    headless: bool = True,
    dry_run: bool = False,
    max_steps: int = 8,
    confidence_threshold: float = 0.5,
    job_title: str = "",
    company: str = "",
) -> tuple[bool, str]:
    """
    Universal AI-powered form filler. Uses LLM to understand and fill any job form.

    Args:
        apply_url: Any job application URL
        candidate: Candidate profile dict
        resume_path: Path to resume file
        llm_client: LLMClient instance (from autoapply.scoring.llm_client)
        cover_letter_text: Cover letter body text
        cover_letter_path: Path to cover letter file
        headless: Run browser headlessly
        dry_run: Fill but don't submit
        max_steps: Max form steps to navigate
        confidence_threshold: Min LLM confidence to proceed with fill

    Returns:
        (success, message) tuple
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "Playwright not installed"

    if not llm_client or not llm_client.available:
        return False, "AI Form Agent requires LLM client — no LLM configured"

    if dry_run:
        console.print(f"  [dim][DRY RUN] AI Form Agent would process: {apply_url}[/dim]")
        return True, "dry_run"

    console.print(f"  [dim]AI Form Agent starting: {apply_url[:80]}...[/dim]")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        page = await context.new_page()

        try:
            await page.goto(apply_url, timeout=30000, wait_until="domcontentloaded")
            # Wait for page to fully settle before reading content
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # Timeout on networkidle is OK — proceed anyway
            await asyncio.sleep(random.uniform(2, 4))

            # ── Early exit: detect login walls before doing any work ──────────
            content_check = await page.content()
            content_lower = content_check.lower()
            url_now = page.url

            # LinkedIn login wall detection
            if "linkedin.com" in url_now.lower():
                has_login = any(s in content_lower for s in ["sign in", "join now", "authwall"])
                has_easy_apply = "easy apply" in content_lower
                if has_login and not has_easy_apply:
                    console.print("  [yellow]LinkedIn: Login required for Easy Apply — set LINKEDIN_EMAIL + LINKEDIN_PASSWORD in .env[/yellow]")
                    return False, "linkedin_login_required"

            # Greenhouse company portal login wall detection
            if any(s in url_now.lower() for s in [".com/careers", ".com/jobs", "/jobs/search"]):
                if "greenhouse" not in url_now.lower() and any(
                    s in content_lower for s in ["sign in to apply", "log in to apply", "create an account to apply"]
                ):
                    console.print("  [yellow]Company portal requires login — cannot auto-apply[/yellow]")
                    return False, "company_portal_login_required"

            # Navigate to apply page if we're on a job listing
            apply_btn = await page.query_selector(
                'a:has-text("Apply"), button:has-text("Apply Now"), '
                'a:has-text("Apply Now"), button:has-text("Apply for this role"), '
                '[href*="/apply"], [data-testid*="apply"]'
            )
            if apply_btn:
                href = await apply_btn.get_attribute("href")
                if href and href.startswith("http"):
                    await page.goto(href, timeout=30000)
                else:
                    await apply_btn.click()
                await asyncio.sleep(random.uniform(2, 3))

            steps_filled = 0
            pages_seen: set[str] = set()

            # Import the universal dynamic form handler
            from autoapply.applicator.dynamic_form_handler import (
                fill_all_form_fields,
                submit_with_retry,
                is_success_page,
            )

            for step in range(max_steps):
                current_url = page.url
                console.print(f"  [dim]AI Agent Step {step + 1}: {current_url[:60]}...[/dim]")

                # Detect if we've been here before (navigation loop)
                if current_url in pages_seen and step > 0:
                    console.print("  [yellow]AI Agent: navigation loop detected[/yellow]")
                    break
                pages_seen.add(current_url)

                # Check for success
                content = await page.content()
                if is_success_page(content, current_url):
                    console.print(f"  [green]✓ AI Form Agent: Application submitted![/green]")
                    return True, f"Applied via AI Form Agent ({steps_filled} steps filled)"

                # Handle file uploads
                await _handle_file_uploads(page, resume_path, cover_letter_path)

                # Use universal dynamic form handler to fill ALL fields
                # Fixed: now passes job_title and company for richer LLM context
                await fill_all_form_fields(
                    page=page,
                    candidate=candidate,
                    llm_client=llm_client,
                    cover_letter_text=cover_letter_text or "",
                    job_title=job_title,
                    company=company,
                    skip_filled=True,
                )
                steps_filled += 1

                await asyncio.sleep(random.uniform(1, 2))

                # Look for Submit button
                submit_btn_found = False
                for sel in [
                    'button[type="submit"]:not([disabled])',
                    'input[type="submit"]:not([disabled])',
                    'button:has-text("Submit Application")',
                    'button:has-text("Submit")',
                    'button:has-text("Apply Now")',
                    'button:has-text("Apply")',
                    '#submit_app',
                ]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            disabled = await btn.get_attribute("disabled")
                            aria_disabled = await btn.get_attribute("aria-disabled")
                            if not disabled and aria_disabled != "true":
                                submit_btn_found = True
                                break
                    except Exception:
                        continue

                if submit_btn_found or step >= 1:
                    # Try submit with validation retry
                    success_sub, msg_sub = await submit_with_retry(
                        page=page,
                        candidate=candidate,
                        llm_client=llm_client,
                        cover_letter_text=cover_letter_text or "",
                        max_retries=3,
                        dry_run=dry_run,
                    )
                    if success_sub:
                        console.print(f"  [green]✓ AI Form Agent: Application submitted![/green]")
                        return True, "Applied via AI Form Agent"
                    elif not submit_btn_found:
                        # No submit button — try Next
                        pass

                # Navigate forward with Next button
                action, nav_success = await _click_navigation_button(page, prefer_submit=False)

                if action == "none":
                    # Can't navigate — stuck
                    screenshot_path = f"/tmp/ai_agent_step{step}.png"
                    try:
                        await page.screenshot(path=screenshot_path)
                        console.print(f"  [dim]AI Agent stuck — screenshot: {screenshot_path}[/dim]")
                    except Exception:
                        pass
                    break

            # Check final state
            content = await page.content()
            if is_success_page(content, page.url):
                return True, "Applied via AI Form Agent"

            return False, f"AI Form Agent could not complete application (completed {steps_filled} steps)"

        except Exception as e:
            import traceback
            console.print(f"  [red]AI Form Agent error: {e}[/red]")
            console.print(f"  [dim]{traceback.format_exc()[:300]}[/dim]")
            return False, f"AI Form Agent error: {e}"
        finally:
            await browser.close()


def apply_ai_form_agent(
    apply_url: str,
    candidate: dict,
    resume_path: str,
    llm_client,
    cover_letter_text: Optional[str] = None,
    cover_letter_path: Optional[str] = None,
    headless: bool = True,
    dry_run: bool = False,
    max_steps: int = 8,
    confidence_threshold: float = 0.5,
    job_title: str = "",
    company: str = "",
) -> tuple[bool, str]:
    """
    Synchronous wrapper for AI Form Agent. Event-loop safe.
    Fixed: now accepts and forwards job_title and company to async function.
    """
    coro = apply_ai_form_agent_async(
        apply_url=apply_url,
        candidate=candidate,
        resume_path=resume_path,
        llm_client=llm_client,
        cover_letter_text=cover_letter_text,
        cover_letter_path=cover_letter_path,
        headless=headless,
        dry_run=dry_run,
        max_steps=max_steps,
        confidence_threshold=confidence_threshold,
        job_title=job_title,
        company=company,
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
                return future.result(timeout=240)
    else:
        return asyncio.run(coro)
