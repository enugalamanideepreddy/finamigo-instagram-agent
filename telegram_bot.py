"""
Telegram bot for instant draft approval — replaces the Google Form polling loop.

Flow:
  1. Agent sends draft photo + caption + inline keyboard to your Telegram chat.
  2. You tap ✅ Approve → post goes live immediately on next check run.
  3. You tap ✏️ Revise → bot prompts you; reply with revision notes as a text message.
  4. check_approval polls getUpdates (stateless offset stored in agent_state).

Setup:
  - Create a bot via @BotFather → copy the token → GitHub secret: TELEGRAM_BOT_TOKEN
  - Send any message to your new bot, then visit:
      https://api.telegram.org/bot<TOKEN>/getUpdates
    Copy the "chat" → "id" value → GitHub secret: TELEGRAM_CHAT_ID
"""

import os
from typing import Optional, Tuple
import requests

def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")

def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")

def _base() -> str:
    return f"https://api.telegram.org/bot{_token()}"

def _configured() -> bool:
    ok = bool(_token() and _chat_id())
    if not ok:
        print(f"[Telegram] Not configured — BOT_TOKEN={'set' if _token() else 'MISSING'}, CHAT_ID={'set' if _chat_id() else 'MISSING'}")
    return ok


def send_draft(draft: dict, image_url: str) -> Optional[int]:
    """Send the draft to Telegram as a photo with an inline Approve / Revise keyboard.

    Returns the Telegram message_id so it can be stored in state (optional).
    """
    if not _configured():
        print("[Telegram] Not configured — skipping Telegram notification.")
        return None

    draft_id = draft["draft_id"]

    # Use HTML mode — much more forgiving than Markdown (no escaping issues)
    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    caption_preview = _esc(draft["caption"][:900])
    theme_safe      = _esc(draft.get("theme", ""))

    text = (
        f"📸 <b>New FinAmigo Draft</b>\n\n"
        f"<b>Theme:</b> {theme_safe}\n"
        f"<b>Style:</b> {draft.get('caption_style', 'N/A')} · {draft.get('image_style', 'N/A')}\n"
        f"<b>Draft ID:</b> <code>{draft_id}</code>\n\n"
        f"───────────────\n"
        f"{caption_preview}"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve|{draft_id}"},
            {"text": "✏️ Revise",  "callback_data": f"revise|{draft_id}"},
        ]]
    }

    # Try photo first
    try:
        r = requests.post(f"{_base()}/sendPhoto", json={
            "chat_id":      _chat_id(),
            "photo":        image_url,
            "caption":      text,
            "parse_mode":   "HTML",
            "reply_markup": keyboard,
        }, timeout=30)
        data = r.json()
        if data.get("ok"):
            msg_id = data["result"]["message_id"]
            print(f"[Telegram] Draft sent (photo), message_id={msg_id}")
            return msg_id
        print(f"[Telegram] Photo send failed: {data.get('description')} — trying text fallback.")
    except Exception as e:
        print(f"[Telegram] Photo send error: {e}")

    # Fallback: plain text + image link (no parse_mode to avoid any formatting issues)
    try:
        r2 = requests.post(f"{_base()}/sendMessage", json={
            "chat_id":      _chat_id(),
            "text":         f"📸 New FinAmigo Draft\nDraft ID: {draft_id}\n\nImage: {image_url}",
            "reply_markup": keyboard,
        }, timeout=20)
        data2 = r2.json()
        if data2.get("ok"):
            msg_id = data2["result"]["message_id"]
            print(f"[Telegram] Draft sent (text fallback), message_id={msg_id}")
            return msg_id
        print(f"[Telegram] Text fallback also failed: {data2}")
    except Exception as e:
        print(f"[Telegram] Text fallback error: {e}")

    return None


def check_response(
    draft_id: str,
    offset: int = 0,
    awaiting_remarks: bool = False,
) -> Tuple[str, Optional[str], int, bool]:
    """Poll Telegram getUpdates for an approval decision.

    Args:
        draft_id:         The current draft's ID to match against callback data.
        offset:           Last processed update_id + 1 (persisted in agent_state).
        awaiting_remarks: True if a revise button was pressed and we're waiting for a text reply.

    Returns:
        (status, remarks, new_offset, new_awaiting_remarks)
        status is "approved", "remarks", or "pending".
    """
    if not _configured():
        return ("pending", None, offset, awaiting_remarks)

    try:
        r = requests.get(f"{_base()}/getUpdates", params={
            "offset":  offset,
            "timeout": 0,
            "limit":   100,
        }, timeout=20)
        data = r.json()
    except Exception as e:
        print(f"[Telegram] getUpdates error: {e}")
        return ("pending", None, offset, awaiting_remarks)

    if not data.get("ok"):
        print(f"[Telegram] getUpdates failed: {data.get('description')}")
        return ("pending", None, offset, awaiting_remarks)

    updates = data.get("result", [])
    new_offset = offset
    status = "pending"
    remarks_text = None

    for update in updates:
        new_offset = max(new_offset, update["update_id"] + 1)

        # ── Inline button press ──────────────────────────────────────────────
        if "callback_query" in update:
            cq = update["callback_query"]
            from_chat = str(cq["message"]["chat"]["id"])
            if from_chat != str(_chat_id()):
                continue

            cb_data = cq.get("data", "")
            if "|" not in cb_data:
                continue
            action, cb_draft_id = cb_data.split("|", 1)

            if cb_draft_id != draft_id:
                continue

            # Acknowledge the button press (removes loading spinner in Telegram)
            try:
                requests.post(f"{_base()}/answerCallbackQuery", json={
                    "callback_query_id": cq["id"],
                    "text": "Got it!" if action == "approve" else "Send your notes as a reply.",
                }, timeout=10)
            except Exception:
                pass

            if action == "approve":
                status = "approved"
                awaiting_remarks = False
            elif action == "revise":
                awaiting_remarks = True
                # Prompt user for remarks
                try:
                    requests.post(f"{_base()}/sendMessage", json={
                        "chat_id":  _chat_id(),
                        "text":     "✏️ What should I change? Reply with your revision notes:",
                        "parse_mode": "Markdown",
                    }, timeout=10)
                except Exception:
                    pass

        # ── Text message (revision notes) ────────────────────────────────────
        elif "message" in update and awaiting_remarks:
            msg = update["message"]
            from_chat = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()

            if from_chat != str(_chat_id()):
                continue
            if not text or text.startswith("/"):
                continue

            status = "remarks"
            remarks_text = text
            awaiting_remarks = False

    return (status, remarks_text, new_offset, awaiting_remarks)


def notify(message: str) -> None:
    """Send a plain notification message to the Telegram chat."""
    if not _configured():
        return
    try:
        requests.post(f"{_base()}/sendMessage", json={
            "chat_id":    _chat_id(),
            "text":       message,
            "parse_mode": "Markdown",
        }, timeout=15)
    except Exception as e:
        print(f"[Telegram] Notify error: {e}")
