from datetime import date, timedelta

from flask import (
    Blueprint, flash, g, redirect, render_template,
    request, url_for,
)
from sqlalchemy import func

from models import db, User, RegularSchedule, ShiftAssignment, EmailProcessingLog
from auth_utils import admin_required, owner_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ── Dashboard ─────────────────────────────────────────────────────────────────

@admin_bp.route("/")
@admin_required
def dashboard():
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    stats = {
        "volunteers": User.query.filter(
            User.active.is_(True), User.role == "volunteer"
        ).count(),
        "admins": User.query.filter(
            User.active.is_(True), User.role == "admin"
        ).count(),
        "week_assignments": ShiftAssignment.query.filter(
            ShiftAssignment.date >= week_start,
            ShiftAssignment.date <= week_end,
        ).count(),
        "regular_entries": RegularSchedule.query.count(),
    }

    recent_email_logs = (
        EmailProcessingLog.query
        .order_by(EmailProcessingLog.processed_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        "admin_dashboard.html",
        stats=stats,
        week_start=week_start,
        recent_email_logs=recent_email_logs,
    )


# ── Volunteers ────────────────────────────────────────────────────────────────

@admin_bp.route("/volunteers")
@admin_required
def volunteers():
    all_users = (
        User.query
        .filter(User.active.is_(True), User.role.in_(["volunteer", "admin", "owner"]))
        .order_by(User.name)
        .all()
    )

    regular_counts = dict(
        db.session.query(RegularSchedule.user_id, func.count(RegularSchedule.id))
        .group_by(RegularSchedule.user_id)
        .all()
    )

    today = date.today()
    upcoming_counts = dict(
        db.session.query(ShiftAssignment.user_id, func.count(ShiftAssignment.id))
        .filter(
            ShiftAssignment.date >= today,
            ShiftAssignment.date <= today + timedelta(days=7),
        )
        .group_by(ShiftAssignment.user_id)
        .all()
    )

    return render_template(
        "admin_volunteers.html",
        volunteers=all_users,
        regular_counts=regular_counts,
        upcoming_counts=upcoming_counts,
    )


@admin_bp.route("/volunteers/add", methods=["POST"])
@admin_required
def add_volunteer():
    name  = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()
    role  = request.form.get("role", "volunteer").strip()

    if role not in ("volunteer", "admin") or (role == "admin" and g.user.role != "owner"):
        role = "volunteer"

    if not name or not email:
        flash("Name and email are required.", "error")
        return redirect(url_for("admin.volunteers"))

    existing = User.query.filter_by(email=email).first()
    if existing:
        if not existing.active:
            existing.active = True
            existing.name = name
            existing.phone = phone or existing.phone
            existing.role = role
            db.session.commit()
            flash(f"{name} has been re-activated.", "success")
        else:
            flash(f"A user with that email already exists ({existing.name}).", "error")
        return redirect(url_for("admin.volunteers"))

    db.session.add(User(name=name, email=email, phone=phone, role=role))
    db.session.commit()
    flash(f"{name} has been added as {role}.", "success")
    return redirect(url_for("admin.volunteers"))


@admin_bp.route("/volunteers/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_volunteer(user_id):
    user = User.query.get_or_404(user_id)

    if request.method == "POST":
        name  = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        role  = request.form.get("role", user.role).strip()

        if not name or not email:
            flash("Name and email are required.", "error")
            return render_template("admin_edit_volunteer.html", volunteer=user)

        clash = User.query.filter_by(email=email).first()
        if clash and clash.id != user_id:
            flash("That email is already used by another account.", "error")
            return render_template("admin_edit_volunteer.html", volunteer=user)

        if user.role != "owner" and role in ("volunteer", "admin") and g.user.role == "owner":
            user.role = role

        user.name  = name
        user.email = email
        user.phone = phone
        db.session.commit()
        flash(f"{name} has been updated.", "success")
        return redirect(url_for("admin.volunteers"))

    return render_template("admin_edit_volunteer.html", volunteer=user)


@admin_bp.route("/volunteers/<int:user_id>/deactivate", methods=["POST"])
@admin_required
def deactivate_volunteer(user_id):
    user = User.query.get_or_404(user_id)

    if user.role == "owner":
        flash("Cannot deactivate the owner account.", "error")
        return redirect(url_for("admin.volunteers"))

    if user.role == "admin" and g.user.role != "owner":
        flash("Only the owner can deactivate admin accounts.", "error")
        return redirect(url_for("admin.volunteers"))

    user.active = False
    db.session.commit()
    flash(f"{user.name} has been deactivated.", "success")
    return redirect(url_for("admin.volunteers"))


# ── Regular Schedule ──────────────────────────────────────────────────────────

@admin_bp.route("/regular")
@admin_required
def regular_schedule():
    volunteers = (
        User.query
        .filter(User.active.is_(True))
        .order_by(User.name)
        .all()
    )
    regular_set = {
        (rs.user_id, rs.day_of_week, rs.shift_type)
        for rs in RegularSchedule.query.all()
    }
    return render_template(
        "admin_regular.html",
        volunteers=volunteers,
        regular_set=regular_set,
        days=DAYS_OF_WEEK,
    )


@admin_bp.route("/regular/save", methods=["POST"])
@admin_required
def save_regular_schedule():
    active_ids = {u.id for u in User.query.filter_by(active=True).all()}

    new_set: set[tuple] = set()
    for key in request.form:
        if key.startswith("shift_"):
            parts = key.split("_", 3)
            if len(parts) == 4:
                try:
                    uid, dow, stype = int(parts[1]), int(parts[2]), parts[3]
                    if uid in active_ids and 0 <= dow <= 6 and stype in ("AM", "PM"):
                        new_set.add((uid, dow, stype))
                except (ValueError, IndexError):
                    pass

    current = {
        (rs.user_id, rs.day_of_week, rs.shift_type): rs
        for rs in RegularSchedule.query.all()
    }

    for key, rs in current.items():
        if key not in new_set:
            db.session.delete(rs)

    for key in new_set:
        if key not in current:
            db.session.add(RegularSchedule(user_id=key[0], day_of_week=key[1], shift_type=key[2]))

    db.session.commit()
    flash("Regular schedule saved.", "success")
    return redirect(url_for("admin.regular_schedule"))


# ── Admin Management (owner only) ─────────────────────────────────────────────

@admin_bp.route("/admins")
@owner_required
def manage_admins():
    admins = User.query.filter_by(active=True, role="admin").order_by(User.name).all()
    volunteers = User.query.filter_by(active=True, role="volunteer").order_by(User.name).all()
    return render_template("admin_admins.html", admins=admins, volunteers=volunteers)


@admin_bp.route("/admins/grant", methods=["POST"])
@owner_required
def grant_admin():
    user = User.query.get_or_404(int(request.form.get("user_id", 0)))
    if user.role in ("admin", "owner"):
        flash(f"{user.name} is already an admin or owner.", "warning")
    else:
        user.role = "admin"
        db.session.commit()
        flash(f"{user.name} has been granted admin access.", "success")
    return redirect(url_for("admin.manage_admins"))


@admin_bp.route("/admins/revoke", methods=["POST"])
@owner_required
def revoke_admin():
    user = User.query.get_or_404(int(request.form.get("user_id", 0)))
    if user.role == "owner":
        flash("Cannot revoke the owner.", "error")
    elif user.role != "admin":
        flash(f"{user.name} is not an admin.", "warning")
    else:
        user.role = "volunteer"
        db.session.commit()
        flash(f"{user.name} has been revoked admin access.", "success")
    return redirect(url_for("admin.manage_admins"))


# ── Email log ─────────────────────────────────────────────────────────────────

@admin_bp.route("/email-log")
@admin_required
def email_log():
    logs = (
        EmailProcessingLog.query
        .order_by(EmailProcessingLog.processed_at.desc())
        .limit(100)
        .all()
    )
    return render_template("admin_email_log.html", logs=logs)
