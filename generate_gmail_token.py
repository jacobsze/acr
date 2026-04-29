#!/usr/bin/env python3
"""Generate Gmail API token interactively."""
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
token_file = "token.json"

print("Initializing Gmail API token generation...")
print("A browser window should open asking for permission.\n")

try:
    flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
    creds = flow.run_local_server(port=8080)

    # Save token to file
    with open(token_file, "w") as f:
        f.write(creds.to_json())

    print("✓ Gmail API token generated successfully!")
    print(f"✓ Token saved to {token_file}")
    print("\nYou can now use email notifications in the app.")
except Exception as e:
    print(f"✗ Error: {e}")
    exit(1)
