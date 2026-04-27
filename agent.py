"""
FinAmigo Instagram Agent
Generates accurate, feature-grounded Instagram posts for @finamigox.

Modes:
  --generate   Generate draft → Telegram + email approval request
  --check      Poll Telegram for approval → post or regenerate
  --dry-run    Generate and print only (no notifications, no post)
  --post-now   Skip approval, generate and post immediately
  --metrics    Fetch Instagram insights for recent posts (run weekly)
"""

import json
import os
import random
import sys
import tempfile
import time
from datetime import datetime
from typing import Optional, Tuple

import requests as req
from dotenv import load_dotenv

load_dotenv(override=True)

from approval import send_draft_email
from features_loader import fetch_features
from gist_store import delete_draft_gist, load_draft_from_gist, save_draft_to_gist
from telegram_bot import notify as tg_notify, send_draft as tg_send
from image_composer import upload_composited
import approval as _approval_mod

# ── Config ────────────────────────────────────────────────────────────────────

GEMINI_API_KEY         = os.environ["GEMINI_API_KEY"]
REPLICATE_API_TOKEN    = os.environ["REPLICATE_API_TOKEN"]
INSTAGRAM_ACCESS_TOKEN = os.environ["INSTAGRAM_ACCESS_TOKEN"]
INSTAGRAM_ACCOUNT_ID   = os.environ["INSTAGRAM_ACCOUNT_ID"]
IMGBB_API_KEY          = os.environ.get("IMGBB_API_KEY", "")

def _gemini_url(model: str) -> str:
    # Always v1beta — required for system_instruction support
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={GEMINI_API_KEY}"
    )

# Models in order — try newest first, stable fallbacks last
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]

# Local fallback draft path (used for --dry-run and --post-now only)
DRAFT_PATH = os.path.join(os.path.dirname(__file__), "draft.json")

# ── State ─────────────────────────────────────────────────────────────────────

_STATE_PATH = os.path.join(os.path.dirname(__file__), "agent_state.json")


def _load_state() -> dict:
    if os.path.exists(_STATE_PATH):
        try:
            with open(_STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "used_themes": [],
        "used_images": [],
        "used_caption_styles": [],
        "used_image_styles": [],
        "current_gist_id": None,
        "posted_drafts": [],        # [{draft_id, post_id, theme, caption_style, image_style, date}]
        "engagement_scores": {},    # {theme: avg_score}
    }


def _save_state(state: dict) -> None:
    with open(_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ── HTTP / Gemini ─────────────────────────────────────────────────────────────

def _make_payload(system_prompt: str, user_msg: str, max_tokens: int,
                  use_system_instruction: bool = True) -> dict:
    if use_system_instruction:
        return {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_msg}]}],
            "generationConfig": {"temperature": 0.9, "maxOutputTokens": max_tokens},
        }
    # Fallback: merge system prompt into user message
    return {
        "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_msg}"}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": max_tokens},
    }


def gemini_generate(system_prompt: str, user_msg: str, max_tokens: int = 600) -> str:
    """Call Gemini API. Cascades through models on 404/rate-limit."""
    last_errors = {}
    for model in GEMINI_MODELS:
        url = _gemini_url(model)
        # Try with system_instruction first, then without if model rejects it
        for use_si in (True, False):
            payload = _make_payload(system_prompt, user_msg, max_tokens, use_si)
            label = model if use_si else f"{model}(no-SI)"
            print(f"[Gemini] Trying {label}")
            for attempt in range(3):
                try:
                    r = req.post(url, json=payload, timeout=90)
                    print(f"[Gemini] HTTP {r.status_code} from {label}")
                    data = r.json()
                except req.exceptions.Timeout:
                    print(f"[Gemini] Timeout ({label}, attempt {attempt+1}/3). Waiting 65s...")
                    time.sleep(65)
                    continue
                if "error" in data:
                    err    = data["error"]
                    code   = err.get("code") or 0
                    status = err.get("status", "")
                    msg    = err.get("message", "")
                    print(f"[Gemini] {label} — {code} {status}: {msg[:120]}")
                    last_errors[label] = f"{code}: {msg[:80]}"
                    # Model not available → try next model
                    if code == 404 or status == "NOT_FOUND":
                        break
                    # system_instruction not supported → retry without it
                    if code == 400 and "system_instruction" in msg:
                        break  # breaks inner attempt loop → use_si=False next
                    # Permission denied → bad key, fail fast
                    if code == 403 or status == "PERMISSION_DENIED":
                        raise RuntimeError(
                            f"Gemini API key rejected ({status}): {msg}\n"
                            "→ Update GEMINI_API_KEY in GitHub repo Settings → Secrets."
                        )
                    # Rate limited → wait then try next model
                    if code == 429 or "RESOURCE_EXHAUSTED" in status:
                        if attempt < 2:
                            print(f"[Gemini] Rate limited. Waiting 65s...")
                            time.sleep(65)
                            continue
                        break
                    # Other error → skip model
                    break
                # Success
                print(f"[Gemini] ✓ Success with {label}")
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            else:
                continue  # all 3 attempts exhausted → try next use_si
            if code == 404 or status == "NOT_FOUND":
                break  # skip remaining use_si variants, go to next model
    raise RuntimeError(f"All Gemini models failed.\n{last_errors}")


