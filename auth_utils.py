from functools import wraps
from flask import request, redirect, url_for, g, current_app
from models import db, User


def _get_effective_user(actual_user: User) -> User:
    """Return the user being viewed as, or actual_user if no view-as session is set."""
    from flask import session
    if actual_user.role != "owner":
        return actual_user
    view_as_id = session.get("view_as_id")
    if not view_as_id:
        return actual_user
    target = User.query.filter_by(id=view_as_id, active=True).first()
    return target or actual_user


def get_current_user() -> User | None:
    """
    Verify the Clerk session cookie and return the matching User record.

    On first sign-in, we call the Clerk Users API to get the email, link it
    to a pre-registered User, and cache the Clerk user ID for future lookups.
    Returns None if not signed in or email not pre-registered.
    """
    # Fast path: already resolved for this request
    if hasattr(g, "_current_user"):
        return g._current_user

    user = _resolve_clerk_user()
    g._current_user = user
    return user


def _resolve_clerk_user() -> User | None:
    # For local testing: allow any email to log in
    if current_app.config.get("LOCAL_TEST_MODE"):
        from flask import session
        email = session.get("_test_email")
        if email:
            user = User.query.filter_by(email=email, active=True).first()
            if not user:
                user = User(email=email, name="Test User", role="volunteer", active=True)
                db.session.add(user)
                db.session.commit()
            return user

    secret_key = current_app.config.get("CLERK_SECRET_KEY", "")
    if not secret_key:
        return None

    try:
        from clerk_backend_api import Clerk
        from clerk_backend_api.jwks_helpers import (
            AuthenticateRequestOptions,
            authenticate_request,
        )

        sdk = Clerk(bearer_auth=secret_key)
        app_url = current_app.config.get("APP_URL", "")
        options = AuthenticateRequestOptions(
            authorized_parties=[app_url] if app_url else []
        )
        state = sdk.authenticate_request(request, options)

        if not state.is_signed_in:
            return None

        clerk_user_id = state.payload.get("sub")
        if not clerk_user_id:
            return None

        # Fast lookup by Clerk user ID (set after first sign-in)
        user = User.query.filter_by(clerk_user_id=clerk_user_id, active=True).first()
        if user:
            return user

        # First sign-in: fetch email from Clerk and link to our User record
        clerk_user = sdk.users.get(user_id=clerk_user_id)
        email_objs = clerk_user.email_addresses or []
        if not email_objs:
            return None

        raw_email = getattr(email_objs[0], "email_address", None) or getattr(email_objs[0], "email", "")
        email = raw_email.strip().lower()
        if not email:
            return None

        user = User.query.filter_by(email=email, active=True).first()
        if user:
            user.clerk_user_id = clerk_user_id
            # Sync name if our record still has the placeholder "Owner"
            if not user.name or user.name == "Owner":
                first = clerk_user.first_name or ""
                last = clerk_user.last_name or ""
                full = f"{first} {last}".strip()
                if full:
                    user.name = full
            db.session.commit()
            return user

        return None  # Email not pre-registered – access denied

    except Exception as exc:
        current_app.logger.warning("Clerk auth error: %s", exc)
        g._clerk_lookup_error = True
        return None


def _redirect_login():
    # If Clerk says they're signed in but their email isn't in our system,
    # send them to the not-registered page rather than an infinite login loop.
    from clerk_backend_api import Clerk
    secret_key = ""
    try:
        from flask import current_app
        secret_key = current_app.config.get("CLERK_SECRET_KEY", "")
    except Exception:
        pass

    if secret_key:
        try:
            from clerk_backend_api.jwks_helpers import AuthenticateRequestOptions
            sdk = Clerk(bearer_auth=secret_key)
            state = sdk.authenticate_request(request, AuthenticateRequestOptions())
            if state.is_signed_in:
                # Only show "not registered" if we're certain there's no lookup error
                if getattr(g, "_clerk_lookup_error", False):
                    return redirect(url_for("auth.login"))
                return redirect(url_for("auth.not_registered"))
        except Exception:
            pass

    return redirect(url_for("auth.login"))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return _redirect_login()
        g.user = user
        g.effective_user = _get_effective_user(user)
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return _redirect_login()
        if not user.is_admin_or_owner():
            return redirect(url_for("schedule.home"))
        g.user = user
        g.effective_user = _get_effective_user(user)
        return f(*args, **kwargs)
    return decorated


def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return _redirect_login()
        if user.role != "owner":
            return redirect(url_for("schedule.home"))
        g.user = user
        g.effective_user = _get_effective_user(user)
        return f(*args, **kwargs)
    return decorated
