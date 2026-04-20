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


def check_and_process(app) -> None:
    """
    Poll for new group emails, parse them with Claude, and apply any
    schedule changes.  Designed to be called from a background scheduler.
    """
    from models import db, User, ShiftAssignment, EmailProcessingLog, ScheduleChangeLog
    from routes.schedule_routes import materialize_if_needed
    from services.llm_parser import parse_email_schedule_request

    with app.app_context():
        try:
            service = _get_service(app)
        except Exception as exc:
            app.logger.error("Gmail monitor: cannot get service – %s", exc)
            return

        group_email = app.config["GMAIL_MONITOR_EMAIL"]
        query = f"to:{group_email} OR from:{group_email}"

        try:
            result = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=50)
                .execute()
            )
        except Exception as exc:
            app.logger.error("Gmail monitor: list() failed – %s", exc)
            return

        messages = result.get("messages", [])
        volunteers = User.query.filter_by(active=True).all()

        for meta in messages:
            msg_id = meta["id"]

            if EmailProcessingLog.query.filter_by(gmail_message_id=msg_id).first():
                continue  # Already processed

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

                parsed = parse_email_schedule_request(
                    email_subject=content["subject"],
                    email_body=content["body"],
                    email_from=content["from_email"],
                    volunteers=volunteers,
                )

                action = parsed.get("action")
                confidence = parsed.get("confidence", "low")

                if action in ("add", "remove") and confidence in ("high", "medium"):
                    vol_email = parsed.get("volunteer_email")
                    date_str = parsed.get("date")
                    shift_type = parsed.get("shift_type")

                    if vol_email and date_str and shift_type:
                        target_user = User.query.filter_by(
                            email=vol_email, active=True
                        ).first()
                        target_date = date.fromisoformat(date_str)

                        if target_user:
                            materialize_if_needed(target_date, shift_type)

                            if action == "add":
                                existing = ShiftAssignment.query.filter_by(
                                    date=target_date,
                                    shift_type=shift_type,
                                    user_id=target_user.id,
                                ).first()
                                count = ShiftAssignment.query.filter_by(
                                    date=target_date, shift_type=shift_type
                                ).count()
                                cap = app.config["MAX_VOLUNTEERS_PER_SHIFT"]

                                if not existing and count < cap:
                                    db.session.add(
                                        ShiftAssignment(
                                            date=target_date,
                                            shift_type=shift_type,
                                            user_id=target_user.id,
                                            notes=f"Added via email: {content['subject']}",
                                        )
                                    )
                                    db.session.add(ScheduleChangeLog(
                                        log_type="upcoming",
                                        date=target_date,
                                        shift_type=shift_type,
                                        action="add",
                                        volunteer_id=target_user.id,
                                        volunteer_name=target_user.name,
                                    ))
                                    db.session.commit()
                                    status = "success"

                            elif action == "remove":
                                existing = ShiftAssignment.query.filter_by(
                                    date=target_date,
                                    shift_type=shift_type,
                                    user_id=target_user.id,
                                ).first()
                                if existing:
                                    db.session.delete(existing)
                                    db.session.add(ScheduleChangeLog(
                                        log_type="upcoming",
                                        date=target_date,
                                        shift_type=shift_type,
                                        action="remove",
                                        volunteer_id=target_user.id,
                                        volunteer_name=target_user.name,
                                    ))
                                    db.session.commit()
                                    status = "success"

            except Exception as exc:
                app.logger.error("Gmail monitor: error on msg %s – %s", msg_id, exc)
                status = "failed"
                error_msg = str(exc)

            log = EmailProcessingLog(
                gmail_message_id=msg_id,
                sender_email=content.get("from_email", ""),
                subject=content.get("subject", ""),
                body_snippet=content.get("body", "")[:500],
                parsed_action=json.dumps(parsed) if parsed else None,
                status=status,
                error_message=error_msg,
            )
            db.session.add(log)
            db.session.commit()
