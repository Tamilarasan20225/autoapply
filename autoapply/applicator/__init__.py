"""
Application engine — Phase 2 orchestrator. Improvements v2:
- Priority 0a/0b: Greenhouse API + Lever API (before Playwright, lighter+more reliable)
- Slug/ID extracted from URL when DB fields are empty
- CAPTCHA detection helper added
- apply_attempts counter incremented on each attempt
- Configurable delay (was hardcoded 5s)
- AI Form Agent receives job_title+company
- apply_url sanity-checked before browser launch
- Workday URL gets /apply suffix if missing
"""

import re
from pathlib import Path
from rich.console import Console

from autoapply.generator.resume_builder import (
    tailor_resume, build_resume_html, build_plain_text_resume, save_resume,
)
from autoapply.generator.cover_letter import generate_cover_letter, save_cover_letter
from autoapply.tracker.db import mark_applied, update_job_status
from autoapply.scoring.scorer import ScoreResult

console = Console()

# Companies known to require login on their Greenhouse portal (SSO-gated)
# These will be routed to manual_review immediately instead of wasting browser time
KNOWN_LOGIN_PORTALS: set[str] = {
    "stripe", "notion", "figma", "apple", "google", "meta", "facebook",
    "airbnb", "netflix", "uber", "lyft", "robinhood", "coinbase",
    "mongodb",  # uses custom portal with auth
}


def _extract_greenhouse_slug_from_url(url: str) -> tuple[str | None, str | None]:
    """Extract GH company slug and job ID from various URL formats."""
    if not url:
        return None, None
    m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"[?&]gh_jid=(\d+)", url)
    if m:
        jid = m.group(1)
        dm = re.search(r"https?://(?:www\.)?([^./]+)\.", url)
        return (dm.group(1) if dm else None), jid
    return None, None


