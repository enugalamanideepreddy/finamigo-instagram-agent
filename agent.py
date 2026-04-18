"""
FinAmigo Instagram Agent
Generates accurate, feature-grounded Instagram posts for @finamigox.

Modes:
  --generate   Generate draft → email to reviewer with Google Form link
  --check      Check Google Sheet for approval → post or regenerate
  --dry-run    Generate and print only (no email, no post)
  --post-now   Skip approval, generate and post immediately (use with caution)
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime

from dotenv import load_dotenv
load_dotenv(override=True)

from approval import (
    check_form_response,
    generate_draft_id,
    send_draft_email,
)
from features_loader import fetch_features

# ── Config ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY         = os.environ["GEMINI_API_KEY"]
REPLICATE_API_TOKEN    = os.environ["REPLICATE_API_TOKEN"]
INSTAGRAM_ACCESS_TOKEN = os.environ["INSTAGRAM_ACCESS_TOKEN"]
INSTAGRAM_ACCOUNT_ID   = os.environ["INSTAGRAM_ACCOUNT_ID"]

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

DRAFT_PATH = os.path.join(os.path.dirname(__file__), "draft.json")


# ── HTTP Helper (curl-based, avoids Python 3.9 SSL issues) ──────────────────

def _curl_post(url: str, payload: dict, headers: dict = None, timeout: int = 30) -> dict:
    """POST JSON via curl and return parsed response."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        tmpfile = f.name
    try:
        cmd = ["curl", "-s", "-m", str(timeout), "-X", "POST", url,
               "-H", "Content-Type: application/json", "-d", f"@{tmpfile}"]
        if headers:
            for k, v in headers.items():
                cmd += ["-H", f"{k}: {v}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        if result.returncode != 0:
            raise RuntimeError(f"curl failed ({result.returncode}): {result.stderr}")
        return json.loads(result.stdout)
    finally:
        os.unlink(tmpfile)


def _curl_get(url: str, headers: dict = None, timeout: int = 15) -> dict:
    """GET via curl and return parsed response."""
    cmd = ["curl", "-s", "-m", str(timeout), url]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed ({result.returncode}): {result.stderr}")
    return json.loads(result.stdout)


# ── Gemini Helper ────────────────────────────────────────────────────────────

def gemini_generate(system_prompt: str, user_msg: str, max_tokens: int = 600) -> str:
    """Call Gemini 2.0 Flash and return the text response. Retries up to 3x on 429."""
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_msg}]}],
        "generationConfig": {
            "temperature": 0.9,
            "maxOutputTokens": max_tokens,
        },
    }
    for attempt in range(3):
        data = _curl_post(GEMINI_URL, payload, timeout=30)
        if "error" in data:
            code = data["error"].get("code") or data["error"].get("status", "")
            if code == 429 or "RESOURCE_EXHAUSTED" in str(code):
                wait = 30 * (attempt + 1)
                print(f"[Gemini] Rate limited (429). Waiting {wait}s before retry {attempt + 1}/3...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Gemini error: {data['error']}")
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    raise RuntimeError("Gemini rate limit not resolved after 3 retries.")


# ── Features-Grounded System Prompt ──────────────────────────────────────────

def build_system_prompt(features_text: str) -> str:
    return f"""You are a world-class App Marketing specialist for FinAmigo — an offline-first personal finance app for Indian users. Your goal is to generate high-conversion, trust-building Instagram content that drives app downloads.

WRITING STYLE:
- Write like a premium global brand (Apple, Stripe, CRED).
- SHORT and PUNCHY. Max 40–60 words in the main body.
- Lead with a bold value proposition hook (Stop the scroll).
- Focus on the USER BENEFIT (Peace of mind, organized life, financial clarity).
- NEVER mention technical jargon (parsers, thresholds, CV, algorithms).
- Use 1–2 emojis max for professional emphasis.
- End with a clear marketing CTA (Link in bio).
- Use 6–8 targeted hashtags for growth.

ACCURACY RULES:
1. Only make claims supported by the Feature Reference below.
2. If something is under "What FinAmigo Does NOT Do", NEVER claim it.
3. Never claim real-time monitoring, live alerts, or bank API connections.
4. Don't invent features. Keep it real.

=== FEATURE REFERENCE (source of truth) ===
{features_text}
=== END FEATURE REFERENCE ==="""


