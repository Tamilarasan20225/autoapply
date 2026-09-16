"""
Dynamic Form Handler — Universal intelligent form filler for job applications.

Handles ALL field types found on real ATS forms (Greenhouse, Lever, Workday, etc.):
  - Standard fields: first name, last name, email, phone
  - Social profiles: LinkedIn (via label matching, not fixed selector), GitHub, portfolio
  - Location: city autocomplete, country autocomplete
  - Custom screening questions: work auth, sponsorship, onsite availability, notice period
  - Acknowledgements & consent checkboxes
  - EEO/voluntary disclosures (decline to answer)
  - Open-ended textareas (LLM-generated answers)
  - Radio button groups (Lever-style cards[UUID] patterns)
  - Select dropdowns (standard and custom)
  - Pre-submit validation: detects errors, fixes them, retries

Key insight from real form inspection:
  - Greenhouse custom fields use id=`question_XXXXXXX` with a proper <label>
  - The label text is the ONLY reliable way to know what the field asks
  - Lever uses name=`urls[LinkedIn]`, name=`cards[UUID]` (radio groups)
  - Autocomplete fields (country, city) need type-then-select handling
  - "By submitting I acknowledge" = a required text field needing "Yes" (Vercel!)
"""

import asyncio
import json
import random
import re
from typing import Optional
from rich.console import Console

console = Console()


# ══════════════════════════════════════════════════════════════════════════════
# LABEL → VALUE RESOLVER (heuristic, no LLM needed)
# ══════════════════════════════════════════════════════════════════════════════

