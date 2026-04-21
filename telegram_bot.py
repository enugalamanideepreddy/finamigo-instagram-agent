"""
Telegram bot for draft approval and on-demand post generation.

Approval flow:
  1. Agent sends draft photo + caption + inline keyboard.
  2. Tap ✅ Approve → post goes live on next check run.
  3. Tap ✏️ Revise → reply with revision notes.

/post wizard flow:
  1. Send /post
  2. Pick image tone  (inline keyboard)
  3. Pick caption style (inline keyboard)
  4. Type optional context (or /skip)
  5. Agent generates and sends draft for approval.

Commands:
  /post    → start new post wizard
  /status  → show current draft status
  /help    → same as /status
"""

import os
from typing import Optional, Tuple
import requests

# ── Internals ──────────────────────────────────────────────────────────────────

def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")

def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")

def _base() -> str:
    return f"https://api.telegram.org/bot{_token()}"

def _configured() -> bool:
    ok = bool(_token() and _chat_id())
    if not ok:
        print(f"[Telegram] Not configured — BOT_TOKEN={'set' if _token() else 'MISSING'}, "
              f"CHAT_ID={'set' if _chat_id() else 'MISSING'}")
    return ok

def _send(payload: dict, timeout: int = 15) -> bool:
    try:
        r = requests.post(f"{_base()}/sendMessage", json=payload, timeout=timeout)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"[Telegram] sendMessage error: {e}")
        return False

def _answer_cb(cq_id: str, text: str = "", alert: bool = False) -> None:
    try:
        requests.post(f"{_base()}/answerCallbackQuery", json={
            "callback_query_id": cq_id,
            "text": text,
            "show_alert": alert,
        }, timeout=10)
    except Exception:
        pass

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Wizard option tables ───────────────────────────────────────────────────────

IMAGE_STYLE_OPTIONS = [
    ("🌑 Dark & Premium",    "phone_hero_dark"),
    ("🖥️ Dual Screen",      "dual_screen_split"),
    ("🃏 Floating Cards",    "ui_cards_floating"),
    ("🏛️ Isometric 3D",     "isometric_3d"),
    ("🔍 UI Close-up",       "ui_closeup_immersive"),
    ("⬜ Minimal Light",     "minimal_light_flat"),
    ("⚡ Neon Cyberpunk",    "neon_cyberpunk"),
    ("📐 Perspective Tilt",  "perspective_tilt"),
    ("🎲 Auto (random)",     "auto"),
]

CAPTION_STYLE_OPTIONS = [
    ("💪 Bold Statement",    "bold_statement"),
    ("❓ Question Hook",     "question_hook"),
    ("📊 Stat Lead",         "stat_lead"),
    ("📖 Story Moment",      "story_moment"),
    ("↔️ Contrast",          "contrast"),
    ("🎲 Auto (random)",     "auto"),
]

def _img_label(value: str) -> str:
    for label, v in IMAGE_STYLE_OPTIONS:
        if v == value:
            return label
    return value

def _cap_label(value: str) -> str:
    for label, v in CAPTION_STYLE_OPTIONS:
        if v == value:
            return label
    return value

def _make_keyboard(options: list, prefix: str, cols: int = 2) -> dict:
    """Build inline keyboard from [(label, value), ...] with given prefix."""
    buttons = [
        {"text": label, "callback_data": f"{prefix}|{value}"}
        for label, value in options
    ]
    rows = [buttons[i:i+cols] for i in range(0, len(buttons), cols)]
    return {"inline_keyboard": rows}


# ── Wizard send helpers ────────────────────────────────────────────────────────

def send_post_wizard_start() -> None:
    """Step 1 — send image tone picker."""
    if not _configured():
        return
    _send({
        "chat_id":      _chat_id(),
        "text":         "🎨 <b>Step 1 of 3 — Choose image tone:</b>",
        "parse_mode":   "HTML",
        "reply_markup": _make_keyboard(IMAGE_STYLE_OPTIONS, "wizard_img", cols=2),
    })


def send_caption_style_picker(image_label: str) -> None:
    """Step 2 — send caption style picker."""
    if not _configured():
        return
    _send({
        "chat_id":      _chat_id(),
        "text":         f"✅ Image: <b>{_esc(image_label)}</b>\n\n✍️ <b>Step 2 of 3 — Choose caption style:</b>",
        "parse_mode":   "HTML",
        "reply_markup": _make_keyboard(CAPTION_STYLE_OPTIONS, "wizard_cap", cols=2),
    })


def send_context_prompt(image_label: str, caption_label: str) -> None:
    """Step 3 — ask for optional context."""
    if not _configured():
        return
    _send({
        "chat_id":    _chat_id(),
        "text": (
            f"✅ Image: <b>{_esc(image_label)}</b>  |  Caption: <b>{_esc(caption_label)}</b>\n\n"
            f"📝 <b>Step 3 of 3 — Any context for this post?</b>\n\n"
            f"Type a note (e.g. <i>\"focus on salary detection feature\"</i>, "
            f"<i>\"mention Diwali offer\"</i>) or send /skip to let me choose."
        ),
        "parse_mode": "HTML",
    })


# ── Draft send ─────────────────────────────────────────────────────────────────

