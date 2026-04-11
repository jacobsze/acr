import json
import smtplib
import urllib.request
import urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app


def send_otp_email(to_email: str, otp_code: str, recipient_name: str) -> None:
    """
    Send a one-time passcode.  Uses SendGrid Web API if SENDGRID_API_KEY is set
    (recommended for cloud hosting), otherwise falls back to SMTP.
    """
    cfg = current_app.config

    if cfg.get("SENDGRID_API_KEY"):
        _send_via_sendgrid(to_email, otp_code, recipient_name, cfg)
    else:
        _send_via_smtp(to_email, otp_code, recipient_name, cfg)


def _build_email_content(otp_code: str, recipient_name: str, expiry: int) -> tuple[str, str]:
    plain = (
        f"Hi {recipient_name},\n\n"
        f"Your login code is: {otp_code}\n\n"
        f"This code expires in {expiry} minutes.\n\n"
        "If you did not request this, please ignore this message."
    )
    html = f"""\
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px;">
  <h2 style="color:#6368DA;margin-bottom:4px;">Cat Rescue Scheduler</h2>
  <p>Hi {recipient_name},</p>
  <p>Use the code below to log in:</p>
  <div style="background:#f0f0f8;border-radius:8px;padding:24px;text-align:center;margin:24px 0;">
    <span style="font-size:36px;font-weight:bold;letter-spacing:10px;color:#373F51;">
      {otp_code}
    </span>
  </div>
  <p style="color:#666;font-size:13px;">Expires in {expiry} minutes.</p>
  <p style="color:#aaa;font-size:11px;">
    If you did not request this code, you can safely ignore this email.
  </p>
</body>
</html>"""
    return plain, html


def _send_via_sendgrid(to_email: str, otp_code: str, recipient_name: str, cfg: dict) -> None:
    expiry = cfg["OTP_EXPIRY_MINUTES"]
    from_name = cfg.get("SMTP_FROM_NAME", "Cat Rescue Scheduler")
    from_email = cfg.get("SENDGRID_FROM_EMAIL") or cfg.get("SMTP_USER") or "noreply@example.com"

    plain, html = _build_email_content(otp_code, recipient_name, expiry)

    payload = json.dumps({
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": from_name},
        "subject": "Your Cat Rescue Scheduler login code",
        "content": [
            {"type": "text/plain", "value": plain},
            {"type": "text/html",  "value": html},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={
            "Authorization": f'Bearer {cfg["SENDGRID_API_KEY"]}',
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        if resp.status not in (200, 202):
            raise RuntimeError(f"SendGrid returned HTTP {resp.status}")


def _send_via_smtp(to_email: str, otp_code: str, recipient_name: str, cfg: dict) -> None:
    expiry = cfg["OTP_EXPIRY_MINUTES"]
    plain, html = _build_email_content(otp_code, recipient_name, expiry)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Cat Rescue Scheduler login code"
    msg["From"] = f'{cfg["SMTP_FROM_NAME"]} <{cfg["SMTP_USER"]}>'
    msg["To"] = to_email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"]) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
        smtp.sendmail(cfg["SMTP_USER"], to_email, msg.as_string())
