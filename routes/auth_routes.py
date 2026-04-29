from flask import Blueprint, redirect, render_template, url_for, request, session, current_app
from auth_utils import get_current_user

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Show Clerk's sign-in widget. Redirects home if already signed in."""
    if get_current_user():
        return redirect(url_for("schedule.home"))

    # For local testing: allow email submission
    if current_app.config.get("LOCAL_TEST_MODE") and request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if email:
            session["_test_email"] = email
            return redirect(url_for("schedule.home"))

    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    """
    Clerk's JS calls signOut() which clears the session cookie.
    This route is the post-sign-out redirect target.
    """
    return redirect(url_for("auth.login"))


@auth_bp.route("/not-registered")
def not_registered():
    """Shown when a Clerk-authenticated user's email isn't in our system."""
    return render_template("not_registered.html")
