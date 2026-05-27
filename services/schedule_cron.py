"""Sunday cron job to extend the 52-week rolling schedule."""
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def extend_52week_schedule(app):
    """
    Generate the next week of ShiftAssignments to maintain ~52 weeks of future schedule.

    Called weekly (Sundays). Finds the last date in the 52-week window and generates
    the next week (7 days) based on current RegularSchedule.
    """
    from models import db, RegularSchedule, ShiftAssignment, User

    with app.app_context():
        today = date.today()

        # Find the furthest date currently scheduled
        last_assignment = (
            ShiftAssignment.query
            .filter(ShiftAssignment.date >= today)
            .order_by(ShiftAssignment.date.desc())
            .first()
        )

        if last_assignment:
            next_week_start = last_assignment.date + timedelta(days=1)
            # Round to nearest Sunday if needed
            days_until_sunday = (6 - next_week_start.weekday()) % 7
            if days_until_sunday > 0:
                next_week_start = next_week_start + timedelta(days=days_until_sunday)
        else:
            # No assignments exist yet — start from today
            next_week_start = today

        next_week_end = next_week_start + timedelta(days=6)

        app.logger.info(
            "[SCHEDULE_CRON] Extending 52-week window: %s → %s",
            next_week_start, next_week_end
        )

        # Check if this week is already generated
        existing_count = ShiftAssignment.query.filter(
            ShiftAssignment.date >= next_week_start,
            ShiftAssignment.date <= next_week_end,
        ).count()

        if existing_count > 0:
            app.logger.info(
                "[SCHEDULE_CRON] Week already generated (%d assignments). Skipping.",
                existing_count
            )
            return {"status": "skipped", "reason": "week_already_generated"}

        # Generate the week
        assignments_added = 0

        for day_offset in range(7):
            target_date = next_week_start + timedelta(days=day_offset)
            dow = target_date.weekday()  # 0=Monday … 6=Sunday

            for shift_type in ("AM", "PM"):
                # Get all RegularSchedule entries for this day/shift
                reg_entries = (
                    RegularSchedule.query
                    .filter_by(day_of_week=dow, shift_type=shift_type)
                    .join(User)
                    .filter(User.active.is_(True))
                    .all()
                )

                for rs in reg_entries:
                    db.session.add(ShiftAssignment(
                        date=target_date,
                        shift_type=shift_type,
                        user_id=rs.user_id,
                        notes="Generated from regular schedule",
                    ))
                    assignments_added += 1

        db.session.commit()

        app.logger.info(
            "[SCHEDULE_CRON] ✓ Generated %d assignments for week %s–%s",
            assignments_added, next_week_start, next_week_end
        )

        return {
            "status": "success",
            "week_start": str(next_week_start),
            "week_end": str(next_week_end),
            "assignments_added": assignments_added,
        }


def handle_regular_schedule_change(app, action: str, user_id: int, day_of_week: int, shift_type: str):
    """
    When RegularSchedule is edited, cascade changes to all future ShiftAssignments.

    Args:
        app: Flask app
        action: 'add' or 'remove'
        user_id: the volunteer affected
        day_of_week: 0=Monday … 6=Sunday
        shift_type: 'AM' or 'PM'
    """
    from models import db, ShiftAssignment

    with app.app_context():
        today = date.today()

        # Find all ShiftAssignments matching this pattern in the future
        assignments = ShiftAssignment.query.filter(
            ShiftAssignment.date >= today,
            ShiftAssignment.user_id == user_id,
            ShiftAssignment.shift_type == shift_type,
        ).all()

        # Filter to only those on the matching day of week
        matching = [
            a for a in assignments
            if a.date.weekday() == day_of_week
        ]

        if action == "remove":
            # Delete all matching assignments
            for assign in matching:
                db.session.delete(assign)
                app.logger.info(
                    "[REGULAR_SCHEDULE] Removed user %d from %s (date %s, from pattern %s %s)",
                    user_id, assign.date, day_of_week, day_of_week, shift_type
                )
            db.session.commit()
            return {"status": "success", "removed": len(matching)}

        elif action == "add":
            # This is handled by bootstrap/cron; RegularSchedule.add just updates the template
            app.logger.info(
                "[REGULAR_SCHEDULE] Added user %d to pattern %s %s (will be generated by cron)",
                user_id, day_of_week, shift_type
            )
            return {"status": "queued", "note": "will be generated by Sunday cron"}

        return {"status": "unknown"}
