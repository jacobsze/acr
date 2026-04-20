"""
Monitor the configured Gmail inbox for messages from / to the Google Group
and apply schedule changes extracted by the LLM parser.

Setup:
1. Create a Google Cloud project and enable the Gmail API.
2. Create OAuth 2.0 credentials (Desktop app) and download credentials.json.
3. Set GMAIL_CREDENTIALS_FILE and GMAIL_TOKEN_FILE in .env.
4. On first run, a browser window will open to authorise access and write token.json.
"""
import base64
import json
import os
from datetime import date

from flask import current_app


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _get_service(app):
    """Return an authenticated Gmail API service object."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds_file = app.config["GMAIL_CREDENTIALS_FILE"]
    token_file = app.config["GMAIL_TOKEN_FILE"]

    if not os.path.exists(creds_file):
        raise FileNotFoundError(
            f"Gmail credentials file not found: {creds_file}. "
            "Download it from Google Cloud Console."
        )

    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _extract_content(msg: dict) -> dict:
    """Pull subject, sender, and plain-text body out of a Gmail message object."""
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    subject = headers.get("Subject", "")
    from_raw = headers.get("From", "")

    # "Display Name <addr@example.com>" → "addr@example.com"
    if "<" in from_raw and ">" in from_raw:
        from_email = from_raw.split("<")[1].split(">")[0].strip()
    else:
        from_email = from_raw.strip()

    body = ""
    payload = msg["payload"]
    parts = payload.get("parts", [])
    if parts:
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    break
    else:
        data = payload.get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    return {"subject": subject, "from_email": from_email, "body": body}


def _apply_parsed(app, parsed, content):
    """
    Try to apply an add/remove action from a parsed result.
    Returns the resulting status string: 'success' or 'no_action'.
    """
    from models import db, User, ShiftAssignment, ScheduleChangeLog
    from routes.schedule_routes import materialize_if_needed

    action = parsed.get("action")
    confidence = parsed.get("confidence", "low")

    if action not in ("add", "remove") or confidence not in ("high", "medium"):
        return "no_action"

    vol_email = parsed.get("volunteer_email")
    date_str = parsed.get("date")
    shift_type = parsed.get("shift_type")

    if not (vol_email and date_str and shift_type):
        return "no_action"

    target_user = User.query.filter_by(email=vol_email, active=True).first()
    if not target_user:
        return "no_action"

    target_date = date.fromisoformat(date_str)
    materialize_if_needed(target_date, shift_type)
    cap = app.config["MAX_VOLUNTEERS_PER_SHIFT"]

    if action == "add":
        existing = ShiftAssignment.query.filter_by(
            date=target_date, shift_type=shift_type, user_id=target_user.id,
        ).first()
        count = ShiftAssignment.query.filter_by(
            date=target_date, shift_type=shift_type,
        ).count()
        if not existing and count < cap:
            db.session.add(ShiftAssignment(
                date=target_date, shift_type=shift_type, user_id=target_user.id,
                notes=f"Added via email: {content['subject']}",
            ))
            db.session.add(ScheduleChangeLog(
                log_type="upcoming", date=target_date, shift_type=shift_type,
                action="add", volunteer_id=target_user.id, volunteer_name=target_user.name,
            ))
            db.session.commit()
            return "success"

    elif action == "remove":
        existing = ShiftAssignment.query.filter_by(
            date=target_date, shift_type=shift_type, user_id=target_user.id,
        ).first()
        if existing:
            db.session.delete(existing)
            db.session.add(ScheduleChangeLog(
                log_type="upcoming", date=target_date, shift_type=shift_type,
                action="remove", volunteer_id=target_user.id, volunteer_name=target_user.name,
            ))
            db.session.commit()
            return "success"

    return "no_action"


def _process_one(app, service, msg_id, volunteers):
    """
    Fetch, parse, and apply a single Gmail message.
    Returns (status, error_msg, parsed, content).
    """
    from services.llm_parser import parse_email_schedule_request

    volunteer_emails = {v.email.lower() for v in volunteers}
    status = "no_action"
    error_msg = None
    parsed = {}
    content = {"subject": "", "from_email": "", "body": ""}

    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )
        content = _extract_content(msg)

        if content["from_email"].lower() not in volunteer_emails:
            app.logger.info(
                "Gmail monitor: skipping msg %s – sender %s not a volunteer",
                msg_id, content["from_email"],
            )
        else:
            parsed = parse_email_schedule_request(
                email_subject=content["subject"],
                email_body=content["body"],
                email_from=content["from_email"],
                volunteers=volunteers,
            )
            status = _apply_parsed(app, parsed, content)

    except Exception as exc:
        app.logger.error("Gmail monitor: error on msg %s – %s", msg_id, exc)
        status = "failed"
        error_msg = str(exc)

    return status, error_msg, parsed, content


def check_and_process(app) -> None:
    """
    Poll for new group emails, parse them with Claude, and apply any
    schedule changes.  Designed to be called from a background scheduler.
    """
    from models import db, User, EmailProcessingLog

    with app.app_context():
        try:
            service = _get_service(app)
        except Exception as exc:
            app.logger.error("Gmail monitor: cannot get service – %s", exc)
            return

        inbox_email = app.config["GMAIL_MONITOR_EMAIL"]
        try:
            result = (
                service.users()
                .messages()
                .list(userId="me", q=f"to:{inbox_email}", maxResults=50)
                .execute()
            )
        except Exception as exc:
            app.logger.error("Gmail monitor: list() failed – %s", exc)
            return

        volunteers = User.query.filter_by(active=True).all()

        for meta in result.get("messages", []):
            msg_id = meta["id"]
            if EmailProcessingLog.query.filter_by(gmail_message_id=msg_id).first():
                continue  # Already processed

            status, error_msg, parsed, content = _process_one(app, service, msg_id, volunteers)

            db.session.add(EmailProcessingLog(
                gmail_message_id=msg_id,
                sender_email=content.get("from_email", ""),
                subject=content.get("subject", ""),
                body_snippet=content.get("body", "")[:500],
                parsed_action=json.dumps(parsed) if parsed else None,
                status=status,
                error_message=error_msg,
            ))
            db.session.commit()


def reprocess_message(app, log_id: int) -> None:
    """Re-fetch and re-parse a previously logged email, updating the log entry in place.
    Must be called from within an active app/request context (i.e. from a route)."""
    from datetime import datetime
    from models import db, User, EmailProcessingLog

    log = db.session.get(EmailProcessingLog, log_id)
    if log is None:
        raise ValueError(f"EmailProcessingLog {log_id} not found")

    try:
        service = _get_service(app)
    except Exception as exc:
        log.status = "failed"
        log.error_message = str(exc)
        log.processed_at = datetime.utcnow()
        db.session.commit()
        return

    volunteers = User.query.filter_by(active=True).all()
    status, error_msg, parsed, content = _process_one(app, service, log.gmail_message_id, volunteers)

    log.status = status
    log.error_message = error_msg
    log.parsed_action = json.dumps(parsed) if parsed else None
    log.processed_at = datetime.utcnow()
    if content.get("from_email"):
        log.sender_email = content["from_email"]
    if content.get("subject"):
        log.subject = content["subject"]
    if content.get("body"):
        log.body_snippet = content["body"][:500]
    db.session.commit()
