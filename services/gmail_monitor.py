"""
Monitor the configured Gmail inbox for messages from / to the Google Group
and apply schedule changes extracted by the LLM parser.

Setup:
1. Create a Google Cloud project and enable the Gmail API.
2. Create OAuth 2.0 credentials (Desktop app) and download credentials.json.
3. Set GMAIL_CREDENTIALS_FILE and GMAIL_TOKEN_FILE in .env.
4. On first run, a browser window will open to authorise access and write token.json.

Note: SCOPES now includes gmail.send so the app can email the owner with results.
If you previously authorised with the read-only scope, delete token.json and
re-run the local OAuth flow to get a new token with the updated scopes.
"""
import base64
import json
import os
from datetime import date
from email.mime.text import MIMEText

from flask import current_app


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


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
    """Pull subject, sender, plain-text body, Message-ID, and thread ID out of a Gmail message."""
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    subject = headers.get("Subject", "")
    from_raw = headers.get("From", "")
    message_id = headers.get("Message-ID", "")

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

    return {
        "subject": subject,
        "from_email": from_email,
        "body": body,
        "message_id": message_id,
        "thread_id": msg.get("threadId", ""),
    }


def _apply_parsed(app, parsed, content, sender_email=None):
    """
    Try to apply an add/remove action from a parsed result.
    Returns a list of result dicts, one per attempted date, each with:
      date, shift_type, action, volunteer_name, status, message
    Possible statuses: success, skipped_past, already_assigned, not_found,
                       at_capacity, low_confidence, unknown_action
    """
    from models import db, User, ShiftAssignment, ScheduleChangeLog
    from routes.schedule_routes import materialize_if_needed

    action = parsed.get("action")
    confidence = parsed.get("confidence", "low")
    vol_email = parsed.get("volunteer_email")
    date_val = parsed.get("date")
    shift_type = parsed.get("shift_type")

    if action not in ("add", "remove"):
        return []

    if confidence not in ("high", "medium"):
        return [{
            "date": date_val,
            "shift_type": shift_type,
            "action": action,
            "volunteer_name": vol_email,
            "status": "low_confidence",
            "message": f"Confidence too low ({confidence}) to apply automatically.",
        }]

    if not (vol_email and date_val and shift_type):
        return []

    target_user = User.query.filter_by(email=vol_email, active=True).first()
    if not target_user:
        return [{
            "date": date_val,
            "shift_type": shift_type,
            "action": action,
            "volunteer_name": vol_email,
            "status": "not_found",
            "message": f"No active volunteer found with email {vol_email}.",
        }]

    date_strs = date_val if isinstance(date_val, list) else [date_val]
    cap = app.config["MAX_VOLUNTEERS_PER_SHIFT"]
    today = date.today()
    results = []
    any_success = False
    changed_by_note = f"Email from {sender_email}" if sender_email else "Email (LLM)"

    for date_str in date_strs:
        target_date = date.fromisoformat(date_str)
        r = {
            "date": date_str,
            "shift_type": shift_type,
            "action": action,
            "volunteer_name": target_user.name,
            "status": None,
            "message": None,
        }

        if target_date < today:
            r["status"] = "skipped_past"
            r["message"] = f"{date_str} is in the past — skipped."
            results.append(r)
            continue

        materialize_if_needed(target_date, shift_type)

        if action == "add":
            existing = ShiftAssignment.query.filter_by(
                date=target_date, shift_type=shift_type, user_id=target_user.id,
            ).first()
            if existing:
                r["status"] = "already_assigned"
                r["message"] = f"{target_user.name} is already on {shift_type} on {date_str}."
                results.append(r)
                continue
            count = ShiftAssignment.query.filter_by(
                date=target_date, shift_type=shift_type,
            ).count()
            if count >= cap:
                r["status"] = "at_capacity"
                r["message"] = f"{shift_type} shift on {date_str} is full ({cap}/{cap} volunteers)."
                results.append(r)
                continue
            db.session.add(ShiftAssignment(
                date=target_date, shift_type=shift_type, user_id=target_user.id,
                notes=f"Added via email: {content['subject']}",
            ))
            db.session.add(ScheduleChangeLog(
                log_type="upcoming", date=target_date, shift_type=shift_type,
                action="add", volunteer_id=target_user.id, volunteer_name=target_user.name,
                changed_by_note=changed_by_note,
            ))
            r["status"] = "success"
            r["message"] = f"Added {target_user.name} to {shift_type} on {date_str}."
            any_success = True

        elif action == "remove":
            existing = ShiftAssignment.query.filter_by(
                date=target_date, shift_type=shift_type, user_id=target_user.id,
            ).first()
            if not existing:
                r["status"] = "not_found"
                r["message"] = f"{target_user.name} is not on {shift_type} on {date_str}."
                results.append(r)
                continue
            db.session.delete(existing)
            db.session.add(ScheduleChangeLog(
                log_type="upcoming", date=target_date, shift_type=shift_type,
                action="remove", volunteer_id=target_user.id, volunteer_name=target_user.name,
                changed_by_note=changed_by_note,
            ))
            r["status"] = "success"
            r["message"] = f"Removed {target_user.name} from {shift_type} on {date_str}."
            any_success = True

        results.append(r)

    if any_success:
        db.session.commit()

    return results