def resolve_value_from_label(label: str, candidate: dict, cover_letter_text: str = "") -> Optional[str]:
    """
    Given a field label/question text, return the appropriate answer from candidate data.
    Returns None if no heuristic match — LLM will handle it.

    Designed from real form inspection of Postman, Groww, Vercel, Neon forms.
    """
    if not label:
        return None

    lbl = label.lower().strip().rstrip("*").strip()

    # ── Identity ──────────────────────────────────────────────────────────────
    if re.search(r'\bfirst\s*name\b', lbl):
        return candidate.get("first_name", candidate.get("name", "").split()[0])

    if re.search(r'\blast\s*name\b', lbl):
        name_parts = candidate.get("name", "").split()
        return candidate.get("last_name", name_parts[-1] if len(name_parts) > 1 else "")

    if re.search(r'\bfull\s*name\b|\bname\b', lbl) and "company" not in lbl and "brand" not in lbl:
        return candidate.get("name", "")

    if re.search(r'\bemail\b', lbl):
        return candidate.get("email", "")

    if re.search(r'\bphone|mobile|contact\s*number\b', lbl):
        return candidate.get("phone", "")

    # ── Social profiles — only if clearly a profile field, not a question ─────
    # Must check these BEFORE broad location/availability checks
    if re.search(r'linkedin', lbl) and re.search(r'profile|url|link|handle|account', lbl):
        return candidate.get("linkedin", "")
    if re.search(r'^linkedin', lbl):  # starts with linkedin
        return candidate.get("linkedin", "")

    if re.search(r'\bgithub\b', lbl) and re.search(r'profile|url|link|handle|account|username', lbl):
        return candidate.get("github", "")
    if re.search(r'^github', lbl):  # starts with github
        return candidate.get("github", "")

    if re.search(r'portfolio|personal\s*site|personal\s*website\b', lbl):
        return candidate.get("portfolio_url", candidate.get("github", ""))

    if re.search(r'\bwebsite\b|\burl\b|\bsite\b', lbl) and "company" not in lbl and "are you" not in lbl:
        return candidate.get("portfolio_url", candidate.get("github", ""))

    if re.search(r'twitter|x\.com', lbl):
        return candidate.get("twitter", "")

    # ── Work authorization — check BEFORE country to avoid "authorization to work in the country" matching \bcountry\b
    # "Your authorization to work in the country" — Vercel form pattern
    if re.search(r'your\s*authoriz|authoriz.*work\s*in\b|authorized\s*to\s*work', lbl):
        return "Yes"

    # ── Location ──────────────────────────────────────────────────────────────
    if re.search(r'\bcity\b|\bcurrent\s*location\b|\blocation\b', lbl) and "office" not in lbl:
        return candidate.get("city", "Bangalore")

    if re.search(r'\bcountry\b', lbl) and "authoriz" not in lbl:
        return candidate.get("country", "India")

    if re.search(r'\bstate\b|\bprovince\b', lbl):
        return candidate.get("state", "Karnataka")

    if re.search(r'\baddress\b|\bstreet\b', lbl):
        return candidate.get("address_line1", candidate.get("city", "Bangalore"))

    if re.search(r'\bzip\b|\bpincode\b|\bpostal\b', lbl):
        return candidate.get("zip_code", "560001")

    # ── Employment ────────────────────────────────────────────────────────────
    # Notice period checked FIRST (before current company) to avoid org pattern conflict
    if re.search(r'notice\s*period|when\s*can\s*you\s*start|availability\b|start\s*date', lbl):
        return candidate.get("notice_period", "60 days")

    if re.search(r'earliest\s*start', lbl):
        return candidate.get("earliest_start", "2 months from offer")

    # "What is the notice period in your current organization?" — already caught above
    # Now match company/employer (must not contain notice/period)
    if re.search(r'current\s*(company|employer|organization|org)\b', lbl) and "notice" not in lbl and "period" not in lbl:
        return candidate.get("current_company", "Zoho Corporation")

    if re.search(r'current\s*(title|role|position|designation)\b|job\s*title\b', lbl):
        return candidate.get("current_title", "Software Engineer")

    if re.search(r'(years|yrs)[\s\w]*experience|experience[\s\w]*(years|yrs)\b', lbl):
        return candidate.get("years_of_experience", "3")

    if re.search(r'\bjava\b.*(year|exp)|experience.*\bjava\b', lbl):
        return candidate.get("years_java", "3")

    if re.search(r'\bpython\b.*(year|exp)|experience.*\bpython\b', lbl):
        return candidate.get("years_python", "2")

    # ── Work authorization ────────────────────────────────────────────────────
    if re.search(r'(eligible|authorized|legally\s*eligible|legal\s*right|right|eligible\s*to\s*legally)\s*(to\s*)?(work|employ)', lbl):
        return "Yes"
    # Catch "currently eligible to legally work" pattern
    if re.search(r'currently\s*eligible|eligible.*legally|legally.*eligible', lbl):
        return "Yes"

    if re.search(r'(require|need|will\s*you\s*require)\s*(visa\s*)?sponsor', lbl):
        return "No"

    # "Your authorization to work in the country" — Vercel form pattern
    if re.search(r'your\s*authoriz|authoriz.*work\s*in\b|authorized\s*to\s*work', lbl):
        return "Yes"

    if re.search(r'work\s*authoriz', lbl):
        return candidate.get("work_authorization", "Yes - Indian citizen")

    if re.search(r'\bvisa\b|\bwork\s*permit\b', lbl) and "sponsor" not in lbl:
        return "Not required - Indian citizen"

    if re.search(r'\bcitizenship\b', lbl):
        return candidate.get("citizenship", "Indian")

    # ── Location/Relocation questions ─────────────────────────────────────────
    if re.search(r'relocat\b', lbl):
        return candidate.get("willing_to_relocate", "Yes")

    if re.search(r'available\s*(to\s*)?(work\s*)?(on.?site|in\s*office|onsite)', lbl):
        return "Yes"

    if re.search(r'(work|based)\s*(in|at|from)\s*(office|onsite|on-site|bangalore|india)', lbl):
        return "Yes"

    if re.search(r'(currently\s*based|currently\s*located|based\s*in)', lbl):
        return "Yes" if "india" in lbl or "bangalore" in lbl else "India - Bangalore"

    if re.search(r'remote|work\s*from\s*home|wfh', lbl):
        return candidate.get("remote_preference", "Open to remote or hybrid")

    # ── Compensation ──────────────────────────────────────────────────────────
    if re.search(r'salary|compensation|ctc|package|lpa|pay\b|remuneration', lbl):
        return candidate.get("expected_salary_inr", "25-30 LPA")

    if re.search(r'current\s*salary|current\s*ctc', lbl):
        return candidate.get("current_salary_inr", "Confidential")

    # ── Company-specific boolean questions ────────────────────────────────────
    if re.search(r'relative|family\s*member.*(work|employ|company)', lbl):
        return candidate.get("has_relative_at_company", "No")

    if re.search(r'previously\s*(employ|work|hired)|worked.*before|former.*employ|been\s*employ', lbl):
        return candidate.get("previously_employed_here", "No")

    if re.search(r'convicted|criminal|felony', lbl):
        return "No"

    if re.search(r'18\s*years\s*old|over\s*18|age\s*requirement', lbl):
        return "Yes"

    # ── Consent / Acknowledgement / Certification (Vercel "by submitting..." field) ──
    if re.search(r'(by\s*submitting|i\s*acknowledge|i\s*agree|i\s*certif|i\s*confirm|i\s*consent|i\s*understand|i\s*authorize)', lbl):
        return "Yes"

    if re.search(r'consent|gdpr|privacy\s*policy|data\s*process|terms\s*(and\s*condition)?', lbl):
        return candidate.get("consent_to_data_processing", "Yes")

    if re.search(r'double.?check|verify.*information|please.*confirm|confirm.*detail', lbl):
        return "Yes"

    # ── Source / referral ─────────────────────────────────────────────────────
    if re.search(r'hear\s*about|how\s*did\s*you\s*(find|learn|discover)|referral|source\b', lbl):
        return candidate.get("how_did_you_hear", "LinkedIn / Job Board")

    # ── EEO / Voluntary disclosures ───────────────────────────────────────────
    if re.search(r'\bgender\b|\bgender\s*ident', lbl):
        return candidate.get("gender", "Decline to identify")

    if re.search(r'racial|ethnic|race\b|hispanic|latino', lbl):
        return candidate.get("ethnicity", "Decline to identify")

    if re.search(r'sexual\s*orient', lbl):
        return candidate.get("sexual_orientation", "Decline to identify")

    if re.search(r'transgender\b', lbl):
        return candidate.get("transgender", "Decline to identify")

    if re.search(r'\bdisability\b|\bchronic\s*condition\b', lbl):
        return candidate.get("disability_status", "I don't wish to answer")

    if re.search(r'\bveteran\b|\bmilitary\b', lbl):
        return candidate.get("veteran_status", "I am not a veteran")

    # ── Cover letter / Open-ended motivation ─────────────────────────────────
    if re.search(r'cover\s*letter\b', lbl):
        return cover_letter_text[:2000] if cover_letter_text else candidate.get("cover_letter_one_liner", "")

    if re.search(r'(why|what).*(join|role|interest|passion|company|position)', lbl):
        return candidate.get("cover_letter_one_liner", "")

    if re.search(r'about\s*yourself|introduce\s*yourself|tell\s*us\s*about\s*you', lbl):
        return candidate.get("cover_letter_one_liner", "")

    if re.search(r'what\s*makes\s*you\s*(ideal|good|best|suitable)', lbl):
        return cover_letter_text[:500] if cover_letter_text else candidate.get("cover_letter_one_liner", "")

    if re.search(r'additional\s*(info|information|comment|detail)', lbl):
        return ""  # Optional — leave empty

    # ── No match — return None for LLM handling ───────────────────────────────
    return None


