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

# Guards against concurrent runs of the request-driven email fallbacks
_open_shift_lock = threading.Lock()
_weekly_email_lock = threading.Lock()


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
        _migrate_schema(app)
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

    # ── Update last_login on each authenticated request ────────────────────
    @app.before_request
    def _update_last_login():
        user = get_current_user()
        if user:
            from datetime import datetime
            user.last_login = datetime.utcnow()
            db.session.commit()

    # ── Gmail lazy-check (fires on requests when overdue) ─────────────────
    _wire_gmail_check(app)

    # ── 10 am open-shift alert (APScheduler CronTrigger) ─────────────────
    _start_open_shift_cron(app)

    # ── Request-driven fallback for outbound scheduled emails ────────────
    # APScheduler can silently miss its tick on Render (deploy/restart near
    # the scheduled time drops the run). This safety net fires the alert /
    # weekly email on the first request after the target time, deduped per
    # day/week via AppSetting markers so nothing double-sends.
    _wire_scheduled_email_fallback(app)

    return app


def _wire_gmail_check(app: Flask) -> None:
    """Register a before_request hook that triggers Gmail polling when overdue."""
    imap_user = app.config.get("GMAIL_IMAP_USER", "")
    imap_password = app.config.get("GMAIL_IMAP_PASSWORD", "")
    if not imap_user or not imap_password:
        app.logger.info(
            "Gmail IMAP credentials not configured – email monitor disabled. "
            "Set GMAIL_IMAP_USER and GMAIL_IMAP_PASSWORD to enable."
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
    """Start APScheduler CronTrigger for the daily 10 am open-shift alert.

    These jobs only *send* email, so they are gated on SMTP credentials (not
    IMAP). Jobs route through the deduped wrappers so they never double-send
    with the request-driven fallback.
    """
    smtp_user = app.config.get("GMAIL_SMTP_USER", "")
    smtp_password = app.config.get("GMAIL_SMTP_PASSWORD", "")
    if not smtp_user or not smtp_password:
        app.logger.info(
            "SMTP credentials not configured – scheduled emails disabled. "
            "Set GMAIL_SMTP_USER and GMAIL_SMTP_PASSWORD to enable."
        )
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from zoneinfo import ZoneInfo
        from services.schedule_cron import extend_52week_schedule

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            func=_run_open_shift_alert,
            args=[app],
            trigger=CronTrigger(hour=10, minute=0, timezone=ZoneInfo("America/New_York")),
            id="open_shift_alert",
            replace_existing=True,
        )
        scheduler.add_job(
            func=_run_weekly_email,
            args=[app],
            trigger=CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=ZoneInfo("America/New_York")),
            id="weekly_schedule_email",
            replace_existing=True,
        )
        scheduler.add_job(
            func=extend_52week_schedule,
            args=[app],
            trigger=CronTrigger(day_of_week="sun", hour=8, minute=0, timezone=ZoneInfo("America/New_York")),
            id="extend_52week_schedule",
            replace_existing=True,
        )
        scheduler.start()
        app.email_scheduler = scheduler
        app.logger.info("Crons started (52-week 8am ET Sundays, weekly schedule 9am ET Sundays, open-shift 10am ET daily).")
    except ImportError:
        app.logger.warning("APScheduler not installed – open-shift alert disabled.")
    except Exception as exc:
        app.logger.error("Failed to start open-shift cron: %s", exc)


def _run_open_shift_alert(app: Flask) -> None:
    """Run the open-shift alert at most once per ET day (cron + request fallback share this)."""
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if not _open_shift_lock.acquire(blocking=False):
        return  # Another run already in progress
    try:
        if not _claim_daily_marker(app, "last_open_shift_alert_date", today.isoformat()):
            return  # Already done today
        from services.weekly_email import check_and_send_open_shift_alert
        check_and_send_open_shift_alert(app)
    except Exception as exc:
        app.logger.error("Open-shift alert failed: %s", exc, exc_info=True)
    finally:
        _open_shift_lock.release()


def _run_weekly_email(app: Flask) -> None:
    """Send the weekly schedule email at most once per ET week (cron + request fallback share this)."""
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if not _weekly_email_lock.acquire(blocking=False):
        return
    try:
        if not _claim_daily_marker(app, "last_weekly_email_date", today.isoformat()):
            return  # Already sent for this Sunday
        from services.weekly_email import send_weekly_schedule_email
        send_weekly_schedule_email(app)
    except Exception as exc:
        app.logger.error("Weekly schedule email failed: %s", exc, exc_info=True)
    finally:
        _weekly_email_lock.release()


def _claim_daily_marker(app: Flask, key: str, value: str) -> bool:
    """Atomically claim a per-period marker. Returns True if newly claimed (caller should send)."""
    from models import db, AppSetting
    with app.app_context():
        setting = db.session.get(AppSetting, key)
        if setting and setting.value == value:
            return False
        if setting:
            setting.value = value
        else:
            db.session.add(AppSetting(key=key, value=value))
        db.session.commit()
    return True


def _wire_scheduled_email_fallback(app: Flask) -> None:
    """Register a before_request hook that fires overdue scheduled emails.

    Acts as a safety net for the APScheduler crons, which can miss their tick
    on Render when the instance restarts near the scheduled time. Dedup is via
    AppSetting markers, so this never causes a duplicate send.
    """
    smtp_user = app.config.get("GMAIL_SMTP_USER", "")
    smtp_password = app.config.get("GMAIL_SMTP_PASSWORD", "")
    if not smtp_user or not smtp_password:
        return

    @app.before_request
    def _scheduled_email_fallback():
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("America/New_York"))

            # Open-shift alert: any time at/after 10:00 ET, once per day.
            # Skip if a run is already in flight; the run itself dedupes via marker.
            if now.hour >= 10 and not _open_shift_lock.locked():
                threading.Thread(
                    target=_run_open_shift_alert, args=[app], daemon=True,
                    name="open-shift-fallback",
                ).start()

            # Weekly schedule email: Sundays at/after 9:00 ET, once per week.
            if now.weekday() == 6 and now.hour >= 9 and not _weekly_email_lock.locked():
                threading.Thread(
                    target=_run_weekly_email, args=[app], daemon=True,
                    name="weekly-email-fallback",
                ).start()
        except Exception as exc:
            app.logger.warning("Scheduled-email fallback hook error (ignored): %s", exc)


def _migrate_schema(app: Flask) -> None:
    """Apply incremental schema changes not handled by db.create_all()."""
    from sqlalchemy import text
    migrations = [
        "ALTER TABLE cat_logs ADD COLUMN shift_type VARCHAR(2)",
        "ALTER TABLE cat_logs ADD COLUMN bowel_movement VARCHAR(100)",
        "ALTER TABLE cat_logs ADD COLUMN food_intake VARCHAR(20)",
        "ALTER TABLE regular_schedule ADD COLUMN frequency VARCHAR(20) DEFAULT 'weekly'",
        "ALTER TABLE regular_schedule ADD COLUMN start_week INTEGER",
        "ALTER TABLE regular_schedule ADD COLUMN start_date DATE",
        "ALTER TABLE users ADD COLUMN last_login TIMESTAMP",
    ]
    for sql in migrations:
        try:
            with db.engine.begin() as conn:
                conn.execute(text(sql))
        except Exception:
            pass  # Column already exists


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