def _send_summary_email(app, service, content, parsed, results, processing_error=None):
    """Reply to the original email thread with a processing summary for the owner."""
    owner_email = app.config.get("OWNER_EMAIL", "")
    monitor_email = app.config.get("GMAIL_MONITOR_EMAIL", "")
    if not owner_email:
        return

    lines = []
    if processing_error:
        lines.append(f"ERROR during processing: {processing_error}")
    elif not results and parsed.get("action") in (None, "unknown"):
        reason = parsed.get("reason", "No schedule change detected.")
        lines.append("No schedule action found.")
        lines.append(f"Claude's interpretation: {reason}")
    elif not results:
        lines.append("No changes applied.")
    else:
        status_icons = {
            "success": "✅",
            "skipped_past": "⏭️",
            "already_assigned": "ℹ️",
            "not_found": "ℹ️",
            "at_capacity": "⚠️",
            "low_confidence": "⚠️",
        }
        for r in results:
            icon = status_icons.get(r["status"], "❓")
            lines.append(f"{icon} {r['message']}")

    body = "\n".join(lines)
    original_subject = content.get("subject", "")
    reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"

    msg = MIMEText(body)
    msg["to"] = owner_email
    msg["from"] = monitor_email
    msg["subject"] = reply_subject

    original_message_id = content.get("message_id", "")
    if original_message_id:
        msg["In-Reply-To"] = original_message_id
        msg["References"] = original_message_id

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    send_body = {"raw": raw}
    thread_id = content.get("thread_id", "")
    if thread_id:
        send_body["threadId"] = thread_id

    try:
        service.users().messages().send(userId="me", body=send_body).execute()
    except Exception as exc:
        app.logger.error("Gmail monitor: failed to send summary email – %s", exc)


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
    is_volunteer = False

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
            is_volunteer = True
            parsed = parse_email_schedule_request(
                email_subject=content["subject"],
                email_body=content["body"],
                email_from=content["from_email"],
                volunteers=volunteers,
            )

            if parsed.get("error") and parsed.get("action") == "unknown":
                # LLM/API-level error — treat as failed
                status = "failed"
                error_msg = parsed.get("error")
            else:
                results = _apply_parsed(app, parsed, content, sender_email=content["from_email"])
                status = "success" if any(r["status"] == "success" for r in results) else "no_action"
                # Reply only when a change was made or Claude flagged low confidence
                if any(r["status"] in ("success", "low_confidence") for r in results):
                    _send_summary_email(app, service, content, parsed, results)

    except Exception as exc:
        app.logger.error("Gmail monitor: error on msg %s – %s", msg_id, exc)
        status = "failed"
        error_msg = str(exc)
        if is_volunteer:
            _send_summary_email(app, service, content, parsed, [], processing_error=str(exc))

    return status, error_msg, parsed, content


def check_and_process(app) -> None:
    """
    Poll for new group emails, parse them with Claude, and apply any
    schedule changes.  Designed to be called from a background scheduler.
    """
    from datetime import datetime
    from models import db, User, EmailProcessingLog, AppSetting

    with app.app_context():
        setting = db.session.get(AppSetting, "last_email_check")
        if setting:
            setting.value = datetime.utcnow().isoformat()
        else:
            db.session.add(AppSetting(key="last_email_check", value=datetime.utcnow().isoformat()))
        db.session.commit()
        try:
            service = _get_service(app)
        except Exception as exc:
            app.logger.error("Gmail monitor: cannot get service – %s", exc)
            return

        try:
            result = (
                service.users()
                .messages()
                .list(userId="me", q="in:inbox", maxResults=50)
                .execute()
            )
        except Exception as exc:
            app.logger.error("Gmail monitor: list() failed – %s", exc)
            return

        volunteers = User.query.filter_by(active=True).all()

        for meta in result.get("messages", []):
            msg_id = meta["id"]
            if EmailProcessingLog.query.filter_by(gmail_message_id=msg_id).first():
                continue

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
