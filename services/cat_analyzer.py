"""Analyze volunteer emails to extract per-cat updates using exact quotes."""
import json
import logging
from datetime import datetime, timedelta, date as _date, timezone as _tz

logger = logging.getLogger(__name__)

_ET = None

def _get_et():
    global _ET
    if _ET is None:
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")
    return _ET


def _sent_at_to_et_date(sent_at):
    """Convert a naive-UTC datetime from DB to an Eastern date."""
    if sent_at is None:
        return _date.today()
    return sent_at.replace(tzinfo=_tz.utc).astimezone(_get_et()).date()


def _strip_reply_chain(body):
    """Remove quoted reply lines (starting with '>') from an email body."""
    lines = body.split("\n")
    filtered = [line for line in lines if not line.lstrip().startswith(">")]
    return "\n".join(filtered).strip()


def _detect_shift_from_assignment(user_id, email_date, app):
    """Return AM or PM based on the sender's ShiftAssignment on that date."""
    from models import ShiftAssignment
    assignments = ShiftAssignment.query.filter_by(
        user_id=user_id, date=email_date,
    ).all()
    if len(assignments) == 1:
        return assignments[0].shift_type
    return None  # No assignment or ambiguous (both shifts)


def _detect_shift_from_text(subject, body):
    """Fall back to keyword parsing in email subject/body."""
    text = ((subject or "") + " " + (body or "")).lower()
    am_keywords = ["am shift", "morning shift", " am ", "a.m.", "this morning"]
    pm_keywords = ["pm shift", "afternoon shift", "evening shift", " pm ", "p.m.", "this afternoon", "this evening"]
    if any(kw in text for kw in am_keywords):
        return "AM"
    if any(kw in text for kw in pm_keywords):
        return "PM"
    return None


