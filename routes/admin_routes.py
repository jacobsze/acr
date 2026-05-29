from datetime import date, timedelta

from flask import (
    Blueprint, flash, g, redirect, render_template,
    request, url_for, current_app,
)
from sqlalchemy import func

from models import db, User, RegularSchedule, ShiftAssignment, EmailProcessingLog, ScheduleChangeLog, AppSetting, Cat, CatLog
from auth_utils import login_required, admin_required, owner_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def normalize_phone(raw: str) -> tuple[str, str]:
    """Return (formatted, error). Strips non-digits; requires exactly 10."""
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return "", ""
    if len(digits) != 10:
        return "", "Phone number must be exactly 10 digits."
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}", ""


# ── Cat logs ──────────────────────────────────────────────────────────────────

@admin_bp.route("/analyze-cat-emails", methods=["GET"])
@owner_required
def analyze_cat_emails():
    """Analyze past emails to extract cat information and save to database.

    Query params:
      days_back=N  — how many days back to look (default 21)
      force_since=YYYY-MM-DD  — delete existing CatLogs on/after this date and re-analyze
    """
    from services.cat_analyzer import analyze_emails_for_cats
    from datetime import date as _date

    days_back = int(request.args.get("days_back", 21))
    force_since_raw = request.args.get("force_since", "").strip()
    force_since = None
    if force_since_raw:
        try:
            force_since = _date.fromisoformat(force_since_raw)
            # Make days_back wide enough to cover force_since
            days_since = (_date.today() - force_since).days + 1
            days_back = max(days_back, days_since)
        except ValueError:
            flash(f"Invalid force_since date: {force_since_raw}", "error")
            return redirect(url_for("admin.cats"))

    try:
        result = analyze_emails_for_cats(
            current_app._get_current_object(),
            days_back=days_back,
            force_since=force_since,
        )
        return render_template(
            "admin_cat_analysis.html",
            result=result,
            force_since=force_since,
        )
    except Exception as e:
        flash(f"Analysis failed: {str(e)}", "error")
        current_app.logger.exception("Cat email analysis failed: %s", str(e))
        return redirect(url_for("admin.cats"))


@admin_bp.route("/cats", methods=["GET"])
@admin_required
def cats():
    """Cat activity matrix: last 7 days × all cats."""
    today = date.today()
    days = [today - timedelta(days=i) for i in range(7)]

    all_cats = Cat.query.order_by(Cat.name).all()
    email_to_name = {u.email.lower(): u.name for u in User.query.filter_by(active=True).all()}

    logs = (
        CatLog.query
        .filter(CatLog.date.in_(days))
        .order_by(CatLog.date.desc(), CatLog.created_at.asc())
        .all()
    )

    # {(date, shift_type): {cat_id: {notes, bowel, food}}}
    from collections import defaultdict
    matrix = defaultdict(dict)
    volunteer_by_slot = {}
    for log in logs:
        shift = getattr(log, "shift_type", None) or "AM"
        key = (log.date, shift)
        if log.cat_id not in matrix[key]:
            matrix[key][log.cat_id] = {
                "notes": log.notes,
                "bowel": log.bowel_movement,
                "food": log.food_intake,
            }
        raw_vol = (log.volunteer_name or "").lower()
        if key not in volunteer_by_slot:
            volunteer_by_slot[key] = email_to_name.get(raw_vol, log.volunteer_name or "—")

    rows = []
    for d in days:
        for shift in ("PM", "AM"):   # PM first — afternoon is more recent
            key = (d, shift)
            cells = {}
            for cat in all_cats:
                cells[cat.id] = matrix[key].get(cat.id)
            rows.append({
                "date": d,
                "shift": shift,
                "volunteer": volunteer_by_slot.get(key, "—"),
                "cells": cells,
            })

    return render_template(
        "admin_cats.html",
        cats=all_cats,
        rows=rows,
    )


