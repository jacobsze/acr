#!/usr/bin/env python3
"""
Bootstrap script: Generate 52 weeks of ShiftAssignments from current RegularSchedule.

This is a one-time operation to populate ShiftAssignments based on RegularSchedule.
After this, the Sunday cron will extend the rolling window.

Usage:
  python scripts/bootstrap_52weeks.py [--dry-run] [--force]

  --dry-run: show what would be generated without committing
  --force: overwrite existing assignments (use with caution)
"""
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from models import db, RegularSchedule, ShiftAssignment, User, ScheduleChangeLog

def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    app = create_app()
    with app.app_context():
        today = date.today()
        end_date = today + timedelta(weeks=52)

        print("=" * 80)
        print(f"  BOOTSTRAP 52-WEEK SHIFT ASSIGNMENTS")
        print("=" * 80)
        print(f"\n  Period: {today} → {end_date}")
        print(f"  Dry-run: {dry_run}")
        print(f"  Force overwrite: {force}\n")

        # Get all RegularSchedule entries
        regulars = RegularSchedule.query.all()
        if not regulars:
            print("  ERROR: No RegularSchedule entries found. Define regular schedules first.")
            return

        print(f"  Found {len(regulars)} RegularSchedule entries to expand\n")

        # Count existing assignments
        existing = ShiftAssignment.query.filter(
            ShiftAssignment.date >= today,
            ShiftAssignment.date < end_date,
        ).count()

        if existing and not force:
            print(f"  WARNING: {existing} ShiftAssignments already exist in this period.")
            print(f"           Use --force to overwrite, or run on a clean slate.\n")
            return

        # Prepare data
        assignments_to_add = []
        assignments_to_remove = []

        if force:
            assignments_to_remove = ShiftAssignment.query.filter(
                ShiftAssignment.date >= today,
                ShiftAssignment.date < end_date,
            ).all()

        # Generate assignments
        for week_offset in range(52):
            week_start = today + timedelta(weeks=week_offset)

            for day_offset in range(7):
                target_date = week_start + timedelta(days=day_offset)
                if target_date >= end_date:
                    break

                dow = target_date.weekday()  # 0=Monday … 6=Sunday

                for shift_type in ("AM", "PM"):
                    # Get RegularSchedule entries for this day/shift
                    reg_entries = (
                        RegularSchedule.query
                        .filter_by(day_of_week=dow, shift_type=shift_type)
                        .join(User)
                        .filter(User.active.is_(True))
                        .all()
                    )

                    for rs in reg_entries:
                        assignments_to_add.append({
                            "date": target_date,
                            "shift_type": shift_type,
                            "user_id": rs.user_id,
                            "notes": "Generated from regular schedule",
                        })

        print(f"  Generated: {len(assignments_to_add)} new assignments")
        if assignments_to_remove:
            print(f"  Removing: {len(assignments_to_remove)} existing assignments (--force)")

        if dry_run:
            print("\n  DRY-RUN: not committing changes\n")
            # Show sample
            for i, assign in enumerate(assignments_to_add[:10]):
                print(f"    {assign['date']} {assign['shift_type']} user_id={assign['user_id']}")
            if len(assignments_to_add) > 10:
                print(f"    ... and {len(assignments_to_add) - 10} more")
            return

        # Commit
        if assignments_to_remove:
            for assign in assignments_to_remove:
                db.session.delete(assign)

        for assign_data in assignments_to_add:
            db.session.add(ShiftAssignment(**assign_data))

        db.session.commit()

        print(f"\n  ✓ Successfully generated 52 weeks of ShiftAssignments")
        print(f"  ✓ Data committed to database\n")

        # Show distribution
        by_vol = {}
        for assign_data in assignments_to_add:
            user = User.query.get(assign_data["user_id"])
            if user:
                by_vol.setdefault(user.id, {"name": user.name, "count": 0})
                by_vol[user.id]["count"] += 1

        print(f"  Distribution (assignments per volunteer):\n")
        for vol_id in sorted(by_vol.keys(), key=lambda x: -by_vol[x]["count"]):
            name = by_vol[vol_id]["name"]
            count = by_vol[vol_id]["count"]
            print(f"    {name:25} | {count:3} shifts")

if __name__ == "__main__":
    main()
