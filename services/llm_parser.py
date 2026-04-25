"""
Use Claude to interpret an email from a volunteer and extract a schedule
change request as structured data.
"""
import json
from datetime import date

from flask import current_app


DEFAULT_INSTRUCTIONS = """\
Determine:
1. Is this a schedule change request?
2. Which volunteer is making the request? Match their name or email to the list above.
3. What action: "add" (they want to pick up a shift) or "remove" (they want to drop a shift)?
4. What date? Convert relative expressions ("this Saturday", "next Tuesday") to YYYY-MM-DD.\
 If only a day of week is given with no specific date, assume the next upcoming occurrence of that day.
5. Which shift: AM or PM?

Coverage requests ("I can't make it", "can someone cover my shift?") should be treated as\
 action "remove" with confidence "low" — the volunteer wants to drop the shift but a human\
 needs to confirm and find a replacement. Do not set action to "unknown" for coverage requests.

If this is not a schedule request at all, set action to "unknown".\
"""


def get_instructions() -> str:
    """Load custom instructions from DB, falling back to the default."""
    try:
        from models import AppSetting
        setting = AppSetting.query.get("llm_instructions")
        if setting and setting.value.strip():
            return setting.value
    except Exception:
        pass
    return DEFAULT_INSTRUCTIONS


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
    instructions = get_instructions()

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

{instructions}

ALWAYS FOLLOW THIS RULE: If a volunteer says they cannot make a shift, need someone to \
cover for them, or is asking for coverage (e.g. "I can't make it", "can someone cover \
my shift", "I won't be able to come"), classify as action="remove" with confidence="low" \
and identify the date/shift from context. Never return action="unknown" for coverage requests.

Respond with ONLY a JSON object — no markdown, no extra text:
{{
  "action": "add" | "remove" | "unknown",
  "volunteer_email": "matched@email.com" | null,
  "date": "YYYY-MM-DD" | null,
  "shift_type": "AM" | "PM" | null,
  "confidence": "high" | "medium" | "low",
  "reason": "one-sentence explanation"
}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if Claude ignored the formatting instruction
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        if not raw:
            return {
                "action": "unknown",
                "volunteer_email": None,
                "date": None,
                "shift_type": None,
                "confidence": "low",
                "reason": "Claude returned an empty response.",
                "error": "Empty response from API",
            }

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
            "error": f"{exc} | raw: {raw!r}",
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
