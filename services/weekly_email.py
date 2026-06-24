"""Send the weekly volunteer schedule email and daily open-shift alerts."""
import base64
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


WEEKLY_EMAIL_RECIPIENT       = "acrpetco86@googlegroups.com"
OPEN_SHIFT_EMAIL_RECIPIENT   = "acrpetco86@googlegroups.com"
CHANGE_NOTIFICATION_RECIPIENT = "acrpetco86@googlegroups.com"

_AM_HDR  = "#b8cedd"
_PM_HDR  = "#b8d8b8"
_AM_CELL = "#daeaf3"
_PM_CELL = "#daf0da"
_TD_BASE = "border:1px solid #ccc; padding:7px 10px; vertical-align:top;"


# ── Shared HTML helpers ───────────────────────────────────────────────────────

def _cell(names, bg, highlight=False):
    """Render one AM or PM schedule cell."""
    if not names:
        style = f"{_TD_BASE} background:{bg}; text-align:center; color:#cc0000; font-weight:bold;"
        return f'<td style="{style}">Need Volunteers</td>'
    outline = " outline:3px solid #cc0000; outline-offset:-3px;" if highlight else ""
    style = f"{_TD_BASE} background:{bg}; text-align:center;{outline}"
    return f'<td style="{style}">' + "<br>".join(names) + "</td>"


def _build_table(all_weeks, highlight_date=None, highlight_shifts=None):
    """Build the <table>…</table> HTML. Optionally highlight specific cells."""
    hs = set(highlight_shifts or [])
    rows = [
        '<table style="border-collapse:collapse; font-family:Arial,Helvetica,sans-serif;'
        ' font-size:14px; width:520px; max-width:100%;">' ,
        f'<tr><td colspan="3" style="{_TD_BASE} font-weight:bold; text-align:center;'
        ' background:#f0f0f0;">Volunteer Schedule</td></tr>',
    ]

    for week in all_weeks:
        ws, we = week["week_start"], week["week_end"]
        week_label = f"{ws.strftime('%b %-d')} - {we.strftime('%b %-d')}"
        rows += [
            f'<tr><td colspan="3" style="background:#1a1a1a; color:#fff; font-weight:bold;'
            f' text-align:center; padding:8px 0;">{week_label}</td></tr>',
            f'<tr>'
            f'<td style="{_TD_BASE}"></td>'
            f'<td style="{_TD_BASE} background:{_AM_HDR}; font-weight:bold; text-align:center;">AM</td>'
            f'<td style="{_TD_BASE} background:{_PM_HDR}; font-weight:bold; text-align:center;">PM</td>'
            f'</tr>',
        ]
        for d in week["week_dates"]:
            sched = week["schedule"][d]
            am_names = [u.name for u in sched["AM"]["volunteers"]]
            pm_names = [u.name for u in sched["PM"]["volunteers"]]
            hi_am = (d == highlight_date and "AM" in hs)
            hi_pm = (d == highlight_date and "PM" in hs)
            date_cell = (
                f'<td style="{_TD_BASE} white-space:nowrap; font-size:0.85em;"'
                f'{d.strftime("%A")}<br>{d.strftime("%-m/%-d/%Y")}</td>'
            )
            rows.append(f"<tr>{date_cell}{_cell(am_names, _AM_CELL, hi_am)}{_cell(pm_names, _PM_CELL, hi_pm)}</tr>")

    rows.append("</table>")
    return "\n".join(rows)


