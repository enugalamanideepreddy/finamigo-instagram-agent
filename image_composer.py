"""
Composites clean FinAmigo branding onto a generated image using Pillow.

Why: AI image models (including Ideogram) cannot reliably render legible text.
This module downloads the raw generated image and draws crisp vector-quality
text on top — brand name, tagline — then re-uploads to imgbb.
"""

import io
import os
import textwrap
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont

IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

# ── Font loading ───────────────────────────────────────────────────────────────
# Uses system fonts available on Ubuntu GitHub Actions runners.
# Priority: bold sans-serif → fallback to PIL default.

def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",  # macOS
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ── Core compositing ──────────────────────────────────────────────────────────

def _draw_text_with_shadow(
    draw: ImageDraw.ImageDraw,
    xy: tuple,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple,
    shadow_color: tuple = (0, 0, 0, 160),
    shadow_offset: int = 3,
):
    """Draw text with a drop shadow for legibility on any background."""
    x, y = xy
    # Shadow
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=shadow_color)
    # Main text
    draw.text((x, y), text, font=font, fill=fill)


def composite_branding(
    image_url: str,
    tagline: str = "",
    brand_name: str = "FinAmigo",
) -> bytes:
    """Download image, draw branding, return composited JPEG bytes."""
    # Download raw image
    r = requests.get(image_url, timeout=60)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGBA")
    w, h = img.size

    # Create overlay layer (transparent)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # ── Brand name: top-left ─────────────────────────────────────────────────
    brand_size = max(48, w // 14)
    brand_font = _get_font(brand_size, bold=True)
    pad = w // 28

    # Semi-transparent dark pill behind brand name
    bbox = draw.textbbox((pad, pad), brand_name, font=brand_font)
    pill_pad = 12
    draw.rounded_rectangle(
        (bbox[0] - pill_pad, bbox[1] - pill_pad // 2,
         bbox[2] + pill_pad, bbox[3] + pill_pad // 2),
        radius=8,
        fill=(0, 0, 0, 140),
    )
    # Teal brand text
    _draw_text_with_shadow(
        draw, (pad, pad), brand_name, brand_font,
        fill=(13, 148, 136, 255),  # #0d9488 teal
    )

    # ── Tagline: bottom-center ───────────────────────────────────────────────
    if tagline:
        tag_size = max(24, w // 30)
        tag_font = _get_font(tag_size, bold=False)

        # Wrap long taglines
        max_chars = max(20, w // (tag_size // 2))
        wrapped = "\n".join(textwrap.wrap(tagline, width=max_chars))

        tag_bbox = draw.textbbox((0, 0), wrapped, font=tag_font)
        tag_w = tag_bbox[2] - tag_bbox[0]
        tag_h = tag_bbox[3] - tag_bbox[1]
        tag_x = (w - tag_w) // 2
        tag_y = h - tag_h - pad * 2

        # Dark pill behind tagline
        pill_pad = 16
        draw.rounded_rectangle(
            (tag_x - pill_pad, tag_y - pill_pad // 2,
             tag_x + tag_w + pill_pad, tag_y + tag_h + pill_pad // 2),
            radius=8,
            fill=(0, 0, 0, 150),
        )
        _draw_text_with_shadow(
            draw, (tag_x, tag_y), wrapped, tag_font,
            fill=(255, 255, 255, 240),
        )

    # ── "Coming Soon" badge: bottom-right ───────────────────────────────────
    badge_text = "Coming Soon"
    badge_size = max(18, w // 40)
    badge_font = _get_font(badge_size, bold=True)
    badge_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    bw = badge_bbox[2] - badge_bbox[0]
    bh = badge_bbox[3] - badge_bbox[1]
    bx = w - bw - pad * 2
    by = h - bh - pad

    draw.rounded_rectangle(
        (bx - 10, by - 6, bx + bw + 10, by + bh + 6),
        radius=6,
        fill=(13, 148, 136, 200),
    )
    draw.text((bx, by), badge_text, font=badge_font, fill=(255, 255, 255, 255))

    # Composite overlay onto image
    composited = Image.alpha_composite(img, overlay).convert("RGB")

    # Return as JPEG bytes
    buf = io.BytesIO()
    composited.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _upload_catbox(jpg_bytes: bytes) -> str:
    """Upload to catbox.moe — free, no hotlink protection, Instagram-friendly."""
    # Must use files= for ALL fields in multipart (including text fields)
    r = requests.post(
        "https://catbox.moe/user/api.php",
        files={
            "reqtype":      (None, "fileupload"),
            "fileToUpload": ("finamigo_post.jpg", jpg_bytes, "image/jpeg"),
        },
        timeout=60,
    )
    url = r.text.strip()
    if not url.startswith("https://files.catbox.moe/"):
        raise RuntimeError(f"catbox.moe: {r.text[:100]}")
    return url


def _upload_litterbox(jpg_bytes: bytes) -> str:
    """Upload to litterbox.catbox.moe — 72h temp storage, same CDN, no hotlink block."""
    r = requests.post(
        "https://litterbox.catbox.moe/resources/internals/api.php",
        files={
            "reqtype":      (None, "fileupload"),
            "time":         (None, "72h"),
            "fileToUpload": ("finamigo_post.jpg", jpg_bytes, "image/jpeg"),
        },
        timeout=60,
    )
    url = r.text.strip()
    if not url.startswith("https://"):
        raise RuntimeError(f"litterbox: {r.text[:100]}")
    return url


def _upload_imgbb(jpg_bytes: bytes) -> str:
    """Upload to imgbb — last resort fallback."""
    if not IMGBB_API_KEY:
        raise RuntimeError("No IMGBB_API_KEY")
    r = requests.post(
        f"https://api.imgbb.com/1/upload?key={IMGBB_API_KEY}",
        files={"image": ("finamigo_post.jpg", jpg_bytes, "image/jpeg")},
        timeout=40,
    )
    data = r.json()
    if data.get("success"):
        return data["data"]["image"]["url"]
    raise RuntimeError(f"imgbb: {data}")


def upload_composited(image_url: str, tagline: str = "") -> str:
    """Composite FinAmigo branding and upload to a public host. Returns URL."""
    print("[Composer] Compositing FinAmigo branding onto image...")
    try:
        jpg_bytes = composite_branding(image_url, tagline=tagline)
    except Exception as e:
        print(f"[Composer] Compositing failed: {e} — using original URL.")
        return image_url

    hosts = [
        ("catbox.moe",    _upload_catbox),
        ("litterbox",     _upload_litterbox),
        ("imgbb",         _upload_imgbb),
    ]
    for name, fn in hosts:
        try:
            url = fn(jpg_bytes)
            print(f"[Composer] Uploaded to {name}: {url}")
            return url
        except Exception as e:
            print(f"[Composer] {name} failed: {e}")

    print("[Composer] All hosts failed — using original URL.")
    return image_url