def send_draft(draft: dict, image_url: str) -> Optional[int]:
    """Send draft photo + caption + Approve/Revise keyboard. Returns message_id."""
    if not _configured():
        print("[Telegram] Not configured — skipping.")
        return None

    draft_id        = draft["draft_id"]
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

    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve|{draft_id}"},
        {"text": "✏️ Revise",  "callback_data": f"revise|{draft_id}"},
    ]]}

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
        print(f"[Telegram] Photo failed: {data.get('description')} — text fallback.")
    except Exception as e:
        print(f"[Telegram] Photo error: {e}")

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
        print(f"[Telegram] Text fallback failed: {data2}")
    except Exception as e:
        print(f"[Telegram] Text fallback error: {e}")

    return None


# ── Update polling ─────────────────────────────────────────────────────────────

def check_response(
    draft_id: Optional[str],
    offset: int = 0,
    awaiting_remarks: bool = False,
    wizard_step: Optional[str] = None,
) -> Tuple[str, Optional[str], int, bool]:
    """Poll getUpdates and return the highest-priority event seen.

    Args:
        draft_id:         Current pending draft ID (or None).
        offset:           Last processed update_id + 1.
        awaiting_remarks: Waiting for revision text from user.
        wizard_step:      Current wizard step: "img_style" | "cap_style" | "context" | None.

    Returns:
        (status, payload, new_offset, new_awaiting_remarks)

        status values:
          "approved"          — user approved the draft
          "remarks"           — user sent revision notes (payload = notes text)
          "post_requested"    — /post command (wizard_step was None)
          "wizard_img_picked" — user picked image style (payload = style key)
          "wizard_cap_picked" — user picked caption style (payload = style key)
          "wizard_context"    — user sent context or /skip (payload = text | None)
          "pending"           — nothing actionable yet
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

    updates    = data.get("result", [])
    new_offset = offset
    status     = "pending"
    payload    = None

    for update in updates:
        new_offset = max(new_offset, update["update_id"] + 1)

        # ── Callback query (button press) ────────────────────────────────────
        if "callback_query" in update:
            cq        = update["callback_query"]
            from_chat = str(cq["message"]["chat"]["id"])
            if from_chat != str(_chat_id()):
                continue

            cb_data = cq.get("data", "")
            if "|" not in cb_data:
                continue
            action, cb_payload = cb_data.split("|", 1)

            # Wizard: image style picked
            if action == "wizard_img":
                _answer_cb(cq["id"], f"✅ Image: {_img_label(cb_payload)}")
                status  = "wizard_img_picked"
                payload = cb_payload
                continue

            # Wizard: caption style picked
            if action == "wizard_cap":
                _answer_cb(cq["id"], f"✅ Caption: {_cap_label(cb_payload)}")
                status  = "wizard_cap_picked"
                payload = cb_payload
                continue

            # Draft approval / revision (must match current draft_id)
            if cb_payload != draft_id:
                continue

            if action == "approve":
                status          = "approved"
                awaiting_remarks = False
                _answer_cb(cq["id"], "✅ Approved! Posting to Instagram shortly...", alert=True)
                _send({
                    "chat_id":    _chat_id(),
                    "text": (
                        "✅ <b>Post approved!</b>\n\n"
                        "The agent will upload it to Instagram within the next 10 minutes. "
                        "You'll get a message when it's live."
                    ),
                    "parse_mode": "HTML",
                })

            elif action == "revise":
                awaiting_remarks = True
                _answer_cb(cq["id"], "✏️ Send your revision notes below.")
                _send({
                    "chat_id":    _chat_id(),
                    "text":       "✏️ <b>What should I change?</b>\n\nReply with your revision notes:",
                    "parse_mode": "HTML",
                })

        # ── Text message ─────────────────────────────────────────────────────
        elif "message" in update:
            msg       = update["message"]
            from_chat = str(msg.get("chat", {}).get("id", ""))
            text      = msg.get("text", "").strip()

            if from_chat != str(_chat_id()) or not text:
                continue

            cmd = text.lower().split()[0]

            if cmd in ("/post", "/post@finamigobot"):
                if wizard_step is None and status == "pending":
                    status = "post_requested"
                continue

            if cmd in ("/status", "/help"):
                _send_status_reply(draft_id, wizard_step)
                continue

            if cmd == "/skip" and wizard_step == "context":
                status  = "wizard_context"
                payload = None
                continue

            # Wizard context text
            if wizard_step == "context" and not text.startswith("/"):
                status  = "wizard_context"
                payload = text
                continue

            # Revision remarks
            if awaiting_remarks and not text.startswith("/"):
                status          = "remarks"
                payload         = text
                awaiting_remarks = False

    return (status, payload, new_offset, awaiting_remarks)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _send_status_reply(draft_id: Optional[str], wizard_step: Optional[str] = None) -> None:
    if not _configured():
        return
    if wizard_step:
        steps = {"img_style": "1/3 image tone", "cap_style": "2/3 caption style", "context": "3/3 context"}
        msg = (
            f"🔄 <b>Post wizard in progress</b> — step {steps.get(wizard_step, wizard_step)}\n\n"
            f"Complete the wizard or send /post to restart."
        )
    elif draft_id:
        msg = (
            f"📋 <b>Draft pending approval</b>\n"
            f"<b>Draft ID:</b> <code>{draft_id}</code>\n\n"
            f"Tap ✅ Approve or ✏️ Revise on the draft above.\n"
            f"Send /post to generate a new draft (replaces current)."
        )
    else:
        msg = (
            "💤 <b>No pending draft.</b>\n\n"
            "Send /post to generate a new Instagram post."
        )
    _send({"chat_id": _chat_id(), "text": msg, "parse_mode": "HTML"})


def notify(message: str) -> None:
    """Send a plain notification to the chat."""
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
