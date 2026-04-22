"""
Composites clean FinAmigo branding onto a generated image using Pillow.

Why: AI image models (including Ideogram) cannot reliably render legible text.
This module downloads the raw generated image and draws crisp vector-quality
text on top — brand name, tagline — then re-uploads to catbox.moe.
"""

import io
import os
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont

IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

# ── Font loading ───────────────────────────────────────────────────────────────
# Priority: bold/regular sans-serif → PIL default fallback.

def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
        if bold else
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    """Return pixel width of a single-line string."""
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _wrap_to_pixels(draw: ImageDraw.ImageDraw, text: str, font, max_px: int) -> str:
    """Word-wrap text so no line exceeds max_px wide."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if _text_w(draw, candidate, font) <= max_px:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return "\n".join(lines)


def _draw_text_shadowed(
    draw: ImageDraw.ImageDraw,
    xy: tuple,
    text: str,
    font,
    fill: tuple,
    shadow_color: tuple = (0, 0, 0, 180),
    shadow_offset: int = 2,
    align: str = "left",
) -> None:
    """Draw text with a crisp drop shadow."""
    x, y = xy
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font,
              fill=shadow_color, align=align)
    draw.text((x, y), text, font=font, fill=fill, align=align)


# ── Gradient helpers ──────────────────────────────────────────────────────────

def _draw_gradient_band(draw: ImageDraw.ImageDraw, w: int, y_start: int, y_end: int,
                        color: tuple = (0, 0, 0)) -> None:
    """Vertical gradient strip from transparent → opaque (bottom-weighted)."""
    band_h = y_end - y_start
    for i in range(band_h):
        # Ease-in: slow rise then steep near bottom
        t = i / max(band_h - 1, 1)
        alpha = int(200 * (t ** 0.6))
        draw.rectangle([(0, y_start + i), (w, y_start + i + 1)],
                        fill=(*color, alpha))


# ── Core compositing ──────────────────────────────────────────────────────────

def composite_branding(
    image_url: str,
    tagline: str = "",
    brand_name: str = "FinAmigo",
) -> bytes:
    """Download image, composite FinAmigo branding, return JPEG bytes."""
    r = requests.get(image_url, timeout=60)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGBA")
    w, h = img.size

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # ── Spacing constants (scale with image size) ────────────────────────────
    edge   = max(28, w // 22)   # outer margin from image edge
    inner  = max(14, w // 40)   # padding inside pills / band

    # ── Brand name: top-left pill ────────────────────────────────────────────
    brand_size = max(42, w // 15)
    brand_font = _get_font(brand_size, bold=True)

    bx, by = edge, edge
    bb = draw.textbbox((bx, by), brand_name, font=brand_font)
    pill = (bb[0] - inner,      bb[1] - inner // 2,
            bb[2] + inner,      bb[3] + inner // 2)

    # Dark pill
    draw.rounded_rectangle(pill, radius=10, fill=(0, 0, 0, 155))
    # Teal left accent bar
    draw.rounded_rectangle(
        (pill[0], pill[1], pill[0] + 5, pill[3]),
        radius=3, fill=(13, 148, 136, 255),
    )
    _draw_text_shadowed(draw, (bx, by), brand_name, brand_font,
                        fill=(13, 148, 136, 255))

    # ── Badge size (needed to calculate bottom band height) ──────────────────
    badge_text = "Coming Soon"
    badge_size = max(17, w // 44)
    badge_font = _get_font(badge_size, bold=True)
    bbl        = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w    = bbl[2] - bbl[0]
    badge_h    = bbl[3] - bbl[1]
    badge_pill_h = inner // 2   # vertical padding inside badge pill
    badge_pill_w = inner         # horizontal padding inside badge pill
    badge_total_h = badge_h + badge_pill_h * 2

    # ── Tagline layout ───────────────────────────────────────────────────────
    if tagline:
        tag_size = max(23, w // 30)
        tag_font = _get_font(tag_size, bold=False)

        # Pixel-accurate word-wrap — safe zone keeps text well inside edges
        safe_w  = w - edge * 4
        wrapped = _wrap_to_pixels(draw, tagline, tag_font, safe_w)

        tbb    = draw.textbbox((0, 0), wrapped, font=tag_font)
        tag_w  = tbb[2] - tbb[0]
        tag_h  = tbb[3] - tbb[1]

        # Band: tagline block + gap + badge row, all with inner padding
        gap        = inner
        band_inner = inner                         # top padding inside band
        band_h     = band_inner + tag_h + gap + badge_total_h + edge
        band_y     = h - band_h

        # Gradient band across full width
        _draw_gradient_band(draw, w, band_y, h)

        # Tagline — centered horizontally
        tag_x = max(edge * 2, (w - tag_w) // 2)
        tag_y = band_y + band_inner
        _draw_text_shadowed(draw, (tag_x, tag_y), wrapped, tag_font,
                            fill=(255, 255, 255, 248),
                            shadow_color=(0, 0, 0, 210),
                            align="center")

        # "Coming Soon" teal pill badge — bottom-right
        badge_x = w - badge_w - badge_pill_w - edge
        badge_y = tag_y + tag_h + gap + badge_pill_h
        draw.rounded_rectangle(
            (badge_x - badge_pill_w,
             badge_y - badge_pill_h,
             badge_x + badge_w + badge_pill_w,
             badge_y + badge_h + badge_pill_h),
            radius=20,
            fill=(13, 148, 136, 235),
        )
        draw.text((badge_x, badge_y), badge_text,
                  font=badge_font, fill=(255, 255, 255, 255))

    else:
        # No tagline — minimal gradient strip + badge only
        band_h = badge_total_h + edge * 2
        band_y = h - band_h
        _draw_gradient_band(draw, w, band_y, h)

        badge_x = w - badge_w - badge_pill_w - edge
        badge_y = band_y + edge + badge_pill_h
        draw.rounded_rectangle(
            (badge_x - badge_pill_w,
             badge_y - badge_pill_h,
             badge_x + badge_w + badge_pill_w,
             badge_y + badge_h + badge_pill_h),
            radius=20,
            fill=(13, 148, 136, 235),
        )
        draw.text((badge_x, badge_y), badge_text,
                  font=badge_font, fill=(255, 255, 255, 255))

    composited = Image.alpha_composite(img, overlay).convert("RGB")
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
