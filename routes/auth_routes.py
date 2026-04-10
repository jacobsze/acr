import random
import string
from datetime import datetime, timedelta

from flask import (
    Blueprint, flash, g, make_response, redirect,
    render_template, request, url_for, current_app,
)

from models import db, User, OTPToken
from auth_utils import (
    get_current_user, set_session_cookie,
    generate_session_token, COOKIE_NAME,
)
from services.email_sender import send_otp_email

auth_bp = Blueprint("auth", __name__)


def _generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if get_current_user():
        return redirect(url_for("schedule.index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email:
            flash("Please enter your email address.", "error")
            return render_template("login.html")

        user = User.query.filter_by(email=email, active=True).first()
        if not user:
            flash(
                "That email is not registered. Please contact the administrator.",
                "error",
            )
            return render_template("login.html")

        otp = _generate_otp()
        expires_at = datetime.utcnow() + timedelta(
            minutes=current_app.config["OTP_EXPIRY_MINUTES"]
        )
        db.session.add(OTPToken(email=email, token=otp, expires_at=expires_at))
        db.session.commit()

        try:
            send_otp_email(email, otp, user.name)
        except Exception as exc:
            current_app.logger.error("Failed to send OTP to %s: %s", email, exc)
            flash(
                "Could not send the verification email. Please try again or contact the administrator.",
                "error",
            )
            return render_template("login.html")

        return redirect(url_for("auth.verify", email=email))

    return render_template("login.html")


@auth_bp.route("/verify", methods=["GET", "POST"])
def verify():
    email = (request.args.get("email") or request.form.get("email", "")).strip().lower()
    if not email:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        otp = request.form.get("otp", "").strip()
        now = datetime.utcnow()

        otp_record = (
            OTPToken.query
            .filter_by(email=email, token=otp, used=False)
            .filter(OTPToken.expires_at > now)
            .order_by(OTPToken.created_at.desc())
            .first()
        )

        if not otp_record:
            flash("Invalid or expired code. Please try again.", "error")
            return render_template("otp_verify.html", email=email)

        otp_record.used = True

        user = User.query.filter_by(email=email, active=True).first()
        if not user:
            flash("Account not found. Please contact the administrator.", "error")
            return redirect(url_for("auth.login"))

        token = generate_session_token()
        user.session_token = token
        db.session.commit()

        response = make_response(redirect(url_for("schedule.index")))
        set_session_cookie(response, token)
        return response

    return render_template("otp_verify.html", email=email)


@auth_bp.route("/resend-otp", methods=["POST"])
def resend_otp():
    email = request.form.get("email", "").strip().lower()
    if not email:
        return redirect(url_for("auth.login"))

    user = User.query.filter_by(email=email, active=True).first()
    if not user:
        return redirect(url_for("auth.login"))

    otp = _generate_otp()
    expires_at = datetime.utcnow() + timedelta(
        minutes=current_app.config["OTP_EXPIRY_MINUTES"]
    )
    db.session.add(OTPToken(email=email, token=otp, expires_at=expires_at))
    db.session.commit()

    try:
        send_otp_email(email, otp, user.name)
        flash("A new code has been sent to your email.", "success")
    except Exception as exc:
        current_app.logger.error("Failed to resend OTP to %s: %s", email, exc)
        flash("Failed to send email. Please try again.", "error")

    return redirect(url_for("auth.verify", email=email))


@auth_bp.route("/logout")
def logout():
    user = get_current_user()
    if user:
        user.session_token = None
        db.session.commit()

    response = make_response(redirect(url_for("auth.login")))
    response.delete_cookie(COOKIE_NAME)
    return response