def _extract_lever_slug_from_url(url: str) -> tuple[str | None, str | None]:
    """Extract Lever slug and job ID from jobs.lever.co/{slug}/{uuid} URLs."""
    if not url:
        return None, None
    m = re.search(r"jobs\.lever\.co/([^/]+)/([a-f0-9-]{36})", url, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    return None, None


def _detect_captcha(content: str) -> bool:
    """Detect CAPTCHA on a page."""
    indicators = ["captcha", "recaptcha", "hcaptcha", "i am not a robot",
                  "verify you are human", "cloudflare", "ddos-guard"]
    cl = content.lower()
    return any(i in cl for i in indicators)


def apply_to_job(
    job,
    score_result,
    master_resume: dict,
    llm_client,
    config: dict,
) -> tuple:
    app_cfg = config.get("application", {})
    dry_run = app_cfg.get("dry_run", False)
    auto_submit = app_cfg.get("auto_submit", True)
    paths_cfg = config.get("paths", {})
    phase2_cfg = config.get("phase2", {})

    candidate = config.get("candidate", {})
    candidate.update({
        "linkedin": master_resume["personal"].get("linkedin", ""),
        "github": master_resume["personal"].get("github", ""),
        "location": candidate.get("location", "Bangalore, Karnataka, India"),
        "city": candidate.get("city", "Bangalore"),
        "years_of_experience": candidate.get("years_of_experience", "3"),
        "expected_salary": candidate.get("expected_salary_inr", "Open to discussion"),
    })

    console.print(
        f"\n[bold]Applying:[/bold] [cyan]{job.title}[/cyan] @ "
        f"[magenta]{job.company}[/magenta] ([dim]{job.ats_type or 'unknown'}[/dim])"
    )

    try:
        from autoapply.tracker.db import get_session
        from autoapply.tracker.models import Job as JobModel
        _s = get_session(); _dj = _s.get(JobModel, job.id)
        if _dj: _dj.apply_attempts = (_dj.apply_attempts or 0) + 1; _s.commit()
    except Exception: pass

    import time
    time.sleep(app_cfg.get("apply_delay_seconds", 3))

    console.print("  [dim]Tailoring resume...[/dim]")
    tailored = tailor_resume(
        master_resume=master_resume, job_title=job.title, company=job.company,
        job_description=job.description or "", tailoring_variant=score_result.tailoring_variant,
        score_hint=score_result.summary_hint, llm_client=llm_client,
        max_tokens=config.get("llm", {}).get("max_tokens_resume", 2048),
    )
    html = build_resume_html(master_resume=master_resume, tailored_data=tailored,
                              job_title=job.title, company=job.company)
    plain_txt = build_plain_text_resume(master_resume=master_resume, tailored_data=tailored)
    html_path, pdf_path = save_resume(
        html=html, company=job.company, job_title=job.title,
        output_dir=paths_cfg.get("resumes_dir", "outputs/resumes"), plain_text=plain_txt,
    )
    resume_path = pdf_path or html_path

    console.print("  [dim]Writing cover letter...[/dim]")
    cover = generate_cover_letter(
        master_resume=master_resume, job_title=job.title, company=job.company,
        job_description=job.description or "", tailoring_variant=score_result.tailoring_variant,
        score_hint=score_result.summary_hint, llm_client=llm_client,
    )
    cl_text = cover.get("body", "") if cover else ""
    cl_path = None
    if cover:
        cl_path = save_cover_letter(
            cover_letter=cover, company=job.company, job_title=job.title,
            output_dir=paths_cfg.get("cover_letters_dir", "outputs/cover_letters"),
        )

    if not auto_submit:
        update_job_status(job.id, "manual_review", resume_path=resume_path, cover_letter_path=cl_path)
        _send_manual_alert(job, resume_path, cl_path, config)
        return False, "manual_docs_generated"

    ats = (job.ats_type or "other").lower()
    job_url_raw = str(job.job_url or "")
    apply_url_raw = str(job.apply_url or job.job_url or "")
    if apply_url_raw in ("nan", "None", ""): apply_url_raw = job_url_raw
    if job_url_raw in ("nan", "None", ""): job_url_raw = ""
    apply_url = apply_url_raw

    from autoapply.applicator.url_resolver import resolve_job_url, resolve_linkedin_apply_url
    from autoapply.applicator.ats_detector import detect_ats_from_url
    if apply_url and apply_url not in ("nan", "None"):
        url_detected = detect_ats_from_url(apply_url)
        is_agg = any(a in apply_url.lower() for a in ["adzuna.","indeed.com","naukri.com","shine.com","foundit.in","monster.in"])
        if is_agg:
            r = resolve_job_url(apply_url_raw, apply_url_raw, ats)
            if r["resolved_url"] and r["resolved_url"] != apply_url_raw:
                apply_url = r["resolved_url"]
                if r["detected_ats"] and r["detected_ats"] != "unknown":
                    ats = r["detected_ats"]; console.print(f"  [dim]ATS resolved: {ats}[/dim]")
        elif url_detected and url_detected != ats:
            ats = url_detected; console.print(f"  [dim]ATS from URL: {ats}[/dim]")

    if ats == "linkedin":
        apply_url = resolve_linkedin_apply_url(apply_url or job_url_raw)
        if not apply_url or apply_url in ("nan","None",""): apply_url = resolve_linkedin_apply_url(job_url_raw)

    ats_company_slug = job.ats_company_slug
    ats_job_id = job.ats_job_id
    if ats == "greenhouse" and (not ats_company_slug or not ats_job_id):
        sl, ji = _extract_greenhouse_slug_from_url(apply_url or job_url_raw)
        if sl and ji: ats_company_slug = ats_company_slug or sl; ats_job_id = ats_job_id or ji
        console.print(f"  [dim]GH slug from URL: {ats_company_slug}/{ats_job_id}[/dim]")
    if ats == "lever" and (not ats_company_slug or not ats_job_id):
        sl, ji = _extract_lever_slug_from_url(apply_url or job_url_raw)
        if sl and ji: ats_company_slug = ats_company_slug or sl; ats_job_id = ats_job_id or ji
        console.print(f"  [dim]Lever slug from URL: {ats_company_slug}/{ats_job_id}[/dim]")

    console.print(f"  [dim]ATS: {ats} | URL: {(apply_url or '')[:65]}[/dim]")

    if not apply_url or apply_url in ("nan", "None", ""):
        update_job_status(job.id, "manual_review", resume_path=resume_path, cover_letter_path=cl_path)
        _send_manual_alert(job, resume_path, cl_path, config)
        return False, "no_apply_url"

    success, method, msg = False, "none", ""
    headless = app_cfg.get("browser", {}).get("headless", True)

    if not success and ats == "greenhouse" and ats_company_slug and ats_job_id:
        from autoapply.applicator.greenhouse_api import submit_greenhouse_application
        success, msg = submit_greenhouse_application(ats_company_slug, ats_job_id, candidate, resume_path, cl_path, dry_run)
        method = "greenhouse_api"
        if not success and msg == "custom_portal_404": success = False

    if not success and ats == "lever" and ats_company_slug and ats_job_id:
        from autoapply.applicator.lever_api import submit_lever_application
        success, msg = submit_lever_application(ats_company_slug, ats_job_id, candidate, resume_path, cl_text, dry_run)
        method = "lever_api"

    if not success and ats == "greenhouse" and ats_company_slug and ats_job_id:
        from autoapply.applicator.playwright_runner import apply_greenhouse_playwright_sync
        success, msg = apply_greenhouse_playwright_sync(
            company_slug=ats_company_slug, job_id=ats_job_id, candidate=candidate,
            resume_path=resume_path, cover_letter_path=cl_path, headless=headless,
            dry_run=dry_run, llm_client=llm_client, cover_letter_text=cl_text,
            job_title=job.title or "", company_name=job.company or "",
        )
        method = "greenhouse_playwright"
        if not success and ("sign_in" in str(msg) or "form not found" in str(msg).lower()): ats = "unknown"

    if not success and ats == "lever" and ats_company_slug and ats_job_id:
        from autoapply.applicator.playwright_runner import apply_lever_playwright_sync
        success, msg = apply_lever_playwright_sync(
            company_slug=ats_company_slug, job_id=ats_job_id, candidate=candidate,
            resume_path=resume_path, cover_letter_text=cl_text, headless=headless,
            dry_run=dry_run, llm_client=llm_client, job_title=job.title or "", company_name=job.company or "",
        )
        method = "lever_playwright"

    if not success and ats == "ashby" and ats_company_slug and ats_job_id and phase2_cfg.get("enable_ashby_apply", True):
        from autoapply.applicator.ashby_apply import apply_ashby
        success, msg = apply_ashby(ats_company_slug, ats_job_id, candidate, resume_path, cl_text, cl_path, headless, dry_run)
        method = "ashby_api"

    if not success and ats == "smartrecruiters" and apply_url and phase2_cfg.get("enable_smartrecruiters", True):
        from autoapply.applicator.smartrecruiters import apply_smartrecruiters
        success, msg = apply_smartrecruiters(apply_url, candidate, resume_path, cl_text, headless, dry_run)
        method = "smartrecruiters"

    if not success and ats == "workday" and apply_url and phase2_cfg.get("enable_workday", True):
        wd_url = apply_url
        if "myworkdayjobs.com" in apply_url.lower() and "/apply" not in apply_url.lower():
            wd_url = apply_url.rstrip("/") + "/apply"
        from autoapply.applicator.workday import apply_workday
        success, msg = apply_workday(wd_url, candidate, resume_path, cl_text, headless, dry_run, llm_client)
        method = "workday_playwright"
        if msg == "workday_signin_required": ats = "unknown"

    if not success and ats == "icims" and apply_url and phase2_cfg.get("enable_icims", True):
        from autoapply.applicator.icims import apply_icims
        pwd_seed = phase2_cfg.get("ats_accounts", {}).get("password_seed", "AutoAppy")
        success, msg = apply_icims(apply_url, candidate, resume_path, cl_text, headless, dry_run, pwd_seed)
        method = "icims_playwright"

    if not success and ats == "linkedin" and apply_url:
        from autoapply.applicator.playwright_runner import apply_linkedin_sync
        success, msg = apply_linkedin_sync(apply_url, resume_path, cl_text, candidate, config)
        method = "linkedin_playwright"

    if not success and apply_url and phase2_cfg.get("enable_ai_form_agent", True) and llm_client and llm_client.available:
        from autoapply.applicator.ai_form_agent import apply_ai_form_agent
        agent_cfg = phase2_cfg.get("ai_agent", {})
        success, msg = apply_ai_form_agent(
            apply_url=apply_url, candidate=candidate, resume_path=resume_path, llm_client=llm_client,
            cover_letter_text=cl_text, cover_letter_path=cl_path, headless=headless, dry_run=dry_run,
            max_steps=agent_cfg.get("max_steps", 8), confidence_threshold=agent_cfg.get("confidence_threshold", 0.5),
            job_title=job.title or "", company=job.company or "",
        )
        method = "ai_form_agent"

    if not success and not dry_run:
        update_job_status(job.id, "manual_review", resume_path=resume_path, cover_letter_path=cl_path)
        _send_manual_alert(job, resume_path, cl_path, config)
        return False, "manual_required"

    if success or dry_run:
        mark_applied(job_id=job.id, method=method, resume_path=resume_path, cover_letter_path=cl_path)
        console.print(f"  [green bold]✓ Applied[/green bold] via {method}")
        try:
            from autoapply.notifier.telegram import notify_auto_applied, is_configured
            if is_configured(): notify_auto_applied(company=job.company, title=job.title, score=job.match_score or 0, method=method)
        except Exception: pass
    else:
        update_job_status(job.id, "failed", notes=msg, resume_path=resume_path, cover_letter_path=cl_path)
        console.print(f"  [red]✗ Failed:[/red] {msg}")
        _send_manual_alert(job, resume_path, cl_path, config)

    return success, method

def _send_manual_alert(job, resume_path: str, cl_path: str | None, config: dict):
    """
    Send alert for jobs that need manual application — via Telegram + Email + Console.
    The documents are already prepared — the user just needs to submit.
    """
    alert_enabled = config.get("application", {}).get("alert_manual_apply", True)
    if not alert_enabled:
        return

    score_display = f"{job.match_score:.0f}/100" if job.match_score else "—"

    # Console alert (always shown)
    console.print(f"\n  [bold yellow]📋 MANUAL APPLY NEEDED[/bold yellow]")
    console.print(f"  Job:   {job.title} @ {job.company}  [{score_display}]")
    console.print(f"  URL:   {job.apply_url or job.job_url}")
    console.print(f"  Resume: {resume_path}")
    if cl_path:
        console.print(f"  Cover Letter: {cl_path}")
    console.print(f"  [dim](Documents ready — should take ~2 minutes)[/dim]\n")

    # ── Telegram alert (preferred — instant, free) ─────────────────────────
    try:
        from autoapply.notifier.telegram import notify_manual_apply, is_configured
        if is_configured():
            sent = notify_manual_apply(
                company=job.company,
                title=job.title,
                score=job.match_score or 0,
                apply_url=job.apply_url or job.job_url or "",
                resume_path=resume_path,
                cover_letter_path=cl_path,
            )
            if sent:
                console.print("  [green]Telegram alert sent ✓[/green]")
    except Exception:
        pass

    # ── Email alert (optional — if SMTP configured) ────────────────────────
    try:
        import os
        smtp_email = os.environ.get("SMTP_EMAIL")
        smtp_password = os.environ.get("SMTP_PASSWORD")
        alert_email = os.environ.get("ALERT_EMAIL")

        if smtp_email and smtp_password and alert_email:
            import smtplib
            from email.mime.text import MIMEText

            body = (
                f"Manual application needed:\n\n"
                f"Role: {job.title}\n"
                f"Company: {job.company}\n"
                f"Score: {score_display}\n"
                f"Apply URL: {job.apply_url or job.job_url}\n\n"
                f"Resume: {resume_path}\n"
                f"Cover Letter: {cl_path or 'N/A'}\n\n"
                f"Documents ready — apply in ~2 minutes!"
            )

            msg = MIMEText(body)
            msg["Subject"] = f"[AutoAppy] Apply now: {job.title} @ {job.company}"
            msg["From"] = smtp_email
            msg["To"] = alert_email

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(smtp_email, smtp_password)
                server.send_message(msg)

            console.print(f"  [green]Email alert sent to {alert_email}[/green]")
    except Exception:
        pass  # Email alerts are optional — don't crash if not configured