def _extract_cat_updates(email_data, known_cat_names):
    """Use Claude Haiku to extract structured per-cat data from a volunteer email."""
    from anthropic import Anthropic
    client = Anthropic()

    known_cats_str = ", ".join(known_cat_names) if known_cat_names else "none on record yet"

    prompt = f"""You are analyzing a volunteer shift report email for a cat shelter.

Known cats at this shelter: {known_cats_str}

EMAIL:
Subject: {email_data['subject']}
From: {email_data['sender']}

Body:
{email_data['body']}

---

Your task: For each cat mentioned, extract structured information.

Rules:
1. Use the volunteer's EXACT words for the "notes" field — do not paraphrase or summarize.
2. Capture complete sentences — never cut off a sentence mid-way.
3. If a sentence mentions multiple cats, include it verbatim in "notes" for ALL of those cats.
4. If the email uses "all the cats", "everybody", "they all", or similar group language, include that exact text for EVERY known cat.
5. If the email refers to "the pair" or "both of them" or similar implicit group, include the text for all cats that are plausibly in that group.
6. Only include cats that are actually mentioned (directly or implicitly) — skip cats with no mention.
7. Ignore reply-chain lines (already stripped), email signatures, and scheduling content.
8. Match cat names case-insensitively to the known cats list.

For "bowel" — if the email mentions litter box / bathroom habits for this cat, classify as one of:
  pee only | poop only | pee and poop | none | other: <exact description>
  Use null if not mentioned.

For "food" — if the email mentions eating/food for this cat, classify as one of:
  all | 3/4 | 1/2 | 1/4 | none
  Use null if not mentioned.

Return ONLY valid JSON, no other text:
{{
  "cats": [
    {{
      "name": "cat name (matched to known cats list)",
      "bowel": "pee only|poop only|pee and poop|none|other: <description>|null",
      "food": "all|3/4|1/2|1/4|none|null",
      "notes": "exact text from the email about this cat (complete sentences)"
    }}
  ]
}}

If no cats are mentioned return: {{"cats": []}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response


def analyze_emails_for_cats(app, days_back=None, sample_size=None, force_since=None):
    """
    Analyze volunteer emails to extract per-cat exact-quote updates.

    Skips emails already represented in CatLog (dedup by Gmail message ID).
    Detects shift (AM/PM) from ShiftAssignment first, then email text.

    force_since: if provided (a date), delete all CatLog entries on/after that date
                 so those emails get re-analyzed from scratch.
    """
    with app.app_context():
        from models import EmailProcessingLog, Cat, CatLog, User, db

        # Optionally purge existing CatLog entries to force re-analysis
        if force_since:
            deleted = CatLog.query.filter(CatLog.date >= force_since).delete()
            db.session.commit()
            app.logger.info("[CAT_ANALYZER] Deleted %d CatLog entries since %s for re-analysis", deleted, force_since)

        # Known cats from DB (used in the Claude prompt)
        known_cats = Cat.query.order_by(Cat.name).all()
        known_cat_names = [c.name for c in known_cats]

        # Message IDs already saved to CatLog — don't reprocess
        processed_ids = {
            row[0]
            for row in db.session.query(CatLog.email_message_id)
            .filter(CatLog.email_message_id.isnot(None))
            .distinct()
            .all()
        }

        # Fetch candidate emails filtered by sent_at (ET-aware)
        query = (
            EmailProcessingLog.query
            .filter(EmailProcessingLog.body_snippet.isnot(None))
            .order_by(EmailProcessingLog.sent_at.desc())
        )
        if days_back:
            cutoff = datetime.utcnow() - timedelta(days=days_back)
            query = query.filter(EmailProcessingLog.sent_at >= cutoff)

        all_emails = query.all()
        emails = [e for e in all_emails if e.gmail_message_id not in processed_ids]

        if sample_size:
            emails = emails[:sample_size]

        app.logger.info(
            "[CAT_ANALYZER] %d new emails to process (skipped %d already done)",
            len(emails), len(all_emails) - len(emails),
        )

        email_to_user = {
            u.email.lower(): u
            for u in User.query.filter_by(active=True).all()
        }

        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0
        cats_processed = 0
        cats_created = 0

        for i, email_log in enumerate(emails, 1):
            app.logger.info("[%d/%d] %s", i, len(emails), email_log.subject)
            try:
                # Use Eastern time for date assignment to avoid UTC midnight rollover
                email_date = _sent_at_to_et_date(email_log.sent_at)

                # Determine shift: ShiftAssignment first, then text fallback
                sender = (email_log.sender_email or "").lower()
                sender_user = email_to_user.get(sender)
                shift_type = None
                if sender_user:
                    shift_type = _detect_shift_from_assignment(sender_user.id, email_date, app)
                if not shift_type:
                    shift_type = _detect_shift_from_text(email_log.subject, email_log.body_snippet)

                # Strip reply-chain content before sending to Claude
                clean_body = _strip_reply_chain(email_log.body_snippet or "")

                response = _extract_cat_updates(
                    {
                        "subject": email_log.subject or "",
                        "sender": email_log.sender_email or "",
                        "body": clean_body,
                    },
                    known_cat_names,
                )

                if not response or not response.content:
                    app.logger.warning("  Empty response for %s", email_log.gmail_message_id)
                    continue

                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                # Haiku pricing: $0.80/M input, $4.00/M output
                total_cost += (
                    response.usage.input_tokens / 1_000_000 * 0.80
                    + response.usage.output_tokens / 1_000_000 * 4.00
                )

                raw = response.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.lstrip("`").lstrip("json").strip().rstrip("`").strip()

                data = json.loads(raw)
                cat_updates = data.get("cats", [])

                for entry in cat_updates:
                    cat_name = (entry.get("name") or "").strip()
                    update_text = (entry.get("notes") or "").strip()
                    bowel = entry.get("bowel") or None
                    food = entry.get("food") or None

                    if not cat_name:
                        continue
                    # Normalize null strings from Claude
                    if bowel in ("null", ""):
                        bowel = None
                    if food in ("null", ""):
                        food = None

                    cat = Cat.query.filter_by(name=cat_name).first()
                    if not cat:
                        cat = Cat(name=cat_name, status="at_shelter")
                        db.session.add(cat)
                        db.session.flush()
                        cats_created += 1
                        known_cat_names.append(cat_name)
                        app.logger.info("  Created new cat: %s", cat_name)

                    cat.last_seen_date = email_date

                    db.session.add(CatLog(
                        cat_id=cat.id,
                        date=email_date,
                        shift_type=shift_type,
                        notes=update_text or None,
                        bowel_movement=bowel,
                        food_intake=food,
                        volunteer_name=email_log.sender_email,
                        email_message_id=email_log.gmail_message_id,
                    ))
                    cats_processed += 1

                db.session.commit()
                app.logger.info(
                    "  ✓ %d update(s) [shift=%s]", len(cat_updates), shift_type or "unknown"
                )

            except json.JSONDecodeError as e:
                app.logger.warning("  ✗ JSON parse error for %s: %s", email_log.gmail_message_id, e)
            except Exception as e:
                app.logger.exception("  ✗ Error on %s: %s", email_log.gmail_message_id, e)
                db.session.rollback()

        app.logger.info(
            "[CAT_ANALYZER] Done. %d updates, %d new cats, cost $%.4f",
            cats_processed, cats_created, total_cost,
        )

        return {
            "total_emails": len(emails),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cost": total_cost,
            "cats_processed": cats_processed,
            "cats_created": cats_created,
        }
