from datetime import date, timedelta

from flask import (
    Blueprint, flash, g, redirect, render_template,
    request, url_for, current_app,
)

from models import db, User, RegularSchedule, ShiftAssignment
from auth_utils import login_required, get_current_user

schedule_bp = Blueprint("schedule", __name__)

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ── helpers ───────────────────────────────────────────────────────────────────

def get_week_start(for_date: date | None = None) -> date:
    """Return the Monday that starts the week containing *for_date*."""
    if for_date is None:
        for_date = date.today()
    return for_date - timedelta(days=for_date.weekday())


def get_week_dates(week_start: date) -> list[date]:
    return [week_start + timedelta(days=i) for i in range(7)]


def materialize_if_needed(target_date: date, shift_type: str) -> None:
    """
    If no ShiftAssignment rows exist for (target_date, shift_type), copy the
    regular schedule for that day-of-week into concrete assignments so we have
    a baseline to modify.
    """
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


def build_schedule(week_dates: list[date], current_user: User | None) -> dict:
    """
    Returns::

        {
            date: {
                "AM": {
                    "volunteers": [User, ...],
                    "count": int,
                    "is_full": bool,
                    "is_tentative": bool,   # True = shown from regular schedule
                    "user_assigned": bool,
                },
                "PM": { ... }
            },
            ...
        }
    """
    # Fetch concrete assignments for the whole week in one query
    week_assignments = (
        ShiftAssignment.query
        .filter(ShiftAssignment.date.in_(week_dates))
        .join(User, ShiftAssignment.user_id == User.id)
        .all()
    )

    # Group by (date, shift_type)
    actual: dict[tuple, list] = {}
    for a in week_assignments:
        key = (a.date, a.shift_type)
        actual.setdefault(key, []).append(a.user)

    materialized_keys = set(actual.keys())

    # Fetch regular schedule for lookup
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
                volunteers = actual[key]
                is_tentative = False
            else:
                volunteers = regular_by_dow.get((dow, shift_type), [])
                is_tentative = True

            schedule[d][shift_type] = {
                "volunteers": volunteers,
                "count": len(volunteers),
                "is_full": len(volunteers) >= cap,
                "is_tentative": is_tentative,
                "user_assigned": current_user is not None and any(
                    v.id == current_user.id for v in volunteers
                ),
            }

    return schedule


# ── routes ────────────────────────────────────────────────────────────────────

@schedule_bp.route("/")
def index():
    user = get_current_user()
    if not user:
        return redirect(url_for("auth.login"))
    ws = get_week_start()
    return redirect(url_for("schedule.week_view", week_start=ws.isoformat()))


@schedule_bp.route("/schedule/<week_start>")
@login_required
def week_view(week_start: str):
    try:
        ws = get_week_start(date.fromisoformat(week_start))
    except ValueError:
        ws = get_week_start()
        return redirect(url_for("schedule.week_view", week_start=ws.isoformat()))

    week_dates = get_week_dates(ws)
    schedule = build_schedule(week_dates, g.user)

    all_volunteers = (
        User.query.filter_by(active=True).order_by(User.name).all()
        if g.user.is_admin_or_owner()
        else []
    )

    today = date.today()
    return render_template(
        "schedule_week.html",
        week_start=ws,
        week_end=ws + timedelta(days=6),
        week_dates=week_dates,
        day_names=DAY_NAMES,
        schedule=schedule,
        prev_week=(ws - timedelta(weeks=1)).isoformat(),
        next_week=(ws + timedelta(weeks=1)).isoformat(),
        current_week_start=get_week_start(today).isoformat(),
        all_volunteers=all_volunteers,
        today=today,
    )


@schedule_bp.route("/schedule/assign", methods=["POST"])
@login_required
def assign():
    date_str = request.form.get("date", "")
    shift_type = request.form.get("shift_type", "")
    user_id_str = request.form.get("user_id", "")

    # Admins may assign any volunteer; volunteers can only add themselves
    if user_id_str and g.user.is_admin_or_owner():
        target = User.query.get(int(user_id_str))
        if not target:
            flash("Volunteer not found.", "error")
            return redirect(request.referrer or url_for("schedule.index"))
    else:
        target = g.user

    try:
        target_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        flash("Invalid date.", "error")
        return redirect(request.referrer or url_for("schedule.index"))

    if shift_type not in ("AM", "PM"):
        flash("Invalid shift type.", "error")
        return redirect(request.referrer or url_for("schedule.index"))

    materialize_if_needed(target_date, shift_type)

    if ShiftAssignment.query.filter_by(
        date=target_date, shift_type=shift_type, user_id=target.id
    ).first():
        flash(f"{target.name} is already on that shift.", "warning")
    elif (
        ShiftAssignment.query.filter_by(date=target_date, shift_type=shift_type).count()
        >= current_app.config["MAX_VOLUNTEERS_PER_SHIFT"]
    ):
        flash("That shift is already full (3 volunteers).", "error")
    else:
        db.session.add(
            ShiftAssignment(
                date=target_date,
                shift_type=shift_type,
                user_id=target.id,
                created_by_id=g.user.id,
            )
        )
        db.session.commit()
        flash(
            f"{target.name} added to the {shift_type} shift on "
            f"{target_date.strftime('%A, %B %-d')}.",
            "success",
        )

    ws = get_week_start(target_date)
    return redirect(url_for("schedule.week_view", week_start=ws.isoformat()))


@schedule_bp.route("/schedule/unassign", methods=["POST"])
@login_required
def unassign():
    date_str = request.form.get("date", "")
    shift_type = request.form.get("shift_type", "")
    user_id_str = request.form.get("user_id", "")

    if user_id_str and g.user.is_admin_or_owner():
        target_user_id = int(user_id_str)
    else:
        target_user_id = g.user.id

    try:
        target_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        flash("Invalid date.", "error")
        return redirect(request.referrer or url_for("schedule.index"))

    if shift_type not in ("AM", "PM"):
        flash("Invalid shift type.", "error")
        return redirect(request.referrer or url_for("schedule.index"))

    # Materialize first so the row actually exists to delete
    materialize_if_needed(target_date, shift_type)

    assignment = ShiftAssignment.query.filter_by(
        date=target_date, shift_type=shift_type, user_id=target_user_id
    ).first()

    if not assignment:
        flash("Assignment not found.", "warning")
    else:
        name = assignment.user.name
        db.session.delete(assignment)
        db.session.commit()
        flash(
            f"{name} removed from the {shift_type} shift on "
            f"{target_date.strftime('%A, %B %-d')}.",
            "success",
        )

    ws = get_week_start(target_date)
    return redirect(url_for("schedule.week_view", week_start=ws.isoformat()))
