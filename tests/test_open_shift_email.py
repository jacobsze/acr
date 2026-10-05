import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
from flask import Flask
from models import db, User, ShiftAssignment, AppSetting
from services.weekly_email import check_and_send_open_shift_alert
from app import _run_open_shift_alert


class OpenShiftAlertTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://', MAX_VOLUNTEERS_PER_SHIFT=3)
        db.init_app(self.app)
        with self.app.app_context():
            db.create_all()
            db.session.add(User(id=1, name='Volunteer', email='volunteer@example.com'))
            db.session.add(ShiftAssignment(user_id=1, date=date(2026, 10, 7), shift_type='AM'))
            db.session.commit()
        self.clock = patch('services.weekly_email.datetime')
        self.now = self.clock.start()
        self.now.now.return_value = datetime(2026, 10, 5, 10, tzinfo=ZoneInfo('America/New_York'))
        self.addCleanup(self.clock.stop)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    @patch('services.weekly_email._send_gmail')
    def test_october_7_pm_alert(self, send):
        check_and_send_open_shift_alert(self.app)
        send.assert_called_once()
        _, recipient, subject, body = send.call_args.args
        self.assertEqual(recipient, 'acrpetco86@googlegroups.com')
        self.assertEqual(subject, 'PM shift is open on 10/7 (Wed) - can anyone cover?')
        self.assertIn('10/7/2026', body)
        self.assertIn('Need Volunteers', body)
        self.assertEqual(str(self.now.now.call_args.args[0]), 'America/New_York')

    @patch('services.weekly_email._send_gmail')
    def test_one_volunteer_per_shift_is_covered(self, send):
        with self.app.app_context():
            db.session.add(ShiftAssignment(user_id=1, date=date(2026, 10, 7), shift_type='PM'))
            db.session.commit()
        check_and_send_open_shift_alert(self.app)
        send.assert_not_called()

    @patch('services.weekly_email._send_gmail')
    def test_both_empty_shifts_in_one_email(self, send):
        with self.app.app_context():
            ShiftAssignment.query.delete()
            db.session.commit()
        check_and_send_open_shift_alert(self.app)
        send.assert_called_once()
        self.assertIn('AM and PM shifts are open on 10/7', send.call_args.args[2])

    @patch('services.weekly_email._send_gmail')
    def test_failed_send_retries_then_deduplicates(self, send):
        send.side_effect = RuntimeError('SMTP unavailable')
        _run_open_shift_alert(self.app)
        with self.app.app_context():
            self.assertIsNone(db.session.get(AppSetting, 'last_open_shift_alert_date'))
        send.side_effect = None
        _run_open_shift_alert(self.app)
        _run_open_shift_alert(self.app)
        self.assertEqual(send.call_count, 2)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(AppSetting, 'last_open_shift_alert_date'))