# ══════════════════════════════════════════════════════════════════════════════
# LABEL EXTRACTION (multi-strategy)
# ══════════════════════════════════════════════════════════════════════════════

async def get_field_label(page, element, element_id: str = "") -> str:
    """
    Find the label for a form field using multiple strategies.
    Returns the label text string.
    """
    # Strategy 1: <label for="id">
    if element_id:
        try:
            lbl = await page.query_selector(f"label[for='{element_id}']")
            if lbl:
                text = (await lbl.text_content() or "").strip()
                if text:
                    return text
        except Exception:
            pass

    # Strategy 2: aria-label attribute
    try:
        aria_label = await element.get_attribute("aria-label") or ""
        if aria_label:
            return aria_label.strip()
    except Exception:
        pass

    # Strategy 3: aria-labelledby
    try:
        labelledby = await element.get_attribute("aria-labelledby") or ""
        if labelledby:
            for ref_id in labelledby.split():
                ref = await page.query_selector(f"#{ref_id}")
                if ref:
                    text = (await ref.text_content() or "").strip()
                    if text:
                        return text
    except Exception:
        pass

    # Strategy 4: placeholder
    try:
        placeholder = await element.get_attribute("placeholder") or ""
        if placeholder:
            return placeholder.strip()
    except Exception:
        pass

    # Strategy 5: JavaScript DOM traversal (find wrapping/preceding label)
    try:
        label = await element.evaluate("""el => {
            // Check ancestors for <label> wrapper
            let node = el.parentElement;
            while (node && node.tagName !== 'FORM' && node.tagName !== 'BODY') {
                if (node.tagName === 'LABEL') return node.innerText.trim();
                // Check direct children of parent for a label
                for (let child of node.children) {
                    if (child.tagName === 'LABEL' && !child.contains(el)) {
                        return child.innerText.trim();
                    }
                    if ((child.tagName === 'SPAN' || child.tagName === 'P' || child.tagName === 'DIV') 
                        && child.getAttribute('class') && 
                        (child.getAttribute('class').includes('label') || child.getAttribute('class').includes('question'))) {
                        return child.innerText.trim();
                    }
                }
                node = node.parentElement;
            }
            // Check previous siblings
            let prev = el.previousElementSibling;
            while (prev) {
                if (prev.tagName === 'LABEL' || prev.tagName === 'SPAN' || prev.tagName === 'P') {
                    const text = prev.innerText.trim();
                    if (text.length > 1) return text;
                }
                prev = prev.previousElementSibling;
            }
            return el.getAttribute('name') || el.getAttribute('id') || '';
        }""")
        if label:
            return label.strip()[:200]
    except Exception:
        pass

    # Strategy 6: name attribute as fallback
    try:
        name = await element.get_attribute("name") or ""
        if name:
            return name.replace("_", " ").replace("[", " ").replace("]", " ").strip()
    except Exception:
        pass

    return ""


# ══════════════════════════════════════════════════════════════════════════════
# CHECKBOX HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def _get_checkbox_context(page, checkbox_element) -> str:
    """Get the text context around a checkbox to understand what it represents."""
    try:
        # Get surrounding text via JS
        context = await checkbox_element.evaluate("""el => {
            // Check parent/ancestor for label text
            let node = el.parentElement;
            for (let i = 0; i < 4; i++) {
                if (!node) break;
                const text = node.innerText?.trim() || '';
                if (text.length > 5) return text.substring(0, 300);
                node = node.parentElement;
            }
            // Check associated label
            const id = el.getAttribute('id');
            if (id) {
                const lbl = document.querySelector('label[for="' + id + '"]');
                if (lbl) return lbl.innerText.trim();
            }
            return el.getAttribute('value') || el.getAttribute('name') || '';
        }""")
        return (context or "").strip()
    except Exception:
        return ""


