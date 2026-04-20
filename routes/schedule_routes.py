from datetime import date, timedelta
import json

from flask import (
    Blueprint, flash, g, redirect, render_template,
    request, url_for, current_app,
)

from models import db, User, RegularSchedule, ShiftAssignment, ScheduleChangeLog
from auth_utils import login_required, get_current_user

schedule_bp = Blueprint("schedule", __name__)

DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def get_week_start(for_date: date | None = None) -> date:
    """Return the Sunday that starts the week containing for_date."""
    if for_date is None:
        for_date = date.today()
    # weekday(): Mon=0 … Sun=6; (weekday+1)%7 = days since last Sunday
    return for_date - timedelta(days=(for_date.weekday() + 1) % 7)


def get_week_dates(week_start: date) -> list[date]:
    return [week_start + timedelta(days=i) for i in range(7)]


def materialize_if_needed(target_date: date, shift_type: str) -> None:
    existing = ShiftAssignment.query.filter_by(
        date=target_date, shift_type=shift_type
    ).count()
    if existing:
        return

    dow = target_date.weekday()
    regular_entries = (
        RegularSchedule.query
        .filter_by(day_of_week=dow, shift_type=shift_type)
        .join(User)
        .filter(User.active.is_(True))
        .all()
    )
    for rs in regular_entries:
        db.session.add(
            ShiftAssignment(
                date=target_date,
                shift_type=shift_type,
                user_id=rs.user_id,
                notes="Copied from regular schedule",
            )
        )
    if regular_entries:
        db.session.commit()


def build_schedule(week_dates: list[date], effective_user: User | None) -> dict:
    week_assignments = (
        ShiftAssignment.query
        .filter(ShiftAssignment.date.in_(week_dates))
        .join(User, ShiftAssignment.user_id == User.id)
        .all()
    )

    actual: dict[tuple, list] = {}
    for a in week_assignments:
        key = (a.date, a.shift_type)
        actual.setdefault(key, []).append(a.user)

    materialized_keys = set(actual.keys())

    regular = (
        RegularSchedule.query
        .join(User)
        .filter(User.active.is_(True))
        .all()
    )
    regular_by_dow: dict[tuple, list] = {}
    for rs in regular:
        key = (rs.day_of_week, rs.shift_type)
        regular_by_dow.setdefault(key, []).append(rs.user)

    cap = current_app.config["MAX_VOLUNTEERS_PER_SHIFT"]
    schedule = {}
    for d in week_dates:
        schedule[d] = {}
        dow = d.weekday()
        for shift_type in ("AM", "PM"):
            key = (d, shift_type)
            if key in materialized_keys:
                volunteers = sorted(actual[key], key=lambda u: u.name)
                is_tentative = False
            else:
                volunteers = sorted(regular_by_dow.get((dow, shift_type), []), key=lambda u: u.name)
                is_tentative = True

            schedule[d][shift_type] = {
                "volunteers": volunteers,
                "count": len(volunteers),
                "is_full": len(volunteers) >= cap,
                "is_tentative": is_tentative,
                "user_assigned": effective_user is not None and any(
                    v.id == effective_user.id for v in volunteers
                ),
            }

    return schedule


# ── routes ────────────────────────────────────────────────────────────────────

@schedule_bp.route("/")
@login_required
def home():
    today = date.today()
    end = today + timedelta(days=27)

    all_assignments = (
        ShiftAssignment.query
        .filter(ShiftAssignment.date >= today, ShiftAssignment.date <= end)
        .join(User, ShiftAssignment.user_id == User.id)
        .all()
    )
    shift_map: dict[tuple, list] = {}
    for a in all_assignments:
        shift_map.setdefault((a.date, a.shift_type), []).append(a.user)

    all_regular = RegularSchedule.query.join(User).filter(User.active.is_(True)).all()
    regular_by_dow: dict[tuple, list] = {}
    for rs in all_regular:
        regular_by_dow.setdefault((rs.day_of_week, rs.shift_type), []).append(rs.user)

    days = []
    for i in range(28):
        d = today + timedelta(days=i)
        my_shifts = []
        for st in ["AM", "PM"]:
            key = (d, st)
            if key in shift_map:
                vols = sorted(shift_map[key], key=lambda u: u.name)
            else:
                vols = sorted(regular_by_dow.get((d.weekday(), st), []), key=lambda u: u.name)
            if any(v.id == g.user.id for v in vols):
                my_shifts.append({
                    "shift_type": st,
                    "others": [v for v in vols if v.id != g.user.id],
                })
        if my_shifts:
            days.append({"date": d, "shifts": my_shifts})

    return render_template("home.html", days=days, today=today)


