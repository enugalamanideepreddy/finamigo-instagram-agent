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
    """Call Gemini 2.0 Flash and return the text response."""
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_msg}]}],
        "generationConfig": {
            "temperature": 0.9,
            "maxOutputTokens": max_tokens,
        },
    }
    data = _curl_post(GEMINI_URL, payload, timeout=30)
    if "error" in data:
        raise RuntimeError(f"Gemini error: {data['error']}")
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


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


_STATE_PATH = os.path.join(os.path.dirname(__file__), "agent_state.json")

def _load_state() -> dict:
    if os.path.exists(_STATE_PATH):
        with open(_STATE_PATH) as f:
            return json.load(f)
    return {"used_themes": [], "used_images": []}

def _save_state(state: dict) -> None:
    with open(_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def pick_theme() -> str:
    """Pick the next unused theme, cycling through the full pool before repeating."""
    state = _load_state()
    used = state.get("used_themes", [])
    remaining = [t for t in THEME_POOL if t not in used]
    if not remaining:
        # Full cycle complete — reset and start again
        used = []
        remaining = list(THEME_POOL)
    theme = remaining[0]
    used.append(theme)
    state["used_themes"] = used
    _save_state(state)
    return theme


# ── Content Generation ───────────────────────────────────────────────────────

from typing import Union, Optional, List, Dict

# ... in agent.py
def generate_caption(features_text: str, theme: str, remarks: Optional[str] = None) -> str:
    """Use Gemini to write an Instagram caption grounded in FEATURES.md."""
    user_msg = (
        f"Write an Instagram caption for FinAmigo.\n\n"
        f"Today's theme/angle: {theme}\n\n"
        f"Rules:\n"
        f"- MAX 40–60 words in the body. Short. Punchy. No fluff.\n"
        f"- Bold first line that stops the scroll\n"
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


def generate_image_prompt(features_text: str, theme: str) -> str:
    """Build a strict app-marketing image prompt. Gemini only picks screen UI details."""
    system = (
        "You write ONE short sentence (max 15 words) describing what a finance app screen shows. "
        "Examples: 'score dial at 720 with expense pie chart' or "
        "'salary notification card with income bar chart'. "
        "ONLY describe app UI elements. No background, no phone, no scenery, no text."
    )
    user_msg = f"Theme: {theme}\nDescribe the app screen UI in one short sentence."
    screen_detail = gemini_generate(system, user_msg, max_tokens=60)

    prompt = (
        f"Professional 3D app marketing render. A premium smartphone floating at a slight angle "
        f"against a smooth gradient background from dark navy blue to bright sky blue. "
        f"The phone screen glows with a clean fintech app interface showing colorful {screen_detail}. "
        f"Translucent glassmorphism cards with pie charts and bar graphs float beside the phone. "
        f"Soft neon blue glow effects, bokeh light particles. "
        f"Ultra clean, minimal, premium tech product photography style."
    )

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
    """Pick the next unused static image, cycling through the full pool before repeating."""
    images = load_static_images()
    if not images:
        return ""
    state = _load_state()
    used = state.get("used_images", [])
    remaining = [img for img in images if img not in used]
    if not remaining:
        used = []
        remaining = list(images)
    img = remaining[0]
    used.append(img)
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
            output = poll["output"]
            url = output[0] if isinstance(output, list) else output
            print(f"[Agent] Image ready: {url[:60]}...")
            return url
        if poll["status"] in ("failed", "canceled"):
            raise RuntimeError(f"Ideogram failed: {poll.get('error')}")

    raise TimeoutError("Ideogram did not complete in time.")


# ── Instagram Publishing ─────────────────────────────────────────────────────

def post_to_instagram(caption: str, image_url: str) -> dict:
    """Publish a photo to Instagram via the Graph API."""
    import urllib.parse
    base = "https://graph.facebook.com/v21.0"

    # Step 1: create media container
    params = urllib.parse.urlencode({
        "image_url": image_url, "caption": caption, "access_token": INSTAGRAM_ACCESS_TOKEN
    })
    url = f"{base}/{INSTAGRAM_ACCOUNT_ID}/media?{params}"
    data = _curl_post(url, {}, timeout=30)
    if "error" in data:
        raise RuntimeError(f"Container error: {data['error']}")
    creation_id = data["id"]
    print(f"[Agent] Container created: {creation_id}")

    # Step 2: publish
    params2 = urllib.parse.urlencode({
        "creation_id": creation_id, "access_token": INSTAGRAM_ACCESS_TOKEN
    })
    url2 = f"{base}/{INSTAGRAM_ACCOUNT_ID}/media_publish?{params2}"
    result = _curl_post(url2, {}, timeout=30)
    if "error" in result:
        raise RuntimeError(f"Publish error: {result['error']}")
    print(f"[Agent] Published! Post ID: {result.get('id')}")
    return result


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
    """Generate a complete post draft (caption + image)."""
    features_text = fetch_features()
    theme = pick_theme()
    print(f"\n[Agent] Theme: {theme}")

    # Generate caption with fact-checking (up to 2 retries)
    caption = generate_caption(features_text, theme, remarks)
    for attempt in range(2):
        is_valid, reason = fact_check_caption(features_text, caption)
        if is_valid:
            print(f"[Agent] Caption passed fact-check.")
            break
        print(f"[Agent] Fact-check FAILED (attempt {attempt + 1}): {reason}")
        caption = generate_caption(
            features_text, theme,
            remarks=f"Previous caption failed fact-check: {reason}. Fix the issues.",
        )
    else:
        print("[Agent] WARNING: Caption may still have issues after retries.")

    # Generate image
    image_prompt, negative_prompt = generate_image_prompt(features_text, theme)
    image_url = generate_image(image_prompt, negative_prompt)

    draft = {
        "draft_id": generate_draft_id(),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "theme": theme,
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


def run_check() -> None:
    """Check for approval response and act accordingly."""
    draft = load_draft()
    if not draft:
        print("[Agent] No pending draft. Nothing to check.")
        return

    status, remarks = check_form_response(draft["draft_id"])

    if status == "approved":
        print("[Agent] Posting approved draft to Instagram...")
        post_to_instagram(draft["caption"], draft["image_url"])
        clear_draft()
        print("[Agent] Done! Post is live.")

    elif status == "remarks":
        print(f"[Agent] Revision requested: {remarks}")
        features_text = fetch_features()
        theme = draft["theme"]

        # Regenerate with remarks
        caption = generate_caption(features_text, theme, remarks)
        is_valid, reason = fact_check_caption(features_text, caption)
        if not is_valid:
            print(f"[Agent] Fact-check issue: {reason} — regenerating...")
            caption = generate_caption(
                features_text, theme,
                remarks=f"{remarks}\n\nAlso fix: {reason}",
            )

        # Generate new image too (theme may have shifted)
        image_prompt, negative_prompt = generate_image_prompt(features_text, theme)
        image_url = generate_image(image_prompt, negative_prompt)

        new_draft = {
            "draft_id": generate_draft_id(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "theme": theme,
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
    post_to_instagram(draft["caption"], draft["image_url"])
    clear_draft()
    print("[Agent] Done! Published.")


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
