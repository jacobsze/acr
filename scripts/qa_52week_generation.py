#!/usr/bin/env python3
"""
QA script: verify that 52-week generation would account for all recent changes.

Usage:
  python scripts/qa_52week_generation.py [days_back] [weeks_to_generate]

  days_back: how many days back to review changes (default 30)
  weeks_to_generate: simulate generating N weeks into future (default 52)
"""
import sys
import os
from datetime import date, timedelta

# Add app to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from models import db, ScheduleChangeLog, RegularSchedule, ShiftAssignment, User, EmailProcessingLog

def print_section(title):
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80)

def format_dow(dow: int) -> str:
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dow]

def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    weeks_to_gen = int(sys.argv[2]) if len(sys.argv) > 2 else 52

    app = create_app()
    with app.app_context():
        today = date.today()
        cutoff_date = today - timedelta(days=days_back)

        # ── Recent Changes ─────────────────────────────────────────────────────
        print_section("RECENT SCHEDULE CHANGES (past 30 days)")

        changes = (
            ScheduleChangeLog.query
            .filter(ScheduleChangeLog.changed_at >= cutoff_date)
            .order_by(ScheduleChangeLog.changed_at.desc())
            .all()
        )

        if changes:
            print(f"  Total changes: {len(changes)}\n")
            for c in changes:
                vol = c.volunteer_name or (c.volunteer.name if c.volunteer else "?")
                if c.log_type == "upcoming":
                    icon = "✓" if c.action == "add" else "✗"
                    print(f"    [{icon}] {c.changed_at.strftime('%m/%d %H:%M')} | {vol:20} | {c.date} {c.shift_type} ({c.action})")
                else:
                    dow = format_dow(c.day_of_week)
                    icon = "✓" if c.action == "add" else "✗"
                    print(f"    [{icon}] {c.changed_at.strftime('%m/%d %H:%M')} | {vol:20} | {dow} {c.shift_type} (recurring, {c.action})")
        else:
            print("  (no changes in past 30 days)")

        # ── Current RegularSchedule ────────────────────────────────────────────
        print_section("CURRENT REGULAR SCHEDULE TEMPLATE")

        regulars = RegularSchedule.query.all()
        if regulars:
            by_vol = {}
            for rs in regulars:
                user = User.query.get(rs.user_id)
                if user:
                    if user.id not in by_vol:
                        by_vol[user.id] = {"name": user.name, "shifts": []}
                    by_vol[user.id]["shifts"].append((rs.day_of_week, rs.shift_type))

            print(f"  Total entries: {len(regulars)}\n")
            for vol_id in sorted(by_vol.keys()):
                name = by_vol[vol_id]["name"]
                shifts = sorted(by_vol[vol_id]["shifts"], key=lambda x: (x[0], x[1]))
                shifts_str = ", ".join(f"{format_dow(dow)} {st}" for dow, st in shifts)
                print(f"    {name:25} | {shifts_str}")
        else:
            print("  (empty)")

        # ── Simulate 52-week generation ────────────────────────────────────────
        print_section(f"SIMULATING {weeks_to_gen}-WEEK GENERATION")

        print(f"  Starting from: {today}")
        print(f"  Ending at:     {today + timedelta(weeks=weeks_to_gen) - timedelta(days=1)}")
        print(f"\n  This will generate ShiftAssignments based on current RegularSchedule")
        print(f"  and verify that recent changes are preserved.\n")

        # Simulate what would be generated
        def generate_weeks(from_date: date, weeks: int) -> dict:
            """Simulate generating assignments for N weeks from from_date."""
            generated = {}  # (date, shift_type, user_id) -> bool
            for week_offset in range(weeks):
                week_start = from_date + timedelta(weeks=week_offset)
                for day_offset in range(7):
                    target_date = week_start + timedelta(days=day_offset)
                    dow = target_date.weekday()
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
                            generated[(target_date, shift_type, rs.user_id)] = True
            return generated

        simulated = generate_weeks(today, weeks_to_gen)
        print(f"  Generated: {len(simulated)} total assignments\n")

        # ── Verify Recent Changes ──────────────────────────────────────────────
        print_section("VERIFICATION: Do simulated assignments match recent changes?")

        # Collect all recent additions from ScheduleChangeLog
        recent_adds = (
            ScheduleChangeLog.query
            .filter(ScheduleChangeLog.changed_at >= cutoff_date, ScheduleChangeLog.action == "add")
            .filter(ScheduleChangeLog.log_type == "upcoming")
            .all()
        )
        recent_removes = (
            ScheduleChangeLog.query
            .filter(ScheduleChangeLog.changed_at >= cutoff_date, ScheduleChangeLog.action == "remove")
            .filter(ScheduleChangeLog.log_type == "upcoming")
            .all()
        )

        issues = []

        # Check: all recent adds should exist in simulation OR be in future
        for add in recent_adds:
            key = (add.date, add.shift_type, add.volunteer_id)
            if add.date >= today and add.date < today + timedelta(weeks=weeks_to_gen):
                if key not in simulated:
                    # Check if it's a manual assignment (not from RegularSchedule)
                    user = User.query.get(add.volunteer_id)
                    rs_entry = RegularSchedule.query.filter_by(
                        user_id=add.volunteer_id,
                        day_of_week=add.date.weekday(),
                        shift_type=add.shift_type
                    ).first()
                    if not rs_entry:
                        issues.append(f"  ⚠️  Manual add not in simulation: {user.name} {add.date} {add.shift_type}")

        # Check: all recent removes should NOT exist in simulation (or be from different pattern)
        for remove in recent_removes:
            key = (remove.date, remove.shift_type, remove.volunteer_id)
            if remove.date >= today and remove.date < today + timedelta(weeks=weeks_to_gen):
                if key in simulated:
                    user = User.query.get(remove.volunteer_id)
                    # It's in the simulation because RegularSchedule says so
                    # This is a conflict: RegularSchedule still includes them
                    issues.append(f"  ⚠️  Recent removal conflicts: {user.name} still in RegularSchedule for {format_dow(remove.date.weekday())} {remove.shift_type}")

        if issues:
            print(f"  Found {len(issues)} potential issues:\n")
            for issue in issues:
                print(issue)
        else:
            print("  ✓ All recent changes are compatible with simulation")

        # ── Recent Emails ──────────────────────────────────────────────────────
        print_section("RECENT EMAILS (past 7 days)")

        cutoff_email = today - timedelta(days=7)
        emails = (
            EmailProcessingLog.query
            .filter(EmailProcessingLog.sent_at >= cutoff_email)
            .order_by(EmailProcessingLog.sent_at.desc())
            .all()
        )

        if emails:
            print(f"  Total emails: {len(emails)}\n")
            for email in emails:
                status_map = {"success": "✓", "no_action": "–", "failed": "✗"}
                icon = status_map.get(email.status, "?")
                sent = email.sent_at.strftime('%m/%d %H:%M') if email.sent_at else "?"
                preview = email.subject[:50] if email.subject else "(no subject)"
                print(f"    [{icon}] {sent} | {email.sender_email:25} | {preview}")
        else:
            print("  (no emails in past 7 days)")

        # ── Summary ────────────────────────────────────────────────────────────
        print_section("SUMMARY")
        print(f"  • Recent changes to review: {len(changes)}")
        print(f"  • RegularSchedule entries: {len(regulars)}")
        print(f"  • Simulated assignments: {len(simulated)}")
        print(f"  • Verification issues: {len(issues)}")
        print(f"\n  Next step: Bootstrap 52-week ShiftAssignments using:")
        print(f"    python scripts/bootstrap_52weeks.py")

if __name__ == "__main__":
    main()
