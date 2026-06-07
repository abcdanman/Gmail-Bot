# Gmail Cleaner MCP

An MCP (Model Context Protocol) server that lets Claude clean up your Gmail storage — delete old emails, duplicates, newsletters, and large attachments.

## Setup

### 1. Install Python dependencies

```bash
cd "C:\Users\hkami\Downloads\FYP UM\Gmail bot"
pip install -r requirements.txt
```

### 2. Create Google Cloud credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Enable the **Gmail API**:
   - Navigate to **APIs & Services → Library**
   - Search for "Gmail API" → click **Enable**
4. Create OAuth credentials:
   - Go to **APIs & Services → Credentials**
   - Click **+ CREATE CREDENTIALS → OAuth client ID**
   - Application type: **Desktop app**
   - Click **Create**, then **Download JSON**
5. Rename the downloaded file to `credentials.json` and place it in this folder:
   ```
   C:\Users\hkami\Downloads\FYP UM\Gmail bot\credentials.json
   ```
6. If prompted to configure the OAuth consent screen:
   - User Type: **External**
   - Fill in app name and your email
   - Add your Gmail address as a **Test user**

### 3. Authenticate with Gmail

Run this once to authorize the app and save your token:

```bash
python setup_oauth.py
```

A browser window will open. Sign in with your Gmail account and grant permission. The token is saved locally — you won't need to do this again unless the token expires.

### 4. Connect to Claude Desktop

Edit your Claude Desktop config file:

- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Add this block inside the `mcpServers` object:

```json
{
  "mcpServers": {
    "gmail-cleaner": {
      "command": "python",
      "args": ["C:\\Users\\hkami\\Downloads\\FYP UM\\Gmail bot\\gmail_cleaner_mcp.py"]
    }
  }
}
```

Restart Claude Desktop. You should see **gmail-cleaner** in the connected tools.

---

## Available Tools

| Tool | Description |
|------|-------------|
| `get_storage_info` | Show your Gmail account stats |
| `list_old_emails` | Preview emails older than N days |
| `find_duplicates` | Find duplicate emails (same sender + subject) |
| `list_emails_by_sender` | List all emails from a specific sender |
| `list_large_emails` | Find biggest emails taking up space |
| `list_promotional_emails` | List newsletter/promotional emails |
| `delete_old_emails` | Delete emails older than N days |
| `delete_duplicate_emails` | Keep only the newest copy of duplicates |
| `delete_emails_by_sender` | Delete all emails from a sender |
| `delete_promotional_emails` | Delete all promotional emails |
| `empty_trash` | Empty Trash to reclaim storage |
| `empty_spam` | Empty Spam folder |

---

## Example conversation with Claude

> "Scan my Gmail and tell me what's taking up the most space"

> "Delete all emails older than 2 years, but keep my starred ones"

> "Find and remove duplicate emails from my inbox"

> "Delete everything from noreply@newsletter.com"

> "Clean up my promotions folder"

---

## Safety notes

- **All delete tools default to moving emails to Trash** (`use_trash: True`) — you have 30 days to recover them before Gmail permanently deletes them.
- Starred and Important emails are excluded from bulk old-email deletion by default.
- Always use the `list_*` tools first to preview what will be deleted.
- Set `use_trash: False` only if you are certain you want permanent deletion.