def _send_gmail(app, recipient, subject, html_body):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_user = app.config.get("GMAIL_SMTP_USER", "")
    smtp_password = app.config.get("GMAIL_SMTP_PASSWORD", "")

    if not smtp_user or not smtp_password:
        raise ValueError("GMAIL_SMTP_USER and GMAIL_SMTP_PASSWORD not configured")

    msg = MIMEMultipart("alternative")
    msg["to"] = recipient
    msg["from"] = smtp_user
    msg["subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        app.logger.info("Email sent successfully to %s: %s", recipient, subject)
    except Exception as e:
        app.logger.error("Failed to send email to %s: %s", recipient, str(e), exc_info=True)
        raise


# ── Weekly schedule email ─────────────────────────────────────────────────────

def send_weekly_schedule_email(app, recipient=None):
    """Build and send the 3-week schedule email. Safe to call from background threads."""
    with app.app_context():
        return _send_weekly_schedule_email(app, recipient=recipient)


def _send_weekly_schedule_email(app, recipient=None):
    from routes.schedule_routes import get_week_start, get_week_dates, build_schedule
    from zoneinfo import ZoneInfo
    from datetime import datetime

    ny_now = datetime.now(ZoneInfo("America/New_York"))
    today = ny_now.date()
    week_start = get_week_start(today)

    all_weeks = []
    for i in range(3):
        ws = week_start + timedelta(weeks=i)
        wd = get_week_dates(ws)
        all_weeks.append({
            "week_start": ws,
            "week_end": ws + timedelta(days=6),
            "week_dates": wd,
            "schedule": build_schedule(wd, None),
        })

    start_label = week_start.strftime("%b %-d")
    end_label   = (week_start + timedelta(days=20)).strftime("%b %-d")
    subject     = f"ACR Schedule for {start_label} - {end_label}"

    app_url = "https://acr-schedule.onrender.com/"
    public_schedule_url = "https://acr-schedule.onrender.com/public"
    procedures_url = (
        "https://docs.google.com/document/d/e/"
        "2PACX-1vQv4SN1q_8k4F51oN3MmrDqv1CYDIZ1cowAdF7YwmURoUp1lVa40yin52SDs6k1Gn83WtkdA8iH42wi"
        "/pub?urp=gmail_link"
    )
    table_html = _build_table(all_weeks)
    html_body = f"""<html><body style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:#222;">
<p>
<a href="{app_url}">Access the app</a><br>
<a href="{public_schedule_url}">Access the schedule</a><br>
<a href="{procedures_url}">Volunteer Procedures</a>
</p>
<p>Here is the schedule for the next three weeks:</p>
{table_html}
</body></html>"""

    target_recipient = recipient or WEEKLY_EMAIL_RECIPIENT
    _send_gmail(app, target_recipient, subject, html_body)
    app.logger.info("Weekly schedule email sent – %s", subject)
    return {"recipient": target_recipient, "subject": subject}


def send_schedule_change_email(app, changed_by_name, adds, removes, is_admin, changed_by_email):
    """Send notification of schedule changes to the group."""
    with app.app_context():
        return _send_schedule_change_email(app, changed_by_name, adds, removes, is_admin, changed_by_email)


def _send_schedule_change_email(app, changed_by_name, adds, removes, is_admin, changed_by_email):
    """Build and send schedule change notification email."""
    from zoneinfo import ZoneInfo
    from datetime import datetime

    change_list = []
    if removes:
        for r in removes:
            change_list.append(f"<li><strong>{r['name']}</strong> removed from {r['date'].strftime('%a %b %-d')} {r['shift_type']}</li>")
    if adds:
        for a in adds:
            change_list.append(f"<li><strong>{a['name']}</strong> added to {a['date'].strftime('%a %b %-d')} {a['shift_type']}</li>")

    change_html = "\n".join(change_list)
    change_count = len(removes) + len(adds)
    subject = f"Schedule Update: {change_count} change{'s' if change_count != 1 else ''}"

    html_body = f"""<html><body style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:#222;">
<p>
Schedule has been updated by <strong>{changed_by_name}</strong> ({changed_by_email}):
</p>
<ul>
{change_html}
</ul>
<p>
<a href="https://acr-schedule.onrender.com/schedule">View the schedule</a>
</p>
</body></html>"""

    _send_gmail(app, CHANGE_NOTIFICATION_RECIPIENT, subject, html_body)
    app.logger.info("Schedule change email sent by %s – %s", changed_by_name, subject)
    return {"recipient": CHANGE_NOTIFICATION_RECIPIENT, "subject": subject}