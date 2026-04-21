"""
Telegram helpers used by agent.py for fallback notifications
when BOT_SERVER_URL is not configured.
"""

import os
import requests
from typing import Optional


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")

def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")

def _base() -> str:
    return f"https://api.telegram.org/bot{_token()}"

def _configured() -> bool:
    return bool(_token() and _chat_id())


def send_draft(draft: dict, image_url: str) -> None:
    """Fallback: send draft directly to Telegram (used when no bot server)."""
    if not _configured():
        return

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    draft_id = draft["draft_id"]
    text = (
        f"📸 <b>New FinAmigo Draft</b>\n\n"
        f"<b>Theme:</b> {_esc(draft.get('theme', ''))}\n"
        f"<b>Style:</b> {draft.get('caption_style', 'N/A')} · {draft.get('image_style', 'N/A')}\n"
        f"<b>Draft ID:</b> <code>{draft_id}</code>\n\n"
        f"───────────────\n{_esc(draft['caption'][:900])}"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve|{draft_id}"},
        {"text": "✏️ Revise",  "callback_data": f"revise|{draft_id}"},
    ]]}
    try:
        r = requests.post(f"{_base()}/sendPhoto", json={
            "chat_id": _chat_id(), "photo": image_url,
            "caption": text, "parse_mode": "HTML", "reply_markup": keyboard,
        }, timeout=30)
        if not r.json().get("ok"):
            requests.post(f"{_base()}/sendMessage", json={
                "chat_id": _chat_id(), "text": text,
                "parse_mode": "HTML", "reply_markup": keyboard,
            }, timeout=15)
    except Exception as e:
        print(f"[Telegram] send_draft error: {e}")


def notify(message: str) -> None:
    """Send a plain notification to the Telegram chat."""
    if not _configured():
        return
    try:
        requests.post(f"{_base()}/sendMessage", json={
            "chat_id":    _chat_id(),
            "text":       message,
            "parse_mode": "Markdown",
        }, timeout=15)
    except Exception as e:
        print(f"[Telegram] notify error: {e}")
