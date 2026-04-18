"""
Google Form + Email approval flow for Instagram post drafts.

Flow:
  1. Agent generates caption + image → saves draft
  2. Sends email to reviewer with preview + Google Form link
  3. Reviewer fills form: Approve (Yes/No) + optional remarks
  4. Agent checks Google Sheet (published CSV) for response
"""

import csv
import io
import os
import smtplib
import subprocess
import uuid
import time
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote
from typing import Union, Optional, Tuple

AGENT_EMAIL = os.environ.get("AGENT_EMAIL", "")
AGENT_EMAIL_PASSWORD = os.environ.get("AGENT_EMAIL_PASSWORD", "")
APPROVAL_EMAIL = os.environ.get("APPROVAL_EMAIL", "")
GOOGLE_FORM_URL = os.environ.get("GOOGLE_FORM_URL", "")
GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL", "")


def generate_draft_id() -> str:
    """Create a unique draft ID for tracking through the form."""
    from datetime import datetime

    date_str = datetime.now().strftime("%Y-%m-%d")
    short_id = uuid.uuid4().hex[:6]
    return f"{date_str}-{short_id}"


def build_form_url(draft_id: str) -> str:
    """Build a pre-filled Google Form URL with the draft ID.

    The form should have a 'Draft ID' field. The pre-fill parameter name
    depends on the form's entry IDs. Set GOOGLE_FORM_PREFILL_PARAM in .env
    to match your form's draft ID field entry (e.g., 'entry.123456789').
    """
    prefill_param = os.environ.get("GOOGLE_FORM_PREFILL_PARAM", "entry.0")
    return f"{GOOGLE_FORM_URL}?{prefill_param}={quote(draft_id)}"


def _download_image(url: str) -> Optional[bytes]:
    """Download image bytes from a URL using curl. Returns None on failure."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "-m", "30", url],
            capture_output=True, timeout=35,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception as e:
        print(f"[Approval] Image download failed: {e}")
    return None


def send_draft_email(draft: dict) -> None:
    """Send an HTML email with the draft caption, image embedded inline, and form link."""
    if not all([AGENT_EMAIL, AGENT_EMAIL_PASSWORD, APPROVAL_EMAIL]):
        print("[Approval] Email credentials not configured — skipping email.")
        print(f"[Approval] Draft ID: {draft['draft_id']}")
        print(f"[Approval] Form URL: {build_form_url(draft['draft_id'])}")
        return

    form_url = build_form_url(draft["draft_id"])
    attempt = draft.get("attempt", 1)
    subject = f"[FinAmigo Post] {draft['date']} — Review & Approve"
    if attempt > 1:
        subject += f" (Revision #{attempt})"

    # Try to download and inline the image so it always shows regardless of email client settings
    image_bytes = _download_image(draft["image_url"])
    use_inline = image_bytes is not None
    img_src = "cid:preview_image" if use_inline else draft["image_url"]
    if not use_inline:
        print(f"[Approval] Could not download image — falling back to external URL in email.")

    html = f"""\
<html>
<body style="font-family: -apple-system, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
  <h2 style="color: #0d9488;">FinAmigo Instagram Post Draft</h2>
  <p><strong>Date:</strong> {draft['date']}</p>
  <p><strong>Theme:</strong> {draft.get('theme', 'N/A')}</p>
  <p><strong>Caption style:</strong> {draft.get('caption_style', 'N/A')} &nbsp;|&nbsp; <strong>Image style:</strong> {draft.get('image_style', 'N/A')}</p>
  {"<p><strong>Attempt:</strong> #" + str(attempt) + "</p>" if attempt > 1 else ""}

  <hr style="border: 1px solid #e5e7eb;">

  <h3>Caption</h3>
  <div style="background: #f9fafb; border-left: 4px solid #0d9488; padding: 16px; white-space: pre-wrap; line-height: 1.6;">
{draft['caption']}
  </div>

  <h3>Image Preview</h3>
  <img src="{img_src}" alt="Generated post image" style="max-width: 400px; border-radius: 8px; border: 1px solid #e5e7eb; display: block; margin-bottom: 8px;">
  <p style="margin-top: 6px;"><a href="{draft['image_url']}" style="color: #0d9488; font-size: 13px;">↗ Open full image in browser</a></p>

  <hr style="border: 1px solid #e5e7eb;">

  <h3>Review & Approve</h3>
  <p>Click the button below to approve or request changes:</p>
  <a href="{form_url}"
     style="display: inline-block; background: #0d9488; color: white; padding: 12px 24px;
            text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px;">
    Review & Approve
  </a>

  <p style="color: #6b7280; font-size: 12px; margin-top: 24px;">
    Draft ID: {draft['draft_id']}<br>
    Image prompt: {draft.get('image_prompt', 'N/A')[:100]}...
  </p>