# ── System Prompt ─────────────────────────────────────────────────────────────

def build_system_prompt(features_text: str) -> str:
    return f"""You are a world-class App Marketing specialist for FinAmigo — an offline-first personal finance app for Indian users. Your goal is to generate high-conversion, trust-building Instagram content that drives app downloads.

WRITING STYLE:
- Write like a premium global brand (Apple, Stripe, CRED).
- SHORT and PUNCHY. Max 40–60 words in the main body.
- Lead with a bold value proposition hook (Stop the scroll).
- Focus on the USER BENEFIT (Peace of mind, organized life, financial clarity).
- NEVER mention technical jargon (parsers, thresholds, CV, algorithms).
- Use 1–2 emojis max for professional emphasis.
- End with a "Coming Soon" teaser CTA (app not yet launched — do NOT say "link in bio").
- Use 6–8 targeted hashtags for growth.

ACCURACY RULES:
1. Only make claims supported by the Feature Reference below.
2. If something is under "What FinAmigo Does NOT Do", NEVER claim it.
3. Never claim real-time monitoring, live alerts, or bank API connections.
4. Don't invent features. Keep it real.

=== FEATURE REFERENCE (source of truth) ===
{features_text}
=== END FEATURE REFERENCE ==="""


# ── Themes ────────────────────────────────────────────────────────────────────

THEME_POOL = [
    "Know your financial health — your FinAmigo Score tells the truth",
    "Your salary, auto-detected. No manual entry needed.",
    "Where did your money go? FinAmigo categorizes every rupee.",
    "That Netflix subscription you forgot? FinAmigo didn't.",
    "One app, all your banks. HDFC, SBI, ICICI & more.",
    "Your data stays on YOUR phone. Zero cloud. Zero compromise.",
    "Connect Gmail once — statements imported forever.",
    "Stop guessing. Start seeing your money clearly.",
    "Your spending patterns reveal more than you think.",
    "Financial health isn't about how much you earn — it's how you spend.",
    "EMIs, rent, insurance — know your fixed costs at a glance.",
    "Your phone, your data, your rules. Privacy-first finance.",
]

# ── Caption Styles ────────────────────────────────────────────────────────────

CAPTION_STYLES = [
    {
        "name": "bold_statement",
        "instruction": (
            "Open with a bold, provocative one-liner statement (no question). "
            "Follow with 2 punchy benefit lines. End with CTA."
        ),
    },
    {
        "name": "question_hook",
        "instruction": (
            "Open with a short rhetorical question that calls out a common money pain point. "
            "Answer it in 2 lines with what FinAmigo does. End with CTA."
        ),
    },
    {
        "name": "stat_lead",
        "instruction": (
            "Open with a specific, concrete fact or number from the Feature Reference "
            "(e.g., supported banks, score range, transaction categories). "
            "Frame it as a surprising insight. 2 follow-up benefit lines. End with CTA."
        ),
    },
    {
        "name": "story_moment",
        "instruction": (
            "Paint a 1-sentence relatable financial scenario (salary day, bill shock, month-end). "
            "Then show how FinAmigo resolves it in 2 lines. End with CTA."
        ),
    },
    {
        "name": "contrast",
        "instruction": (
            "Use a 'before vs after' or 'without vs with FinAmigo' contrast structure. "
            "2 lines of contrast, 1 punch line. End with CTA."
        ),
    },
]

# ── Image Styles ──────────────────────────────────────────────────────────────

