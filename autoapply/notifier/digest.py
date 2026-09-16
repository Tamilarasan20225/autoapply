"""
Daily Digest Notifier for AutoAppy.

Sends a summary at the end of each pipeline run (or on demand) covering:
  - Applications submitted today
  - Manual apply queue
  - High-score jobs found
  - Follow-up reminders

Sends via Telegram (if configured) and optionally email.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def build_digest_text(
    discovered: int = 0,
    scored: int = 0,
    auto_applied: int = 0,
    manual_needed: int = 0,
    review_queue: int = 0,
    high_score_jobs: list | None = None,
    follow_ups: list | None = None,
    duration_secs: int = 0,
) -> str:
    """
    Build a formatted digest text for Telegram HTML.
    """
    now = _utcnow()
    date_str = now.strftime("%a %b %d, %Y — %H:%M UTC")
    duration_str = f"{duration_secs // 60}m {duration_secs % 60}s" if duration_secs else "—"

    lines = [
        f"🤖 <b>AutoAppy Daily Digest</b>",
        f"<i>{date_str}</i>",
        "",
        "📊 <b>Pipeline Summary:</b>",
        f"  🔍 Discovered: <b>{discovered}</b> new jobs",
        f"  📈 Scored: <b>{scored}</b> jobs",
        f"  ✅ Auto-Applied: <b>{auto_applied}</b>",
        f"  📋 Manual Queue: <b>{manual_needed}</b>",
        f"  🔍 Review Queue: <b>{review_queue}</b>",
        f"  ⏱ Duration: {duration_str}",
    ]

    # High-score jobs section
    if high_score_jobs:
        lines.append("")
        lines.append("🔥 <b>Top Matches Today:</b>")
        for job in high_score_jobs[:5]:
            score = job.get("score", 0)
            company = job.get("company", "—")
            title = job.get("title", "—")[:40]
            url = job.get("url", "")
            if url:
                lines.append(f"  • <b>{score:.0f}/100</b> — <a href='{url}'>{company}: {title}</a>")
            else:
                lines.append(f"  • <b>{score:.0f}/100</b> — {company}: {title}")

    # Follow-up reminders
    if follow_ups:
        lines.append("")
        lines.append("🔔 <b>Follow-Up Reminders:</b>")
        for fu in follow_ups[:3]:
            company = fu.get("company", "—")
            title = fu.get("title", "—")[:30]
            date = fu.get("follow_up_date", "")
            lines.append(f"  • {company} ({title}) — follow up by {date}")

    if manual_needed > 0:
        lines.append("")
        lines.append(f"⚡ <b>{manual_needed} job(s) need your attention!</b>")
        lines.append("Open the dashboard to apply: <code>streamlit run autoapply/dashboard/app.py</code>")

    return "\n".join(lines)


def send_daily_digest(
    discovered: int = 0,
    scored: int = 0,
    auto_applied: int = 0,
    manual_needed: int = 0,
    review_queue: int = 0,
    high_score_jobs: list | None = None,
    follow_ups: list | None = None,
    duration_secs: int = 0,
) -> bool:
    """
    Send the daily digest via Telegram (if configured).
    Also optionally via email if SMTP is configured.

    Returns True if at least one channel succeeded.
    """
    text = build_digest_text(
        discovered=discovered,
        scored=scored,
        auto_applied=auto_applied,
        manual_needed=manual_needed,
        review_queue=review_queue,
        high_score_jobs=high_score_jobs,
        follow_ups=follow_ups,
        duration_secs=duration_secs,
    )

    sent = False

    # ── Telegram ──────────────────────────────────────────────────────────────
    try:
        from autoapply.notifier.telegram import send_message, is_configured
        if is_configured():
            sent = send_message(text)
    except Exception:
        pass

    return sent


def collect_digest_data(db_path: str = "data/autoapply.db") -> dict:
    """
    Collect today's data from the database for the digest.
    Returns a dict with all the digest fields populated.
    """
    try:
        from autoapply.tracker.db import init_db, get_session
        from autoapply.tracker.models import Job

        init_db(db_path)
        session = get_session()

        today_start = _utcnow().replace(hour=0, minute=0, second=0)

        # Jobs discovered today
        discovered_today = session.query(Job).filter(
            Job.discovered_at >= today_start
        ).count()

        # Jobs scored today
        scored_today = session.query(Job).filter(
            Job.updated_at >= today_start,
            Job.status == "scored",
        ).count()

        # Applications submitted today
        applied_today = session.query(Job).filter(
            Job.applied_at >= today_start,
            Job.status == "applied",
        ).count()

        # Manual review pending
        manual_pending = session.query(Job).filter(
            Job.status == "manual_review"
        ).count()

        # Review queue (scored 60-74)
        review_queue = session.query(Job).filter(
            Job.status == "scored",
            Job.match_score >= 60,
            Job.match_score < 75,
        ).count()

        # Top scoring jobs found today
        top_jobs_db = session.query(Job).filter(
            Job.discovered_at >= today_start,
            Job.match_score.isnot(None),
        ).order_by(Job.match_score.desc()).limit(5).all()

        high_score_jobs = [
            {
                "company": j.company,
                "title": j.title,
                "score": j.match_score,
                "url": j.job_url or "",
            }
            for j in top_jobs_db
        ]

        # Follow-up reminders (applied >7 days ago without response)
        week_ago = _utcnow() - timedelta(days=7)
        follow_ups_db = session.query(Job).filter(
            Job.status == "applied",
            Job.follow_up_date <= _utcnow(),
            Job.response_received == False,
        ).limit(5).all()

        follow_ups = [
            {
                "company": j.company,
                "title": j.title,
                "follow_up_date": j.follow_up_date.strftime("%b %d") if j.follow_up_date else "—",
            }
            for j in follow_ups_db
        ]

        session.close()

        return {
            "discovered": discovered_today,
            "scored": scored_today,
            "auto_applied": applied_today,
            "manual_needed": manual_pending,
            "review_queue": review_queue,
            "high_score_jobs": high_score_jobs,
            "follow_ups": follow_ups,
        }

    except Exception as e:
        return {
            "discovered": 0, "scored": 0, "auto_applied": 0,
            "manual_needed": 0, "review_queue": 0,
            "high_score_jobs": [], "follow_ups": [],
        }
