import json
import unittest
from datetime import date
from flask import Flask, g
from models import db, User, ShiftAssignment, ShiftClosure, RegularSchedule, ScheduleChangeLog
from routes.schedule_routes import schedule_bp, build_schedule
from services.weekly_email import _open_shifts_for_date, _cell


class ShiftClosureTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='test', SQLALCHEMY_DATABASE_URI='sqlite://', LOCAL_TEST_MODE=True, MAX_VOLUNTEERS_PER_SHIFT=3)
        db.init_app(self.app)
        self.app.register_blueprint(schedule_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        db.session.add_all([User(id=1,name='Owner',email='owner@example.com',role='owner'),User(id=2,name='Volunteer',email='vol@example.com',role='volunteer')])
        self.d = date(2026,11,26)
        db.session.add(ShiftAssignment(date=self.d,shift_type='PM',user_id=2))
        db.session.add(RegularSchedule(day_of_week=3,shift_type='PM',user_id=2))
        db.session.commit()
        self.client = self.app.test_client()
        self.login('owner@example.com')

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def login(self,email):
        g.pop('_current_user', None)
        with self.client.session_transaction() as s:s['_test_email']=email

    def change(self,state):
        return self.client.post('/shifts/closure',data={'date':self.d.isoformat(),'shift_type':'PM','state':state})

    def test_close_reopen_preserves_assignments_and_audits(self):
        self.assertEqual(self.change('closed').status_code,302)
        self.change('closed')
        slot=build_schedule([self.d],None)[self.d]['PM']
        self.assertTrue(slot['is_closed'])
        self.assertEqual(slot['volunteers'],[])
        self.assertEqual(ShiftAssignment.query.count(),1)
        self.assertEqual(_open_shifts_for_date(self.d),['AM'])
        self.assertIn('Closed',_cell([], '#fff', closed=True))
        self.assertNotIn('Need Volunteers',_cell([], '#fff', closed=True))
        self.change('open')
        self.assertEqual(build_schedule([self.d],None)[self.d]['PM']['count'],1)
        self.assertEqual([r.action for r in ScheduleChangeLog.query.order_by(ScheduleChangeLog.id)],['close','reopen'])

    def test_empty_closed_shift_does_not_fallback_or_alert(self):
        ShiftAssignment.query.delete()
        db.session.commit()
        self.change('closed')
        self.assertEqual(build_schedule([self.d],None)[self.d]['PM']['count'],0)
        self.assertNotIn('PM',_open_shifts_for_date(self.d))

    def test_volunteer_cannot_close_and_stale_add_is_blocked(self):
        self.login('vol@example.com')
        self.change('closed')
        self.assertEqual(ShiftClosure.query.count(),0)
        self.login('owner@example.com')
        self.change('closed')
        self.login('vol@example.com')
        self.client.post('/shifts/add',data={'date':self.d.isoformat(),'shift_type':'PM'})
        changes={'adds':[{'date':self.d.isoformat(),'shift_type':'PM','user_id':2}], 'removes':[{'date':self.d.isoformat(),'shift_type':'PM','user_id':2}]}
        self.client.post('/schedule/bulk_save',data={'changes_json':json.dumps(changes)})
        self.assertEqual(ShiftAssignment.query.count(),1)
        self.assertEqual(ScheduleChangeLog.query.count(),1)

    def test_invalid_closure(self):
        response=self.client.post('/shifts/closure',data={'date':'2026-11-26','shift_type':'XX','state':'closed'})
        self.assertEqual(response.status_code,400)
        self.assertEqual(ShiftClosure.query.count(),0)
