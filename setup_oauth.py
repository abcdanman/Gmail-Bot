"""Run this once to authenticate with Gmail and save the token."""
import pickle
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE = Path(__file__).parent / "token.pickle"

if not CREDENTIALS_FILE.exists():
    print("ERROR: credentials.json not found.")
    print("Steps to get it:")
    print("  1. Go to https://console.cloud.google.com/")
    print("  2. Create a project (or select one)")
    print("  3. Enable the Gmail API: APIs & Services → Enable APIs → search Gmail API")
    print("  4. Create credentials: APIs & Services → Credentials → + CREATE CREDENTIALS")
    print("     → OAuth client ID → Desktop app → Download JSON")
    print(f"  5. Rename the file to 'credentials.json' and place it at:\n     {CREDENTIALS_FILE}")
else:
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "wb") as f:
        pickle.dump(creds, f)
    print(f"Authentication successful! Token saved to {TOKEN_FILE}")
    print("You can now start the MCP server.")