IMAGE_VISUAL_STYLES = [
    {
        "name": "phone_hero_dark",
        "template": (
            "Professional 3D app marketing render. A single premium smartphone centered and floating "
            "at a slight 15-degree angle against a deep charcoal to midnight black gradient. "
            "The phone screen glows with a clean fintech app interface showing {screen_detail}. "
            "Translucent glassmorphism stat cards float beside the phone. "
            "Electric blue and violet neon rim lighting, subtle bokeh particles. "
            "No text, no words, no labels anywhere. Ultra clean premium visual only."
        ),
    },
    {
        "name": "dual_screen_split",
        "template": (
            "Premium app marketing visual. Two smartphones side by side, slightly angled toward each other, "
            "against a soft white to pale sky blue gradient. "
            "Left screen shows {screen_detail}, right screen shows a summary dashboard. "
            "Clean drop shadows, teal and coral accent highlights. "
            "No text, no words, no labels. Flat-minimal editorial product photography."
        ),
    },
    {
        "name": "ui_cards_floating",
        "template": (
            "Abstract fintech app marketing artwork. NO phone frame — only floating UI cards "
            "arranged in a dynamic staggered layout against a warm sunset gradient from deep orange to magenta. "
            "Cards display {screen_detail} with glassmorphism frosted effect. "
            "Golden yellow highlights, soft ambient glow. "
            "No text, no words, no labels. Modern editorial design, ultra-clean."
        ),
    },
    {
        "name": "isometric_3d",
        "template": (
            "Isometric 3D app marketing illustration. A premium smartphone from a 45-degree isometric angle "
            "against a deep forest green to emerald gradient. "
            "The screen displays {screen_detail} in crisp detail. "
            "Flat isometric style with subtle 3D depth. Lime green and white accents. "
            "No text, no words, no labels. Clean vector-style product render."
        ),
    },
    {
        "name": "ui_closeup_immersive",
        "template": (
            "Immersive app UI close-up render. The entire frame filled with a zoomed-in fintech app screen "
            "showing {screen_detail}, dark navy to electric blue vignette at edges. "
            "No phone frame visible — just the glowing app interface. "
            "Floating micro data chips and sparkline graphs. "
            "No text, no words, no labels. Cinematic depth-of-field, premium digital art."
        ),
    },
    {
        "name": "minimal_light_flat",
        "template": (
            "Minimalist flat app marketing render. A smartphone at a slight angle on a pure white "
            "to soft lavender gradient. The screen shows {screen_detail}. "
            "Lots of breathing room, thin teal geometric line accents. "
            "No text, no words, no labels. Swiss editorial aesthetic."
        ),
    },
    {
        "name": "neon_cyberpunk",
        "template": (
            "High-energy fintech app marketing visual. A smartphone floating against a deep midnight purple "
            "to hot pink gradient. The screen blazes with {screen_detail}. "
            "Intense neon pink and cyan glow halos, light streaks, lens flares. "
            "No text, no words, no labels. Bold high-contrast cyberpunk aesthetic."
        ),
    },
    {
        "name": "perspective_tilt",
        "template": (
            "Dynamic perspective app marketing render. A premium smartphone tilted at 30 degrees from a "
            "dramatic low angle against a rich deep teal to dark navy gradient. "
            "The screen shows {screen_detail} in vivid color. "
            "Long dramatic shadow, cinematic studio lighting from above. Chrome bezel detail. "
            "No text, no words, no labels. Studio product photography style."
        ),
    },
]

# ── Random Cycling ────────────────────────────────────────────────────────────

def _pick_random_unused(pool: list, used: list, key: str = None):
    def item_id(x):
        return x[key] if key else x
    remaining = [x for x in pool if item_id(x) not in used]
    if not remaining:
        used.clear()
        remaining = list(pool)
    choice = random.choice(remaining)
    used.append(item_id(choice))
    return choice


def pick_theme(state: dict) -> str:
    used = state.setdefault("used_themes", [])
    # Weight towards higher-engagement themes if we have data
    scores = state.get("engagement_scores", {})
    if scores:
        remaining = [t for t in THEME_POOL if t not in used]
        if not remaining:
            used.clear()
            remaining = list(THEME_POOL)
        # Weighted by (1 + engagement_score) so unscored themes still get picked
        weights = [1 + scores.get(t, 0) for t in remaining]
        choice = random.choices(remaining, weights=weights, k=1)[0]
        used.append(choice)
        return choice
    return _pick_random_unused(THEME_POOL, used)


def pick_caption_style(state: dict) -> dict:
    used = state.setdefault("used_caption_styles", [])
    return _pick_random_unused(CAPTION_STYLES, used, key="name")


def pick_image_style(state: dict) -> dict:
    used = state.setdefault("used_image_styles", [])
    return _pick_random_unused(IMAGE_VISUAL_STYLES, used, key="name")


# ── Context refinement ────────────────────────────────────────────────────────

def refine_context_to_theme(features_text: str, raw_context: str) -> str:
    """
    Turn a short user note (e.g. "Internal Transfers") into a punchy marketing
    theme angle like the entries in THEME_POOL — benefit-led, specific, Instagram-ready.
    """
    system = (
        "You are a fintech marketing strategist for FinAmigo — an offline-first personal "
        "finance app for Indian users. Your job is to turn a short user note into a "
        "single punchy Instagram theme angle.\n\n"
        "Rules:\n"
        "- One sentence, max 15 words\n"
        "- Lead with the USER BENEFIT (peace of mind, clarity, control, savings)\n"
        "- Ground it in the Feature Reference — only reference real features\n"
        "- Write like CRED or Apple — minimal, impactful\n"
        "- NO technical jargon, NO emojis\n\n"
        f"=== FEATURE REFERENCE ===\n{features_text}\n=== END ==="
    )
    user_msg = (
        f"User note: \"{raw_context}\"\n\n"
        "Write ONE punchy theme angle sentence for an Instagram post about this topic."
    )
    return gemini_generate(system, user_msg, max_tokens=60)


