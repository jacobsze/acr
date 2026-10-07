import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
from flask import Flask
from models import db, User, ShiftAssignment, RegularSchedule
from routes.admin_routes import _most_recent_volunteer_shifts


class RecentShiftTest(unittest.TestCase):
    def test_latest_recorded_shift_excludes_today_and_future(self):
        app = Flask(__name__)
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        db.init_app(app)
        with app.app_context(), patch('routes.admin_routes.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 7, 12, tzinfo=ZoneInfo('America/New_York'))
            db.create_all()
            db.session.add_all([User(id=i, name=str(i), email=f'{i}@example.com') for i in (1, 2, 3)])
            db.session.add(RegularSchedule(user_id=3, day_of_week=0, shift_type='PM'))
            for uid, day, shift in [(1, 5, 'PM'), (1, 6, 'AM'), (1, 6, 'PM'), (1, 7, 'AM'), (1, 8, 'PM'), (2, 8, 'AM')]:
                db.session.add(ShiftAssignment(user_id=uid, date=date(2026, 10, day), shift_type=shift))
            db.session.commit()
            recent = _most_recent_volunteer_shifts()
            self.assertEqual(set(recent), {1})
            self.assertEqual((recent[1].date, recent[1].shift_type), (date(2026, 10, 6), 'PM'))
            self.assertEqual(str(clock.now.call_args.args[0]), 'America/New_York')
            db.session.remove()
            db.engine.dispose()
