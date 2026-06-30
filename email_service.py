import aiosmtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")
NOTIFY_NAME = os.getenv("NOTIFY_NAME", "Admin")


def _build_overdue_email(overdue_list: list[dict]) -> MIMEMultipart:
    """Build the HTML email for multiple overdue subscribers."""
    rows = ""
    for sub in overdue_list:
        due_date = sub["next_payment_date"]
        if isinstance(due_date, datetime):
            due_str = due_date.strftime("%Y-%m-%d %H:%M UTC")
        else:
            due_str = str(due_date)

        rows += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd">{sub["customer_name"]}</td>
          <td style="padding:8px;border:1px solid #ddd">{sub["customer_email"]}</td>
          <td style="padding:8px;border:1px solid #ddd">{sub["product_name"]}</td>
          <td style="padding:8px;border:1px solid #ddd">${sub["amount"]:.2f}</td>
          <td style="padding:8px;border:1px solid #ddd;color:#c0392b">{due_str}</td>
        </tr>"""

    count = len(overdue_list)
    subject = f"[ThriveCart] {count} overdue subscription{'s' if count != 1 else ''} — payment 24h+ late"

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333">
      <h2 style="color:#c0392b">&#9888; Overdue Subscription Alert</h2>
      <p>Hi {NOTIFY_NAME},</p>
      <p>The following <strong>{count} subscriber{'s' if count != 1 else ''}</strong>
         {'have' if count != 1 else 'has'} not paid their monthly subscription
         and {'are' if count != 1 else 'is'} now <strong>24+ hours overdue</strong>:</p>

      <table style="border-collapse:collapse;width:100%;font-size:14px">
        <thead>
          <tr style="background:#f2f2f2">
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Name</th>
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Email</th>
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Product</th>
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Amount</th>
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Was Due</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>

      <p style="margin-top:24px">Please follow up with these subscribers or check your
         ThriveCart dashboard for more details.</p>
      <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
      <p style="font-size:12px;color:#999">
        This alert was sent automatically by your ThriveCart payment monitor.
        Sent at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}.
      </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))
    return msg


async def send_overdue_alert(overdue_list: list[dict]) -> bool:
    """Send an overdue-payment alert email. Returns True on success."""
    if not overdue_list:
        return True
    if not NOTIFY_EMAIL:
        raise ValueError("NOTIFY_EMAIL is not configured in .env")
    if not SMTP_USER or not SMTP_PASS:
        raise ValueError("SMTP credentials are not configured in .env")

    msg = _build_overdue_email(overdue_list)

    await aiosmtplib.send(
        msg,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=SMTP_USER,
        password=SMTP_PASS,
        start_tls=True,
    )
    return True