@admin_bp.route("/cats/<int:cat_id>", methods=["GET"])
@admin_required
def cat_detail(cat_id):
    """View detailed history for a specific cat."""
    cat = Cat.query.get_or_404(cat_id)
    email_to_name = {u.email.lower(): u.name for u in User.query.filter_by(active=True).all()}

    logs = (
        CatLog.query
        .filter_by(cat_id=cat_id)
        .order_by(CatLog.date.desc(), CatLog.created_at.asc())
        .all()
    )

    enriched = []
    for log in logs:
        raw_vol = (log.volunteer_name or "").lower()
        enriched.append({
            "log": log,
            "shift": getattr(log, "shift_type", None) or "—",
            "volunteer": email_to_name.get(raw_vol, log.volunteer_name or "—"),
            "bowel": log.bowel_movement,
            "food": log.food_intake,
        })

    return render_template(
        "admin_cat_detail.html",
        cat=cat,
        logs=enriched,
    )


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
        .order_by(func.coalesce(EmailProcessingLog.sent_at, EmailProcessingLog.processed_at).desc())
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
@login_required
def volunteers():
    all_users = (
        User.query
        .filter(User.active.is_(True), User.role.in_(["volunteer", "admin", "owner"]))
        .order_by(User.name)
        .all()
    )
    # Current user first, rest alphabetical
    all_users.sort(key=lambda u: (0 if u.id == g.user.id else 1, u.name))

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
            ShiftAssignment.date < today + timedelta(days=14),
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

    phone, phone_err = normalize_phone(phone)
    if phone_err:
        flash(phone_err, "error")
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
@login_required
def edit_volunteer(user_id):
    user = User.query.get_or_404(user_id)

    if not g.user.is_admin_or_owner() and g.user.id != user_id:
        flash("You can only edit your own profile.", "error")
        return redirect(url_for("admin.volunteers"))

    if request.method == "POST":
        name  = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        role  = request.form.get("role", user.role).strip()

        if not name or not email:
            flash("Name and email are required.", "error")
            return render_template("admin_edit_volunteer.html", volunteer=user)

        phone, phone_err = normalize_phone(phone)
        if phone_err:
            flash(phone_err, "error")
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

DAYS_DISPLAY = [
    (6, "Sun"), (0, "Mon"), (1, "Tue"), (2, "Wed"),
    (3, "Thu"), (4, "Fri"), (5, "Sat"),
]


@admin_bp.route("/bootstrap-schedule", methods=["POST"])
@owner_required
def bootstrap_schedule():
    """Bootstrap: generate 52 weeks of ShiftAssignments from RegularSchedule.

    Additive only — only fills in dates that don't have any assignments yet.
    Never overwrites existing ShiftAssignments (whether from RegularSchedule or manual edits).
    """
    from datetime import timedelta
    from services.schedule_cron import should_schedule_on_week

    try:
        today = date.today()
        end = today + timedelta(weeks=52)

        # Find all dates that already have ANY assignments
        existing_dates = set(
            row[0] for row in ShiftAssignment.query
            .filter(ShiftAssignment.date >= today, ShiftAssignment.date < end)
            .with_entities(ShiftAssignment.date)
            .distinct()
        )

        # Generate 52 weeks, skipping dates that already have assignments
        assignments = 0
        skipped = 0

        for week_offset in range(52):
            week_start = today + timedelta(weeks=week_offset)
            for day_offset in range(7):
                target_date = week_start + timedelta(days=day_offset)
                if target_date >= end:
                    break

                # Skip dates that already have assignments
                if target_date in existing_dates:
                    skipped += 1
                    continue

                dow = target_date.weekday()
                for shift_type in ("AM", "PM"):
                    reg_entries = (
                        RegularSchedule.query
                        .filter_by(day_of_week=dow, shift_type=shift_type)
                        .join(User)
                        .filter(User.active.is_(True))
                        .all()
                    )
                    for rs in reg_entries:
                        # Check if this date should be scheduled based on frequency
                        if not should_schedule_on_week(target_date, rs.frequency, rs.start_date):
                            continue

                        db.session.add(ShiftAssignment(
                            date=target_date,
                            shift_type=shift_type,
                            user_id=rs.user_id,
                            notes="Generated from regular schedule",
                        ))
                        assignments += 1

        db.session.commit()
        flash(
            f"✓ Bootstrap complete: generated {assignments} assignments, skipped {skipped} dates with existing assignments.",
            "success"
        )
        current_app.logger.info(
            "Bootstrap generated %d assignments, skipped %d dates with existing data",
            assignments, skipped
        )

    except Exception as exc:
        db.session.rollback()
        flash(f"Bootstrap failed: {exc}", "error")
        current_app.logger.exception("Bootstrap failed: %s", exc)

    return redirect(url_for("admin.regular_schedule"))


