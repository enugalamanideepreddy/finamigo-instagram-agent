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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

# ── Font loading ───────────────────────────────────────────────────────────────
# Priority: bundled project fonts → modern system fonts → PIL default.

_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    weight = "Bold" if bold else "Regular"
    # Bundled project fonts take priority (drop Inter/Outfit .ttf files in fonts/)
    bundled = [
        os.path.join(_FONT_DIR, f"Inter-{weight}.ttf"),
        os.path.join(_FONT_DIR, f"Outfit-{weight}.ttf"),
        os.path.join(_FONT_DIR, f"Plus_Jakarta_Sans-{weight}.ttf"),
    ]
    system = (
        [
            # Noto Sans (clean, modern, available on Ubuntu/GitHub Actions)
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans_Condensed-Bold.ttf",
            # Montserrat if installed
            "/usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf",
            # macOS — SF Pro / Helvetica Neue
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
            # Liberation (better fallback than DejaVu)
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-Bold.ttf",
        ]
        if bold else
        [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/montserrat/Montserrat-Regular.ttf",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        ]
    )
    for path in bundled + system:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ── Text helpers ───────────────────────────────────────────────────────────────

def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _text_h(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]


def _wrap_to_pixels(draw: ImageDraw.ImageDraw, text: str, font, max_px: int) -> str:
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


def _draw_text_with_spacing(draw: ImageDraw.ImageDraw, x: int, y: int,
                             text: str, font, fill: tuple,
                             spacing: int = 0) -> int:
    """Draw text char-by-char with extra letter spacing. Returns final x."""
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        bb = draw.textbbox((x, y), ch, font=font)
        x += (bb[2] - bb[0]) + spacing
    return x


def _draw_text_glow(
    base: Image.Image,
    xy: tuple,
    text: str,
    font,
    fill: tuple,
    glow_color: tuple = (0, 0, 0),
    glow_radius: int = 8,
    glow_alpha: int = 200,
    align: str = "left",
    letter_spacing: int = 0,
) -> None:
    """
    Draw text with a soft Gaussian glow shadow — modern alternative to hard offset shadows.
    Renders on a temporary RGBA layer, blurs it, then composites onto base.
    """
    w, h = base.size
    # Shadow layer
    shadow_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    shadow_fill = (*glow_color, glow_alpha)
    if letter_spacing:
        _draw_text_with_spacing(sd, xy[0], xy[1], text, font, shadow_fill, letter_spacing)
    else:
        sd.text(xy, text, font=font, fill=shadow_fill, align=align)
    blurred = shadow_layer.filter(ImageFilter.GaussianBlur(radius=glow_radius))

    # Text layer
    text_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    td = ImageDraw.Draw(text_layer)
    if letter_spacing:
        _draw_text_with_spacing(td, xy[0], xy[1], text, font, fill, letter_spacing)
    else:
        td.text(xy, text, font=font, fill=fill, align=align)

    base.alpha_composite(blurred)
    base.alpha_composite(text_layer)


# ── Gradient helpers ──────────────────────────────────────────────────────────

def _draw_gradient_band(
    draw: ImageDraw.ImageDraw,
    w: int,
    y_start: int,
    y_end: int,
    color: tuple = (8, 10, 20),   # dark navy, not pure black
) -> None:
    """Smooth linear gradient strip — dark navy, modern feel."""
    band_h = y_end - y_start
    for i in range(band_h):
        t = i / max(band_h - 1, 1)
        # Smooth S-curve: starts transparent, ends fully opaque
        alpha = int(235 * (t * t * (3 - 2 * t)))
        draw.rectangle(
            [(0, y_start + i), (w, y_start + i + 1)],
            fill=(*color, alpha),
        )


# ── Pill / badge drawing ───────────────────────────────────────────────────────

def _draw_frosted_pill(
    overlay: Image.Image,
    box: tuple,
    radius: int = 12,
    fill: tuple = (18, 20, 30, 175),
    border_color: tuple = (13, 148, 136, 180),
    border_width: int = 1,
) -> None:
    """Draw a frosted-glass rounded pill with optional thin border."""
    pill_layer = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill_layer)
    pd.rounded_rectangle(box, radius=radius, fill=fill)
    if border_width:
        pd.rounded_rectangle(box, radius=radius, outline=border_color, width=border_width)
    overlay.alpha_composite(pill_layer)


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

    # ── Spacing (scales with image size) ─────────────────────────────────────
    edge  = max(28, w // 22)
    inner = max(12, w // 44)   # padding inside pills

    # ── Brand name pill (top-left) ────────────────────────────────────────────
    brand_size = max(36, w // 17)
    brand_font = _get_font(brand_size, bold=True)

    bx, by = edge, edge
    bb = draw.textbbox((bx, by), brand_name, font=brand_font)
    # Slightly wider horizontal padding for a modern airy feel
    h_pad = inner + 4
    v_pad = inner // 2 + 2
    pill = (
        bb[0] - h_pad,
        bb[1] - v_pad,
        bb[2] + h_pad,
        bb[3] + v_pad,
    )
    _draw_frosted_pill(
        overlay, pill,
        radius=14,
        fill=(15, 17, 26, 185),       # deep navy, semi-transparent
        border_color=(13, 148, 136, 160),
        border_width=1,
    )

    # Brand name in crisp white (more premium than teal-on-dark)
    _draw_text_glow(
        overlay, (bx, by), brand_name, brand_font,
        fill=(255, 255, 255, 250),
        glow_color=(13, 148, 136),
        glow_radius=10,
        glow_alpha=100,
    )

    # ── Badge metrics ─────────────────────────────────────────────────────────
    badge_text  = "COMING SOON"
    badge_size  = max(14, w // 50)
    badge_font  = _get_font(badge_size, bold=True)
    bbl         = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w     = bbl[2] - bbl[0]
    badge_h     = bbl[3] - bbl[1]
    badge_h_pad = inner
    badge_v_pad = max(6, inner // 2)
    badge_total_h = badge_h + badge_v_pad * 2

    # ── Tagline + bottom section ──────────────────────────────────────────────
    if tagline:
        tag_size = max(22, w // 28)
        tag_font = _get_font(tag_size, bold=False)

        safe_w  = w - edge * 4
        wrapped = _wrap_to_pixels(draw, tagline, tag_font, safe_w)

        tbb    = draw.textbbox((0, 0), wrapped, font=tag_font)
        tag_w  = tbb[2] - tbb[0]
        tag_h  = tbb[3] - tbb[1]

        gap        = inner + 4
        band_inner = inner + 4
        band_h     = band_inner + tag_h + gap + badge_total_h + edge
        band_y     = h - band_h

        _draw_gradient_band(draw, w, band_y, h)

        # Tagline — centered, with soft glow shadow
        tag_x = max(edge * 2, (w - tag_w) // 2)
        tag_y = band_y + band_inner
        _draw_text_glow(
            overlay, (tag_x, tag_y), wrapped, tag_font,
            fill=(255, 255, 255, 250),
            glow_color=(0, 0, 0),
            glow_radius=10,
            glow_alpha=220,
            align="center",
        )

        # "COMING SOON" badge — modern outlined frosted pill, bottom-right
        badge_x = w - badge_w - badge_h_pad - edge
        badge_y = tag_y + tag_h + gap + badge_v_pad
        badge_box = (
            badge_x - badge_h_pad,
            badge_y - badge_v_pad,
            badge_x + badge_w + badge_h_pad,
            badge_y + badge_h + badge_v_pad,
        )
        _draw_frosted_pill(
            overlay, badge_box,
            radius=20,
            fill=(13, 148, 136, 40),         # very light teal tint (frosted)
            border_color=(13, 148, 136, 230),
            border_width=1,
        )
        # Badge text with subtle glow
        _draw_text_glow(
            overlay, (badge_x, badge_y), badge_text, badge_font,
            fill=(255, 255, 255, 245),
            glow_color=(13, 148, 136),
            glow_radius=6,
            glow_alpha=140,
        )

    else:
        # No tagline — slim gradient strip + badge only
        band_h = badge_total_h + edge * 2
        band_y = h - band_h
        _draw_gradient_band(draw, w, band_y, h)

        badge_x = w - badge_w - badge_h_pad - edge
        badge_y = band_y + edge + badge_v_pad
        badge_box = (
            badge_x - badge_h_pad,
            badge_y - badge_v_pad,
            badge_x + badge_w + badge_h_pad,
            badge_y + badge_h + badge_v_pad,
        )
        _draw_frosted_pill(
            overlay, badge_box,
            radius=20,
            fill=(13, 148, 136, 40),
            border_color=(13, 148, 136, 230),
            border_width=1,
        )
        _draw_text_glow(
            overlay, (badge_x, badge_y), badge_text, badge_font,
            fill=(255, 255, 255, 245),
            glow_color=(13, 148, 136),
            glow_radius=6,
            glow_alpha=140,
        )

    composited = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    composited.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _upload_catbox(jpg_bytes: bytes) -> str:
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
        ("catbox.moe", _upload_catbox),
        ("litterbox",  _upload_litterbox),
        ("imgbb",      _upload_imgbb),
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
