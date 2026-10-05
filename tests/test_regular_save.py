import unittest
from datetime import date, timedelta
from unittest.mock import patch
from flask import Flask
from models import db, User, RegularSchedule, ShiftAssignment, ScheduleChangeLog
from routes.admin_routes import admin_bp


class RegularSaveTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='test', SQLALCHEMY_DATABASE_URI='sqlite://', LOCAL_TEST_MODE=True, MAX_VOLUNTEERS_PER_SHIFT=3)
        db.init_app(self.app)
        self.app.register_blueprint(admin_bp)
        with self.app.app_context():
            db.create_all()
            db.session.add_all([User(id=1, name='Owner', email='owner@example.com', role='owner'), User(id=2, name='Volunteer', email='volunteer@example.com'), User(id=3, name='Replacement', email='replacement@example.com')])
            db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_test_email'] = 'owner@example.com'

    def test_add_weekly(self):
        response = self.client.post('/admin/regular/save', data={'spot_0_AM_0':'2'})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            shift = RegularSchedule.query.one()
            self.assertEqual(shift.frequency, 'weekly')
            self.assertIsNone(shift.start_date)
            self.assertEqual(ScheduleChangeLog.query.one().action, 'add')

    def test_alternating_date_choices(self):
        for week in (0, 1):
            with self.subTest(week=week):
                with self.app.app_context():
                    RegularSchedule.query.delete()
                    db.session.commit()
                dow = date.today().weekday()
                response = self.client.post('/admin/regular/save', data={f'spot_{dow}_PM_0':'2', f'freq_{dow}_PM_0':'every_other_week', f'week_{dow}_PM_0':str(week)})
                self.assertEqual(response.status_code, 302)
                with self.app.app_context():
                    self.assertEqual(RegularSchedule.query.one().start_date, date.today() + timedelta(weeks=1+week))

    def seed_removal(self):
        today = date.today()
        with self.app.app_context():
            db.session.add(RegularSchedule(user_id=2, day_of_week=today.weekday(), shift_type='AM'))
            db.session.add_all([ShiftAssignment(user_id=2, date=today-timedelta(weeks=1), shift_type='AM'), ShiftAssignment(user_id=2, date=today+timedelta(weeks=1), shift_type='AM')])
            db.session.commit()

    def test_removal_keeps_past(self):
        self.seed_removal()
        self.assertEqual(self.client.post('/admin/regular/save', data={}).status_code, 302)
        with self.app.app_context():
            self.assertEqual(RegularSchedule.query.count(), 0)
            self.assertLess(ShiftAssignment.query.one().date, date.today())

    def test_failure_rolls_back_removal(self):
        self.seed_removal()
        with patch('routes.admin_routes.RegularSchedule', wraps=RegularSchedule) as constructor:
            constructor.side_effect = RuntimeError('simulated failed addition')
            with self.assertRaises(RuntimeError):
                self.client.post('/admin/regular/save', data={'spot_0_PM_0':'3'})
        with self.app.app_context():
            self.assertEqual(RegularSchedule.query.count(), 1)
            self.assertEqual(ShiftAssignment.query.count(), 2)
            self.assertEqual(ScheduleChangeLog.query.count(), 0)

if __name__ == '__main__':
    unittest.main()
