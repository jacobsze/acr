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
    """Return an authenticated Gmail API service object.

    Token is stored in AppSetting table ('gmail_token_json') and persists
    across restarts. When the token is refreshed, it's automatically saved.
    Falls back to token.json for migration purposes.
    """
    import json as _json
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from models import db, AppSetting

    creds_file = app.config["GMAIL_CREDENTIALS_FILE"]
    token_file = app.config.get("GMAIL_TOKEN_FILE", "token.json")

    if not os.path.exists(creds_file):
        raise FileNotFoundError(
            f"Gmail credentials file not found: {creds_file}. "
            "Download it from Google Cloud Console."
        )

    creds = None

    # Load token from database (persists across restarts)
    with app.app_context():
        setting = db.session.get(AppSetting, "gmail_token_json")
        if setting:
            try:
                creds = Credentials.from_authorized_user_info(
                    _json.loads(setting.value), SCOPES
                )
            except Exception as e:
                app.logger.warning("Failed to load Gmail token from database: %s", e)

    # Fallback to token.json if not in database (migration from local dev)
    if not creds and token_file and os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
            # Migrate to database
            with app.app_context():
                setting = AppSetting.query.get("gmail_token_json")
                if setting:
                    setting.value = creds.to_json()
                else:
                    setting = AppSetting(key="gmail_token_json", value=creds.to_json())
                    db.session.add(setting)
                db.session.commit()
            app.logger.info("Gmail token migrated from file to database")
        except Exception as e:
            app.logger.warning("Failed to load Gmail token from file: %s", e)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save refreshed token back to database
            with app.app_context():
                setting = db.session.get(AppSetting, "gmail_token_json")
                if setting:
                    setting.value = creds.to_json()
                else:
                    setting = AppSetting(key="gmail_token_json", value=creds.to_json())
                    db.session.add(setting)
                db.session.commit()
            app.logger.info("Gmail token refreshed and saved to database")
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
            # Save new token to database
            with app.app_context():
                setting = db.session.get(AppSetting, "gmail_token_json")
                if setting:
                    setting.value = creds.to_json()
                else:
                    setting = AppSetting(key="gmail_token_json", value=creds.to_json())
                    db.session.add(setting)
                db.session.commit()
            app.logger.info("New Gmail token generated and saved to database")

    return build("gmail", "v1", credentials=creds)


def _extract_content(msg: dict) -> dict:
    """Pull subject, sender, plain-text body, Message-ID, thread ID, and sent time."""
    from datetime import datetime
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

    # If from_email is a Google Group, extract volunteer name from subject or body
    # Google Group emails have "From: Group Name <group@googlegroups.com>" or similar
    sender_name = None
    if "googlegroups.com" in from_email.lower():
        # Try to extract actual sender from "On behalf of" or "From:" patterns in body
        lines = body.split("\n")
        for line in lines[:10]:  # Check first 10 lines
            if "on behalf of" in line.lower():
                # Extract name after "on behalf of"
                parts = line.split("on behalf of")
                if len(parts) > 1:
                    sender_name = parts[1].strip().split("<")[0].strip()
                    break
            elif line.startswith("From:") or line.startswith("from:"):
                sender_name = line.split(":", 1)[1].strip().split("<")[0].strip()
                break

    # internalDate is epoch milliseconds (UTC) set by Gmail when it received the message
    sent_at = None
    try:
        ms = int(msg.get("internalDate") or 0)
        if ms:
            sent_at = datetime.utcfromtimestamp(ms / 1000)
    except (ValueError, TypeError):
        pass

    return {
        "subject": subject,
        "from_email": from_email,
        "sender_name": sender_name,  # New field for Google Group emails
        "body": body,
        "message_id": message_id,
        "thread_id": msg.get("threadId", ""),
        "sent_at": sent_at,
    }


