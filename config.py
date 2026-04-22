import os
from dotenv import load_dotenv

load_dotenv()


def _fix_db_url(url: str) -> str:
    """Render provides postgres:// but SQLAlchemy requires postgresql://"""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    # Flask
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

    # Database: prefer DATABASE_URL (Render Postgres), fall back to SQLite
    _db_url = os.environ.get("DATABASE_URL", "")
    SQLALCHEMY_DATABASE_URI = (
        _fix_db_url(_db_url) if _db_url
        else "sqlite:///" + os.environ.get("DATABASE_PATH", "./acr.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Re-check connections before use; prevents stale-connection ISEs after app sleep
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Owner – promoted to owner role automatically on startup
    OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "").strip().lower()

    # Public URL of this app (used by Clerk for authorized_parties check)
    APP_URL = os.environ.get("APP_URL", "")

    # Clerk auth
    CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
    CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")
    # Frontend API URL from Clerk dashboard (e.g. https://prepared-dove-17.clerk.accounts.dev)
    CLERK_FRONTEND_API_URL = os.environ.get("CLERK_FRONTEND_API_URL", "")

    # Claude API (for email parsing)
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    # Gmail API (for monitoring the Google Group inbox)
    GMAIL_CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    GMAIL_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    GMAIL_MONITOR_EMAIL = os.environ.get("GMAIL_MONITOR_EMAIL", "acr86.schedule@gmail.com")
    GMAIL_CHECK_INTERVAL_MINUTES = int(os.environ.get("GMAIL_CHECK_INTERVAL_MINUTES", "5"))

    # Business rules
    MAX_VOLUNTEERS_PER_SHIFT = 3