@schedule_bp.route("/shifts/add", methods=["POST"])
@login_required
def add_to_shift():
    try:
        d = date.fromisoformat(request.form["date"])
        shift_type = request.form["shift_type"]
    except (KeyError, ValueError):
        flash("Invalid request.", "error")
        return redirect(url_for("schedule.home"))

    if d < date.today():
        flash("Cannot add to a past shift.", "error")
        return redirect(url_for("schedule.home"))

    materialize_if_needed(d, shift_type)

    if ShiftAssignment.query.filter_by(date=d, shift_type=shift_type, user_id=g.user.id).first():
        flash("You're already on that shift.", "info")
        return redirect(url_for("schedule.home"))

    if ShiftAssignment.query.filter_by(date=d, shift_type=shift_type).count() >= current_app.config["MAX_VOLUNTEERS_PER_SHIFT"]:
        flash("That shift is full.", "error")
        return redirect(url_for("schedule.home"))

    db.session.add(ShiftAssignment(date=d, shift_type=shift_type, user_id=g.user.id, created_by_id=g.user.id))
    db.session.add(ScheduleChangeLog(
        log_type="upcoming", date=d, shift_type=shift_type,
        action="add", volunteer_id=g.user.id, volunteer_name=g.user.name,
        changed_by_id=g.user.id,
    ))
    db.session.commit()
    flash(f"Added you to {shift_type} on {d.strftime('%b %-d')}.", "success")
    return redirect(url_for("schedule.home"))


@schedule_bp.route("/shifts/remove", methods=["POST"])
@login_required
def remove_from_shift():
    try:
        d = date.fromisoformat(request.form["date"])
        shift_type = request.form["shift_type"]
    except (KeyError, ValueError):
        flash("Invalid request.", "error")
        return redirect(url_for("schedule.home"))

    materialize_if_needed(d, shift_type)

    assignment = ShiftAssignment.query.filter_by(date=d, shift_type=shift_type, user_id=g.user.id).first()
    if not assignment:
        flash("You're not on that shift.", "info")
        return redirect(url_for("schedule.home"))

    db.session.delete(assignment)
    db.session.add(ScheduleChangeLog(
        log_type="upcoming", date=d, shift_type=shift_type,
        action="remove", volunteer_id=g.user.id, volunteer_name=g.user.name,
        changed_by_id=g.user.id,
    ))
    db.session.commit()
    flash(f"Removed you from {shift_type} on {d.strftime('%b %-d')}.", "success")
    return redirect(url_for("schedule.home"))


@schedule_bp.route("/schedule/<week_start>")
@login_required
def week_view(week_start: str):
    try:
        ws = get_week_start(date.fromisoformat(week_start))
    except ValueError:
        ws = get_week_start()
        return redirect(url_for("schedule.week_view", week_start=ws.isoformat()))

    # "View as" — owner only
    view_as_user = None
    view_as_id = request.args.get("view_as", type=int)
    if g.user.role == "owner" and view_as_id:
        view_as_user = User.query.filter_by(id=view_as_id, active=True).first()

    effective_user = view_as_user or g.user
    is_admin_mode = g.user.is_admin_or_owner() and view_as_user is None

    # Build 2 weeks
    today = date.today()
    all_weeks = []
    for i in range(1):
        ws_i = ws + timedelta(weeks=i)
        week_dates_i = get_week_dates(ws_i)
        all_weeks.append({
            "week_start": ws_i,
            "week_end": ws_i + timedelta(days=6),
            "week_dates": week_dates_i,
            "schedule": build_schedule(week_dates_i, effective_user),
        })

    all_volunteers = (
        User.query.filter_by(active=True).order_by(User.name).all()
        if is_admin_mode
        else []
    )
    all_active_users = User.query.filter_by(active=True).order_by(User.name).all()

    return render_template(
        "schedule_week.html",
        all_weeks=all_weeks,
        week_start=ws,
        day_names=DAY_NAMES,
        prev_week=(ws - timedelta(weeks=1)).isoformat(),
        next_week=(ws + timedelta(weeks=1)).isoformat(),
        current_week_start=get_week_start(today).isoformat(),
        all_volunteers=all_volunteers,
        all_active_users=all_active_users,
        today=today,
        cap=current_app.config["MAX_VOLUNTEERS_PER_SHIFT"],
        view_as_user=view_as_user,
        effective_user=effective_user,
        is_admin_mode=is_admin_mode,
    )


