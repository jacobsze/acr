import os
import subprocess
import threading
from datetime import datetime, timezone

from flask import Flask

from config import Config
from models import db

# Prevents concurrent Gmail checks within a single process
_gmail_check_running = threading.Event()
_gmail_check_running.set()   # "set" means idle / not running


def _get_version() -> str:
    """Return short git SHA — prefers RENDER_GIT_COMMIT env var set by Render."""
    commit = os.environ.get("RENDER_GIT_COMMIT", "")
    if commit:
        return commit[:7]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)
    app.config["APP_VERSION"] = _get_version()

    # ── Database ──────────────────────────────────────────────────────────
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _ensure_owner_exists(app)
        _log_db_backend(app)

    # ── Blueprints ────────────────────────────────────────────────────────
    from routes.auth_routes import auth_bp
    from routes.schedule_routes import schedule_bp
    from routes.admin_routes import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(schedule_bp)
    app.register_blueprint(admin_bp)

    # ── Template context & filters ────────────────────────────────────────
    from auth_utils import get_current_user
    import json as _json

    @app.context_processor
    def inject_user():
        from datetime import date
        from flask import session
        from routes.schedule_routes import get_week_start
        from models import User
        today = date.today()
        actual_user = get_current_user()
        view_as_user = None
        view_as_candidates = []
        if actual_user and actual_user.role == "owner":
            view_as_id = session.get("view_as_id")
            if view_as_id:
                view_as_user = User.query.filter_by(id=view_as_id, active=True).first()
            view_as_candidates = User.query.filter_by(active=True).order_by(User.name).all()
        return {
            "current_user": actual_user,
            "today": today,
            "current_week_start": get_week_start(today),
            "view_as_user": view_as_user,
            "view_as_candidates": view_as_candidates,
        }

    @app.template_filter("fromjson")
    def fromjson_filter(value):
        try:
            return _json.loads(value)
        except Exception:
            return {}

    @app.template_filter("et_fmt")
    def et_fmt_filter(dt):
        """Format a naive-UTC datetime as 12h ET, e.g. '4/22 9:46 AM'."""
        if dt is None:
            return "—"
        from zoneinfo import ZoneInfo
        from datetime import timezone as _tz
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%-m/%-d %-I:%M %p")

    # ── Gmail lazy-check (fires on requests when overdue) ─────────────────
    _wire_gmail_check(app)

    # ── 10 am open-shift alert (APScheduler CronTrigger) ─────────────────
    _start_open_shift_cron(app)

    return app


def _wire_gmail_check(app: Flask) -> None:
    """Register a before_request hook that triggers Gmail polling when overdue."""
    creds_file = app.config.get("GMAIL_CREDENTIALS_FILE", "")
    if not creds_file or not os.path.exists(creds_file):
        app.logger.info(
            "Gmail credentials not found – email monitor disabled. "
            "Set GMAIL_CREDENTIALS_FILE in .env to enable."
        )
        return

    interval = app.config["GMAIL_CHECK_INTERVAL_MINUTES"]
    app.logger.info("Gmail lazy-check wired (fires on request when >%d min overdue).", interval)

    @app.before_request
    def _lazy_gmail_check():
        try:
            if not _gmail_check_running.is_set():
                return  # Already running

            interval_sec = app.config["GMAIL_CHECK_INTERVAL_MINUTES"] * 60
            from models import AppSetting
            setting = AppSetting.query.get("last_email_check")
            if setting:
                try:
                    last = datetime.fromisoformat(setting.value).replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - last).total_seconds() < interval_sec:
                        return  # Checked recently enough
                except ValueError:
                    pass

            # Claim the slot (non-blocking test-and-clear)
            if not _gmail_check_running.is_set():
                return
            _gmail_check_running.clear()

            def _run():
                try:
                    from services.gmail_monitor import check_and_process
                    check_and_process(app)
                finally:
                    _gmail_check_running.set()

            threading.Thread(target=_run, daemon=True, name="gmail-check").start()
        except Exception as exc:
            app.logger.warning("Gmail lazy-check hook error (ignored): %s", exc)


def _start_open_shift_cron(app: Flask) -> None:
    """Start APScheduler CronTrigger for the daily 10 am open-shift alert."""
    creds_file = app.config.get("GMAIL_CREDENTIALS_FILE", "")
    if not creds_file or not os.path.exists(creds_file):
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from zoneinfo import ZoneInfo
        from services.weekly_email import check_and_send_open_shift_alert

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            func=check_and_send_open_shift_alert,
            args=[app],
            trigger=CronTrigger(hour=10, minute=0, timezone=ZoneInfo("America/New_York")),
            id="open_shift_alert",
            replace_existing=True,
        )
        scheduler.start()
        app.email_scheduler = scheduler
        app.logger.info("Open-shift alert cron started (10 am ET daily).")
    except ImportError:
        app.logger.warning("APScheduler not installed – open-shift alert disabled.")
    except Exception as exc:
        app.logger.error("Failed to start open-shift cron: %s", exc)


def _ensure_owner_exists(app: Flask) -> None:
    """Create (or promote) the owner account on startup."""
    from models import User

    owner_email = app.config.get("OWNER_EMAIL", "")
    if not owner_email:
        return

    owner = User.query.filter_by(email=owner_email).first()
    if not owner:
        owner = User(name="Owner", email=owner_email, role="owner")
        db.session.add(owner)
        db.session.commit()
        app.logger.info("Created owner account: %s", owner_email)
    elif owner.role != "owner":
        owner.role = "owner"
        db.session.commit()
        app.logger.info("Promoted %s to owner", owner_email)


def _log_db_backend(app: Flask) -> None:
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("postgresql"):
        app.logger.info("Database: PostgreSQL (%s)", uri.split("@")[-1])
    else:
        app.logger.warning("Database: SQLite (ephemeral – data will not persist on Render!): %s", uri)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    application = create_app()
    application.run(debug=True, host="0.0.0.0", port=5000)
