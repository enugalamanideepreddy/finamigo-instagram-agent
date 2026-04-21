"""
FinAmigo Telegram Bot Server
Runs on Render (free tier) as a persistent webhook server.
Handles all Telegram interaction instantly; triggers GitHub Actions for heavy work.

Endpoints:
  POST /webhook         — Telegram webhook (set via setWebhook)
  POST /register_draft  — Called by generate.yml after creating a draft
  POST /notify          — Called by post.yml after posting to Instagram
  GET  /health          — Health check
"""

import json
import os
import threading

import requests
from fastapi import FastAPI, HTTPException, Request

app = FastAPI()

# ── Config ─────────────────────────────────────────────────────────────────────

def _tok():    return os.environ.get("TELEGRAM_BOT_TOKEN", "")
def _cid():    return os.environ.get("TELEGRAM_CHAT_ID", "")
def _pat():    return os.environ.get("GH_PAT", "")
def _repo():   return os.environ.get("GH_REPO", "")          # owner/repo
def _secret(): return os.environ.get("BOT_API_SECRET", "")   # shared secret for internal calls

STATE_FILE = "bot_state.json"
_lock = threading.Lock()

# ── State ──────────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        pass
    return {"wizard": None, "draft": None, "awaiting_remarks": False}

def _save(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Telegram helpers ───────────────────────────────────────────────────────────

def _tg(method: str, **kw) -> dict:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_tok()}/{method}",
            json=kw, timeout=15,
        )
        return r.json()
    except Exception as e:
        print(f"[TG] {method} error: {e}")
        return {}

def _send(text: str, keyboard: dict = None, parse_mode: str = "HTML") -> None:
    payload = {"chat_id": _cid(), "text": text, "parse_mode": parse_mode}
    if keyboard:
        payload["reply_markup"] = keyboard
    _tg("sendMessage", **payload)

def _answer(cq_id: str, text: str = "", alert: bool = False) -> None:
    _tg("answerCallbackQuery", callback_query_id=cq_id, text=text, show_alert=alert)

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ── GitHub Actions trigger ─────────────────────────────────────────────────────