@schedule_bp.route("/schedule/bulk_save", methods=["POST"])
@login_required
def bulk_save():
    try:
        changes = json.loads(request.form.get("changes_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        flash("Invalid save data.", "error")
        return redirect(request.referrer or url_for("schedule.home"))

    adds = changes.get("adds", [])
    removes = changes.get("removes", [])
    week_start_str = request.form.get("week_start", "")
    view_as_id = request.form.get("view_as_id", type=int)

    is_admin = g.user.is_admin_or_owner()
    effective_user = g.user

    if view_as_id and g.user.role == "owner":
        view_as_user = User.query.filter_by(id=view_as_id, active=True).first()
        if view_as_user:
            effective_user = view_as_user
            is_admin = False  # Owner in "view as" mode acts as that volunteer

    errors = []
    successes = 0

    for r in removes:
        try:
            d = date.fromisoformat(r["date"])
            shift_type = r["shift_type"]
            user_id = int(r["user_id"])
        except (KeyError, ValueError):
            continue

        if not is_admin and user_id != effective_user.id:
            errors.append("You can only remove yourself from shifts.")
            continue

        materialize_if_needed(d, shift_type)

        assignment = ShiftAssignment.query.filter_by(
            date=d, shift_type=shift_type, user_id=user_id
        ).first()
        if assignment:
            db.session.delete(assignment)
            vol = User.query.get(user_id)
            db.session.add(ScheduleChangeLog(
                log_type="upcoming", date=d, shift_type=shift_type,
                action="remove", volunteer_id=user_id,
                volunteer_name=vol.name if vol else str(user_id),
                changed_by_id=g.user.id,
            ))
            successes += 1

    for a in adds:
        try:
            d = date.fromisoformat(a["date"])
            shift_type = a["shift_type"]
            user_id = int(a["user_id"])
        except (KeyError, ValueError):
            continue

        if not is_admin and user_id != effective_user.id:
            errors.append("You can only add yourself to shifts.")
            continue

        target = User.query.get(user_id)
        if not target:
            continue

        materialize_if_needed(d, shift_type)

        if ShiftAssignment.query.filter_by(date=d, shift_type=shift_type, user_id=user_id).first():
            continue

        count = ShiftAssignment.query.filter_by(date=d, shift_type=shift_type).count()
        if count >= current_app.config["MAX_VOLUNTEERS_PER_SHIFT"]:
            errors.append(f"Shift on {d.strftime('%b %-d')} {shift_type} is full.")
            continue

        target = User.query.get(user_id)
        db.session.add(ShiftAssignment(
            date=d, shift_type=shift_type,
            user_id=user_id, created_by_id=g.user.id,
        ))
        db.session.add(ScheduleChangeLog(
            log_type="upcoming", date=d, shift_type=shift_type,
            action="add", volunteer_id=user_id,
            volunteer_name=target.name if target else str(user_id),
            changed_by_id=g.user.id,
        ))
        successes += 1

    db.session.commit()

    if successes:
        flash(f"Schedule updated ({successes} change{'s' if successes != 1 else ''}).", "success")
    for e in errors[:3]:
        flash(e, "error")

    try:
        ws = date.fromisoformat(week_start_str)
    except ValueError:
        ws = get_week_start()

    redirect_url = url_for("schedule.week_view", week_start=ws.isoformat())
    if view_as_id:
        redirect_url += f"?view_as={view_as_id}"
    return redirect(redirect_url)
