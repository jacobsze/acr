import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app


def send_otp_email(to_email: str, otp_code: str, recipient_name: str) -> None:
    """Send a one-time passcode to *to_email* via SMTP (Gmail app password)."""
    cfg = current_app.config
    expiry = cfg["OTP_EXPIRY_MINUTES"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Cat Rescue Scheduler login code"
    msg["From"] = f'{cfg["SMTP_FROM_NAME"]} <{cfg["SMTP_USER"]}>'
    msg["To"] = to_email

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

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"]) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
        smtp.sendmail(cfg["SMTP_USER"], to_email, msg.as_string())
