"""
ATS Fingerprinter — detects which Applicant Tracking System (ATS) a job URL uses.

Detection strategy:
  1. URL pattern matching (fast, no network needed)
  2. Page DOM inspection (loads page, checks for ATS-specific signatures)

Supports:
  - Workday (myworkdayjobs.com, workday.com)
  - iCIMS (icims.com)
  - Taleo (taleo.net)
  - SmartRecruiters (smartrecruiters.com)
  - Ashby (ashbyhq.com)
  - Greenhouse (greenhouse.io)
  - Lever (lever.co)
  - LinkedIn (linkedin.com)
  - Naukri (naukri.com)
  - BambooHR (bamboohr.com)
  - Jobvite (jobvite.com)
  - Paycom (paycom.com)
  - Rippling (rippling.com)
  - Keka (keka.com) — Indian HR platform
  - Darwinbox (darwinbox.com) — Indian HR platform
"""

import re
from typing import Optional
from urllib.parse import urlparse
from rich.console import Console

console = Console()

# ── URL-based detection patterns (ordered by specificity) ────────────────────
URL_PATTERNS: list[tuple[str, str]] = [
    # Greenhouse
    (r"boards\.greenhouse\.io", "greenhouse"),
    (r"job-boards\.greenhouse\.io", "greenhouse"),
    # Lever
    (r"jobs\.lever\.co", "lever"),
    # Ashby
    (r"jobs\.ashbyhq\.com", "ashby"),
    # Workday
    (r"myworkdayjobs\.com", "workday"),
    (r"workday\.com/.*jobs", "workday"),
    (r"wd\d+\.myworkdayjobs\.com", "workday"),
    # iCIMS
    (r"careers\.icims\.com", "icims"),
    (r"icims\.com/jobs", "icims"),
    # Taleo
    (r"taleo\.net", "taleo"),
    (r"oracle\.taleo\.net", "taleo"),
    # SmartRecruiters
    (r"careers\.smartrecruiters\.com", "smartrecruiters"),
    (r"smartrecruiters\.com/jobs", "smartrecruiters"),
    # BambooHR
    (r"bamboohr\.com/careers", "bamboohr"),
    # Jobvite
    (r"jobs\.jobvite\.com", "jobvite"),
    (r"jobvite\.com/careers", "jobvite"),
    # LinkedIn
    (r"linkedin\.com/jobs", "linkedin"),
    # Naukri (India)
    (r"naukri\.com", "naukri"),
    # Keka (India)
    (r"keka\.com", "keka"),
    # Darwinbox (India)
    (r"darwinbox\.com", "darwinbox"),
    # Rippling
    (r"app\.rippling\.com/ats", "rippling"),
    # Paycom
    (r"paycomonline\.net", "paycom"),
    # Workable
    (r"apply\.workable\.com", "workable"),
    # Greenhouse (new format)
    (r"greenhouse\.io", "greenhouse"),
]

# ── DOM-based ATS signatures (checked when URL is ambiguous) ──────────────────
DOM_SIGNATURES: list[tuple[str, str]] = [
    # Workday: data-automation-id attributes are unique to Workday
    ('[data-automation-id="formContainer"]', "workday"),
    ('[data-automation-id="firstName"]', "workday"),
    ('meta[name="application-name"][content*="Workday"]', "workday"),
    ('.WGUL', "workday"),  # Workday CSS class
    # iCIMS: iCIMS_* prefixed classes
    ('.iCIMS_JobTitle', "icims"),
    ('#iCIMS_Content', "icims"),
    ('form[action*="icims"]', "icims"),
    # Taleo
    ('#oracle-apex', "taleo"),
    ('.taleosearch', "taleo"),
    ('#taleo-container', "taleo"),
    # SmartRecruiters
    ('[data-hook="sr-apply-btn"]', "smartrecruiters"),
    ('.smartrecruiters-widget', "smartrecruiters"),
    # Greenhouse
    ('#application-form', "greenhouse"),
    ('form[action*="greenhouse"]', "greenhouse"),
    # Lever
    ('.lever-job-listing', "lever"),
    ('form[action*="lever"]', "lever"),
    # Ashby
    ('[data-testid="ashby-job-posting-apply-form"]', "ashby"),
    ('.ashby-job-posting', "ashby"),
    # BambooHR
    ('#bamboo-application-form', "bamboohr"),
    # Workable
    ('.workable-application', "workable"),
]


