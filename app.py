import os

from flask import Flask

from config import Config
from models import db


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

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
        from routes.schedule_routes import get_week_start
        today = date.today()
        return {
            "current_user": get_current_user(),
            "today": today,
            "current_week_start": get_week_start(today),
        }

    @app.template_filter("fromjson")
    def fromjson_filter(value):
        try:
            return _json.loads(value)
        except Exception:
            return {}

    # ── Background email monitor ──────────────────────────────────────────
    _start_email_monitor(app)

    return app


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


def _start_email_monitor(app: Flask) -> None:
    """Start a background APScheduler job to poll Gmail, if configured."""
    creds_file = app.config.get("GMAIL_CREDENTIALS_FILE", "")
    if not creds_file or not os.path.exists(creds_file):
        app.logger.info(
            "Gmail credentials not found – email monitor disabled. "
            "Set GMAIL_CREDENTIALS_FILE in .env to enable."
        )
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from services.gmail_monitor import check_and_process

        interval = app.config["GMAIL_CHECK_INTERVAL_MINUTES"]
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            func=check_and_process,
            args=[app],
            trigger="interval",
            minutes=interval,
            id="gmail_monitor",
            replace_existing=True,
        )
        scheduler.start()
        app.email_scheduler = scheduler
        app.logger.info("Email monitor started (every %d min).", interval)
    except ImportError:
        app.logger.warning("APScheduler not installed – email monitor disabled.")
    except Exception as exc:
        app.logger.error("Failed to start email monitor: %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    application = create_app()
    application.run(debug=True, host="0.0.0.0", port=5000)
