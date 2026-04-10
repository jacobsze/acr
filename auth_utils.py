import secrets
from functools import wraps
from flask import request, redirect, url_for, g
from models import User

COOKIE_NAME = "acr_session"
# 10 years in seconds – effectively non-expiring
COOKIE_MAX_AGE = 10 * 365 * 24 * 60 * 60


def get_current_user() -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return User.query.filter_by(session_token=token, active=True).first()


def _redirect_login():
    return redirect(url_for("auth.login"))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return _redirect_login()
        g.user = user
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return _redirect_login()
        if not user.is_admin_or_owner():
            return redirect(url_for("schedule.index"))
        g.user = user
        return f(*args, **kwargs)
    return decorated


def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return _redirect_login()
        if user.role != "owner":
            return redirect(url_for("schedule.index"))
        g.user = user
        return f(*args, **kwargs)
    return decorated


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def set_session_cookie(response, token: str):
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        # Set secure=True when serving over HTTPS
        secure=False,
    )
    return response