def detect_ats_from_url(url: str) -> Optional[str]:
    """
    Fast URL-based ATS detection — no network call needed.

    Args:
        url: Job URL or apply URL

    Returns:
        ATS type string or None if not detected
    """
    if not url:
        return None

    url_lower = url.lower()

    for pattern, ats_type in URL_PATTERNS:
        if re.search(pattern, url_lower):
            return ats_type

    return None


async def detect_ats_from_page(page) -> Optional[str]:
    """
    DOM-based ATS detection — loads the page and checks for known selectors.
    Call this when URL-based detection fails.

    Args:
        page: Playwright Page object (already navigated to the URL)

    Returns:
        ATS type string or None if not detected
    """
    try:
        for selector, ats_type in DOM_SIGNATURES:
            try:
                el = await page.query_selector(selector)
                if el:
                    return ats_type
            except Exception:
                continue

        # Fallback: check page text for ATS-specific phrases
        try:
            content = await page.content()
            content_lower = content.lower()

            if "workday" in content_lower and "data-automation-id" in content_lower:
                return "workday"
            if "icims" in content_lower:
                return "icims"
            if "taleo" in content_lower:
                return "taleo"
            if "smartrecruiters" in content_lower:
                return "smartrecruiters"
            if "bamboohr" in content_lower:
                return "bamboohr"
            if "greenhouse" in content_lower and "application" in content_lower:
                return "greenhouse"
        except Exception:
            pass

    except Exception as e:
        console.print(f"  [dim]DOM ATS detection error: {e}[/dim]")

    return None


async def detect_ats(url: str, page=None) -> str:
    """
    Full ATS detection: URL-first, then DOM-based if available.

    Args:
        url: Job or apply URL
        page: Optional Playwright page (enables DOM detection)

    Returns:
        ATS type string (defaults to "unknown" if not detected)
    """
    # Step 1: Fast URL-based detection
    detected = detect_ats_from_url(url)
    if detected:
        console.print(f"  [dim]ATS detected (URL): {detected}[/dim]")
        return detected

    # Step 2: DOM-based detection if page is available
    if page:
        detected = await detect_ats_from_page(page)
        if detected:
            console.print(f"  [dim]ATS detected (DOM): {detected}[/dim]")
            return detected

    console.print(f"  [dim]ATS unknown for: {url[:80]}[/dim]")
    return "unknown"


def is_company_portal(url: str) -> bool:
    """
    Check if a URL is a company's own career portal (not a known ATS subdomain).
    These often redirect to an embedded ATS and need DOM detection.

    Fixed: was incorrectly flagging Workday URLs as company portals
    because 'myworkdayjobs.com/en-US/Careers/job/...' matched '/job/' regex.
    Now checks direct ATS patterns FIRST before regex.

    Examples:
      stripe.com/careers/apply/...  → True  (company portal wrapping Greenhouse)
      notion.so/jobs/...            → True
      boards.greenhouse.io/stripe   → False (direct ATS URL)
      wd3.myworkdayjobs.com/...     → False (direct ATS URL)
    """
    if not url:
        return False

    # Direct ATS URLs — definitely NOT a company portal
    direct_ats_patterns = [
        "greenhouse.io", "lever.co", "ashbyhq.com", "workday.com",
        "myworkdayjobs.com", "icims.com", "taleo.net", "smartrecruiters.com",
        "bamboohr.com", "jobvite.com", "linkedin.com", "naukri.com",
        "darwinbox.com", "keka.com", "workable.com", "rippling.com",
        "paycomonline.net",
    ]
    url_lower = url.lower()
    for pattern in direct_ats_patterns:
        if pattern in url_lower:
            return False

    # If it's a jobs/careers path on a non-ATS domain → company portal
    return bool(re.search(r"/(careers?|jobs?|apply|positions?|openings?)/", url_lower))


def get_apply_strategy(ats_type: str) -> str:
    """
    Return the recommended apply strategy for a given ATS type.

    Returns:
        Strategy name: "api" | "playwright" | "semi_manual" | "manual"
    """
    strategies = {
        "greenhouse": "playwright",
        "lever": "playwright",
        "ashby": "api",              # Ashby has a clean public API
        "smartrecruiters": "api",    # SmartRecruiters has public candidate API
        "workday": "playwright",     # Multi-step wizard
        "icims": "playwright",       # Form-based, needs session
        "taleo": "playwright",       # Form-based, legacy
        "bamboohr": "playwright",
        "workable": "playwright",
        "jobvite": "playwright",
        "linkedin": "playwright",    # Easy Apply
        "naukri": "semi_manual",     # Profile-based, complex
        "darwinbox": "semi_manual",
        "keka": "semi_manual",
        "unknown": "ai_agent",       # AI vision-based fallback
    }
    return strategies.get(ats_type, "manual")
