"""
Email approval flow for Instagram post drafts (backup channel alongside Telegram).

Sends an HTML email with the draft image embedded inline so it always renders,
regardless of email client image blocking settings.
"""

import io
import os
import re
import smtplib
import uuid
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Tuple

import requests

AGENT_EMAIL       = os.environ.get("AGENT_EMAIL", "")
AGENT_EMAIL_PASSWORD = os.environ.get("AGENT_EMAIL_PASSWORD", "")
APPROVAL_EMAIL    = os.environ.get("APPROVAL_EMAIL", "")
GOOGLE_FORM_URL   = os.environ.get("GOOGLE_FORM_URL", "")
GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL", "")


def generate_draft_id() -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    short_id = uuid.uuid4().hex[:6]
    return f"{date_str}-{short_id}"


def build_form_url(draft_id: str) -> str:
    prefill_param = os.environ.get("GOOGLE_FORM_PREFILL_PARAM", "entry.0")
    from urllib.parse import quote
    return f"{GOOGLE_FORM_URL}?{prefill_param}={quote(draft_id)}"


def _download_image(url: str) -> Optional[bytes]:
    """Download image bytes for email embedding."""
    try:
        r = requests.get(url, timeout=45)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception as e:
        print(f"[Approval] Image download failed: {e}")
    return None


def send_draft_email(draft: dict) -> None:
    """Send HTML approval email with inline image. Silently skips if not configured."""
    if not all([AGENT_EMAIL, AGENT_EMAIL_PASSWORD, APPROVAL_EMAIL]):
        print("[Approval] Email not configured — skipping email (Telegram is primary).")
        return

    form_url = build_form_url(draft["draft_id"])
    attempt  = draft.get("attempt", 1)
    subject  = f"[FinAmigo Post] {draft['date']} — Review & Approve"
    if attempt > 1:
        subject += f" (Revision #{attempt})"

    image_bytes = _download_image(draft["image_url"])
    use_inline  = image_bytes is not None
    img_src     = "cid:preview_image" if use_inline else draft["image_url"]

    html = f"""\
<html>
<body style="font-family: -apple-system, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #0d9488;">FinAmigo Instagram Post Draft</h2>
  <p><strong>Date:</strong> {draft['date']}</p>
  <p><strong>Theme:</strong> {draft.get('theme', 'N/A')}</p>
  <p><strong>Caption style:</strong> {draft.get('caption_style', 'N/A')} &nbsp;|&nbsp;
     <strong>Image style:</strong> {draft.get('image_style', 'N/A')}</p>
  {"<p><strong>Attempt:</strong> #" + str(attempt) + "</p>" if attempt > 1 else ""}

  <hr style="border: 1px solid #e5e7eb;">

  <h3>Caption</h3>
  <div style="background: #f9fafb; border-left: 4px solid #0d9488; padding: 16px;
              white-space: pre-wrap; line-height: 1.6;">
{draft['caption']}
  </div>

  <h3>Image Preview</h3>
  <img src="{img_src}" alt="Generated post image"
       style="max-width: 400px; border-radius: 8px; border: 1px solid #e5e7eb;
              display: block; margin-bottom: 8px;">
  <p style="margin-top: 6px;">
    <a href="{draft['image_url']}" style="color: #0d9488; font-size: 13px;">
      ↗ Open full image in browser
    </a>
  </p>

  <hr style="border: 1px solid #e5e7eb;">

  <h3>Review &amp; Approve</h3>
  <p>
    <strong>Primary approval:</strong> Use the Telegram bot (instant).<br>
    <strong>Backup form:</strong>
  </p>
  <a href="{form_url}"
     style="display: inline-block; background: #0d9488; color: white; padding: 12px 24px;
            text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px;">
    Open Approval Form
  </a>

  <p style="color: #6b7280; font-size: 12px; margin-top: 24px;">
    Draft ID: {draft['draft_id']}<br>
    Image prompt: {draft.get('image_prompt', 'N/A')[:100]}{"..." if len(draft.get('image_prompt', '')) > 100 else ""}
  </p>
</body>
</html>"""

    msg_outer   = MIMEMultipart("mixed")
    msg_outer["Subject"] = subject
    msg_outer["From"]    = AGENT_EMAIL
    msg_outer["To"]      = APPROVAL_EMAIL

    msg_related = MIMEMultipart("related")
    msg_related.attach(MIMEText(html, "html"))

    if use_inline:
        img_part = MIMEImage(image_bytes)
        img_part.add_header("Content-ID", "<preview_image>")
        img_part.add_header("Content-Disposition", "inline", filename="preview.png")
        msg_related.attach(img_part)

    msg_outer.attach(msg_related)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(AGENT_EMAIL, AGENT_EMAIL_PASSWORD)
            server.sendmail(AGENT_EMAIL, APPROVAL_EMAIL, msg_outer.as_string())
        print(f"[Approval] Email sent to {APPROVAL_EMAIL} "
              f"({'inline image' if use_inline else 'external image link'})")
    except Exception as e:
        print(f"[Approval] Email send failed: {e}")
        print(f"[Approval] Form URL (manual): {form_url}")


def check_form_response(draft_id: str) -> Tuple[str, Optional[str]]:
    """Check Google Sheet CSV for a form response (backup if Telegram is unavailable).

    Returns ("approved"|"remarks"|"pending", remarks_text|None).
    """
    if not GOOGLE_SHEET_CSV_URL:
        return ("pending", None)

    import csv, time
    url = GOOGLE_SHEET_CSV_URL
    url += ("&" if "?" in url else "?") + f"t={int(time.time())}"

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        csv_text = r.text
    except Exception as e:
        print(f"[Approval] Failed to fetch Google Sheet: {e}")
        return ("pending", None)

    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader, None)
    if not header:
        return ("pending", None)

    def _words(s):
        return set(re.split(r"[\s_\-/]+", s.strip().lower()))

    approve_col = remarks_col = draft_id_col = None
    for i, col in enumerate(header):
        w = _words(col)
        if w & {"approve", "approved", "approval"}:
            approve_col = i
        elif w & {"remark", "remarks", "feedback", "comment", "comments"}:
            remarks_col = i
        elif "draft" in w and ("id" in w or "ids" in w):
            draft_id_col = i

    if approve_col is None:
        print(f"[Approval] Could not find 'Approve' column in: {header}")
        return ("pending", None)

    matching_row = None
    for row in reader:
        if draft_id_col is not None and len(row) > draft_id_col:
            if row[draft_id_col].strip() == draft_id:
                matching_row = row
        elif len(row) > approve_col:
            matching_row = row  # No ID column — take latest row

    if matching_row is None:
        return ("pending", None)

    approval = matching_row[approve_col].strip().lower() if len(matching_row) > approve_col else ""
    remarks  = matching_row[remarks_col].strip() if remarks_col and len(matching_row) > remarks_col else ""

    if approval in ("yes", "y", "approve", "approved", "go", "post"):
        return ("approved", None)
    elif approval in ("no", "n", "reject", "rejected", "redo"):
        return ("remarks", remarks or "Please improve the post.")
    return ("pending", None)