async def handle_checkboxes(page, candidate: dict):
    """
    Handle ALL checkbox types intelligently:
    - Consent/acknowledgement/GDPR → ALWAYS check
    - Terms of service → ALWAYS check
    - "By submitting I certify..." → ALWAYS check
    - Newsletter/marketing → Leave unchecked
    - EEO voluntary disclosures → Leave unchecked (they're optional)
    """
    try:
        checkboxes = await page.query_selector_all('input[type="checkbox"]')
        for cb in checkboxes:
            try:
                if await cb.is_checked():
                    continue  # Already checked, skip

                if not await cb.is_visible():
                    continue  # Hidden field

                context = await _get_checkbox_context(page, cb)
                ctx_lower = context.lower()

                # Must check: consent, acknowledgement, terms, certify
                should_check = any(kw in ctx_lower for kw in [
                    "consent", "acknowledge", "certif", "agree", "confirm",
                    "gdpr", "privacy policy", "terms", "by submitting",
                    "i accept", "i understand", "i authorize", "process my data",
                    "data processing", "i have read", "i certify", "i affirm",
                    "eula", "legally binding", "background check",
                ])

                # Must NOT check: optional marketing, EEO
                must_not_check = any(kw in ctx_lower for kw in [
                    "newsletter", "marketing", "promotional", "subscribe",
                    "updates from", "product news",
                ])

                if should_check and not must_not_check:
                    await cb.check()
                    await asyncio.sleep(random.uniform(0.2, 0.4))

            except Exception:
                continue
    except Exception as e:
        console.print(f"  [dim]Checkbox handler: {e}[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
# SELECT/DROPDOWN HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_selects(page, candidate: dict):
    """Handle all <select> dropdowns with label-based value matching."""
    try:
        selects = await page.query_selector_all("select")
        for sel in selects:
            try:
                sel_id = await sel.get_attribute("id") or ""
                label_text = await get_field_label(page, sel, sel_id)

                options = await sel.query_selector_all("option")
                opt_texts = []
                for opt in options:
                    txt = (await opt.text_content() or "").strip()
                    if txt and txt.lower() not in ("select", "choose", "please select", "--", ""):
                        opt_texts.append(txt)

                if not opt_texts:
                    continue

                # Get desired value from label
                desired = resolve_value_from_label(label_text, candidate)

                if desired:
                    # Try exact match first
                    for opt_txt in opt_texts:
                        if desired.lower() == opt_txt.lower():
                            await sel.select_option(label=opt_txt)
                            break
                    else:
                        # Partial match
                        desired_lower = desired.lower()
                        for opt_txt in opt_texts:
                            if desired_lower in opt_txt.lower() or opt_txt.lower() in desired_lower:
                                await sel.select_option(label=opt_txt)
                                break

                else:
                    # Fallback heuristics for common selects
                    lbl_lower = label_text.lower()
                    if re.search(r'country', lbl_lower):
                        for pref in ["india", "indian"]:
                            for opt in opt_texts:
                                if pref in opt.lower():
                                    await sel.select_option(label=opt)
                                    break
                    elif re.search(r'authoriz|eligible|sponsor|visa', lbl_lower):
                        pref = "no" if "sponsor" in lbl_lower else "yes"
                        for opt in opt_texts:
                            if opt.lower() in (pref, pref.capitalize()):
                                await sel.select_option(label=opt)
                                break
                    elif re.search(r'gender|ethnic|race|veteran|disability|sexual|transgender', lbl_lower):
                        for decline in ["decline", "prefer not", "choose not", "i don't wish", "no answer"]:
                            for opt in opt_texts:
                                if decline in opt.lower():
                                    await sel.select_option(label=opt)
                                    break

                await asyncio.sleep(random.uniform(0.2, 0.4))

            except Exception:
                continue
    except Exception as e:
        console.print(f"  [dim]Select handler: {e}[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
# AUTOCOMPLETE FIELD HANDLER (Country, City)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_autocomplete_fields(page, candidate: dict):
    """
    Handle Greenhouse-style autocomplete fields for country and city.
    These are text inputs that show a dropdown when you type.
    """
    # Country field
    country_selectors = [
        "#country",
        "input[id*='country']",
        "input[placeholder*='Country']",
        "input[placeholder*='country']",
        "input[aria-label*='Country']",
        "input[aria-label*='country']",
    ]
    for sel in country_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                val = await el.input_value()
                if val and val.strip():
                    break
                await el.click()
                await el.fill("India")
                await asyncio.sleep(0.7)
                # Click autocomplete suggestion
                for opt_sel in [
                    '[role="option"]:has-text("India")',
                    'li:has-text("India")',
                    '.select2-results__option:has-text("India")',
                    '[data-value*="India"]',
                ]:
                    try:
                        opt = await page.query_selector(opt_sel)
                        if opt:
                            await opt.click()
                            break
                    except Exception:
                        pass
                else:
                    await page.keyboard.press("ArrowDown")
                    await asyncio.sleep(0.3)
                    await page.keyboard.press("Enter")
                await asyncio.sleep(0.5)
                break
        except Exception:
            continue

    # City/Location field
    city_selectors = [
        "#candidate-location",
        "input[id*='location']",
        "input[placeholder*='City']",
        "input[placeholder*='city']",
        "input[aria-label*='City']",
    ]
    for sel in city_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                val = await el.input_value()
                if val and val.strip():
                    break
                await el.click()
                await el.fill(candidate.get("city", "Bangalore"))
                await asyncio.sleep(0.5)
                try:
                    opt = await page.query_selector('[role="option"]')
                    if opt:
                        await opt.click()
                except Exception:
                    await page.keyboard.press("Enter")
                await asyncio.sleep(0.4)
                break
        except Exception:
            continue


# ══════════════════════════════════════════════════════════════════════════════
# RADIO BUTTON HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_radio_buttons(page, candidate: dict):
    """
    Handle radio button groups intelligently.

    Lever uses `name=cards[UUID]` pattern for custom questions.
    Greenhouse uses radio groups inside fieldsets.

    Strategy:
    1. Group radios by name attribute
    2. Get the question text (legend/label near the group)
    3. Choose the right answer from candidate data or heuristics
    """
    try:
        # Get all radio inputs
        all_radios = await page.query_selector_all('input[type="radio"]')

        # Group by name
        radio_groups: dict[str, list] = {}
        for radio in all_radios:
            try:
                name = await radio.get_attribute("name") or ""
                if name not in radio_groups:
                    radio_groups[name] = []
                radio_groups[name].append(radio)
            except Exception:
                continue

        for group_name, radios in radio_groups.items():
            if not radios:
                continue

            # Check if any in group already selected
            any_selected = False
            for radio in radios:
                try:
                    if await radio.is_checked():
                        any_selected = True
                        break
                except Exception:
                    pass

            if any_selected:
                continue

            # Get question text for this group
            question_text = ""
            try:
                # Try to get associated legend or fieldset
                first_radio = radios[0]
                question_text = await first_radio.evaluate("""el => {
                    // Look for fieldset > legend
                    let node = el.parentElement;
                    while (node && node.tagName !== 'FORM') {
                        if (node.tagName === 'FIELDSET') {
                            const legend = node.querySelector('legend');
                            if (legend) return legend.innerText.trim();
                        }
                        // Look for preceding heading/label
                        let prev = node.previousElementSibling;
                        if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'P' || 
                                     prev.tagName === 'LEGEND' || prev.tagName === 'H3' ||
                                     prev.tagName === 'H4' || prev.tagName === 'SPAN')) {
                            const text = prev.innerText.trim();
                            if (text.length > 3) return text;
                        }
                        // Check parent's own label
                        const parentLabel = node.querySelector(':scope > label, :scope > legend, :scope > p');
                        if (parentLabel && !parentLabel.contains(el)) {
                            const text = parentLabel.innerText.trim();
                            if (text.length > 3) return text;
                        }
                        node = node.parentElement;
                    }
                    return el.getAttribute('name') || '';
                }""")
            except Exception:
                pass

            # Get option values + labels
            options = []
            for radio in radios:
                try:
                    val = await radio.get_attribute("value") or ""
                    radio_id = await radio.get_attribute("id") or ""
                    opt_label = ""
                    if radio_id:
                        lbl = await page.query_selector(f"label[for='{radio_id}']")
                        if lbl:
                            opt_label = (await lbl.text_content() or "").strip()
                    if not opt_label:
                        opt_label = await radio.evaluate("""el => {
                            const next = el.nextElementSibling;
                            if (next && next.tagName === 'LABEL') return next.innerText.trim();
                            const parent = el.parentElement;
                            if (parent && parent.tagName === 'LABEL') return parent.innerText.trim();
                            return el.getAttribute('value') || '';
                        }""")
                    options.append({"element": radio, "value": val, "label": opt_label or val})
                except Exception:
                    continue

            if not options:
                continue

            # Decide which option to select based on question
            chosen_radio = None
            q_lower = question_text.lower()

            # Determine correct answer based on question text
            if re.search(r'(eligible|authorized|legally|right|can\s*you).*(work|employ)', q_lower):
                # Work authorization → Yes
                for opt in options:
                    if opt["value"].lower() in ("yes", "true", "1") or "yes" in opt["label"].lower():
                        chosen_radio = opt["element"]
                        break

            elif re.search(r'(require|need|will\s*you\s*require|do\s*you\s*need)\s*(visa\s*)?sponsor', q_lower):
                # Sponsorship → No
                for opt in options:
                    if opt["value"].lower() in ("no", "false", "0") or "no" in opt["label"].lower():
                        chosen_radio = opt["element"]
                        break

            elif re.search(r'relocat\b', q_lower):
                # Relocation → Yes
                for opt in options:
                    if opt["value"].lower() in ("yes", "true") or "yes" in opt["label"].lower():
                        chosen_radio = opt["element"]
                        break

            elif re.search(r'currently\s*(employed|working)|employment\s*status', q_lower):
                # Currently employed → Yes
                for opt in options:
                    if "yes" in opt["label"].lower() or opt["value"].lower() == "yes":
                        chosen_radio = opt["element"]
                        break

            elif re.search(r'onsite|on.site|in.office|office', q_lower):
                # Onsite availability — depends on question specifics
                # Default: Yes if asking about "are you available"
                for opt in options:
                    if "yes" in opt["label"].lower() or opt["value"].lower() == "yes":
                        chosen_radio = opt["element"]
                        break

            elif re.search(r'remote\b', q_lower):
                for opt in options:
                    if "yes" in opt["label"].lower() or opt["value"].lower() == "yes":
                        chosen_radio = opt["element"]
                        break

            elif len(options) == 2:
                # Binary Yes/No question we couldn't specifically identify → default Yes
                for opt in options:
                    if opt["value"].lower() in ("yes", "true", "1") or "yes" in opt["label"].lower():
                        chosen_radio = opt["element"]
                        break

            if chosen_radio:
                try:
                    await chosen_radio.check()
                    await asyncio.sleep(random.uniform(0.2, 0.4))
                except Exception:
                    pass

    except Exception as e:
        console.print(f"  [dim]Radio handler: {e}[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
# LLM BATCH QUESTION ANSWERER
# ══════════════════════════════════════════════════════════════════════════════

async def llm_answer_fields(
    unknown_fields: list[dict],
    candidate: dict,
    llm_client,
    cover_letter_text: str = "",
    job_title: str = "",
    company: str = "",
) -> dict[int, str]:
    """
    Use LLM to answer fields that heuristics couldn't handle.

    Args:
        unknown_fields: List of {"index": int, "label": str, "type": str, "options": list}
        candidate: Full candidate profile dict
        llm_client: LLMClient instance
        cover_letter_text: Cover letter body
        job_title: Job title for context
        company: Company name for context

    Returns:
        dict mapping field index → answer string
    """
    if not unknown_fields or not llm_client or not llm_client.available:
        return {}

    # Build concise candidate summary
    cand_summary = {
        "name": candidate.get("name", ""),
        "email": candidate.get("email", ""),
        "phone": candidate.get("phone", ""),
        "linkedin": candidate.get("linkedin", ""),
        "github": candidate.get("github", ""),
        "city": candidate.get("city", "Bangalore"),
        "country": "India",
        "current_company": candidate.get("current_company", "Zoho Corporation"),
        "current_title": candidate.get("current_title", "Software Engineer"),
        "years_experience": candidate.get("years_of_experience", "3"),
        "notice_period": candidate.get("notice_period", "60 days"),
        "expected_salary": candidate.get("expected_salary_inr", "25-30 LPA"),
        "work_authorization": "Yes - Indian citizen",
        "requires_sponsorship": "No",
        "willing_to_relocate": "Yes",
    }

    fields_to_ask = [
        {
            "index": f["index"],
            "question": f["label"][:200],
            "type": f["type"],
            "options": f.get("options", [])[:8],
        }
        for f in unknown_fields[:15]  # Cap at 15 to avoid huge prompts
    ]

    prompt = f"""You are filling out a job application form for "{job_title}" at "{company}".

Candidate profile:
{json.dumps(cand_summary, indent=2)}

Cover letter summary: {cover_letter_text[:300] if cover_letter_text else 'Not provided'}

For each field below, provide the best answer. For Yes/No questions, always answer based on the candidate's actual situation. For open-ended questions, write 1-3 concise sentences.

Fields to fill:
{json.dumps(fields_to_ask, indent=2)}

Return ONLY valid JSON:
{{
  "answers": [
    {{"index": 0, "value": "answer here"}},
    ...
  ]
}}

Rules:
- For work authorization: "Yes" (Indian citizen working in India)
- For visa sponsorship: "No" (not required)
- For salary questions: use "{candidate.get('expected_salary_inr', '25-30 LPA')}"
- For "why this role": write 1-2 sentences from the cover letter summary
- For EEO/demographic: "Decline to identify"
- For consent/acknowledgement: "Yes"
- For select fields with options: choose the most appropriate option from the list
- Keep all answers concise (under 200 chars for text fields)
"""

    try:
        result = llm_client.chat_json(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a job application assistant. Return only valid JSON with answers array.",
            max_tokens=1024,
            temperature=0.1,
        )

        if result and result.get("answers"):
            return {a["index"]: a["value"] for a in result["answers"] if a.get("value") is not None}

    except Exception as e:
        console.print(f"  [dim]LLM question answerer error: {e}[/dim]")

    return {}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN UNIVERSAL FORM FILLER
# ══════════════════════════════════════════════════════════════════════════════

async def fill_all_form_fields(
    page,
    candidate: dict,
    llm_client=None,
    cover_letter_text: str = "",
    job_title: str = "",
    company: str = "",
    skip_filled: bool = True,
):
    """
    Universal form filler — handles ALL field types on ANY ATS form.

    Call this once per page/step in a multi-step form.

    Steps:
    1. Fill all text/email/tel/url/number inputs via label matching
    2. Fill all textareas via label matching
    3. Handle autocomplete fields (country, city)
    4. Handle select dropdowns
    5. Handle checkboxes (consent, acknowledgement)
    6. Handle radio button groups
    7. Any remaining unfilled required fields → LLM

    Args:
        page: Playwright page object
        candidate: Full candidate profile dict from config.yaml
        llm_client: LLMClient instance (optional, used for unknown questions)
        cover_letter_text: Cover letter body text
        job_title: Job title for context
        company: Company name for context
        skip_filled: Skip fields that already have values
    """
    console.print("  [dim]  Universal form fill starting...[/dim]")

    # ── Phase 1: Fill text/email/tel/url/number inputs ────────────────────────
    text_selectors = (
        'input[type="text"]:not([type="hidden"]):not([id="resume"]):not([id="cover_letter"]), '
        'input[type="email"], '
        'input[type="tel"], '
        'input[type="url"], '
        'input[type="number"]'
    )

    unknown_fields = []
    field_elements = []

    try:
        inputs = await page.query_selector_all(text_selectors)
        for inp in inputs:
            try:
                if not await inp.is_visible():
                    continue

                # Skip file inputs
                inp_type = await inp.get_attribute("type") or "text"
                if inp_type == "file":
                    continue

                # Skip if already filled
                if skip_filled:
                    val = await inp.input_value()
                    if val and val.strip():
                        continue

                inp_id = await inp.get_attribute("id") or ""
                label_text = await get_field_label(page, inp, inp_id)

                value = resolve_value_from_label(label_text, candidate, cover_letter_text)

                if value is not None:
                    await inp.fill(str(value))
                    await asyncio.sleep(random.uniform(0.1, 0.3))
                else:
                    # Queue for LLM
                    idx = len(unknown_fields)
                    unknown_fields.append({
                        "index": idx,
                        "label": label_text,
                        "type": "text",
                        "options": [],
                    })
                    field_elements.append(inp)

            except Exception:
                continue

    except Exception as e:
        console.print(f"  [dim]  Text input phase error: {e}[/dim]")

    # ── Phase 2: Fill textareas ───────────────────────────────────────────────
    try:
        textareas = await page.query_selector_all("textarea")
        for ta in textareas:
            try:
                if not await ta.is_visible():
                    continue

                if skip_filled:
                    val = await ta.input_value()
                    if val and val.strip():
                        continue

                ta_id = await ta.get_attribute("id") or ""
                label_text = await get_field_label(page, ta, ta_id)

                value = resolve_value_from_label(label_text, candidate, cover_letter_text)

                if value is not None:
                    await ta.fill(str(value))
                    await asyncio.sleep(random.uniform(0.2, 0.4))
                else:
                    idx = len(unknown_fields)
                    unknown_fields.append({
                        "index": idx,
                        "label": label_text,
                        "type": "textarea",
                        "options": [],
                    })
                    field_elements.append(ta)

            except Exception:
                continue

    except Exception as e:
        console.print(f"  [dim]  Textarea phase error: {e}[/dim]")

    # ── Phase 3: Autocomplete fields (country, city) ──────────────────────────
    await handle_autocomplete_fields(page, candidate)

    # ── Phase 4: Select dropdowns ─────────────────────────────────────────────
    await handle_selects(page, candidate)

    # ── Phase 5: Checkboxes ───────────────────────────────────────────────────
    await handle_checkboxes(page, candidate)

    # ── Phase 6: Radio buttons ────────────────────────────────────────────────
    await handle_radio_buttons(page, candidate)

    # ── Phase 7: LLM for remaining unknown fields ─────────────────────────────
    if unknown_fields and llm_client and llm_client.available:
        console.print(f"  [dim]  LLM answering {len(unknown_fields)} unknown fields...[/dim]")

        # Add select options for select-type fields in unknown list
        # (Currently text inputs, but some may be autocomplete selects)
        answers = await llm_answer_fields(
            unknown_fields=unknown_fields,
            candidate=candidate,
            llm_client=llm_client,
            cover_letter_text=cover_letter_text,
            job_title=job_title,
            company=company,
        )

        for idx, value in answers.items():
            if idx < len(field_elements) and value:
                try:
                    element = field_elements[idx]
                    if await element.is_visible():
                        current_val = await element.input_value()
                        if not current_val or not skip_filled:
                            await element.fill(str(value)[:500])
                            await asyncio.sleep(random.uniform(0.1, 0.2))
                except Exception:
                    continue

    console.print("  [dim]  Universal form fill complete[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
# PRE-SUBMIT VALIDATION + RETRY
# ══════════════════════════════════════════════════════════════════════════════

async def find_validation_error_fields(page) -> list[dict]:
    """
    Find all form fields that have validation errors after a submit attempt.
    Returns list of {"element": el, "label": str}
    """
    error_fields = []

    # aria-invalid=true fields
    try:
        invalid_inputs = await page.query_selector_all('[aria-invalid="true"]')
        for inp in invalid_inputs:
            try:
                if await inp.is_visible():
                    inp_id = await inp.get_attribute("id") or ""
                    label = await get_field_label(page, inp, inp_id)
                    error_fields.append({"element": inp, "label": label})
            except Exception:
                continue
    except Exception:
        pass

    # Fields with adjacent error messages
    try:
        error_msgs = await page.query_selector_all(
            '.error-message:not(:empty), .field-error:not(:empty), '
            '[class*="error-"]:not(:empty), [class*="-error"]:not(:empty), '
            '[role="alert"]:not(:empty), .invalid-feedback:not(:empty), '
            '.help-block.error:not(:empty)'
        )
        for err_el in error_msgs:
            try:
                text = (await err_el.text_content() or "").strip()
                if text and len(text) > 2:
                    # Find the associated input
                    inp = await err_el.evaluate_handle("""el => {
                        // Check previous sibling
                        let prev = el.previousElementSibling;
                        if (prev && (prev.tagName === 'INPUT' || prev.tagName === 'TEXTAREA' || prev.tagName === 'SELECT'))
                            return prev;
                        // Check parent's input
                        let parent = el.parentElement;
                        if (parent) {
                            let inp = parent.querySelector('input:not([type=hidden]), textarea, select');
                            if (inp) return inp;
                        }
                        return null;
                    }""")
                    if inp:
                        inp_id = await inp.get_attribute("id") or ""
                        label = await get_field_label(page, inp, inp_id) or text
                        error_fields.append({"element": inp, "label": label})
            except Exception:
                continue
    except Exception:
        pass

    return error_fields


def is_success_page(content: str, url: str) -> bool:
    """
    Check if the current page indicates a successful submission.
    Expanded phrase list to catch more ATS success pages.
    """
    content_lower = content.lower()
    success_phrases = [
        "thank you for applying",
        "application has been submitted",
        "successfully submitted",
        "application received",
        "we've received your application",
        "application complete",
        "thank you for your interest",
        "you have successfully applied",
        "your application is submitted",
        "application confirmation",
        # New additions
        "your application is being reviewed",
        "we will be in touch",
        "submission received",
        "applied successfully",
        "thanks for applying",
        "we received your application",
        "you have applied",
        "application was submitted",
        "your submission has been received",
        "we'll be in touch",
    ]
    return any(phrase in content_lower for phrase in success_phrases)


async def submit_with_retry(
    page,
    candidate: dict,
    llm_client=None,
    cover_letter_text: str = "",
    job_title: str = "",
    company: str = "",
    max_retries: int = 3,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Click Submit, check for validation errors, fix them, retry.

    Returns:
        (success, message) tuple
    """
    submit_selectors = [
        "button[type='submit']:not([disabled])",
        "input[type='submit']:not([disabled])",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
        "#submit_app",
        "[data-qa='btn-submit']",
        "button:has-text('Apply Now')",
        "button:has-text('Send Application')",
    ]

    for attempt in range(max_retries):
        # Find submit button
        submit_btn = None
        for sel in submit_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    if not disabled and aria_disabled != "true":
                        submit_btn = btn
                        break
            except Exception:
                continue

        if not submit_btn:
            return False, "Submit button not found"

        if dry_run:
            return True, "dry_run"

        console.print(f"  [dim]  Submit attempt {attempt + 1}/{max_retries}...[/dim]")
        await submit_btn.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(0.5, 1.0))
        await submit_btn.click()
        await asyncio.sleep(random.uniform(3, 5))

        # Check for success
        content = await page.content()
        if is_success_page(content, page.url):
            return True, "Submitted successfully"

        # Check for validation errors
        error_fields = await find_validation_error_fields(page)

        if not error_fields:
            # No errors detected — check if we left the form
            still_on_form = bool(await page.query_selector("form"))
            if not still_on_form:
                return True, f"Submitted (verify at {page.url})"
            if attempt < max_retries - 1:
                # Still on form, no errors — try filling again and resubmit
                console.print(f"  [dim]  Still on form — re-filling and retrying...[/dim]")
                await fill_all_form_fields(page, candidate, llm_client, cover_letter_text, job_title, company, skip_filled=False)
                continue
            return True, f"Submitted via Playwright (verify at {page.url})"

        # Fix validation errors
        console.print(f"  [yellow]  {len(error_fields)} validation error(s) — fixing...[/yellow]")

        for field_info in error_fields:
            label = field_info["label"]
            element = field_info["element"]

            # Try heuristic first
            value = resolve_value_from_label(label, candidate, cover_letter_text)

            if value is None and llm_client and llm_client.available:
                # Single-field LLM query for validation error
                answers = await llm_answer_fields(
                    unknown_fields=[{"index": 0, "label": label, "type": "text", "options": []}],
                    candidate=candidate,
                    llm_client=llm_client,
                    cover_letter_text=cover_letter_text,
                    job_title=job_title,
                    company=company,
                )
                value = answers.get(0, "")

            if value:
                try:
                    await element.fill(str(value))
                    await asyncio.sleep(random.uniform(0.2, 0.3))
                except Exception:
                    pass

        await asyncio.sleep(random.uniform(0.5, 1.0))

    return False, f"Form validation failed after {max_retries} attempts"
