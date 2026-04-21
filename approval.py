"""
Email backup channel for Instagram post drafts.
Primary approval is via the Telegram bot (bot_server.py).
"""

import os
import smtplib
import uuid
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests

AGENT_EMAIL       = os.environ.get("AGENT_EMAIL", "")
AGENT_EMAIL_PASSWORD = os.environ.get("AGENT_EMAIL_PASSWORD", "")
APPROVAL_EMAIL    = os.environ.get("APPROVAL_EMAIL", "")


def generate_draft_id() -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    short_id = uuid.uuid4().hex[:6]
    return f"{date_str}-{short_id}"


def send_draft_email(draft: dict) -> None:
    """Send HTML approval email with inline image. Silently skips if not configured."""
    if not all([AGENT_EMAIL, AGENT_EMAIL_PASSWORD, APPROVAL_EMAIL]):
        print("[Approval] Email not configured — skipping (Telegram bot is primary).")
        return

    attempt = draft.get("attempt", 1)
    subject = f"[FinAmigo Post] {draft['date']} — Review Draft"
    if attempt > 1:
        subject += f" (Revision #{attempt})"

    # Try to embed image inline
    image_bytes = None
    try:
        r = requests.get(draft["image_url"], timeout=30)
        if r.status_code == 200 and r.content:
            image_bytes = r.content
    except Exception:
        pass

    img_src = "cid:preview_image" if image_bytes else draft["image_url"]

    html = f"""\
<html>
<body style="font-family: -apple-system, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #0d9488;">FinAmigo Draft — Approve via Telegram</h2>
  <p><strong>Date:</strong> {draft['date']}</p>
  <p><strong>Theme:</strong> {draft.get('theme', 'N/A')}</p>
  <p><strong>Style:</strong> {draft.get('caption_style', 'N/A')} &nbsp;·&nbsp; {draft.get('image_style', 'N/A')}</p>

  <hr style="border: 1px solid #e5e7eb;">

  <h3>Caption</h3>
  <div style="background: #f9fafb; border-left: 4px solid #0d9488; padding: 16px;
              white-space: pre-wrap; line-height: 1.6;">
{draft['caption']}
  </div>

  <h3>Image Preview</h3>
  <img src="{img_src}" alt="Post image"
       style="max-width: 400px; border-radius: 8px; border: 1px solid #e5e7eb; display: block;">

  <p style="color: #6b7280; font-size: 12px; margin-top: 24px;">
    Draft ID: {draft['draft_id']}<br>
    Use the Telegram bot to approve or request revisions.
  </p>
</body>
</html>"""

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = AGENT_EMAIL
    msg["To"]      = APPROVAL_EMAIL

    related = MIMEMultipart("related")
    related.attach(MIMEText(html, "html"))

    if image_bytes:
        img_part = MIMEImage(image_bytes)
        img_part.add_header("Content-ID", "<preview_image>")
        img_part.add_header("Content-Disposition", "inline", filename="preview.jpg")
        related.attach(img_part)

    msg.attach(related)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(AGENT_EMAIL, AGENT_EMAIL_PASSWORD)
            server.sendmail(AGENT_EMAIL, APPROVAL_EMAIL, msg.as_string())
        print(f"[Approval] Email sent to {APPROVAL_EMAIL}")
    except Exception as e:
        print(f"[Approval] Email send failed: {e}")