# ── Theme Selection ──────────────────────────────────────────────────────────

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

# Caption styles — determines the structural format / hook type for each post
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

# Image composition styles — each defines a completely different visual layout/structure.
# These go far beyond just changing colors; the composition, framing, and render style all differ.
IMAGE_VISUAL_STYLES = [
    {
        "name": "phone_hero_dark",
        "template": (
            "Professional 3D app marketing render. A single premium smartphone centered and floating "
            "at a slight 15-degree angle against a deep charcoal to midnight black gradient. "
            "The phone screen glows with a clean fintech app interface showing colorful {screen_detail}. "
            "Translucent glassmorphism stat cards float beside the phone. "
            "Electric blue and violet neon rim lighting, subtle bokeh particles. "
            "Ultra clean premium tech product photography."
        ),
    },
    {
        "name": "dual_screen_split",
        "template": (
            "Premium app marketing visual. Two smartphones side by side, slightly angled toward each other, "
            "against a soft white to pale sky blue gradient. "
            "Left screen shows {screen_detail}, right screen shows a summary dashboard. "
            "Clean drop shadows, teal and coral accent highlights. "
            "Flat-minimal product photography style, crisp and editorial."
        ),
    },
    {
        "name": "ui_cards_floating",
        "template": (
            "Abstract fintech app marketing artwork. NO phone frame — only floating UI cards "
            "arranged in a dynamic staggered layout against a warm sunset gradient from deep orange to magenta. "
            "Cards display {screen_detail}. Each card has glassmorphism effect with frosted transparency. "
            "Golden yellow highlights, soft ambient glow, micro-shadow depth. "
            "Modern editorial design style, ultra-clean."
        ),
    },
    {
        "name": "isometric_3d",
        "template": (
            "Isometric 3D app marketing illustration. A premium smartphone rendered from a 45-degree top-down "
            "isometric angle against a deep forest green to emerald gradient. "
            "The screen displays {screen_detail} in crisp detail. "
            "Flat isometric style with subtle 3D extrusion depth. Lime green and white accents. "
            "Clean vector-style product render, no photo-realism."
        ),
    },
    {
        "name": "ui_closeup_immersive",
        "template": (
            "Immersive app UI close-up render. The entire frame is filled with a zoomed-in view of "
            "a fintech app screen showing {screen_detail}, with a dark navy to electric blue vignette background bleeding from the edges. "
            "No phone frame visible — just the glowing app interface. "
            "Floating micro data chips and sparkline graphs around the edges. "
            "Cinematic depth-of-field, premium digital art style."
        ),
    },
    {
        "name": "minimal_light_flat",
        "template": (
            "Minimalist flat app marketing render. A smartphone lying at a slight angle on a pure white "
            "to soft lavender gradient background. "
            "The screen shows {screen_detail}. "
            "Very clean, lots of breathing room, thin geometric line accents in teal. "
            "No lens flare, no bokeh — editorial Swiss design aesthetic."
        ),
    },
    {
        "name": "neon_cyberpunk",
        "template": (
            "High-energy fintech app marketing visual. A smartphone floating dramatically against a deep "
            "midnight purple to hot pink gradient. The screen blazes with {screen_detail}. "
            "Intense neon pink and cyan glow halos, light streaks, lens flares. "
            "Futuristic cyberpunk aesthetic with glassmorphism cards. "
            "Bold, high-contrast, Instagram-stopping visual."
        ),
    },
    {
        "name": "perspective_tilt",
        "template": (
            "Dynamic perspective app marketing render. A premium smartphone shot from a dramatic low-angle "
            "perspective, tilted at 30 degrees, against a rich deep teal to dark navy gradient. "
            "The screen shows {screen_detail} in vivid color. "
            "Long dramatic shadow cast behind the phone. Cinematic lighting from above. "
            "Silver chrome bezel detail, studio product photography style."
        ),
    },
]


_STATE_PATH = os.path.join(os.path.dirname(__file__), "agent_state.json")

def _load_state() -> dict:
    if os.path.exists(_STATE_PATH):
        with open(_STATE_PATH) as f:
            return json.load(f)
    return {"used_themes": [], "used_images": [], "used_caption_styles": [], "used_image_styles": []}