# ── Caption Generation ────────────────────────────────────────────────────────

_DOW_TONE = {
    0: "Monday — working professionals starting the week. Keep it motivational and actionable.",
    1: "Tuesday — mid-week grind. Focus on clarity and financial control.",
    2: "Wednesday — hump day. Relatable money moments work well.",
    3: "Thursday — weekend is near. Aspirational and forward-looking tone.",
    4: "Friday — end-of-week energy. Celebratory or reflective about the month.",
    5: "Saturday — relaxed weekend scroll. Conversational, warm, less corporate.",
    6: "Sunday — planning mode. Budgeting, goals, financial reset vibes.",
}


def generate_caption(
    features_text: str,
    theme: str,
    remarks: Optional[str] = None,
    caption_style: Optional[dict] = None,
) -> str:
    dow = datetime.now().weekday()
    dow_context = _DOW_TONE.get(dow, "")
    style_instruction = (
        caption_style["instruction"]
        if caption_style
        else "Open with a bold hook. 2–3 benefit lines. End with CTA."
    )
    user_msg = (
        f"Write an Instagram caption for FinAmigo.\n\n"
        f"Today's theme/angle: {theme}\n\n"
        f"Audience context: {dow_context}\n\n"
        f"POST FORMAT — follow this structure exactly:\n"
        f"{style_instruction}\n\n"
        f"Rules:\n"
        f"- MAX 40–60 words in the body. Short. Punchy. No fluff.\n"
        f"- Talk about BENEFITS, not technical details\n"
        f"- NO engineering jargon (no parsers, algorithms, thresholds, CVs)\n"
        f"- Write like CRED or Apple — minimal, impactful\n"
        f"- 1–2 emojis max\n"
        f"- End with a 'Coming Soon' teaser CTA (app not yet launched — do NOT say 'link in bio')\n"
        f"- Last line: 6–8 hashtags only\n"
        f"- Indian financial context (₹, UPI, EMI, salary)"
    )
    if remarks:
        user_msg += f"\n\nREVISION NOTES (from reviewer): {remarks}"
    return gemini_generate(build_system_prompt(features_text), user_msg, max_tokens=1000)


def fact_check_caption(features_text: str, caption: str) -> Tuple[bool, str]:
    system = (
        "You are a strict fact-checker for FinAmigo social media content. "
        "Compare the caption against the Feature Reference. "
        "Flag ANY claim that is inaccurate, exaggerated, or not supported by the reference. "
        "Pay special attention to the 'What FinAmigo Does NOT Do' section."
    )
    user_msg = (
        f"=== FEATURE REFERENCE ===\n{features_text}\n=== END ===\n\n"
        f"=== CAPTION TO CHECK ===\n{caption}\n=== END ===\n\n"
        f"Is every claim in this caption fully accurate and supported by the Feature Reference? "
        f"Reply with exactly 'PASS' if accurate, or 'FAIL: <reason>' if not."
    )
    text = gemini_generate(system, user_msg, max_tokens=400)
    if text.upper().startswith("PASS"):
        return (True, "")
    return (False, text)


# ── Image Generation ──────────────────────────────────────────────────────────

def generate_image_prompt(
    features_text: str, theme: str, image_style: Optional[dict] = None
) -> Tuple[str, str]:
    system = (
        "You write ONE short sentence (max 15 words) describing what a finance app screen shows. "
        "Examples: 'score dial at 720 with expense pie chart' or "
        "'salary notification card with income bar chart'. "
        "ONLY describe app UI elements. No background, no phone, no scenery, no text."
    )
    screen_detail = gemini_generate(
        system, f"Theme: {theme}\nDescribe the app screen UI in one short sentence.", max_tokens=60
    )
    style = image_style or IMAGE_VISUAL_STYLES[0]
    prompt = style["template"].format(screen_detail=screen_detail)
    negative = (
        "misspelled text, garbled letters, distorted fonts, illegible words, "
        "nature, landscape, trees, mountains, animals, people, faces, hands, fingers, "
        "cartoon, sketch, drawing, low quality, blurry, watermark, signature"
    )
    return prompt, negative


