"""Run JXA scripts against Mail.app / Calendar.app via osascript.

Only works on macOS. First runs will trigger Automation permission prompts
("Terminal wants access to control Mail") — click OK once.
"""
from __future__ import annotations

import json
import re
import subprocess
from email.utils import parseaddr
from pathlib import Path

from .models import Message

JXA_DIR = Path(__file__).parent / "jxa"


class BridgeError(RuntimeError):
    pass


def _run_jxa(script: str, args: dict, timeout: int = 1800):
    path = JXA_DIR / script
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", str(path), json.dumps(args)],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise BridgeError("osascript not found — MailSweep's mail/calendar access only runs on macOS.")
    except subprocess.TimeoutExpired:
        raise BridgeError(f"{script} timed out after {timeout}s (try lowering max_messages/lookback_days).")
    if proc.returncode != 0:
        raise BridgeError(f"{script} failed: {proc.stderr.strip() or 'unknown osascript error'}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise BridgeError(f"{script} returned non-JSON output: {proc.stdout[:200]!r}")
    if isinstance(data, dict) and "error" in data:
        raise BridgeError(f"{script}: {data['error']}")
    return data


_WANTED_HEADERS = {
    "list-unsubscribe", "list-unsubscribe-post", "list-id",
    "precedence", "x-mailer", "auto-submitted", "reply-to", "from", "to",
}


def parse_headers(block: str) -> dict[str, str]:
    """Parse a raw header block, keeping only headers we use (folded lines handled)."""
    headers: dict[str, str] = {}
    current_key = None
    for line in block.splitlines():
        if line[:1] in (" ", "\t") and current_key:
            headers[current_key] += " " + line.strip()
            continue
        m = re.match(r"^([A-Za-z0-9\-]+):\s?(.*)$", line)
        if not m:
            current_key = None
            continue
        key = m.group(1).lower()
        if key in _WANTED_HEADERS:
            current_key = key
            # First occurrence wins (top-most header is the most recent hop).
            if key not in headers:
                headers[key] = m.group(2).strip()
            else:
                current_key = None
        else:
            current_key = None
    return headers


def fetch_messages(lookback_days: int, max_messages: int,
                   accounts: list[str], known_message_ids: list[str]) -> list[Message]:
    raw = _run_jxa("fetch_mail.js", {
        "lookbackDays": lookback_days,
        "maxMessages": max_messages,
        "accounts": accounts,
        "knownMessageIds": known_message_ids,
    })
    messages = []
    for item in raw:
        _, addr = parseaddr(item.get("sender", ""))
        messages.append(Message(
            message_id=item.get("message_id", ""),
            account=item.get("account", ""),
            mailbox=item.get("mailbox", "INBOX"),
            sender=item.get("sender", ""),
            sender_email=addr.lower(),
            subject=item.get("subject", ""),
            date_received=item.get("date_received", ""),
            snippet=item.get("snippet", ""),
            headers=parse_headers(item.get("header_block", "")),
            mail_id=item.get("mail_id", 0) or 0,
        ))
    return messages


def fetch_calendar_events(horizon_days: int, calendars: list[str]) -> list[dict]:
    return _run_jxa("calendar_read.js", {
        "horizonDays": horizon_days,
        "calendars": calendars,
    })


def create_calendar_event(calendar: str, title: str, start: str,
                          end: str = "", location: str = "", all_day: bool = False) -> None:
    _run_jxa("calendar_create.js", {
        "calendar": calendar, "title": title, "start": start,
        "end": end, "location": location, "allDay": all_day,
    })


def fetch_junk_messages(lookback_days: int, max_messages: int,
                        accounts: list[str], known_message_ids: list[str]) -> list[dict]:
    """Raw dicts (not Message dataclasses) from each account's Junk/Spam
    mailbox: message_id, account, mailbox, mail_id, sender, subject, date_received."""
    return _run_jxa("fetch_junk.js", {
        "lookbackDays": lookback_days,
        "maxMessages": max_messages,
        "accounts": accounts,
        "knownMessageIds": known_message_ids,
    })


def move_messages(targets: list[dict], dest_mailbox: str) -> dict:
    """Move a batch of {account, mailbox, id} messages to dest_mailbox (e.g.
    "Trash"). Returns the raw {moved, total, errors} result rather than
    raising, so callers can report partial success across a batch."""
    if not targets:
        return {"moved": 0, "total": 0, "errors": []}
    return _run_jxa("move_by_id.js", {"targets": targets, "destMailbox": dest_mailbox})


def move_message(account: str, mailbox: str, mail_id: int, dest_mailbox: str) -> None:
    result = move_messages([{"account": account, "mailbox": mailbox, "id": mail_id}], dest_mailbox)
    if result.get("moved", 0) == 0:
        errs = result.get("errors") or []
        raise BridgeError(errs[0] if errs else "move failed for an unknown reason")


def fetch_purchase_candidates(lookback_days: int, mailbox_names: list[str],
                              accounts: list[str]) -> list[dict]:
    """Cheap metadata (subject/sender/date/account/mailbox/mail_id) for
    messages across mailbox_names (default Inbox+Trash) for every enabled
    account, over the lookback window. Purchase-shaped filtering happens in
    Python (rules.purchase_kind)."""
    return _run_jxa("fetch_purchase_candidates.js", {
        "lookbackDays": lookback_days,
        "mailboxNames": mailbox_names,
        "accounts": accounts,
    })


def fetch_message_content(targets: list[dict]) -> list[dict]:
    """Rendered plain-text content for a batch of {account, mailbox, id}
    messages. Returns [{account, mailbox, id, subject, content}]."""
    if not targets:
        return []
    return _run_jxa("fetch_content_by_id.js", {"targets": targets})


def fetch_message_source(targets: list[dict]) -> list[dict]:
    """Raw message source (headers + HTML/MIME) for a batch of
    {account, mailbox, id} messages -- fallback for senders whose
    plain-text rendering drops item details. Returns [{account, mailbox,
    id, source}]."""
    if not targets:
        return []
    return _run_jxa("fetch_source_by_id.js", {"targets": targets})