def _save_state(state: dict) -> None:
    with open(_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _pick_random_unused(pool: list, used: list, key: str = None) -> any:
    """Pick a random item from pool that hasn't been used yet.
    Once all items are used, resets and starts a fresh random cycle.
    key: if set, compare used list against item[key] instead of the item itself.
    """
    def item_id(x):
        return x[key] if key else x

    remaining = [x for x in pool if item_id(x) not in used]
    if not remaining:
        used.clear()
        remaining = list(pool)
    choice = random.choice(remaining)
    used.append(item_id(choice))
    return choice


def pick_theme() -> str:
    """Pick a random unused theme. Resets once all 12 are exhausted."""
    state = _load_state()
    used = state.get("used_themes", [])
    theme = _pick_random_unused(THEME_POOL, used)
    state["used_themes"] = used
    _save_state(state)
    return theme


def pick_caption_style() -> dict:
    """Pick a random unused caption style. Resets once all 5 are exhausted."""
    state = _load_state()
    used = state.get("used_caption_styles", [])
    style = _pick_random_unused(CAPTION_STYLES, used, key="name")
    state["used_caption_styles"] = used
    _save_state(state)
    return style


def pick_image_style() -> dict:
    """Pick a random unused image composition style. Resets once all 8 are exhausted."""
    state = _load_state()
    used = state.get("used_image_styles", [])
    style = _pick_random_unused(IMAGE_VISUAL_STYLES, used, key="name")
    state["used_image_styles"] = used
    _save_state(state)
    return style


# ── Content Generation ───────────────────────────────────────────────────────

from typing import Union, Optional, List, Dict

# ... in agent.py
def generate_caption(
    features_text: str,
    theme: str,
    remarks: Optional[str] = None,
    caption_style: Optional[dict] = None,
) -> str:
    """Use Gemini to write an Instagram caption grounded in FEATURES.md."""
    style_instruction = (
        caption_style["instruction"]
        if caption_style
        else "Open with a bold hook that stops the scroll. 2–3 benefit lines. End with CTA."
    )
    user_msg = (
        f"Write an Instagram caption for FinAmigo.\n\n"
        f"Today's theme/angle: {theme}\n\n"
        f"POST FORMAT — follow this structure exactly:\n"
        f"{style_instruction}\n\n"
        f"Rules:\n"
        f"- MAX 40–60 words in the body. Short. Punchy. No fluff.\n"
        f"- Talk about BENEFITS, not technical details\n"
        f"- NO engineering jargon (no parsers, algorithms, thresholds, CVs, rules count)\n"
        f"- Write like CRED or Apple — minimal, impactful\n"
        f"- 1–2 emojis max\n"
        f"- End with a short CTA (link in bio)\n"
        f"- Last line: 6–8 hashtags only\n"
        f"- Indian financial context (₹, UPI, EMI, salary)"
    )
    if remarks:
        user_msg += f"\n\nREVISION NOTES (from reviewer): {remarks}"

    return gemini_generate(build_system_prompt(features_text), user_msg, max_tokens=600)


def fact_check_caption(features_text: str, caption: str) -> tuple[bool, str]:
    """Use Gemini to validate the caption against FEATURES.md. Returns (is_valid, reason)."""
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


def generate_image_prompt(features_text: str, theme: str, image_style: Optional[dict] = None) -> str:
    """Build a composition-specific image prompt. Gemini picks UI screen details; style picks the layout."""
    system = (
        "You write ONE short sentence (max 15 words) describing what a finance app screen shows. "
        "Examples: 'score dial at 720 with expense pie chart' or "
        "'salary notification card with income bar chart'. "
        "ONLY describe app UI elements. No background, no phone, no scenery, no text."
    )
    user_msg = f"Theme: {theme}\nDescribe the app screen UI in one short sentence."
    screen_detail = gemini_generate(system, user_msg, max_tokens=60)

    # Fall back to phone_hero_dark if no style passed
    style = image_style or IMAGE_VISUAL_STYLES[0]
    prompt = style["template"].format(screen_detail=screen_detail)

    negative = (
        "text, words, letters, numbers, labels, typography, writing, watermark, signature, "
        "nature, landscape, trees, mountains, animals, people, faces, hands, fingers, "
        "cartoon, sketch, drawing, illustration, low quality, blurry"
    )
    return prompt, negative


# ── Static Image Pool (fallback / override) ─────────────────────────────────

STATIC_IMAGES_PATH = os.path.join(os.path.dirname(__file__), "images.json")

def load_static_images() -> list:
    """Load curated image URLs from images.json if it exists."""
    if os.path.exists(STATIC_IMAGES_PATH):
        with open(STATIC_IMAGES_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else data.get("urls", [])
    return []


def pick_static_image() -> str:
    """Pick a random unused static image, cycling through the full pool before repeating."""
    images = load_static_images()
    if not images:
        return ""
    state = _load_state()
    used = state.get("used_images", [])
    img = _pick_random_unused(images, used)
    state["used_images"] = used
    _save_state(state)
    return img


# ── Image Generation (Ideogram v2 Turbo via Replicate) ──────────────────────

def generate_image(prompt: str, negative_prompt: str = "") -> str:
    """Generate a 1:1 image with Ideogram v2 Turbo via Replicate HTTP API."""
    # Check for static images first
    static_url = pick_static_image()
    if static_url:
        print(f"[Agent] Using static image: {static_url[:60]}...")
        return static_url

    print("[Agent] Generating image with Ideogram v2 Turbo...")
    rep_headers = {"Authorization": f"Bearer {REPLICATE_API_TOKEN}"}
    data = _curl_post(
        "https://api.replicate.com/v1/models/ideogram-ai/ideogram-v2-turbo/predictions",
        {"input": {
            "prompt":          prompt,
            "negative_prompt": negative_prompt,
            "aspect_ratio":    "1:1",
            "style_type":      "Render 3D",
        }},
        headers=rep_headers,
        timeout=30,
    )
    if data.get("error"):
        raise RuntimeError(f"Ideogram error: {data['error']}")
    poll_url = data["urls"]["get"]

    for _ in range(60):
        time.sleep(3)
        poll = _curl_get(poll_url, headers=rep_headers, timeout=15)
        if poll["status"] == "succeeded":
            output = poll.get("output")
            if not output:
                raise RuntimeError("Ideogram succeeded but returned no output URL.")
            url = output[0] if isinstance(output, list) else output
            if not url:
                raise RuntimeError("Ideogram succeeded but output URL is empty.")
            print(f"[Agent] Image ready: {url[:60]}...")
            return url
        if poll["status"] in ("failed", "canceled"):
            raise RuntimeError(f"Ideogram failed: {poll.get('error')}")

    raise TimeoutError("Ideogram did not complete in time.")


# ── Instagram Publishing ─────────────────────────────────────────────────────

def _is_url_accessible(url: str) -> bool:
    """Verify a URL serves actual image content (not an expired/JSON error response).

    Uses a unique separator (--CTEND--) between body and content-type so that
    newlines inside the image binary don't corrupt the parsing.
    """
    separator = b"--CTEND--"
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "-m", "10", "-w", "--CTEND--%{content_type}",
             "--range", "0-15", url],
            capture_output=True, timeout=15,
        )
        if separator not in result.stdout:
            return False
        body, ct_bytes = result.stdout.split(separator, 1)
        content_type = ct_bytes.decode("utf-8", errors="ignore").strip()
        first_bytes = body[:4]
        # PNG magic: \x89PNG  |  JPEG magic: \xff\xd8\xff
        is_image_bytes = first_bytes in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1") \
                         or first_bytes[:3] == b"\xff\xd8\xff"
        is_image_ct = content_type.startswith("image/")
        return is_image_bytes or is_image_ct
    except Exception:
        return False


