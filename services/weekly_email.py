"""Send the weekly volunteer schedule email."""
import base64
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


WEEKLY_EMAIL_RECIPIENT = "jacob.sze@gmail.com"

_AM_HDR  = "#b8cedd"
_PM_HDR  = "#b8d8b8"
_AM_CELL = "#daeaf3"
_PM_CELL = "#daf0da"
_TD_BASE = "border:1px solid #ccc; padding:7px 10px; vertical-align:top;"


def _cell(names, bg):
    if not names:
        return (
            f'<td style="{_TD_BASE} background:{bg}; text-align:center; '
            f'color:#cc0000; font-weight:bold;">Need Volunteers</td>'
        )
    return (
        f'<td style="{_TD_BASE} background:{bg}; text-align:center;">'
        + "<br>".join(names)
        + "</td>"
    )


def _build_html(all_weeks, subject):
    app_url = "https://acr-schedule.onrender.com/"
    procedures_url = (
        "https://docs.google.com/document/d/e/"
        "2PACX-1vQv4SN1q_8k4F51oN3MmrDqv1CYDIZ1cowAdF7YwmURoUp1lVa40yin52SDs6k1Gn83WtkdA8iH42wi"
        "/pub?urp=gmail_link"
    )

    rows = []

    # Top header
    rows.append(
        '<table style="border-collapse:collapse; font-family:Arial,Helvetica,sans-serif;'
        ' font-size:14px; width:520px; max-width:100%;">'
    )
    rows.append(
        f'<tr><td colspan="3" style="{_TD_BASE} font-weight:bold; text-align:center;'
        ' background:#fff;">Volunteer Schedule</td></tr>'
    )

    for week in all_weeks:
        ws = week["week_start"]
        we = week["week_end"]
        week_label = f"{ws.strftime('%b %-d')} - {we.strftime('%b %-d')}"

        # Week header
        rows.append(
            f'<tr><td colspan="3" style="background:#1a1a1a; color:#fff; font-weight:bold;'
            f' text-align:center; padding:8px 0;">{week_label}</td></tr>'
        )
        # Column headers
        rows.append(
            f'<tr>'
            f'<td style="{_TD_BASE}"></td>'
            f'<td style="{_TD_BASE} background:{_AM_HDR}; font-weight:bold; text-align:center;">AM</td>'
            f'<td style="{_TD_BASE} background:{_PM_HDR}; font-weight:bold; text-align:center;">PM</td>'
            f'</tr>'
        )

        for d in week["week_dates"]:
            sched = week["schedule"][d]
            am_names = [u.name for u in sched["AM"]["volunteers"]]
            pm_names = [u.name for u in sched["PM"]["volunteers"]]

            date_cell = (
                f'<td style="{_TD_BASE} white-space:nowrap; font-size:0.85em;">'
                f'{d.strftime("%A")}<br>{d.strftime("%-m/%-d/%Y")}</td>'
            )
            rows.append(f"<tr>{date_cell}{_cell(am_names, _AM_CELL)}{_cell(pm_names, _PM_CELL)}</tr>")

    rows.append("</table>")
    table_html = "\n".join(rows)

    return f"""<html><body style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:#222;">
<p>
  Access the <a href="{app_url}">schedule</a><br>
  <a href="{procedures_url}">Volunteer Procedures</a>
</p>
<p>Here is the schedule for the next two weeks:</p>
{table_html}
</body></html>"""


def send_weekly_schedule_email(app):
    """Build and send the 2-week schedule email. Returns dict with recipient/subject."""
    from services.gmail_monitor import _get_service
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

    html_body = _build_html(all_weeks, subject)

    service = _get_service(app)
    monitor_email = app.config.get("GMAIL_MONITOR_EMAIL", "")
    recipient = WEEKLY_EMAIL_RECIPIENT

    msg = MIMEMultipart("alternative")
    msg["to"]      = recipient
    msg["from"]    = monitor_email
    msg["subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

    app.logger.info("Weekly schedule email sent to %s – %s", recipient, subject)
    return {"recipient": recipient, "subject": subject}
