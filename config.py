import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Flask
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.environ.get("DATABASE_PATH", "./acr.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Owner – gets owner role automatically on startup
    OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "").strip().lower()

    # SMTP (OTP emails)
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASS = os.environ.get("SMTP_PASS", "")
    SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Cat Rescue Scheduler")

    # Claude API
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    # Gmail API
    GMAIL_CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    GMAIL_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    GMAIL_MONITOR_EMAIL = os.environ.get("GMAIL_MONITOR_EMAIL", "acrpetco86@googlegroups.com")
    GMAIL_CHECK_INTERVAL_MINUTES = int(os.environ.get("GMAIL_CHECK_INTERVAL_MINUTES", "5"))

    # Business rules
    MAX_VOLUNTEERS_PER_SHIFT = 3
    OTP_EXPIRY_MINUTES = 10
