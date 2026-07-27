import os
import json
import base64
import hashlib
import pickle
from datetime import datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

SCOPES = ["https://mail.google.com/"]
TOKEN_FILE = Path(__file__).parent / "token.pickle"
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"

app = Server("gmail-cleaner")


def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    "credentials.json not found. Download it from Google Cloud Console "
                    "(APIs & Services → Credentials → OAuth 2.0 Client ID → Download JSON) "
                    f"and place it at: {CREDENTIALS_FILE}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("gmail", "v1", credentials=creds)


def _fetch_messages(service, query: str, max_results: int = 500) -> list[dict]:
    messages = []
    page_token = None
    while len(messages) < max_results:
        batch_size = min(500, max_results - len(messages))
        params = {"userId": "me", "q": query, "maxResults": batch_size}
        if page_token:
            params["pageToken"] = page_token
        result = service.users().messages().list(**params).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def _get_message_details(service, msg_id: str) -> dict:
    msg = service.users().messages().get(userId="me", id=msg_id, format="metadata",
                                         metadataHeaders=["Subject", "From", "Date"]).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg_id,
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", ""),
        "date": headers.get("Date", ""),
        "size": msg.get("sizeEstimate", 0),
        "labels": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
    }


def _batch_get_message_details(service, message_ids: list[str]) -> list[dict]:
    """Fetch details for multiple messages using batch HTTP requests (100 per batch)."""
    results = []
    for i in range(0, len(message_ids), 100):
        chunk = message_ids[i:i + 100]
        batch = service.new_batch_http_request()
        chunk_results = {}

        def make_callback(mid):
            def callback(request_id, response, exception):
                if exception is None and response:
                    headers = {h["name"]: h["value"] for h in response.get("payload", {}).get("headers", [])}
                    chunk_results[mid] = {
                        "id": mid,
                        "subject": headers.get("Subject", "(no subject)"),
                        "from": headers.get("From", ""),
                        "date": headers.get("Date", ""),
                        "size": response.get("sizeEstimate", 0),
                        "labels": response.get("labelIds", []),
                        "snippet": response.get("snippet", ""),
                    }
            return callback

        for mid in chunk:
            batch.add(
                service.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["Subject", "From", "Date"]
                ),
                callback=make_callback(mid)
            )
        batch.execute()
        results.extend(chunk_results.get(mid, {"id": mid, "subject": "", "from": "", "date": "", "size": 0, "labels": [], "snippet": ""}) for mid in chunk)
    return results


def _batch_delete(service, message_ids: list[str]) -> int:
    deleted = 0
    for i in range(0, len(message_ids), 1000):
        chunk = message_ids[i:i + 1000]
        service.users().messages().batchDelete(
            userId="me", body={"ids": chunk}
        ).execute()
        deleted += len(chunk)
    return deleted


