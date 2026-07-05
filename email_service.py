import aiosmtplib
import httpx
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


def _cfg():
    return {
        "host":         os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "port":         int(os.getenv("SMTP_PORT", "587")),
        "user":         os.getenv("SMTP_USER", ""),
        "password":     os.getenv("SMTP_PASS", ""),
        "from_addr":    os.getenv("SMTP_FROM") or os.getenv("SMTP_USER", ""),
        "notify_email": os.getenv("NOTIFY_EMAIL", ""),
        "notify_name":  os.getenv("NOTIFY_NAME", "Admin"),
        "brevo_key":    os.getenv("BREVO_API_KEY", ""),
    }


def _build_overdue_html(overdue_list: list[dict], cfg: dict) -> tuple[str, str]:
    """Returns (subject, html)."""
    rows = ""
    for sub in overdue_list:
        due_date = sub["next_payment_date"]
        due_str = due_date.strftime("%Y-%m-%d %H:%M UTC") if isinstance(due_date, datetime) else str(due_date)
        rows += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd">{sub["customer_name"]}</td>
          <td style="padding:8px;border:1px solid #ddd">{sub["customer_email"]}</td>
          <td style="padding:8px;border:1px solid #ddd">{sub["product_name"]}</td>
          <td style="padding:8px;border:1px solid #ddd">${sub["amount"]:.2f}</td>
          <td style="padding:8px;border:1px solid #ddd;color:#c0392b">{due_str}</td>
        </tr>"""

    count = len(overdue_list)
    subject = f"[ThriveCart] {count} paiement{'s' if count != 1 else ''} en retard (24h+)"

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333">
      <h2 style="color:#c0392b">&#9888; Alerte paiements en retard</h2>
      <p>Bonjour {cfg['notify_name']},</p>
      <p>Les <strong>{count} abonné{'s' if count != 1 else ''}</strong> suivant{'s' if count != 1 else ''}
         n'ont pas payé leur abonnement et sont en retard de <strong>plus de 24h</strong>&nbsp;:</p>

      <table style="border-collapse:collapse;width:100%;font-size:14px">
        <thead>
          <tr style="background:#f2f2f2">
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Nom</th>
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Email</th>
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Produit</th>
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Montant</th>
            <th style="padding:8px;border:1px solid #ddd;text-align:left">Échéance</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>

      <p style="margin-top:24px">Veuillez contacter ces abonnés ou vérifier votre tableau de bord ThriveCart.</p>
      <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
      <p style="font-size:12px;color:#999">
        Alerte envoyée automatiquement par votre moniteur ThriveCart.
        Envoyé le {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}.
      </p>
    </body></html>
    """
    return subject, html


async def _send_via_brevo(subject: str, html: str, cfg: dict):
    """Send via Brevo HTTP API — works on Railway where SMTP ports are blocked."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": cfg["brevo_key"], "content-type": "application/json"},
            json={
                "sender": {"name": "Moniteur ThriveCart", "email": cfg["from_addr"] or cfg["notify_email"]},
                "to": [{"email": cfg["notify_email"]}],
                "subject": subject,
                "htmlContent": html,
            },
            timeout=30,
        )
        if resp.status_code >= 300:
            raise ValueError(f"Brevo API error {resp.status_code}: {resp.text}")


async def _send_via_smtp(subject: str, html: str, cfg: dict):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["notify_email"]
    msg.attach(MIMEText(html, "html"))

    await aiosmtplib.send(
        msg,
        hostname=cfg["host"],
        port=cfg["port"],
        username=cfg["user"],
        password=cfg["password"],
        start_tls=True,
        timeout=30,
    )


async def send_overdue_alert(overdue_list: list[dict]) -> bool:
    if not overdue_list:
        return True

    cfg = _cfg()

    if not cfg["notify_email"]:
        raise ValueError("NOTIFY_EMAIL n'est pas configuré dans Railway → Variables")

    subject, html = _build_overdue_html(overdue_list, cfg)

    # Brevo HTTP API first (Railway blocks SMTP ports on free plans), SMTP as fallback
    if cfg["brevo_key"]:
        await _send_via_brevo(subject, html, cfg)
        return True

    if not cfg["user"] or not cfg["password"]:
        raise ValueError("Ni BREVO_API_KEY ni SMTP_USER/SMTP_PASS ne sont configurés dans Railway → Variables")

    await _send_via_smtp(subject, html, cfg)
    return True