@admin_bp.route("/regular")
@login_required
def regular_schedule():
    from datetime import date as _date, timedelta

    volunteers = (
        User.query
        .filter(User.active.is_(True))
        .order_by(User.name)
        .all()
    )
    cap = current_app.config["MAX_VOLUNTEERS_PER_SHIFT"]

    all_regular = (
        RegularSchedule.query
        .join(User)
        .filter(User.active.is_(True))
        .all()
    )
    regular_by_slot: dict = {}
    for rs in all_regular:
        key = (rs.day_of_week, rs.shift_type)
        regular_by_slot.setdefault(key, []).append(rs)
    for key in regular_by_slot:
        regular_by_slot[key].sort(key=lambda rs: rs.user.name)

    # Calculate the next occurrence of each day_of_week for date display
    today = _date.today()
    next_dates = {}  # day_of_week -> [date for week 0, date for week 1]
    for dow in range(7):
        # Find the next occurrence of this day of week
        days_ahead = (dow - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # If today is that day, get next week
        next_occurrence = today + timedelta(days=days_ahead)
        week0_date = next_occurrence
        week1_date = next_occurrence + timedelta(weeks=1)
        next_dates[dow] = (week0_date, week1_date)

    return render_template(
        "admin_regular.html",
        volunteers=volunteers,
        regular_by_slot=regular_by_slot,
        days_display=DAYS_DISPLAY,
        cap=cap,
        is_admin=g.user.is_admin_or_owner(),
        next_dates=next_dates,
    )


@admin_bp.route("/regular/save", methods=["POST"])
@admin_required
def save_regular_schedule():
    from services.schedule_cron import handle_regular_schedule_change

    active_ids = {u.id for u in User.query.filter_by(active=True).all()}
    cap = current_app.config["MAX_VOLUNTEERS_PER_SHIFT"]

    # Calculate next occurrence of each day of week (for start_date calculation)
    today = date.today()
    next_dates = {}  # day_of_week -> (week0_date, week1_date)
    for dow in range(7):
        days_ahead = (dow - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        next_occurrence = today + timedelta(days=days_ahead)
        next_dates[dow] = (next_occurrence, next_occurrence + timedelta(weeks=1))

    # Build mapping: (dow, shift_type, slot_idx) -> (user_id, frequency, start_date)
    slot_config = {}
    for dow in range(7):
        for shift_type in ("AM", "PM"):
            for slot_idx in range(cap):
                val = request.form.get(f"spot_{dow}_{shift_type}_{slot_idx}", "").strip()
                freq = request.form.get(f"freq_{dow}_{shift_type}_{slot_idx}", "weekly")
                week = request.form.get(f"week_{dow}_{shift_type}_{slot_idx}", "0")
                if val:
                    try:
                        uid = int(val)
                        if uid in active_ids:
                            # Calculate actual start_date based on start_week selection
                            start_date = None
                            if freq == "every_other_week":
                                week_idx = int(week) if week in ("0", "1") else 0
                                start_date = next_dates[dow][week_idx]
                            slot_config[(dow, shift_type, slot_idx)] = {
                                "user_id": uid,
                                "frequency": freq,
                                "start_date": start_date,
                            }
                    except ValueError:
                        pass

    # Build new_set and mapping from (uid, dow, shift_type) -> (frequency, start_date)
    new_set: set[tuple] = set()
    freq_by_entry = {}  # (uid, dow, shift_type) -> (frequency, start_date)
    for (dow, shift_type, slot_idx), config in slot_config.items():
        uid = config["user_id"]
        key = (uid, dow, shift_type)
        new_set.add(key)
        # Use the first (lowest slot_idx) occurrence
        if key not in freq_by_entry:
            freq_by_entry[key] = (config["frequency"], config["start_date"])

    current = {
        (rs.user_id, rs.day_of_week, rs.shift_type): rs
        for rs in RegularSchedule.query.all()
    }

    user_map = {u.id: u.name for u in User.query.all()}

    for key, rs in current.items():
        if key not in new_set:
            # Removal: cascade to ShiftAssignments
            user_id, dow, shift_type = key
            db.session.delete(rs)
            db.session.add(ScheduleChangeLog(
                log_type="regular", day_of_week=dow, shift_type=shift_type,
                action="remove", volunteer_id=user_id,
                volunteer_name=user_map.get(user_id, str(user_id)),
                changed_by_id=g.user.id,
            ))
            # Cascade to ShiftAssignments (future only)
            handle_regular_schedule_change(
                current_app._get_current_object(),
                action="remove",
                user_id=user_id,
                day_of_week=dow,
                shift_type=shift_type,
            )

    new_entries = []
    for key in new_set:
        if key not in current:
            # Addition: update RegularSchedule and immediately generate assignments
            user_id, dow, shift_type = key
            freq, start_dt = freq_by_entry.get(key, ("weekly", None))
            db.session.add(RegularSchedule(
                user_id=user_id,
                day_of_week=dow,
                shift_type=shift_type,
                frequency=freq,
                start_date=start_dt,
            ))
            db.session.add(ScheduleChangeLog(
                log_type="regular", day_of_week=dow, shift_type=shift_type,
                action="add", volunteer_id=user_id,
                volunteer_name=user_map.get(user_id, str(user_id)),
                changed_by_id=g.user.id,
            ))
            new_entries.append((user_id, dow, shift_type, freq, start_dt))

    db.session.commit()

    # Immediately generate assignments for newly added volunteers
    for user_id, dow, shift_type, freq, start_dt in new_entries:
        handle_regular_schedule_change(
            current_app._get_current_object(),
            action="add",
            user_id=user_id,
            day_of_week=dow,
            shift_type=shift_type,
            frequency=freq,
            start_date=start_dt,
        )

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


# ── Change log ────────────────────────────────────────────────────────────────

@admin_bp.route("/change-log")
@admin_required
def change_log():
    logs = (
        ScheduleChangeLog.query
        .order_by(ScheduleChangeLog.changed_at.desc())
        .limit(300)
        .all()
    )
    return render_template("admin_change_log.html", logs=logs)


# ── Weekly schedule email ─────────────────────────────────────────────────────

@admin_bp.route("/send-weekly-email", methods=["POST"])
@admin_required
def send_weekly_email():
    try:
        from services.weekly_email import send_weekly_schedule_email
        result = send_weekly_schedule_email(current_app._get_current_object())
        flash(f"Weekly schedule email sent to {result['recipient']}.", "success")
    except Exception as exc:
        flash(f"Failed to send weekly email: {exc}", "error")
    return redirect(request.referrer or url_for("schedule.week_view",
                                                 week_start=date.today().isoformat()))


@admin_bp.route("/test-open-shift-email", methods=["POST"])
@admin_required
def test_open_shift_email():
    scenario = request.form.get("scenario", "both")
    open_shifts = {"am": ["AM"], "pm": ["PM"], "both": ["AM", "PM"]}.get(scenario, ["AM", "PM"])
    try:
        from services.weekly_email import send_open_shift_alert
        from zoneinfo import ZoneInfo
        from datetime import datetime
        ny_now = datetime.now(ZoneInfo("America/New_York"))
        target_date = ny_now.date() + timedelta(days=2)
        result = send_open_shift_alert(current_app._get_current_object(), target_date, open_shifts)
        flash(f"Test open-shift email sent to {result['recipient']} — {result['subject']}", "success")
    except Exception as exc:
        flash(f"Failed to send test email: {exc}", "error")
    return redirect(request.referrer or url_for("schedule.week_view",
                                                 week_start=date.today().isoformat()))


# ── Email log ─────────────────────────────────────────────────────────────────

@admin_bp.route("/email-log/check", methods=["POST"])
@admin_required
def check_email_now():
    try:
        from services.gmail_monitor import check_and_process
        check_and_process(current_app._get_current_object())
        flash("Email check complete.", "success")
    except Exception as exc:
        flash(f"Email check failed: {exc}", "error")
    return redirect(url_for("admin.email_log"))


@admin_bp.route("/email-log/<int:log_id>/reprocess", methods=["POST"])
@owner_required
def reprocess_email(log_id):
    try:
        from services.gmail_monitor import reprocess_message
        ignore_reg = request.form.get("ignore_registration") == "1"
        reprocess_message(current_app._get_current_object(), log_id, ignore_registration=ignore_reg)
        flash("Email re-processed." + (" (sender registration ignored)" if ignore_reg else ""), "success")
    except Exception as exc:
        flash(f"Reprocess failed: {exc}", "error")
    return redirect(url_for("admin.email_log"))


@admin_bp.route("/email-log")
@admin_required
def email_log():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from sqlalchemy import func
    NY = ZoneInfo("America/New_York")

    logs = (
        EmailProcessingLog.query
        .order_by(func.coalesce(EmailProcessingLog.sent_at, EmailProcessingLog.processed_at).desc())
        .limit(100)
        .all()
    )

    last_check = None
    setting = AppSetting.query.get("last_email_check")
    if setting:
        try:
            last_check = datetime.fromisoformat(setting.value).replace(tzinfo=timezone.utc).astimezone(NY)
        except ValueError:
            pass

    next_check = None
    if last_check:
        from datetime import timedelta
        next_check = last_check + timedelta(minutes=current_app.config.get("GMAIL_CHECK_INTERVAL_MINUTES", 5))

    return render_template(
        "admin_email_log.html",
        logs=logs,
        last_check=last_check,
        next_check=next_check,
        check_interval=current_app.config.get("GMAIL_CHECK_INTERVAL_MINUTES", 5),
    )


# ── AI Settings ───────────────────────────────────────────────────────────────

@admin_bp.route("/settings", methods=["GET", "POST"])
@owner_required
def ai_settings():
    from services.llm_parser import DEFAULT_INSTRUCTIONS

    if request.method == "POST":
        new_value = request.form.get("llm_instructions", "").strip()
        setting = AppSetting.query.get("llm_instructions")
        if setting:
            setting.value = new_value
        else:
            db.session.add(AppSetting(key="llm_instructions", value=new_value))
        db.session.commit()
        flash("AI instructions saved.", "success")
        return redirect(url_for("admin.ai_settings"))

    setting = AppSetting.query.get("llm_instructions")
    current_value = setting.value if setting else DEFAULT_INSTRUCTIONS
    return render_template(
        "admin_settings.html",
        current_value=current_value,
        default_value=DEFAULT_INSTRUCTIONS,
    )