def generate_image_tagline(theme: str) -> str:
    """
    Generate a short, punchy 4–6 word visual tagline for the image overlay.
    Designed to read well as large text on a dark gradient band.
    """
    system = (
        "You write ultra-short visual taglines for app marketing images. "
        "Rules:\n"
        "- Exactly 4–6 words. No more.\n"
        "- Punchy, benefit-led, present tense\n"
        "- NO emojis, NO hashtags, NO punctuation except a period or dash\n"
        "- Write like Apple or CRED — minimal, confident\n"
        "- Examples: 'Your money. Finally clear.' | 'Finance without the noise.' | 'Know every rupee.'"
    )
    return gemini_generate(system, f"Theme: {theme}\nWrite the tagline.", max_tokens=20).strip('"').strip()


STATIC_IMAGES_PATH = os.path.join(os.path.dirname(__file__), "images.json")


def _load_static_images() -> list:
    if os.path.exists(STATIC_IMAGES_PATH):
        with open(STATIC_IMAGES_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else data.get("urls", [])
    return []


def pick_static_image(state: dict) -> str:
    images = _load_static_images()
    if not images:
        return ""
    used = state.setdefault("used_images", [])
    return _pick_random_unused(images, used)


def generate_image(prompt: str, negative_prompt: str = "", state: dict = None) -> str:
    """Generate a 1:1 image with Ideogram v2 Turbo via Replicate."""
    if state:
        static = pick_static_image(state)
        if static:
            print(f"[Agent] Using static image: {static[:60]}...")
            return static

    print("[Agent] Generating image with Ideogram v2 Turbo...")
    headers = {"Authorization": f"Bearer {REPLICATE_API_TOKEN}"}
    r = req.post(
        "https://api.replicate.com/v1/models/ideogram-ai/ideogram-v2-turbo/predictions",
        json={"input": {
            "prompt":          prompt,
            "negative_prompt": negative_prompt,
            "aspect_ratio":    "1:1",
            "style_type":      "Design",
        }},
        headers=headers, timeout=35,
    )
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Ideogram error: {data['error']}")
    poll_url = data["urls"]["get"]

    for _ in range(60):
        time.sleep(3)
        poll = req.get(poll_url, headers=headers, timeout=20).json()
        if poll["status"] == "succeeded":
            output = poll.get("output")
            if not output:
                raise RuntimeError("Ideogram succeeded but returned no output URL.")
            url = output[0] if isinstance(output, list) else output
            print(f"[Agent] Image ready: {url[:60]}...")
            return url
        if poll["status"] in ("failed", "canceled"):
            raise RuntimeError(f"Ideogram failed: {poll.get('error')}")

    raise TimeoutError("Ideogram did not complete in time.")


# ── Image URL Validation & Re-hosting ────────────────────────────────────────

def _is_url_image(url: str) -> bool:
    """Verify the URL actually serves image bytes (not an expired/error JSON)."""
    try:
        r = req.get(url, headers={"Range": "bytes=0-15"}, timeout=12, stream=True)
        first_bytes = b""
        for chunk in r.iter_content(16):
            first_bytes = chunk
            break
        ct = r.headers.get("Content-Type", "")
        is_image_bytes = (
            first_bytes[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1")
            or first_bytes[:3] == b"\xff\xd8\xff"
        )
        return is_image_bytes or ct.startswith("image/")
    except Exception:
        return False



def _ensure_image_url(draft: dict, state: dict) -> str:
    """Return a stable public image URL ready for Instagram.

    The draft already has a composited imgbb URL from run_generate_with_state.
    Only re-generate + re-composite if that URL has expired.
    """
    url = draft["image_url"]
    # Re-upload imgbb URLs — imgbb blocks Instagram's crawler (hotlink protection)
    is_imgbb = "ibb.co" in url
    if _is_url_image(url) and not is_imgbb:
        print(f"[Agent] Image URL valid, using existing: {url[:60]}...")
        return url
    if is_imgbb:
        print("[Agent] imgbb URL detected — re-uploading to catbox.moe for Instagram...")
        from image_composer import upload_composited as _rehost
        tagline = draft.get("image_tagline") or draft.get("theme", "")[:60]
        return _rehost(url, tagline=tagline)

    # URL expired — re-generate image and re-composite branding
    print("[Agent] Image URL expired — re-generating...")
    image_style_name = draft.get("image_style")
    image_style = next(
        (s for s in IMAGE_VISUAL_STYLES if s["name"] == image_style_name), None
    )
    features_text = fetch_features()
    image_prompt, neg = generate_image_prompt(features_text, draft["theme"], image_style)
    new_url = generate_image(image_prompt, neg, state=state)
    draft["image_url"] = new_url
    draft["image_prompt"] = image_prompt
    tagline = draft.get("theme", "")[:60]
    return upload_composited(new_url, tagline=tagline)


# ── Instagram Publishing ──────────────────────────────────────────────────────

def post_to_instagram(caption: str, image_url: str) -> str:
    """Publish photo to Instagram. Returns the post ID."""
    import urllib.parse
    base = f"https://graph.facebook.com/v21.0/{INSTAGRAM_ACCOUNT_ID}"

    # Step 1: create media container
    container_params = urllib.parse.urlencode({
        "image_url":    image_url,
        "caption":      caption,
        "access_token": INSTAGRAM_ACCESS_TOKEN,
    })
    r = req.post(f"{base}/media?{container_params}", timeout=40)
    data = r.json()
    if "error" in data:
        raise RuntimeError(
            f"Container error: {data['error']['message']} (code {data['error'].get('code')})"
        )
    creation_id = data["id"]
    print(f"[Agent] Container created: {creation_id}")
    time.sleep(4)

    # Step 2: publish
    publish_params = urllib.parse.urlencode({
        "creation_id":  creation_id,
        "access_token": INSTAGRAM_ACCESS_TOKEN,
    })
    r2 = req.post(f"{base}/media_publish?{publish_params}", timeout=40)
    pub_data = r2.json()
    if "error" in pub_data:
        raise RuntimeError(
            f"Publish error: {pub_data['error']['message']} (code {pub_data['error'].get('code')})"
        )
    post_id = pub_data.get("id", "")
    print(f"[Agent] Published! Post ID: {post_id}")
    return post_id


# ── Draft Storage (Gist-backed) ───────────────────────────────────────────────

def save_draft(draft: dict, state: dict) -> None:
    # Always write local copy first (used as fallback and for --dry-run)
    with open(DRAFT_PATH, "w") as f:
        json.dump(draft, f, indent=2)
    # Try Gist storage (requires GH_PAT with gist scope)
    try:
        gist_id = state.get("current_gist_id")
        new_gist_id = save_draft_to_gist(draft, gist_id)
        state["current_gist_id"] = new_gist_id
    except Exception as e:
        print(f"[Agent] Gist save failed ({e}) — using local draft.json only.")
        state["current_gist_id"] = None


def load_draft(state: dict) -> Optional[dict]:
    gist_id = state.get("current_gist_id")
    if gist_id:
        try:
            return load_draft_from_gist(gist_id)
        except Exception as e:
            print(f"[Agent] Gist load failed ({e}), trying local fallback...")
    if os.path.exists(DRAFT_PATH):
        with open(DRAFT_PATH) as f:
            return json.load(f)
    return None


def clear_draft(state: dict) -> None:
    gist_id = state.get("current_gist_id")
    if gist_id:
        try:
            delete_draft_gist(gist_id)
        except Exception as e:
            print(f"[Agent] Gist delete failed: {e}")
    state["current_gist_id"] = None
    if os.path.exists(DRAFT_PATH):
        os.remove(DRAFT_PATH)


# ── Draft Generation ──────────────────────────────────────────────────────────

def generate_draft(
    state: dict,
    remarks: Optional[str] = None,
    forced_image_style: Optional[dict] = None,
    forced_caption_style: Optional[dict] = None,
    extra_context: Optional[str] = None,
) -> dict:
    """Generate a complete post draft with caption + image."""
    features_text = fetch_features()
    caption_style = forced_caption_style or pick_caption_style(state)
    image_style   = forced_image_style   or pick_image_style(state)

    if extra_context:
        # Refine the raw user note into a proper marketing angle, then use it
        # as the theme so caption AND image both target it.
        # Don't record it in used_themes — it's user-directed, not a rotation slot.
        theme = refine_context_to_theme(features_text, extra_context)
        print(f"[Agent] Context refined: '{extra_context}' → '{theme}'")
    else:
        theme = pick_theme(state)

    print(f"\n[Agent] Theme: {theme}")
    print(f"[Agent] Caption style: {caption_style['name']} | Image style: {image_style['name']}")

    # Generate caption with fact-checking (up to 3 attempts)
    caption = generate_caption(features_text, theme, remarks, caption_style)
    time.sleep(5)  # avoid burst rate limit between consecutive Gemini calls
    for attempt in range(3):
        is_valid, reason = fact_check_caption(features_text, caption)
        if is_valid:
            print(f"[Agent] Caption passed fact-check (attempt {attempt+1}).")
            break
        print(f"[Agent] Fact-check FAILED ({attempt+1}): {reason}")
        if attempt < 2:
            time.sleep(5)
            caption = generate_caption(
                features_text, theme,
                remarks=f"Previous caption failed fact-check: {reason}. Fix the issues.",
                caption_style=caption_style,
            )
    else:
        print("[Agent] WARNING: Caption still has issues after 3 attempts — proceeding.")

    # Generate image
    time.sleep(5)
    image_prompt, neg_prompt = generate_image_prompt(features_text, theme, image_style)
    image_tagline = generate_image_tagline(theme)
    print(f"[Agent] Image tagline: {image_tagline}")
    image_url = generate_image(image_prompt, neg_prompt, state=state)

    draft = {
        "draft_id":     _approval_mod.generate_draft_id(),
        "date":         datetime.now().strftime("%Y-%m-%d"),
        "theme":        theme,
        "image_tagline": image_tagline,
        "caption_style": caption_style["name"],
        "image_style":  image_style["name"],
        "caption":      caption,
        "image_url":    image_url,
        "image_prompt": image_prompt,
        "status":       "pending",
        "attempt":      1,
    }
    print(f"\n[Agent] Caption:\n{caption}\n")
    print(f"[Agent] Image prompt:\n{image_prompt}\n")
    return draft


# ── Main Workflows ────────────────────────────────────────────────────────────

def _notify_bot_server(draft: dict, gist_id: str) -> bool:
    """POST draft to bot server /register_draft so it sends the Telegram approval message."""
    bot_url = os.environ.get("BOT_SERVER_URL", "").rstrip("/")
    secret  = os.environ.get("BOT_API_SECRET", "")
    if not bot_url:
        return False
    try:
        r = req.post(
            f"{bot_url}/register_draft",
            json={
                "draft_id":     draft["draft_id"],
                "gist_id":      gist_id,
                "image_url":    draft["image_url"],
                "caption":      draft["caption"],
                "theme":        draft.get("theme", ""),
                "image_style":  draft.get("image_style", ""),
                "caption_style": draft.get("caption_style", ""),
            },
            headers={"X-Bot-Secret": secret},
            timeout=20,
        )
        print(f"[Agent] Bot server notified: {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"[Agent] Bot server notify failed: {e}")
        return False


def _notify_bot_posted(message: str) -> None:
    """POST to bot server /notify after Instagram post goes live."""
    bot_url = os.environ.get("BOT_SERVER_URL", "").rstrip("/")
    secret  = os.environ.get("BOT_API_SECRET", "")
    if not bot_url:
        tg_notify(message)
        return
    try:
        req.post(
            f"{bot_url}/notify",
            json={"message": message},
            headers={"X-Bot-Secret": secret},
            timeout=15,
        )
    except Exception as e:
        print(f"[Agent] Bot server /notify failed: {e}")
        tg_notify(message)


def run_generate_with_state(
    state: dict,
    forced_image_style: Optional[dict] = None,
    forced_caption_style: Optional[dict] = None,
    extra_context: Optional[str] = None,
    remarks: Optional[str] = None,
) -> None:
    """Generate a draft → register with bot server (or fall back to direct Telegram)."""
    draft = generate_draft(
        state,
        remarks=remarks,
        forced_image_style=forced_image_style,
        forced_caption_style=forced_caption_style,
        extra_context=extra_context,
    )
    save_draft(draft, state)
    _save_state(state)

    tg_image_url = upload_composited(draft["image_url"], tagline=draft.get("image_tagline", ""))
    draft["image_url"] = tg_image_url
    save_draft(draft, state)

    gist_id = state.get("current_gist_id", "") or ""

    # Primary: bot server (instant Telegram via webhook)
    if not _notify_bot_server(draft, gist_id):
        # Fallback: direct Telegram send (no bot server configured)
        tg_send(draft, tg_image_url)

    send_draft_email(draft)
    print("[Agent] Draft generated and sent for approval.")
    _save_state(state)


def run_generate() -> None:
    """Generate a draft — reads override env vars set by workflow dispatch inputs."""
    state = _load_state()

    img_override = os.environ.get("IMAGE_STYLE_OVERRIDE", "").strip()
    cap_override = os.environ.get("CAPTION_STYLE_OVERRIDE", "").strip()
    ctx_override = os.environ.get("CONTEXT_OVERRIDE", "").strip() or None
    remarks_override = os.environ.get("REMARKS_OVERRIDE", "").strip() or None
    gist_override = os.environ.get("GIST_ID_OVERRIDE", "").strip() or None

    if gist_override:
        state["current_gist_id"] = gist_override

    forced_img = (
        next((s for s in IMAGE_VISUAL_STYLES if s["name"] == img_override), None)
        if img_override and img_override != "auto" else None
    )
    forced_cap = (
        next((s for s in CAPTION_STYLES if s["name"] == cap_override), None)
        if cap_override and cap_override != "auto" else None
    )

    run_generate_with_state(
        state,
        forced_image_style=forced_img,
        forced_caption_style=forced_cap,
        extra_context=ctx_override,
        remarks=remarks_override,
    )


def run_post_gist() -> None:
    """Load draft from Gist (GIST_ID_OVERRIDE env var) and post to Instagram."""
    gist_id = os.environ.get("GIST_ID_OVERRIDE", "").strip()
    if not gist_id:
        print("[Post] GIST_ID_OVERRIDE not set — nothing to post.")
        return

    print(f"[Post] Loading draft from Gist {gist_id}...")
    state = _load_state()
    state["current_gist_id"] = gist_id

    draft = load_draft(state)
    if not draft:
        print("[Post] Could not load draft from Gist.")
        _notify_bot_posted("❌ Post failed: could not load draft from Gist.")
        return

    print(f"[Post] Posting draft {draft['draft_id']} to Instagram...")
    try:
        image_url = _ensure_image_url(draft, state)
        post_id   = post_to_instagram(draft["caption"], image_url)
        state.setdefault("posted_drafts", []).append({
            "draft_id":      draft["draft_id"],
            "post_id":       post_id,
            "theme":         draft["theme"],
            "caption_style": draft.get("caption_style"),
            "image_style":   draft.get("image_style"),
            "date":          datetime.now().strftime("%Y-%m-%d"),
        })
        state["posted_drafts"] = state["posted_drafts"][-20:]
        clear_draft(state)
        _save_state(state)
        _notify_bot_posted("🎉 <b>Post is live on Instagram!</b>")
        print("[Post] Done!")
    except Exception as e:
        _notify_bot_posted(f"❌ Instagram post failed: {e}")
        raise




def run_metrics() -> None:
    """Fetch Instagram insights for recently posted drafts. Updates engagement_scores in state."""
    state = _load_state()
    posted = state.get("posted_drafts", [])
    if not posted:
        print("[Metrics] No posted drafts tracked yet.")
        return

    base = f"https://graph.facebook.com/v21.0"
    token = INSTAGRAM_ACCESS_TOKEN
    updated = 0

    for entry in posted:
        post_id = entry.get("post_id")
        theme = entry.get("theme")
        if not post_id or not theme:
            continue
        if entry.get("metrics_fetched"):
            continue  # Already processed

        try:
            r = req.get(
                f"{base}/{post_id}/insights",
                params={
                    "metric": "impressions,reach,likes_count,comments_count",
                    "access_token": token,
                },
                timeout=20,
            )
            data = r.json()
            if "error" in data:
                print(f"[Metrics] Insights error for {post_id}: {data['error']}")
                continue

            metrics = {m["name"]: m["values"][0]["value"] for m in data.get("data", [])}
            likes    = metrics.get("likes_count", 0)
            comments = metrics.get("comments_count", 0)
            reach    = metrics.get("reach", 1) or 1
            # Engagement rate: (likes + comments*2) / reach * 100
            score = round((likes + comments * 2) / reach * 100, 4)

            entry["metrics"] = metrics
            entry["engagement_score"] = score
            entry["metrics_fetched"] = True

            # Update running average for this theme
            scores = state.setdefault("engagement_scores", {})
            prev = scores.get(theme, 0)
            n = sum(1 for e in posted if e.get("theme") == theme and e.get("metrics_fetched"))
            scores[theme] = round((prev * (n - 1) + score) / n, 4)

            print(f"[Metrics] {post_id} ({theme[:40]}): score={score}")
            updated += 1
        except Exception as e:
            print(f"[Metrics] Failed for {post_id}: {e}")

    _save_state(state)
    print(f"[Metrics] Done. Updated {updated} posts.")
    if state.get("engagement_scores"):
        print("[Metrics] Theme scores:", json.dumps(state["engagement_scores"], indent=2))


def run_dry_run() -> None:
    """Generate and print only — no notifications, no post."""
    state = _load_state()
    draft = generate_draft(state)
    print("\n" + "=" * 60)
    print("DRY RUN — would send this for approval:")
    print("=" * 60)
    print(f"Theme: {draft['theme']}")
    print(f"Caption:\n{draft['caption']}")
    print(f"\nImage URL: {draft['image_url']}")
    print("=" * 60)


def run_post_now() -> None:
    """Generate a new draft and post it immediately — no approval step."""
    print("[Agent] Generating and posting immediately (no approval)...")
    state = _load_state()
    draft = generate_draft(state)
    save_draft(draft, state)
    image_url = _ensure_image_url(draft, state)
    post_id = post_to_instagram(draft["caption"], image_url)
    state.setdefault("posted_drafts", []).append({
        "draft_id": draft["draft_id"], "post_id": post_id,
        "theme": draft["theme"], "date": datetime.now().strftime("%Y-%m-%d"),
    })
    clear_draft(state)
    _save_state(state)
    tg_notify(f"🚀 *Post-now complete!* Post ID: `{post_id}`")
    print("[Agent] Done! Post is live.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--generate"
    if mode == "--generate":
        run_generate()
    elif mode == "--dry-run":
        run_dry_run()
    elif mode == "--post-now":
        run_post_now()
    elif mode == "--metrics":
        run_metrics()
    elif mode == "--post-gist":
        run_post_gist()
    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python agent.py [--generate|--check|--dry-run|--post-now|--post-gist|--metrics]")
        sys.exit(1)


if __name__ == "__main__":
    main()
