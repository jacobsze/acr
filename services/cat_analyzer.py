"""Analyze volunteer emails to extract cat information and track costs."""
import base64
import json
import logging
from datetime import datetime, timedelta
from anthropic import Anthropic

logger = logging.getLogger(__name__)
client = Anthropic()


def analyze_emails_for_cats(app, days_back=21, sample_size=None):
    """
    Analyze emails from the past N days to extract cat information.
    Returns token usage and extracted cat data.

    Args:
        app: Flask app context
        days_back: Number of days to look back (default 21 = 3 weeks)
        sample_size: If set, only analyze this many emails (for testing)
    """
    with app.app_context():
        from models import EmailProcessingLog

        # Get emails from past N days - skip schedule change emails (status=success)
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)
        emails = (
            EmailProcessingLog.query
            .filter(
                EmailProcessingLog.processed_at >= cutoff_date,
                EmailProcessingLog.body_snippet.isnot(None),
                EmailProcessingLog.status != "success",  # Skip schedule change emails
            )
            .order_by(EmailProcessingLog.processed_at.desc())
            .all()
        )

        if sample_size:
            emails = emails[:sample_size]

        app.logger.info(f"Analyzing {len(emails)} emails from past {days_back} days...")

        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0
        results = []

        for i, email in enumerate(emails, 1):
            app.logger.info(f"[{i}/{len(emails)}] Analyzing: {email.subject}")

            try:
                email_data = {
                    "subject": email.subject,
                    "body": email.body_snippet,
                    "sender": email.sender_email,
                    "date": email.sent_at.isoformat() if email.sent_at else None,
                }

                response = _extract_cat_data(app, email_data)

                # Check if response has usage data
                if not response or not hasattr(response, 'usage'):
                    app.logger.warning(f"No usage data in response for email {email.gmail_message_id}")
                    continue

                # Track tokens
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens

                app.logger.info(f"  Tokens: {input_tokens} in + {output_tokens} out")

                # Calculate cost (Claude 3.5 Sonnet pricing)
                input_cost = (input_tokens / 1_000_000) * 3  # $3 per 1M input
                output_cost = (output_tokens / 1_000_000) * 15  # $15 per 1M output
                email_cost = input_cost + output_cost
                total_cost += email_cost

                # Parse response
                if not response.content:
                    app.logger.warning(f"  ✗ Response content is empty")
                    continue

                first_block = response.content[0]
                if not hasattr(first_block, 'text'):
                    app.logger.warning(f"  ✗ First content block has no text (type: {type(first_block).__name__})")
                    continue

                content = first_block.text
                app.logger.info(f"  Claude response length: {len(content)}")
                if content:
                    app.logger.info(f"  Claude response: {content[:500]}")
                else:
                    app.logger.warning(f"  ✗ Response text is empty string")
                    continue

                try:
                    data = json.loads(content)
                    results.append({
                        "email_id": email.gmail_message_id,
                        "date": email.sent_at,
                        "volunteer": email.sender_email,
                        "cats": data.get("cats", []),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost": email_cost,
                    })

                    cats = data.get("cats", [])
                    app.logger.info(f"  ✓ Found {len(cats)} cat(s): {[c.get('name') for c in cats]}")

                except json.JSONDecodeError as e:
                    app.logger.warning(f"  ✗ [v3-deployed] Failed to parse JSON: {e}")

            except Exception as e:
                app.logger.exception(f"  Error analyzing email: {str(e)}")

        app.logger.info(f"Analysis complete. Total cost: ${total_cost:.4f}")

        return {
            "total_emails": len(results),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cost": total_cost,
            "results": results,
        }


def _extract_cat_data(app, email_data):
    """Use Claude to extract cat information from an email."""

    prompt = f"""You are analyzing a volunteer shift report email to extract information about cats mentioned.

EMAIL:
Subject: {email_data['subject']}
From: {email_data['sender']}
Date: {email_data['date']}

Body:
{email_data['body']}

---

Your task: Extract every cat mentioned in this email, including casual references.

Look for:
- Direct mentions: "Maria", "TG", "Kiki", etc.
- Activity mentions: "played with TG", "fed Maria", "Maria ate well"
- Status mentions: "SP was calm", "Kiki watched from below"
- Any proper nouns that refer to individual cats

For each cat found:
1. Name/identifier (as the cat is called)
2. Status/condition (healthy, calm, playful, eating well, etc.)
3. Any notes (what they were doing, observations)

Return ONLY valid JSON, no other text:
{{
  "cats": [
    {{
      "name": "cat name",
      "status": "observed status or condition",
      "notes": "activities or observations"
    }}
  ]
}}

If no cats are mentioned, return: {{"cats": []}}

Example: If email says "played with TG and fed Maria", return:
{{
  "cats": [
    {{"name": "TG", "status": "playful", "notes": "played during shift"}},
    {{"name": "Maria", "status": "eating well", "notes": "was fed"}}
  ]
}}"""

    try:
        app.logger.debug(f"Calling Claude API for email analysis...")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        app.logger.debug(f"Response received. Input tokens: {response.usage.input_tokens}, Output: {response.usage.output_tokens}")
        app.logger.debug(f"Response stop reason: {response.stop_reason}, content blocks: {len(response.content)}")
        return response
    except Exception as e:
        app.logger.error(f"Claude API error: {str(e)}", exc_info=True)
        raise