def post_to_instagram(caption: str, image_url: str) -> dict:
    """Publish a photo to Instagram via the Graph API."""
    import urllib.parse
    base = "https://graph.facebook.com/v21.0"

    # Step 1: create media container.
    # Pass caption via URL-encoded query param to safely handle newlines and emoji.
    container_params = urllib.parse.urlencode({
        "image_url": image_url,
        "caption": caption,
        "access_token": INSTAGRAM_ACCESS_TOKEN,
    })
    container_url = f"{base}/{INSTAGRAM_ACCOUNT_ID}/media?{container_params}"
    result = subprocess.run(
        ["curl", "-s", "-m", "30", "-X", "POST", container_url],
        capture_output=True, text=True, timeout=40,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")
    data = json.loads(result.stdout)
    if "error" in data:
        raise RuntimeError(f"Container error: {data['error']['message']} (code {data['error'].get('code')})")
    creation_id = data["id"]
    print(f"[Agent] Container created: {creation_id}")

    # Small delay before publishing (Meta recommends waiting after container creation)
    time.sleep(3)

    # Step 2: publish
    publish_params = urllib.parse.urlencode({
        "creation_id": creation_id,
        "access_token": INSTAGRAM_ACCESS_TOKEN,
    })
    publish_url = f"{base}/{INSTAGRAM_ACCOUNT_ID}/media_publish?{publish_params}"
    result2 = subprocess.run(
        ["curl", "-s", "-m", "30", "-X", "POST", publish_url],
        capture_output=True, text=True, timeout=40,
    )
    if result2.returncode != 0:
        raise RuntimeError(f"curl failed: {result2.stderr}")
    pub_data = json.loads(result2.stdout)
    if "error" in pub_data:
        raise RuntimeError(f"Publish error: {pub_data['error']['message']} (code {pub_data['error'].get('code')})")
    print(f"[Agent] Published! Post ID: {pub_data.get('id')}")
    return pub_data


# ── Draft Storage ────────────────────────────────────────────────────────────

def save_draft(draft: dict) -> None:
    with open(DRAFT_PATH, "w") as f:
        json.dump(draft, f, indent=2)
    print(f"[Agent] Draft saved: {DRAFT_PATH}")


def load_draft() -> Optional[dict]:
    if not os.path.exists(DRAFT_PATH):
        return None
    with open(DRAFT_PATH) as f:
        return json.load(f)


def clear_draft() -> None:
    if os.path.exists(DRAFT_PATH):
        os.remove(DRAFT_PATH)
        print("[Agent] Draft cleared.")


# ── Main Workflows ───────────────────────────────────────────────────────────

def generate_draft(remarks: Optional[str] = None) -> dict:
    """Generate a complete post draft (caption + image) with style variation."""
    features_text = fetch_features()
    theme = pick_theme()
    caption_style = pick_caption_style()
    image_style = pick_image_style()
    print(f"\n[Agent] Theme: {theme}")
    print(f"[Agent] Caption style: {caption_style['name']}")
    print(f"[Agent] Image style: {image_style['name']}")

    # Generate caption with fact-checking (up to 3 attempts total)
    caption = generate_caption(features_text, theme, remarks, caption_style)
    caption_ok = False
    for attempt in range(3):
        is_valid, reason = fact_check_caption(features_text, caption)
        if is_valid:
            print(f"[Agent] Caption passed fact-check (attempt {attempt + 1}).")
            caption_ok = True
            break
        print(f"[Agent] Fact-check FAILED (attempt {attempt + 1}): {reason}")
        if attempt < 2:
            caption = generate_caption(
                features_text, theme,
                remarks=f"Previous caption failed fact-check: {reason}. Fix the issues.",
                caption_style=caption_style,
            )
    if not caption_ok:
        print("[Agent] WARNING: Caption still has issues after 3 attempts — proceeding anyway.")

    # Generate image
    image_prompt, negative_prompt = generate_image_prompt(features_text, theme, image_style)
    image_url = generate_image(image_prompt, negative_prompt)

    draft = {
        "draft_id": generate_draft_id(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "theme": theme,
        "caption_style": caption_style["name"],
        "image_style": image_style["name"],
        "caption": caption,
        "image_url": image_url,
        "image_prompt": image_prompt,
        "status": "pending",
        "attempt": 1,
    }

    print(f"\n[Agent] Caption:\n{caption}\n")
    print(f"[Agent] Image prompt:\n{image_prompt}\n")
    return draft


def run_generate() -> None:
    """Generate a draft and email it for approval."""
    draft = generate_draft()
    save_draft(draft)
    send_draft_email(draft)
    print("[Agent] Draft generated and sent for approval.")


def _ensure_image_url(draft: dict) -> str:
    """Return the image URL from the draft, re-generating if it has expired."""
    url = draft["image_url"]
    if _is_url_accessible(url):
        return url
    print(f"[Agent] Image URL expired or unreachable. Re-generating image...")
    features_text = fetch_features()
    theme = draft["theme"]
    image_style_name = draft.get("image_style")
    image_style = next((s for s in IMAGE_VISUAL_STYLES if s["name"] == image_style_name), None)
    image_prompt, negative_prompt = generate_image_prompt(features_text, theme, image_style)
    new_url = generate_image(image_prompt, negative_prompt)
    draft["image_url"] = new_url
    draft["image_prompt"] = image_prompt
    save_draft(draft)
    return new_url


def run_check() -> None:
    """Check for approval response and act accordingly."""
    draft = load_draft()
    if not draft:
        print("[Agent] No pending draft. Nothing to check.")
        return

    status, remarks = check_form_response(draft["draft_id"])

    if status == "approved":
        print("[Agent] Posting approved draft to Instagram...")
        image_url = _ensure_image_url(draft)
        post_to_instagram(draft["caption"], image_url)
        clear_draft()
        print("[Agent] Done! Post is live.")

    elif status == "remarks":
        print(f"[Agent] Revision requested: {remarks}")
        features_text = fetch_features()
        theme = draft["theme"]

        # Preserve the same styles from the original draft
        caption_style_name = draft.get("caption_style")
        caption_style = next((s for s in CAPTION_STYLES if s["name"] == caption_style_name), None)
        image_style_name = draft.get("image_style")
        image_style = next((s for s in IMAGE_VISUAL_STYLES if s["name"] == image_style_name), None)

        # Regenerate caption with remarks + fact-check
        caption = generate_caption(features_text, theme, remarks, caption_style)
        is_valid, reason = fact_check_caption(features_text, caption)
        if not is_valid:
            print(f"[Agent] Fact-check issue: {reason} — regenerating...")
            caption = generate_caption(
                features_text, theme,
                remarks=f"{remarks}\n\nAlso fix: {reason}",
                caption_style=caption_style,
            )

        # Generate new image preserving the same composition style
        image_prompt, negative_prompt = generate_image_prompt(features_text, theme, image_style)
        image_url = generate_image(image_prompt, negative_prompt)

        new_draft = {
            "draft_id": generate_draft_id(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "theme": theme,
            "caption_style": caption_style_name,
            "image_style": image_style_name,
            "caption": caption,
            "image_url": image_url,
            "image_prompt": image_prompt,
            "status": "pending",
            "attempt": draft.get("attempt", 1) + 1,
        }
        save_draft(new_draft)
        send_draft_email(new_draft)
        print(f"[Agent] Revision #{new_draft['attempt']} sent for approval.")

    else:
        print("[Agent] No response yet. Will check again later.")


def run_dry_run() -> None:
    """Generate and print only — no email, no post."""
    draft = generate_draft()
    print("\n" + "=" * 60)
    print("DRY RUN — would send this for approval:")
    print("=" * 60)
    print(f"Theme: {draft['theme']}")
    print(f"Caption:\n{draft['caption']}")
    print(f"\nImage URL: {draft['image_url']}")
    print("=" * 60)


def run_approve_local() -> None:
    """Post the currently saved draft immediately."""
    draft = load_draft()
    if not draft:
        print("[Agent] No pending draft found in draft.json.")
        return
    print(f"[Agent] Manually approving and posting Draft {draft['draft_id']}...")
    image_url = _ensure_image_url(draft)
    post_to_instagram(draft["caption"], image_url)
    clear_draft()
    print("[Agent] Done! Published.")


def run_post_now() -> None:
    """Generate a new draft and post it immediately — no approval step."""
    print("[Agent] Generating and posting immediately (no approval)...")
    draft = generate_draft()
    save_draft(draft)
    image_url = _ensure_image_url(draft)
    post_to_instagram(draft["caption"], image_url)
    clear_draft()
    print("[Agent] Done! Post is live.")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    mode = args[0] if args else "--generate"

    if mode == "--generate":
        run_generate()
    elif mode == "--check":
        run_check()
    elif mode == "--dry-run":
        run_dry_run()
    elif mode == "--post-now":
        run_post_now()
    elif mode == "--approve-local":
        run_approve_local()
    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python agent.py [--generate|--check|--dry-run|--post-now]")
        sys.exit(1)


if __name__ == "__main__":
    main()