</body>
</html>"""

    # Build a multipart/related message so the inline CID image is part of the email body
    msg_outer = MIMEMultipart("mixed")
    msg_outer["Subject"] = subject
    msg_outer["From"] = AGENT_EMAIL
    msg_outer["To"] = APPROVAL_EMAIL

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
        print(f"[Approval] Draft email sent to {APPROVAL_EMAIL} {'(image embedded inline)' if use_inline else '(image as external link)'}")
    except Exception as e:
        print(f"[Approval] Email send failed: {e}")
        print(f"[Approval] Form URL (manual): {form_url}")


def check_form_response(draft_id: str) -> Tuple[str, Optional[str]]:
    """Check the Google Sheet for a form response matching this draft ID.

    Returns:
        ("approved", None) — if approved
        ("remarks", "the remarks text") — if rejected with remarks
        ("pending", None) — if no response yet
    """
    if not GOOGLE_SHEET_CSV_URL:
        print("[Approval] GOOGLE_SHEET_CSV_URL not set — cannot check responses.")
        return ("pending", None)

    try:
        url = GOOGLE_SHEET_CSV_URL
        if "?" in url:
            url += f"&t={int(time.time())}"
        else:
            url += f"?t={int(time.time())}"

        result = subprocess.run(
            ["curl", "-s", "-m", "15", "-L", url],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl failed: {result.stderr}")
        csv_text = result.stdout
    except Exception as e:
        print(f"[Approval] Failed to fetch Google Sheet: {e}")
        return ("pending", None)

    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader, None)
    if not header:
        return ("pending", None)

    # Find column indices (flexible matching)
    approve_col = None
    remarks_col = None
    draft_id_col = None

    for i, col in enumerate(header):
        col_lower = col.strip().lower()
        if "approve" in col_lower or "approved" in col_lower or "finamigo" in col_lower or "post" in col_lower:
            approve_col = i
        elif "remark" in col_lower or "feedback" in col_lower or "comment" in col_lower:
            remarks_col = i
        elif "draft" in col_lower and "id" in col_lower:
            draft_id_col = i

    if approve_col is None:
        print(f"[Approval] Could not find 'Approve' column in sheet headers: {header}")
        return ("pending", None)

    # Search for matching draft ID (latest response wins)
    matching_row = None
    print(f"[Debug] Searching for Draft ID: '{draft_id}'")
    for row in reader:
        if draft_id_col is not None and len(row) > draft_id_col:
            val = row[draft_id_col].strip()
            print(f"[Debug] Comparing with row ID: '{val}'")
            if val == draft_id:
                matching_row = row
        elif len(row) > approve_col:
            # If no draft ID column, take the latest row
            matching_row = row

    if matching_row is None:
        return ("pending", None)

    # Parse the approval response
    approval = matching_row[approve_col].strip().lower() if len(matching_row) > approve_col else ""
    remarks = matching_row[remarks_col].strip() if remarks_col and len(matching_row) > remarks_col else ""

    if approval in ("yes", "y", "approve", "approved", "go", "post"):
        print(f"[Approval] Draft {draft_id} APPROVED!")
        return ("approved", None)
    elif approval in ("no", "n", "reject", "rejected", "redo"):
        print(f"[Approval] Draft {draft_id} REJECTED. Remarks: {remarks or '(none)'}")
        return ("remarks", remarks or "Please improve the post.")
    else:
        print(f"[Approval] Unclear response: '{approval}' — treating as pending.")
        return ("pending", None)