def _batch_trash(service, message_ids: list[str]) -> int:
    trashed = 0
    for i in range(0, len(message_ids), 1000):
        chunk = message_ids[i:i + 1000]
        service.users().messages().batchModify(
            userId="me",
            body={"ids": chunk, "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]}
        ).execute()
        trashed += len(chunk)
    return trashed


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_storage_info",
            description="Get your Gmail storage usage and quota.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="list_old_emails",
            description="List emails older than a given number of days. Shows count, senders, and total size.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days_old": {"type": "integer", "description": "Emails older than this many days", "default": 365},
                    "max_results": {"type": "integer", "description": "Max emails to fetch", "default": 100},
                    "exclude_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to exclude (e.g. ['STARRED', 'IMPORTANT'])",
                        "default": ["STARRED"],
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="find_duplicates",
            description="Find duplicate emails (same sender + subject). Returns groups of duplicates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "description": "Max emails to scan", "default": 500},
                    "folder": {"type": "string", "description": "Label/folder to search in (e.g. 'INBOX', 'SPAM')", "default": "INBOX"},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_emails_by_sender",
            description="List emails from a specific sender, showing count and total size.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sender": {"type": "string", "description": "Email address or domain to search for"},
                    "max_results": {"type": "integer", "default": 200},
                },
                "required": ["sender"],
            },
        ),
        Tool(
            name="list_large_emails",
            description="Find the largest emails in your inbox to prioritize what to delete.",
            inputSchema={
                "type": "object",
                "properties": {
                    "min_size_mb": {"type": "number", "description": "Minimum email size in MB", "default": 1.0},
                    "max_results": {"type": "integer", "default": 50},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_promotional_emails",
            description="List promotional and newsletter emails (from CATEGORY_PROMOTIONS label).",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 100},
                },
                "required": [],
            },
        ),
        Tool(
            name="delete_old_emails",
            description="Permanently delete emails older than N days. EXCLUDES starred and important emails by default. Always preview first with list_old_emails.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days_old": {"type": "integer", "description": "Delete emails older than this many days", "default": 365},
                    "max_delete": {"type": "integer", "description": "Max emails to delete in one go", "default": 100},
                    "exclude_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to protect from deletion",
                        "default": ["STARRED", "IMPORTANT"],
                    },
                    "use_trash": {"type": "boolean", "description": "Move to Trash instead of permanent delete", "default": True},
                },
                "required": [],
            },
        ),
        Tool(
            name="delete_duplicate_emails",
            description="Delete duplicate emails keeping only the newest copy. Preview with find_duplicates first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 500},
                    "folder": {"type": "string", "default": "INBOX"},
                    "use_trash": {"type": "boolean", "description": "Move to Trash instead of permanent delete", "default": True},
                },
                "required": [],
            },
        ),
        Tool(
            name="delete_emails_by_sender",
            description="Delete all emails from a specific sender. Preview with list_emails_by_sender first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sender": {"type": "string", "description": "Sender email or domain"},
                    "max_delete": {"type": "integer", "default": 500},
                    "use_trash": {"type": "boolean", "default": True},
                },
                "required": ["sender"],
            },
        ),
        Tool(
            name="delete_promotional_emails",
            description="Delete all promotional/newsletter emails. Preview with list_promotional_emails first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_delete": {"type": "integer", "default": 500},
                    "use_trash": {"type": "boolean", "default": True},
                },
                "required": [],
            },
        ),
        Tool(
            name="empty_trash",
            description="Permanently empty the Gmail Trash folder to reclaim storage.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="empty_spam",
            description="Permanently delete all emails in the Spam folder.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="delete_by_query",
            description="Delete emails matching any Gmail search query (e.g. 'from:reddit before:2025/01/01'). Very flexible — supports any combination of sender, date, label, category, size, etc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search query string"},
                    "max_delete": {"type": "integer", "description": "Max emails to delete", "default": 2000},
                    "use_trash": {"type": "boolean", "description": "Move to Trash instead of permanent delete", "default": True},
                },
                "required": ["query"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        service = get_gmail_service()
        result = await _dispatch(name, arguments, service)
        return [TextContent(type="text", text=result)]
    except FileNotFoundError as e:
        return [TextContent(type="text", text=f"Setup required: {e}")]
    except HttpError as e:
        return [TextContent(type="text", text=f"Gmail API error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _dispatch(name: str, args: dict, service) -> str:
    if name == "get_storage_info":
        return _get_storage_info(service)
    elif name == "list_old_emails":
        return _list_old_emails(service, **args)
    elif name == "find_duplicates":
        return _find_duplicates(service, **args)
    elif name == "list_emails_by_sender":
        return _list_emails_by_sender(service, **args)
    elif name == "list_large_emails":
        return _list_large_emails(service, **args)
    elif name == "list_promotional_emails":
        return _list_promotional_emails(service, **args)
    elif name == "delete_old_emails":
        return _delete_old_emails(service, **args)
    elif name == "delete_duplicate_emails":
        return _delete_duplicate_emails(service, **args)
    elif name == "delete_emails_by_sender":
        return _delete_emails_by_sender(service, **args)
    elif name == "delete_promotional_emails":
        return _delete_promotional_emails(service, **args)
    elif name == "empty_trash":
        return _empty_folder(service, "TRASH")
    elif name == "empty_spam":
        return _empty_folder(service, "SPAM")
    elif name == "delete_by_query":
        return _delete_by_query(service, **args)
    else:
        return f"Unknown tool: {name}"


def _get_storage_info(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    total_bytes = int(profile.get("messagesTotal", 0))
    threads = int(profile.get("threadsTotal", 0))
    return (
        f"Gmail Account: {profile.get('emailAddress')}\n"
        f"Total messages: {total_bytes:,}\n"
        f"Total threads: {threads:,}\n\n"
        "Note: Gmail's free storage is 15 GB shared across Gmail, Drive, and Photos.\n"
        "To see exact storage, visit https://one.google.com/storage"
    )


def _list_old_emails(service, days_old: int = 365, max_results: int = 100,
                     exclude_labels: list = None) -> str:
    if exclude_labels is None:
        exclude_labels = ["STARRED"]
    cutoff = (datetime.now() - timedelta(days=days_old)).strftime("%Y/%m/%d")
    query = f"before:{cutoff}"
    for label in exclude_labels:
        query += f" -label:{label.lower()}"

    messages = _fetch_messages(service, query, max_results)
    if not messages:
        return f"No emails older than {days_old} days found (excluding {exclude_labels})."

    details = [_get_message_details(service, m["id"]) for m in messages[:50]]
    total_size = sum(d["size"] for d in details)
    senders: dict[str, int] = {}
    for d in details:
        senders[d["from"]] = senders.get(d["from"], 0) + 1

    top_senders = sorted(senders.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [
        f"Found {len(messages):,} emails older than {days_old} days",
        f"(Showing details for first 50; estimated size for those: {total_size / 1_048_576:.1f} MB)\n",
        "Top senders:",
    ]
    for sender, count in top_senders:
        lines.append(f"  {count:>4}  {sender}")
    lines.append("\nSample emails:")
    for d in details[:10]:
        lines.append(f"  [{d['date'][:16]}] {d['from'][:30]} — {d['subject'][:50]}")
    lines.append(f"\nUse delete_old_emails(days_old={days_old}) to remove these.")
    return "\n".join(lines)


def _find_duplicates(service, max_results: int = 500, folder: str = "INBOX") -> str:
    query = f"label:{folder.lower()}" if folder.upper() != "INBOX" else "in:inbox"
    messages = _fetch_messages(service, query, max_results)
    if not messages:
        return "No messages found."

    ids = [m["id"] for m in messages]
    details = _batch_get_message_details(service, ids)

    seen: dict[str, list[str]] = {}
    for d in details:
        key = f"{d['from'].lower()}|||{d['subject'].lower().strip()}"
        if key not in seen:
            seen[key] = []
        seen[key].append(d["id"])

    duplicates = {k: v for k, v in seen.items() if len(v) > 1}
    if not duplicates:
        return f"No duplicate emails found in {folder} (scanned {len(messages)} emails)."

    total_dupes = sum(len(v) - 1 for v in duplicates.values())
    lines = [
        f"Found {len(duplicates)} duplicate groups ({total_dupes} extra copies) in {folder}:\n"
    ]
    for key, ids in sorted(duplicates.items(), key=lambda x: len(x[1]), reverse=True)[:20]:
        sender, subject = key.split("|||", 1)
        lines.append(f"  {len(ids)} copies: [{sender[:30]}] {subject[:50]}")
    lines.append(f"\nUse delete_duplicate_emails(folder='{folder}') to keep only the newest copy.")
    return "\n".join(lines)


def _list_emails_by_sender(service, sender: str, max_results: int = 200) -> str:
    messages = _fetch_messages(service, f"from:{sender}", max_results)
    if not messages:
        return f"No emails found from {sender}."
    total_size = 0
    sample = []
    for m in messages[:50]:
        d = _get_message_details(service, m["id"])
        total_size += d["size"]
        if len(sample) < 10:
            sample.append(d)
    lines = [
        f"Found {len(messages):,} emails from: {sender}",
        f"Estimated size (first 50): {total_size / 1_048_576:.1f} MB\n",
        "Sample emails:",
    ]
    for d in sample:
        lines.append(f"  [{d['date'][:16]}] {d['subject'][:60]}")
    lines.append(f"\nUse delete_emails_by_sender(sender='{sender}') to delete all of them.")
    return "\n".join(lines)


def _list_large_emails(service, min_size_mb: float = 1.0, max_results: int = 50) -> str:
    min_bytes = int(min_size_mb * 1_048_576)
    query = f"larger:{min_bytes}"
    messages = _fetch_messages(service, query, max_results)
    if not messages:
        return f"No emails larger than {min_size_mb} MB found."
    details = [_get_message_details(service, m["id"]) for m in messages]
    details.sort(key=lambda x: x["size"], reverse=True)
    total = sum(d["size"] for d in details)
    lines = [
        f"Found {len(details)} emails larger than {min_size_mb} MB",
        f"Total size: {total / 1_048_576:.1f} MB\n",
    ]
    for d in details[:20]:
        size_mb = d["size"] / 1_048_576
        lines.append(f"  {size_mb:5.1f} MB  {d['from'][:30]} — {d['subject'][:40]}")
    return "\n".join(lines)


def _list_promotional_emails(service, max_results: int = 100) -> str:
    messages = _fetch_messages(service, "category:promotions", max_results)
    if not messages:
        return "No promotional emails found."
    total_size = 0
    senders: dict[str, int] = {}
    for m in messages[:100]:
        d = _get_message_details(service, m["id"])
        total_size += d["size"]
        senders[d["from"]] = senders.get(d["from"], 0) + 1
    top_senders = sorted(senders.items(), key=lambda x: x[1], reverse=True)[:15]
    lines = [
        f"Found {len(messages):,} promotional emails",
        f"Estimated size (first 100): {total_size / 1_048_576:.1f} MB\n",
        "Top senders:",
    ]
    for sender, count in top_senders:
        lines.append(f"  {count:>4}  {sender}")
    lines.append("\nUse delete_promotional_emails() to delete all of them.")
    return "\n".join(lines)


def _delete_old_emails(service, days_old: int = 365, max_delete: int = 100,
                       exclude_labels: list = None, use_trash: bool = True) -> str:
    if exclude_labels is None:
        exclude_labels = ["STARRED", "IMPORTANT"]
    cutoff = (datetime.now() - timedelta(days=days_old)).strftime("%Y/%m/%d")
    query = f"before:{cutoff}"
    for label in exclude_labels:
        query += f" -label:{label.lower()}"

    messages = _fetch_messages(service, query, max_delete)
    if not messages:
        return f"No emails older than {days_old} days to delete."

    ids = [m["id"] for m in messages]
    action = "Moved to Trash" if use_trash else "Permanently deleted"
    if use_trash:
        count = _batch_trash(service, ids)
    else:
        count = _batch_delete(service, ids)
    return f"{action} {count:,} emails older than {days_old} days.\nRun empty_trash() to permanently free storage."


def _delete_duplicate_emails(service, max_results: int = 500, folder: str = "INBOX",
                              use_trash: bool = True) -> str:
    query = f"label:{folder.lower()}" if folder.upper() != "INBOX" else "in:inbox"
    messages = _fetch_messages(service, query, max_results)
    if not messages:
        return "No messages found."

    ids = [m["id"] for m in messages]
    details = _batch_get_message_details(service, ids)

    seen: dict[str, str] = {}
    to_delete = []
    for d in details:
        key = f"{d['from'].lower()}|||{d['subject'].lower().strip()}"
        if key in seen:
            to_delete.append(d["id"])
        else:
            seen[key] = d["id"]

    if not to_delete:
        return f"No duplicate emails found in {folder}."

    action = "Moved to Trash" if use_trash else "Permanently deleted"
    if use_trash:
        count = _batch_trash(service, to_delete)
    else:
        count = _batch_delete(service, to_delete)
    return f"{action} {count:,} duplicate emails from {folder} (kept newest copy of each)."


def _delete_emails_by_sender(service, sender: str, max_delete: int = 500,
                              use_trash: bool = True) -> str:
    messages = _fetch_messages(service, f"from:{sender}", max_delete)
    if not messages:
        return f"No emails from {sender} found."

    ids = [m["id"] for m in messages]
    action = "Moved to Trash" if use_trash else "Permanently deleted"
    if use_trash:
        count = _batch_trash(service, ids)
    else:
        count = _batch_delete(service, ids)
    return f"{action} {count:,} emails from {sender}."


def _delete_promotional_emails(service, max_delete: int = 500, use_trash: bool = True) -> str:
    messages = _fetch_messages(service, "category:promotions", max_delete)
    if not messages:
        return "No promotional emails found."

    ids = [m["id"] for m in messages]
    action = "Moved to Trash" if use_trash else "Permanently deleted"
    if use_trash:
        count = _batch_trash(service, ids)
    else:
        count = _batch_delete(service, ids)
    return f"{action} {count:,} promotional emails."


def _delete_by_query(service, query: str, max_delete: int = 2000, use_trash: bool = True) -> str:
    messages = _fetch_messages(service, query, max_delete)
    if not messages:
        return f"No emails found matching: {query}"
    ids = [m["id"] for m in messages]
    action = "Moved to Trash" if use_trash else "Permanently deleted"
    if use_trash:
        count = _batch_trash(service, ids)
    else:
        count = _batch_delete(service, ids)
    return f"{action} {count:,} emails matching query: {query}"


def _empty_folder(service, label: str) -> str:
    messages = _fetch_messages(service, f"label:{label.lower()}", 5000)
    if not messages:
        return f"{label} is already empty."
    ids = [m["id"] for m in messages]
    count = _batch_delete(service, ids)
    return f"Permanently deleted {count:,} emails from {label}."


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