def _trigger(workflow_file: str, inputs: dict) -> bool:
    clean = {k: str(v) for k, v in inputs.items() if v is not None and str(v).strip()}
    r = requests.post(
        f"https://api.github.com/repos/{_repo()}/actions/workflows/{workflow_file}/dispatches",
        headers={
            "Authorization": f"Bearer {_pat()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"ref": "main", "inputs": clean},
        timeout=15,
    )
    print(f"[GH] trigger {workflow_file} → {r.status_code}")
    return r.status_code == 204

# ── Wizard option tables ───────────────────────────────────────────────────────

IMG_OPTIONS = [
    ("🌑 Dark & Premium",    "phone_hero_dark"),
    ("🖥️ Dual Screen",      "dual_screen_split"),
    ("🃏 Floating Cards",    "ui_cards_floating"),
    ("🏛️ Isometric 3D",     "isometric_3d"),
    ("🔍 UI Close-up",       "ui_closeup_immersive"),
    ("⬜ Minimal Light",     "minimal_light_flat"),
    ("⚡ Neon Cyberpunk",    "neon_cyberpunk"),
    ("📐 Perspective Tilt",  "perspective_tilt"),
    ("🎲 Auto",              "auto"),
]

CAP_OPTIONS = [
    ("💪 Bold Statement",   "bold_statement"),
    ("❓ Question Hook",    "question_hook"),
    ("📊 Stat Lead",        "stat_lead"),
    ("📖 Story Moment",     "story_moment"),
    ("↔️ Contrast",         "contrast"),
    ("🎲 Auto",             "auto"),
]

def _img_label(v: str) -> str:
    return next((l for l, val in IMG_OPTIONS if val == v), v)

def _cap_label(v: str) -> str:
    return next((l for l, val in CAP_OPTIONS if val == v), v)

def _kb(options: list, prefix: str, cols: int = 2) -> dict:
    btns = [{"text": l, "callback_data": f"{prefix}|{v}"} for l, v in options]
    rows = [btns[i:i+cols] for i in range(0, len(btns), cols)]
    return {"inline_keyboard": rows}

# ── Wizard send helpers ────────────────────────────────────────────────────────

def _step1():
    _send("🎨 <b>Step 1/3 — Choose image tone:</b>", keyboard=_kb(IMG_OPTIONS, "wiz_img"))

def _step2(img_label: str):
    _send(
        f"✅ Image: <b>{_esc(img_label)}</b>\n\n✍️ <b>Step 2/3 — Choose caption style:</b>",
        keyboard=_kb(CAP_OPTIONS, "wiz_cap"),
    )

def _step3(img_label: str, cap_label: str):
    _send(
        f"✅ <b>{_esc(img_label)}</b>  ·  <b>{_esc(cap_label)}</b>\n\n"
        f"📝 <b>Step 3/3 — Any context for this post?</b>\n\n"
        f"Type a note  (e.g. <i>\"focus on salary feature\"</i>)  or send /skip"
    )

# ── Generation kick-off ────────────────────────────────────────────────────────

def _kick_generate(state: dict, context: str = None) -> None:
    w         = state.get("wizard") or {}
    img_style = w.get("image_style", "auto")
    cap_style = w.get("caption_style", "auto")
    gist_id   = (state.get("draft") or {}).get("gist_id", "")  # for revision context

    state["wizard"]          = None
    state["awaiting_remarks"] = False
    _save(state)

    _send("⏳ <b>On it!</b> Generating your post — I'll send the draft here in ~2–3 minutes.")
    _trigger("generate.yml", {
        "image_style":   img_style,
        "caption_style": cap_style,
        "context":       context or "",
        "gist_id":       gist_id,
    })

# ── Command handler ────────────────────────────────────────────────────────────

def _handle_cmd(cmd: str, state: dict) -> None:
    if cmd in ("/start", "/help", "/status"):
        wizard = state.get("wizard")
        draft  = state.get("draft")
        if wizard:
            steps = {"img_style": "1/3 image tone", "cap_style": "2/3 caption style", "context": "3/3 context"}
            _send(
                f"🔄 <b>Wizard in progress</b> — step {steps.get(wizard.get('step', ''), '?')}\n\n"
                f"Complete the steps above, or send /post to restart."
            )
        elif draft:
            _send(
                f"📋 <b>Draft pending approval</b>\n"
                f"<code>{draft.get('draft_id', '?')}</code>\n\n"
                f"Tap ✅ Approve or ✏️ Revise on the message above.\n"
                f"Send /post to discard and generate a new one."
            )
        else:
            _send("💤 <b>No pending draft.</b>\n\nSend /post to generate one!")

    elif cmd in ("/post", "/post@finamigobot"):
        if state.get("draft"):
            _send(
                "⚠️ <b>There's already a draft pending.</b>\n\n"
                "Approve or revise it first, then send /post again."
            )
        else:
            state["wizard"] = {"step": "img_style"}
            _save(state)
            _step1()

    elif cmd == "/cancel":
        state["wizard"]          = None
        state["awaiting_remarks"] = False
        _save(state)
        _send("❌ Wizard cancelled. Send /post to start a new one.")

# ── Webhook ────────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(req: Request):
    update = await req.json()

    with _lock:
        state = _load()

        # ── Callback query (button press) ─────────────────────────────────────
        if "callback_query" in update:
            cq        = update["callback_query"]
            from_chat = str(cq["message"]["chat"]["id"])
            if from_chat != str(_cid()):
                return {"ok": True}

            data = cq.get("data", "")
            if "|" not in data:
                return {"ok": True}
            action, payload = data.split("|", 1)

            # Wizard: image style
            if action == "wiz_img":
                label = _img_label(payload)
                _answer(cq["id"], f"✅ {label}")
                state["wizard"] = {"step": "cap_style", "image_style": payload}
                _save(state)
                _step2(label)

            # Wizard: caption style
            elif action == "wiz_cap":
                label = _cap_label(payload)
                _answer(cq["id"], f"✅ {label}")
                w = state.get("wizard") or {}
                state["wizard"] = {
                    "step":          "context",
                    "image_style":   w.get("image_style", "auto"),
                    "caption_style": payload,
                }
                _save(state)
                _step3(_img_label(state["wizard"]["image_style"]), label)

            # Draft: approve
            elif action == "approve":
                draft = state.get("draft")
                if draft and draft.get("draft_id") == payload:
                    _answer(cq["id"], "✅ Posting to Instagram...", alert=True)
                    _send("✅ <b>Approved!</b> Posting to Instagram now — I'll confirm when it's live.")
                    state["draft"] = {**draft, "status": "approved"}
                    _save(state)
                    _trigger("post.yml", {"gist_id": draft.get("gist_id", "")})
                else:
                    _answer(cq["id"], "⚠️ Draft not found — it may have already been handled.")

            # Draft: revise
            elif action == "revise":
                draft = state.get("draft")
                if draft and draft.get("draft_id") == payload:
                    _answer(cq["id"], "✏️ Send your revision notes below.")
                    state["awaiting_remarks"] = True
                    _save(state)
                    _send("✏️ <b>What should I change?</b>\n\nReply with your revision notes:")
                else:
                    _answer(cq["id"], "⚠️ Draft not found.")

        # ── Text message ──────────────────────────────────────────────────────
        elif "message" in update:
            msg       = update["message"]
            from_chat = str(msg.get("chat", {}).get("id", ""))
            text      = msg.get("text", "").strip()

            if from_chat != str(_cid()) or not text:
                return {"ok": True}

            cmd = text.lower().split()[0]

            if text.startswith("/"):
                if cmd == "/skip" and (state.get("wizard") or {}).get("step") == "context":
                    _kick_generate(state, context=None)
                else:
                    _handle_cmd(cmd, state)
                    _save(state)

            elif (state.get("wizard") or {}).get("step") == "context":
                _kick_generate(state, context=text)

            elif state.get("awaiting_remarks"):
                draft = state.get("draft")
                state["awaiting_remarks"] = False
                _save(state)
                _send("⏳ <b>Revising the draft...</b> I'll send the updated version in ~2 minutes.")
                _trigger("generate.yml", {
                    "gist_id": (draft or {}).get("gist_id", ""),
                    "remarks": text,
                })

    return {"ok": True}


# ── Internal endpoints (called by GitHub Actions) ──────────────────────────────

@app.post("/register_draft")
async def register_draft(req: Request):
    """generate.yml calls this after creating a draft. Bot sends the Telegram approval message."""
    if _secret() and req.headers.get("X-Bot-Secret") != _secret():
        raise HTTPException(status_code=403)

    data      = await req.json()
    draft_id  = data["draft_id"]
    gist_id   = data.get("gist_id", "")
    image_url = data["image_url"]
    caption   = data["caption"]
    theme     = data.get("theme", "")
    img_style = data.get("image_style", "")
    cap_style = data.get("caption_style", "")

    with _lock:
        state = _load()
        state["draft"]           = {"draft_id": draft_id, "gist_id": gist_id, "status": "pending"}
        state["awaiting_remarks"] = False
        _save(state)

    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve|{draft_id}"},
        {"text": "✏️ Revise",  "callback_data": f"revise|{draft_id}"},
    ]]}
    text = (
        f"📸 <b>New FinAmigo Draft</b>\n\n"
        f"<b>Theme:</b> {_esc(theme)}\n"
        f"<b>Style:</b> {_esc(cap_style)} · {_esc(img_style)}\n"
        f"<b>Draft ID:</b> <code>{draft_id}</code>\n\n"
        f"───────────────\n{_esc(caption[:900])}"
    )
    r = _tg("sendPhoto", chat_id=_cid(), photo=image_url,
            caption=text, parse_mode="HTML", reply_markup=keyboard)
    if not r.get("ok"):
        _tg("sendMessage", chat_id=_cid(), text=text, parse_mode="HTML", reply_markup=keyboard)

    return {"ok": True}


@app.post("/notify")
async def notify_endpoint(req: Request):
    """post.yml calls this after posting to Instagram."""
    if _secret() and req.headers.get("X-Bot-Secret") != _secret():
        raise HTTPException(status_code=403)

    data = await req.json()
    msg  = data.get("message", "✅ Post is live on Instagram!")

    with _lock:
        state = _load()
        state["draft"] = None
        _save(state)

    _send(msg)
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}