def _apply_parsed(app, parsed, content, sender_email=None, ignore_registration=False):
    """
    Try to apply an add/remove action from a parsed result.
    volunteer_email may be a string or a list (multiple volunteers in one email).
    Returns a list of result dicts, one per volunteer+date combination, each with:
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

    # Claude may return a list of emails when multiple volunteers are mentioned
    vol_emails = vol_email if isinstance(vol_email, list) else [vol_email]
    date_strs = date_val if isinstance(date_val, list) else [date_val]
    cap = app.config["MAX_VOLUNTEERS_PER_SHIFT"]
    today = date.today()
    results = []
    any_success = False
    changed_by_note = f"Email from {sender_email}" if sender_email else "Email (LLM)"

    for single_email in vol_emails:
        target_user = User.query.filter_by(email=single_email, active=True).first()
        if not target_user and ignore_registration:
            # Fallback: match by username part of the guessed email against volunteer names.
            # e.g. "mabelcrain@..." matches "Mabel Crain" — useful when Claude guesses an
            # email that isn't registered but the name maps to a known volunteer.
            username = single_email.split("@")[0].lower().replace(".", "").replace("_", "").replace("-", "")
            for candidate in User.query.filter_by(active=True).all():
                candidate_key = candidate.name.lower().replace(" ", "")
                if username in candidate_key or candidate_key in username:
                    target_user = candidate
                    break
        if not target_user:
            for date_str in date_strs:
                results.append({
                    "date": date_str,
                    "shift_type": shift_type,
                    "action": action,
                    "volunteer_name": single_email,
                    "status": "not_found",
                    "message": f"No active volunteer found with email {single_email}.",
                })
            continue

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

            fmt_date = target_date.strftime("%-m/%-d (%a)")

            if target_date < today:
                r["status"] = "skipped_past"
                r["message"] = f"{fmt_date} is in the past — skipped."
                results.append(r)
                continue

            materialize_if_needed(target_date, shift_type)

            if action == "add":
                existing = ShiftAssignment.query.filter_by(
                    date=target_date, shift_type=shift_type, user_id=target_user.id,
                ).first()
                if existing:
                    r["status"] = "already_assigned"
                    r["message"] = f"{target_user.name} is already on {shift_type} on {fmt_date}."
                    results.append(r)
                    continue
                count = ShiftAssignment.query.filter_by(
                    date=target_date, shift_type=shift_type,
                ).count()
                if count >= cap:
                    r["status"] = "at_capacity"
                    r["message"] = f"{shift_type} shift on {fmt_date} is full ({cap}/{cap} volunteers)."
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
                r["message"] = f"Added {target_user.name} to {shift_type} on {fmt_date}."
                any_success = True

            elif action == "remove":
                existing = ShiftAssignment.query.filter_by(
                    date=target_date, shift_type=shift_type, user_id=target_user.id,
                ).first()
                if not existing:
                    r["status"] = "not_found"
                    r["message"] = f"{target_user.name} is not on {shift_type} on {fmt_date}."
                    results.append(r)
                    continue
                db.session.delete(existing)
                db.session.add(ScheduleChangeLog(
                    log_type="upcoming", date=target_date, shift_type=shift_type,
                    action="remove", volunteer_id=target_user.id, volunteer_name=target_user.name,
                    changed_by_note=changed_by_note,
                ))
                r["status"] = "success"
                r["message"] = f"Removed {target_user.name} from {shift_type} on {target_date.strftime('%-m/%-d (%a)')}."
                any_success = True

            results.append(r)

    if any_success:
        db.session.commit()

    return results


def _send_summary_email(app, service, content, parsed, results, processing_error=None):
    """Reply to the original email thread with a processing summary for the owner.
    Also sends to volunteer group if there were successful schedule changes."""
    owner_email = app.config.get("OWNER_EMAIL", "")
    monitor_email = app.config.get("GMAIL_MONITOR_EMAIL", "")
    group_email = "acrpetco86@googlegroups.com"
    if not owner_email:
        return

    status_icons = {
        "success": "✅",
        "skipped_past": "⏭️",
        "already_assigned": "ℹ️",
        "not_found": "ℹ️",
        "not_registered": "⚠️",
        "at_capacity": "⚠️",
        "low_confidence": "⚠️",
    }
    lines = []
    any_success = False
    if processing_error:
        lines.append(f"⚠️ Error during processing — please handle manually:")
        lines.append(f"   {processing_error}")
    elif not results and parsed.get("action") in (None, "unknown"):
        lines.append("Claude could not determine a schedule action — please review:")
        lines.append(f"   {parsed.get('reason', 'No schedule change detected.')}")
    elif not results:
        lines.append("No changes were applied.")
    else:
        any_success = any(r["status"] == "success" for r in results)
        needs_review = any(r["status"] in ("low_confidence", "not_registered") for r in results)
        if any_success and not needs_review:
            lines.append("The schedule has been automatically updated:")
        elif any_success:
            lines.append("The schedule was partially updated — please review the remaining items:")
        else:
            lines.append("No changes were applied — please review:")
        lines.append("")
        for r in results:
            icon = status_icons.get(r["status"], "❓")
            lines.append(f"   {icon} {r['message']}")

    body = "\n".join(lines)
    original_subject = content.get("subject", "")
    reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"

    msg = MIMEText(body)
    # Send to both owner and group if successful changes, else just owner
    if any_success:
        msg["to"] = f"{owner_email}, {group_email}"
    else:
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


def _build_upcoming_schedules(volunteers, today=None):
    """
    Return dict mapping volunteer email (lower) → sorted list of 'YYYY-MM-DD SH' strings
    for the next 120 days.

    Source of truth: ShiftAssignment for days that have been materialized; RegularSchedule
    pattern for days that have not yet been materialized.
    """
    from models import ShiftAssignment, RegularSchedule
    from datetime import date as _date, timedelta

    if today is None:
        today = _date.today()
    end_date = today + timedelta(days=120)
    vol_ids = [v.id for v in volunteers]

    # Volunteer-specific materialized assignments in range
    vol_assignments = ShiftAssignment.query.filter(
        ShiftAssignment.date >= today,
        ShiftAssignment.date <= end_date,
        ShiftAssignment.user_id.in_(vol_ids),
    ).all()

    # All materialized (date, shift_type) pairs in range — regardless of who's on them.
    # If a shift has been written to the DB, ShiftAssignment is the source of truth;
    # absence means the volunteer was removed / was never added after materialization.
    materialized_keys = {
        (row.date, row.shift_type)
        for row in ShiftAssignment.query.filter(
            ShiftAssignment.date >= today,
            ShiftAssignment.date <= end_date,
        ).with_entities(ShiftAssignment.date, ShiftAssignment.shift_type).distinct()
    }

    # Per-volunteer set of confirmed assignments
    confirmed: dict[int, set] = {v.id: set() for v in volunteers}
    for sa in vol_assignments:
        confirmed[sa.user_id].add((sa.date, sa.shift_type))

    # Regular schedule pattern (day_of_week 0=Mon … 6=Sun)
    all_rs = RegularSchedule.query.filter(RegularSchedule.user_id.in_(vol_ids)).all()
    regular_by_user: dict[int, list] = {v.id: [] for v in volunteers}
    for rs in all_rs:
        regular_by_user[rs.user_id].append((rs.day_of_week, rs.shift_type))

    result = {}
    for v in volunteers:
        shifts: set[tuple] = set(confirmed[v.id])  # materialized & confirmed

        # Fill in regular-schedule days that haven't been materialized yet
        cur = today
        while cur <= end_date:
            dow = cur.weekday()  # 0=Monday
            for (reg_dow, reg_st) in regular_by_user[v.id]:
                if reg_dow == dow and (cur, reg_st) not in materialized_keys:
                    shifts.add((cur, reg_st))
            cur += timedelta(days=1)

        result[v.email.lower()] = sorted(
            f"{d.isoformat()} {st}" for (d, st) in shifts
        )

    return result


def _resolve_date_range(parsed, upcoming_schedules):
    """
    If parsed contains a date_range, intersect it with the volunteer's upcoming
    schedule and return the matching dates as a list of YYYY-MM-DD strings.
    Returns None if no matching dates are found or if date_range is absent.
    """
    from datetime import date as _date

    date_range = parsed.get("date_range")
    if not date_range:
        return None

    vol_email = (parsed.get("volunteer_email") or "").lower()
    shift_type = parsed.get("shift_type")

    try:
        start = _date.fromisoformat(date_range["start"])
        end = _date.fromisoformat(date_range["end"])
    except (KeyError, ValueError, TypeError):
        return None

    vol_shifts = upcoming_schedules.get(vol_email, [])
    matching = []
    for entry in vol_shifts:
        # entry format: "YYYY-MM-DD SH"
        parts = entry.split()
        if len(parts) != 2:
            continue
        try:
            d = _date.fromisoformat(parts[0])
        except ValueError:
            continue
        st = parts[1]
        if start <= d <= end and (shift_type is None or st == shift_type):
            matching.append(parts[0])

    return matching or None


def _process_one(app, service, msg_id, volunteers, ignore_registration=False):
    """
    Fetch, parse, and apply a single Gmail message.
    Returns (status, error_msg, parsed, content).
    """
    from services.llm_parser import parse_email_schedule_request

    volunteer_emails = {v.email.lower() for v in volunteers}
    upcoming_schedules = _build_upcoming_schedules(volunteers)
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

        sender = content["from_email"].lower()

        # If email is from Google Group and we extracted a sender_name, try to match by name
        if content.get("sender_name") and "googlegroups.com" in sender:
            # Try to find volunteer by name
            sender_name_lower = content["sender_name"].lower()
            for vol in volunteers:
                if vol["name"].lower() == sender_name_lower:
                    sender = vol["email"].lower()
                    break

        if sender not in volunteer_emails and not ignore_registration:
            app.logger.info(
                "Gmail monitor: sender %s not a registered volunteer – running LLM for review only",
                sender,
            )
            # Still parse with LLM so the owner is notified about schedule-related
            # content (e.g. coverage requests) from group members not yet in the system.
            parsed = parse_email_schedule_request(
                email_subject=content["subject"],
                email_body=content["body"],
                email_from=content["from_email"],
                volunteers=volunteers,
                upcoming_schedules=upcoming_schedules,
            )
            if parsed.get("date_range") and not parsed.get("date"):
                resolved = _resolve_date_range(parsed, upcoming_schedules)
                if resolved:
                    parsed["date"] = resolved
            parsed["_not_registered"] = True
            _send_summary_email(app, service, content, parsed, [{
                "status": "not_registered",
                "message": (
                    f"⚠️ {content['from_email']} is not a registered volunteer — "
                    "no changes were applied. Add them to the volunteer list if needed."
                ),
            }])
        else:
            is_volunteer = True
            parsed = parse_email_schedule_request(
                email_subject=content["subject"],
                email_body=content["body"],
                email_from=content["from_email"],
                volunteers=volunteers,
                upcoming_schedules=upcoming_schedules,
            )
            if parsed.get("date_range") and not parsed.get("date"):
                resolved = _resolve_date_range(parsed, upcoming_schedules)
                if resolved:
                    parsed["date"] = resolved

            if parsed.get("error") and parsed.get("action") == "unknown":
                # LLM/API-level error — treat as failed
                status = "failed"
                error_msg = parsed.get("error")
            else:
                results = _apply_parsed(app, parsed, content, sender_email=content["from_email"],
                                        ignore_registration=ignore_registration)
                status = "success" if any(r["status"] == "success" for r in results) else "no_action"
                # Reply when a change was made, Claude flagged low confidence,
                # couldn't classify the email, or an action was attempted but failed
                # (not_found, at_capacity) so the owner can handle it manually.
                needs_owner_review = any(
                    r["status"] in ("not_found", "at_capacity") for r in results
                )
                if (any(r["status"] in ("success", "low_confidence") for r in results)
                        or needs_owner_review
                        or parsed.get("action") == "unknown"):
                    _send_summary_email(app, service, content, parsed, results)

    except Exception as exc:
        from models import db as _db
        _db.session.rollback()  # Clear any aborted transaction so the log update can still commit
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
                sent_at=content.get("sent_at"),
            ))
            db.session.commit()


def reprocess_message(app, log_id: int, ignore_registration: bool = False) -> None:
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
    status, error_msg, parsed, content = _process_one(
        app, service, log.gmail_message_id, volunteers,
        ignore_registration=ignore_registration,
    )

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
    if content.get("sent_at"):
        log.sent_at = content["sent_at"]
    db.session.commit()
