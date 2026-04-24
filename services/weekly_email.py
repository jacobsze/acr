"""Send the weekly volunteer schedule email and daily open-shift alerts."""
import base64
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


WEEKLY_EMAIL_RECIPIENT       = "jacob.sze@gmail.com"
OPEN_SHIFT_EMAIL_RECIPIENT   = "jacob.sze@gmail.com"
CHANGE_NOTIFICATION_RECIPIENT = "jacob.sze@gmail.com"

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
        ' font-size:14px; width:520px; max-width:100%;">',
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
                f'<td style="{_TD_BASE} white-space:nowrap; font-size:0.85em;">'
                f'{d.strftime("%A")}<br>{d.strftime("%-m/%-d/%Y")}</td>'
            )
            rows.append(f"<tr>{date_cell}{_cell(am_names, _AM_CELL, hi_am)}{_cell(pm_names, _PM_CELL, hi_pm)}</tr>")

    rows.append("</table>")
    return "\n".join(rows)


def _send_gmail(app, recipient, subject, html_body):
    from services.gmail_monitor import _get_service
    service = _get_service(app)
    monitor_email = app.config.get("GMAIL_MONITOR_EMAIL", "")
    msg = MIMEMultipart("alternative")
    msg["to"] = recipient
    msg["from"] = monitor_email
    msg["subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ── Weekly schedule email ─────────────────────────────────────────────────────

def send_weekly_schedule_email(app):
    """Build and send the 2-week schedule email."""
    from routes.schedule_routes import get_week_start, get_week_dates, build_schedule
    from zoneinfo import ZoneInfo
    from datetime import datetime

    ny_now = datetime.now(ZoneInfo("America/New_York"))
    today = ny_now.date()
    week_start = get_week_start(today)

    all_weeks = []
    for i in range(2):
        ws = week_start + timedelta(weeks=i)
        wd = get_week_dates(ws)
        all_weeks.append({
            "week_start": ws,
            "week_end": ws + timedelta(days=6),
            "week_dates": wd,
            "schedule": build_schedule(wd, None),
        })

    start_label = week_start.strftime("%b %-d")
    end_label   = (week_start + timedelta(days=13)).strftime("%b %-d")
    subject     = f"ACR Schedule for {start_label} - {end_label}"

    app_url = "https://acr-schedule.onrender.com/"
    procedures_url = (
        "https://docs.google.com/document/d/e/"
        "2PACX-1vQv4SN1q_8k4F51oN3MmrDqv1CYDIZ1cowAdF7YwmURoUp1lVa40yin52SDs6k1Gn83WtkdA8iH42wi"
        "/pub?urp=gmail_link"
    )
    table_html = _build_table(all_weeks)
    html_body = f"""<html><body style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:#222;">
<p>Access the <a href="{app_url}">schedule</a><br>
<a href="{procedures_url}">Volunteer Procedures</a></p>
<p>Here is the schedule for the next two weeks:</p>
{table_html}
</body></html>"""

    _send_gmail(app, WEEKLY_EMAIL_RECIPIENT, subject, html_body)
    app.logger.info("Weekly schedule email sent – %s", subject)
    return {"recipient": WEEKLY_EMAIL_RECIPIENT, "subject": subject}


# ── Daily open-shift alert ────────────────────────────────────────────────────

def _open_shifts_for_date(target_date, override_open=None):
    """
    Return list of shift types ('AM', 'PM') with 0 volunteers on target_date.
    override_open: if provided, use this list instead of querying the DB (for testing).
    """
    if override_open is not None:
        return list(override_open)
    from routes.schedule_routes import build_schedule
    sched = build_schedule([target_date], None)
    return [st for st in ("AM", "PM") if sched[target_date][st]["count"] == 0]


def _open_shift_subject(open_shifts, target_date):
    date_str = target_date.strftime("%-m/%-d")
    day_str  = target_date.strftime("%a")
    if len(open_shifts) == 2:
        return f"AM and PM shifts are open on {date_str} ({day_str}) - can anyone cover?"
    shift = open_shifts[0]
    return f"{shift} shift is open on {date_str} ({day_str}) - can anyone cover?"


def send_open_shift_alert(app, target_date, open_shifts):
    """Send the open-shift alert for target_date. open_shifts is ['AM'], ['PM'], or ['AM','PM']."""
    from routes.schedule_routes import get_week_start, get_week_dates, build_schedule

    week_start = get_week_start(target_date)
    week_dates = get_week_dates(week_start)
    schedule   = build_schedule(week_dates, None)
    all_weeks  = [{
        "week_start": week_start,
        "week_end":   week_start + timedelta(days=6),
        "week_dates": week_dates,
        "schedule":   schedule,
    }]

    subject  = _open_shift_subject(open_shifts, target_date)
    table    = _build_table(all_weeks, highlight_date=target_date, highlight_shifts=open_shifts)
    html_body = f"""<html><body style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:#222;">
<p>Please let the group know if you can. Thanks!</p>
{table}
</body></html>"""

    _send_gmail(app, OPEN_SHIFT_EMAIL_RECIPIENT, subject, html_body)
    app.logger.info("Open-shift alert sent – %s", subject)
    return {"recipient": OPEN_SHIFT_EMAIL_RECIPIENT, "subject": subject}


def send_schedule_change_email(app, changed_by_name, adds, removes):
    """Send a change-notification email listing which shifts were added or removed."""
    rows = []
    for r in removes:
        rows.append(
            f'<li style="color:#cc0000; margin-bottom:4px;">'
            f'<strong>{r["name"]}</strong> removed from '
            f'{r["shift_type"]} on {r["date"].strftime("%A, %-m/%-d")}</li>'
        )
    for a in adds:
        rows.append(
            f'<li style="color:#1a7a1a; margin-bottom:4px;">'
            f'<strong>{a["name"]}</strong> added to '
            f'{a["shift_type"]} on {a["date"].strftime("%A, %-m/%-d")}</li>'
        )

    n = len(adds) + len(removes)
    subject = f"Schedule update by {changed_by_name}"
    html_body = (
        f'<html><body style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:#222;">'
        f'<p><strong>{changed_by_name}</strong> made '
        f'{n} schedule change{"s" if n != 1 else ""}:</p>'
        f'<ul style="padding-left:20px;">{"".join(rows)}</ul>'
        f'</body></html>'
    )
    _send_gmail(app, CHANGE_NOTIFICATION_RECIPIENT, subject, html_body)
    app.logger.info("Schedule change notification sent – %s", subject)
    return {"recipient": CHANGE_NOTIFICATION_RECIPIENT, "subject": subject}


def check_and_send_open_shift_alert(app):
    """Scheduled job: runs at 10am ET. Sends alert if shifts 2 days out have no volunteers."""
    with app.app_context():
        from zoneinfo import ZoneInfo
        from datetime import datetime
        ny_now = datetime.now(ZoneInfo("America/New_York"))
        target_date = ny_now.date() + timedelta(days=2)
        open_shifts = _open_shifts_for_date(target_date)
        if open_shifts:
            send_open_shift_alert(app, target_date, open_shifts)
        else:
            app.logger.info("Open-shift check: all shifts covered for %s", target_date)
