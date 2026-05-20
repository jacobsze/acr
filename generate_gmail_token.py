#!/usr/bin/env python3
"""Generate Gmail API token and save directly to database."""
import os
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

creds_file = "credentials.json"

print("Initializing Gmail API token generation...")
print("A browser window should open asking for permission.\n")

try:
    # Load credentials config
    if os.path.exists(creds_file):
        with open(creds_file) as f:
            creds_config = json.load(f)
        flow = InstalledAppFlow.from_client_config(creds_config, SCOPES)
    else:
        # Try loading from env var (Render)
        creds_json_env = os.environ.get("GMAIL_CREDENTIALS_FILE", "").strip()
        if creds_json_env and creds_json_env.startswith("{"):
            creds_config = json.loads(creds_json_env)
            flow = InstalledAppFlow.from_client_config(creds_config, SCOPES)
        else:
            raise FileNotFoundError("credentials.json not found and GMAIL_CREDENTIALS_FILE not set")

    creds = flow.run_local_server(port=0)

    # Try to save to database first
    try:
        from app import app
        from models import db, AppSetting

        with app.app_context():
            setting = db.session.get(AppSetting, "gmail_token_json")
            if setting:
                setting.value = creds.to_json()
            else:
                setting = AppSetting(key="gmail_token_json", value=creds.to_json())
                db.session.add(setting)
            db.session.commit()

        print("✓ Gmail API token generated successfully!")
        print("✓ Token saved to database")
        print("\nYou can now use email notifications in the app.")
    except Exception as db_error:
        # Fallback to saving to file
        print(f"⚠ Could not save to database: {db_error}")
        print("Saving to token.json instead...")
        with open("token.json", "w") as f:
            f.write(creds.to_json())
        print("✓ Token saved to token.json")
        print("⚠ Note: On next app startup, it will be migrated to the database")

except Exception as e:
    print(f"✗ Error: {e}")
    exit(1)

