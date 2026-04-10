"""
Use Claude to interpret an email from a volunteer and extract a schedule
change request as structured data.
"""
import json
from datetime import date

from flask import current_app


def parse_email_schedule_request(
    email_subject: str,
    email_body: str,
    email_from: str,
    volunteers: list,
    today: date | None = None,
) -> dict:
    """
    Ask Claude to parse an incoming email for a schedule change.

    Returns a dict::

        {
            "action":          "add" | "remove" | "unknown",
            "volunteer_email": "email@example.com" | None,
            "date":            "YYYY-MM-DD" | None,
            "shift_type":      "AM" | "PM" | None,
            "confidence":      "high" | "medium" | "low",
            "reason":          "human-readable explanation",
            "error":           str | None,
        }
    """
    api_key = current_app.config.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "action": "unknown",
            "volunteer_email": None,
            "date": None,
            "shift_type": None,
            "confidence": "low",
            "reason": "Claude API not configured.",
            "error": "ANTHROPIC_API_KEY not set",
        }

    if today is None:
        today = date.today()

    volunteer_list = "\n".join(f"- {v.name} ({v.email})" for v in volunteers)

    prompt = f"""\
You are an assistant helping manage the volunteer shift schedule for a cat rescue shelter.

Today's date: {today.strftime("%A, %B %d, %Y")}

The shelter runs two shifts every day: AM and PM. Up to 3 volunteers per shift.

Registered volunteers:
{volunteer_list}

An email arrived that may contain a schedule change request. Parse it:

From: {email_from}
Subject: {email_subject}
Body:
{email_body}

---

Determine:
1. Is this a schedule change request?
2. Which volunteer is making the request? Match their name or email to the list above.
3. What action: "add" (they want to pick up a shift) or "remove" (they want to drop a shift)?
4. What date? Convert relative expressions ("this Saturday", "next Tuesday") to YYYY-MM-DD.
5. Which shift: AM or PM?

Respond with ONLY a JSON object — no markdown, no extra text:
{{
  "action": "add" | "remove" | "unknown",
  "volunteer_email": "matched@email.com" | null,
  "date": "YYYY-MM-DD" | null,
  "shift_type": "AM" | "PM" | null,
  "confidence": "high" | "medium" | "low",
  "reason": "one-sentence explanation"
}}

If this is not a schedule request, set action to "unknown"."""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        result = json.loads(raw)
        result.setdefault("error", None)
        return result
    except json.JSONDecodeError as exc:
        return {
            "action": "unknown",
            "volunteer_email": None,
            "date": None,
            "shift_type": None,
            "confidence": "low",
            "reason": "Could not parse Claude response.",
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "action": "unknown",
            "volunteer_email": None,
            "date": None,
            "shift_type": None,
            "confidence": "low",
            "reason": "API error.",
            "error": str(exc),
        }
