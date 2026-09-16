"""
Telegram Bot Notifier for AutoAppy.

Sends instant notifications when:
  - A manual application is needed (documents are ready)
  - Pipeline run completes with summary
  - High-scoring job is found

Setup:
  1. Message @BotFather on Telegram → /newbot → get TELEGRAM_BOT_TOKEN
  2. Message your bot once, then run: python -c "from autoapply.notifier.telegram import get_chat_id; get_chat_id()"
  3. Copy the chat_id and add to .env as TELEGRAM_CHAT_ID

Cost: Free. No rate limits for personal bots.
"""

import os
import json
import urllib.request
import urllib.error
from typing import Optional


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _send_request(token: str, method: str, data: dict) -> dict | None:
    """Send a request to Telegram Bot API."""
    url = TELEGRAM_API.format(token=token, method=method)
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError:
        return None
    except Exception:
        return None


def get_credentials() -> tuple[str | None, str | None]:
    """Get bot token and chat ID from environment."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return token or None, chat_id or None


def is_configured() -> bool:
    """Check if Telegram notifications are configured."""
    token, chat_id = get_credentials()
    return bool(token and chat_id)


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    Send a Telegram message.

    Args:
        text: Message text (HTML or Markdown supported)
        parse_mode: "HTML" or "Markdown"

    Returns:
        True if sent successfully, False otherwise
    """
    token, chat_id = get_credentials()
    if not token or not chat_id:
        return False

    result = _send_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    })
    return bool(result and result.get("ok"))


def get_chat_id() -> None:
    """
    Helper: print your chat ID so you can add it to .env.
    Run after sending any message to your bot.
    """
    token, _ = get_credentials()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return
    result = _send_request(token, "getUpdates", {})
    if result and result.get("result"):
        for update in result["result"]:
            chat = update.get("message", {}).get("chat", {})
            print(f"Chat ID: {chat.get('id')} | Username: {chat.get('username')} | Name: {chat.get('first_name')}")
    else:
        print("No updates found. Please send a message to your bot first, then run this again.")


# ── Notification helpers ──────────────────────────────────────────────────────

def notify_manual_apply(
    company: str,
    title: str,
    score: float,
    apply_url: str,
    resume_path: str,
    cover_letter_path: Optional[str] = None,
) -> bool:
    """Notify about a job that needs manual application (documents ready)."""
    cl_line = f"\n📝 Cover letter: <code>{cover_letter_path}</code>" if cover_letter_path else ""
    text = (
        f"📋 <b>Manual Apply Needed</b>\n\n"
        f"🏢 <b>{company}</b>\n"
        f"💼 {title}\n"
        f"🎯 Score: <b>{score:.0f}/100</b>\n"
        f"\n🔗 <a href='{apply_url}'>Apply Here</a>\n"
        f"📄 Resume: <code>{resume_path}</code>"
        f"{cl_line}\n"
        f"\n⏱ Should take ~2 minutes to submit!"
    )
    return send_message(text)


def notify_auto_applied(
    company: str,
    title: str,
    score: float,
    method: str,
) -> bool:
    """Notify when an application was submitted automatically."""
    text = (
        f"✅ <b>Application Submitted!</b>\n\n"
        f"🏢 <b>{company}</b>\n"
        f"💼 {title}\n"
        f"🎯 Score: {score:.0f}/100\n"
        f"🤖 Method: {method}"
    )
    return send_message(text)


def notify_pipeline_complete(
    discovered: int,
    scored: int,
    applied: int,
    manual_needed: int,
    llm_calls: int,
    duration_secs: int = 0,
) -> bool:
    """Send end-of-run summary notification."""
    duration_str = f"{duration_secs // 60}m {duration_secs % 60}s" if duration_secs else "—"
    text = (
        f"🤖 <b>AutoAppy Run Complete</b>\n\n"
        f"🔍 Discovered: {discovered}\n"
        f"📊 Scored: {scored}\n"
        f"✅ Applied: {applied}\n"
        f"📋 Manual Needed: {manual_needed}\n"
        f"🧠 LLM Calls: {llm_calls}\n"
        f"⏱ Duration: {duration_str}"
    )
    return send_message(text)


def notify_high_score_found(
    company: str,
    title: str,
    score: float,
    job_url: str,
) -> bool:
    """Notify when an exceptional match is found (score >= 90)."""
    text = (
        f"🔥 <b>Exceptional Match Found!</b>\n\n"
        f"🏢 <b>{company}</b>\n"
        f"💼 {title}\n"
        f"🎯 Score: <b>{score:.0f}/100</b>\n"
        f"🔗 <a href='{job_url}'>View Job</a>"
    )
    return send_message(text)
