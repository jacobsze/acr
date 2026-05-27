from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=True)
    # role: owner | admin | volunteer
    role = db.Column(db.String(20), nullable=False, default="volunteer")
    # Clerk user ID – set on first sign-in, used for fast session lookups
    clerk_user_id = db.Column(db.String(100), unique=True, nullable=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)

    regular_shifts = db.relationship(
        "RegularSchedule",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="RegularSchedule.user_id",
    )
    assignments = db.relationship(
        "ShiftAssignment",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="ShiftAssignment.user_id",
    )

    def is_admin_or_owner(self):
        return self.role in ("owner", "admin")


class RegularSchedule(db.Model):
    """The repeating weekly template – not tied to specific dates."""

    __tablename__ = "regular_schedule"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    # 0 = Monday … 6 = Sunday
    day_of_week = db.Column(db.Integer, nullable=False)
    shift_type = db.Column(db.String(2), nullable=False)  # AM | PM
    # Frequency: "weekly" (default) or "every_other_week"
    frequency = db.Column(db.String(20), nullable=False, default="weekly")
    # For every_other_week: actual start date (epoch for week parity calculation)
    start_date = db.Column(db.Date, nullable=True)  # The actual start date selected by user

    user = db.relationship("User", back_populates="regular_shifts", foreign_keys=[user_id])

    __table_args__ = (
        db.UniqueConstraint("user_id", "day_of_week", "shift_type", name="uq_regular_shift"),
    )


class ShiftAssignment(db.Model):
    """Concrete shift assignments for a specific calendar date."""

    __tablename__ = "shift_assignments"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    shift_type = db.Column(db.String(2), nullable=False)  # AM | PM
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    notes = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    user = db.relationship("User", back_populates="assignments", foreign_keys=[user_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        db.UniqueConstraint("date", "shift_type", "user_id", name="uq_shift_assignment"),
    )


class ScheduleChangeLog(db.Model):
    """Audit trail for upcoming and regular schedule changes."""

    __tablename__ = "schedule_change_log"

    id = db.Column(db.Integer, primary_key=True)
    changed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    log_type = db.Column(db.String(10), nullable=False)   # 'upcoming' | 'regular'
    date = db.Column(db.Date, nullable=True)               # upcoming only
    day_of_week = db.Column(db.Integer, nullable=True)     # regular only (0=Mon…6=Sun)
    shift_type = db.Column(db.String(2), nullable=False)   # AM | PM
    action = db.Column(db.String(10), nullable=False)      # 'add' | 'remove'

    volunteer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    volunteer_name = db.Column(db.String(100), nullable=True)  # snapshot

    changed_by = db.relationship("User", foreign_keys=[changed_by_id])
    volunteer = db.relationship("User", foreign_keys=[volunteer_id])
    changed_by_note = db.Column(db.String(200), nullable=True)  # free-text for non-user changes (e.g. email/LLM)


class AppSetting(db.Model):
    """Generic key/value store for admin-editable app settings."""

    __tablename__ = "app_settings"

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EmailProcessingLog(db.Model):
    """Audit trail for emails processed from the Google Group."""

    __tablename__ = "email_processing_log"

    id = db.Column(db.Integer, primary_key=True)
    gmail_message_id = db.Column(db.String(100), unique=True, nullable=False)
    sender_email = db.Column(db.String(200), nullable=True)
    subject = db.Column(db.String(500), nullable=True)
    body_snippet = db.Column(db.Text, nullable=True)
    parsed_action = db.Column(db.Text, nullable=True)  # JSON
    status = db.Column(db.String(50), nullable=True)   # success | no_action | failed
    error_message = db.Column(db.Text, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)     # when the email was sent (from internalDate)
    processed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Cat(db.Model):
    """Individual cat in the shelter."""

    __tablename__ = "cats"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    status = db.Column(db.String(50), nullable=False, default="at_shelter")  # at_shelter | adopted | transferred | other
    last_seen_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    logs = db.relationship("CatLog", back_populates="cat", cascade="all, delete-orphan")


class CatLog(db.Model):
    """Historical log entry for a cat."""

    __tablename__ = "cat_logs"

    id = db.Column(db.Integer, primary_key=True)
    cat_id = db.Column(db.Integer, db.ForeignKey("cats.id"), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    shift_type = db.Column(db.String(2), nullable=True)  # AM | PM
    notes = db.Column(db.Text, nullable=True)
    bowel_movement = db.Column(db.String(100), nullable=True)  # pee only | poop only | pee and poop | none | other: ...
    food_intake = db.Column(db.String(20), nullable=True)   # all | 3/4 | 1/2 | 1/4 | none
    status = db.Column(db.String(50), nullable=True)
    volunteer_name = db.Column(db.String(100), nullable=True)
    email_message_id = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    cat = db.relationship("Cat", back_populates="logs")
    photos = db.relationship("CatPhoto", back_populates="log", cascade="all, delete-orphan")


class CatPhoto(db.Model):
    """Photo attached to a cat log entry."""

    __tablename__ = "cat_photos"

    id = db.Column(db.Integer, primary_key=True)
    cat_log_id = db.Column(db.Integer, db.ForeignKey("cat_logs.id"), nullable=False, index=True)
    filename = db.Column(db.String(200), nullable=True)
    data = db.Column(db.LargeBinary, nullable=True)  # base64 encoded image data
    mime_type = db.Column(db.String(50), nullable=True)  # e.g. image/jpeg
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    log = db.relationship("CatLog", back_populates="photos")
