"""SQLite persistence: processed messages, event candidates, unsubscribe queue."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from .models import Classification, EventCandidate, Message

_SUBJ_PREFIX = re.compile(r"^(re|fw|fwd)\s*:\s*", re.I)


def _normalize_subject(subj: str) -> str:
    """Strip repeated Re:/Fw:/Fwd: prefixes so a forwarded copy's subject
    still matches its original for duplicate grouping."""
    s = (subj or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _SUBJ_PREFIX.sub("", s).strip()
    return s.lower()


def _parse_iso(d: str):
    try:
        return datetime.fromisoformat((d or "").replace("Z", "+00:00"))
    except ValueError:
        return None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,
    account      TEXT, sender TEXT, sender_email TEXT,
    subject      TEXT, date_received TEXT,
    category     TEXT, importance TEXT, reason TEXT,
    used_llm     INTEGER DEFAULT 0,
    processed_at TEXT,
    mail_id      INTEGER,           -- Mail.app numeric id, for move/trash; NULL if unknown
    to_addr      TEXT               -- raw To: header, so review shows which of your addresses it hit
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id TEXT, source_subject TEXT, source_sender TEXT,
    source_sender_email TEXT,           -- normalized, for protect/bulk-dismiss matching
    title TEXT, start TEXT, end TEXT, location TEXT,
    confidence REAL, all_day INTEGER DEFAULT 0,
    status TEXT DEFAULT 'new',          -- new | approved | created | dismissed | on_calendar
    created_at TEXT,
    UNIQUE(source_message_id, title, start)
);
CREATE TABLE IF NOT EXISTS event_protected_senders (
    sender_email TEXT PRIMARY KEY,      -- never suggest events from this sender again
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS spam_review (
    message_id    TEXT PRIMARY KEY,
    account       TEXT, mailbox TEXT, mail_id INTEGER,
    sender        TEXT, sender_email TEXT,
    subject       TEXT, date_received TEXT,
    reason        TEXT, phishing_risk INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'suggested',  -- suggested | rescued | left | protected
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS spam_trusted_senders (
    sender_email TEXT PRIMARY KEY,      -- always rescue this sender's Junk mail
    added_at TEXT
);
CREATE TABLE IF NOT EXISTS unsub_queue (
    sender_email TEXT PRIMARY KEY,
    sender TEXT, account TEXT,
    msg_count INTEGER DEFAULT 0,
    last_subject TEXT,
    targets TEXT,                        -- JSON list of mailto:/https: targets
    one_click INTEGER DEFAULT 0,
    status TEXT DEFAULT 'suggested',     -- suggested | approved | done | skipped | protected
    updated_at TEXT,
    last_to TEXT                         -- raw To: header of the most recent message from this sender
);
CREATE TABLE IF NOT EXISTS purchases (
    key           TEXT PRIMARY KEY,     -- order_ref, or a synthetic per-message key when none found
    order_ref     TEXT,
    vendor        TEXT, item TEXT,
    account       TEXT, mailbox TEXT, mail_id INTEGER,
    order_date    TEXT,
    review_text   TEXT,
    status TEXT DEFAULT 'pending_receipt',
    -- pending_receipt | ready | approved | skipped | returned_excluded | name_unresolvable
    updated_at TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        for stmt in (
            "ALTER TABLE messages ADD COLUMN mail_id INTEGER",
            "ALTER TABLE messages ADD COLUMN to_addr TEXT",
            "ALTER TABLE unsub_queue ADD COLUMN last_to TEXT",
            "ALTER TABLE events ADD COLUMN source_sender_email TEXT",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # already present (fresh DB, or migrated in a previous run)
        self._backfill_event_sender_emails()

    def _backfill_event_sender_emails(self) -> None:
        """One-time repair for rows written before source_sender_email existed
        (added 2026-08-01): those rows have it blank, which silently breaks
        `p` (protect sender) in `events review` -- it can't add the sender to
        event_protected_senders without an address, so it falls back to
        dismissing just that one row, and the same sender keeps generating
        new candidates. Idempotent: only touches rows still blank."""
        rows = self.conn.execute(
            "SELECT id, source_sender FROM events "
            "WHERE source_sender_email IS NULL OR source_sender_email = ''").fetchall()
        for r in rows:
            _, addr = parseaddr(r["source_sender"] or "")
            if addr:
                self.conn.execute(
                    "UPDATE events SET source_sender_email = ? WHERE id = ?",
                    (addr.lower(), r["id"]))
        if rows:
            self.conn.commit()

    # -- messages -----------------------------------------------------------
    def known_message_ids(self, limit: int = 5000) -> list[str]:
        rows = self.conn.execute(
            "SELECT message_id FROM messages ORDER BY processed_at DESC LIMIT ?", (limit,))
        return [r["message_id"] for r in rows]

    def record_message(self, msg: Message, cls: Classification) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO messages
               (message_id, account, sender, sender_email, subject, date_received,
                category, importance, reason, used_llm, processed_at, mail_id, to_addr)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (msg.message_id, msg.account, msg.sender, msg.sender_email, msg.subject,
             msg.date_received, cls.category, cls.importance, cls.reason,
             int(cls.used_llm), datetime.now().isoformat(timespec="seconds"),
             msg.mail_id or None, msg.to or None))

    def messages_by_sender(self, sender_email: str) -> list[sqlite3.Row]:
        """Scanned messages from this sender that we have a Mail.app id for
        (needed to move/trash them). Messages scanned before mail_id was
        tracked won't have one and are silently excluded."""
        return list(self.conn.execute(
            "SELECT message_id, account, mail_id FROM messages "
            "WHERE sender_email=? AND mail_id IS NOT NULL", (sender_email,)))

    def message_ref(self, message_id: str) -> sqlite3.Row | None:
        """Account/mailbox-id/recipient info for a scanned message, for both
        display (inbox/to fields) and trash eligibility (mail_id may be NULL
        if this message predates trash support -- callers check for that)."""
        return self.conn.execute(
            "SELECT account, mail_id, to_addr FROM messages WHERE message_id=?",
            (message_id,)).fetchone()

    def noise_stats(self, days: int = 30) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT sender_email, sender, account, category,
                      COUNT(*) AS n, MAX(subject) AS last_subject
               FROM messages
               WHERE category IN ('newsletter','marketing','notification')
                 AND processed_at > datetime('now', ?)
               GROUP BY sender_email ORDER BY n DESC""", (f"-{days} days",)))

    def category_counts(self, days: int = 7) -> dict[str, int]:
        rows = self.conn.execute(
            """SELECT category, COUNT(*) AS n FROM messages
               WHERE processed_at > datetime('now', ?) GROUP BY category""",
            (f"-{days} days",))
        return {r["category"]: r["n"] for r in rows}

    def important_recent(self, days: int = 7) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT sender, subject, account, date_received FROM messages
               WHERE importance = 'high' AND processed_at > datetime('now', ?)
               ORDER BY date_received DESC LIMIT 20""", (f"-{days} days",)))

    def likely_cross_account_duplicates(self, days: int = 30,
                                        window_hours: int = 72) -> list[dict]:
        """Group scanned messages by (sender_email, normalized subject) that
        appear in more than one account within `window_hours` of each other --
        a proxy for forwarding-caused duplication (e.g. an old auto-forward
        rule copying mail into a second account).

        Caveat: if a forward preserves the original Message-ID byte-for-byte,
        both copies collapse into a single `messages` row (message_id is the
        table's primary key) and won't be visible here. This catches the more
        common case where forwarding/relaying assigns a new Message-ID.
        """
        rows = self.conn.execute(
            """SELECT message_id, account, sender, sender_email, subject, date_received
               FROM messages WHERE processed_at > datetime('now', ?)""",
            (f"-{days} days",)).fetchall()

        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            key = (r["sender_email"], _normalize_subject(r["subject"]))
            groups.setdefault(key, []).append(r)

        out = []
        for (sender_email, norm_subject), items in groups.items():
            if len({i["account"] for i in items}) < 2:
                continue
            times = [_parse_iso(i["date_received"]) for i in items]
            flagged = False
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    if items[i]["account"] == items[j]["account"]:
                        continue
                    if times[i] and times[j] and \
                            abs((times[i] - times[j]).total_seconds()) <= window_hours * 3600:
                        flagged = True
                        break
                if flagged:
                    break
            if not flagged:
                continue
            out.append({
                "sender_email": sender_email,
                "sender": items[0]["sender"],
                "subject": norm_subject or items[0]["subject"],
                "accounts": sorted({i["account"] for i in items}),
                "count": len(items),
                "message_ids": [i["message_id"] for i in items],
            })
        out.sort(key=lambda g: -g["count"])
        return out

    # -- spam audit -----------------------------------------------------------
    def known_spam_message_ids(self, limit: int = 5000) -> list[str]:
        rows = self.conn.execute(
            "SELECT message_id FROM spam_review ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [r["message_id"] for r in rows]

    def is_spam_sender_protected(self, sender_email: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM spam_review WHERE sender_email=? AND status='protected' LIMIT 1",
            (sender_email,)).fetchone()
        return row is not None

    def upsert_spam_flag(self, message_id: str, account: str, mailbox: str, mail_id: int,
                         sender: str, sender_email: str, subject: str, date_received: str,
                         reason: str, phishing_risk: bool) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO spam_review
               (message_id, account, mailbox, mail_id, sender, sender_email,
                subject, date_received, reason, phishing_risk, status, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'suggested', ?)""",
            (message_id, account, mailbox, mail_id, sender, sender_email, subject,
             date_received, reason, int(phishing_risk),
             datetime.now().isoformat(timespec="seconds")))

    def spam_by_status(self, status: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM spam_review WHERE status=? ORDER BY date_received DESC", (status,)))

    def set_spam_status(self, message_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE spam_review SET status=?, updated_at=? WHERE message_id=?",
            (status, datetime.now().isoformat(timespec="seconds"), message_id))

    def is_spam_sender_trusted(self, sender_email: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM spam_trusted_senders WHERE sender_email=?",
            (sender_email,)).fetchone()
        return row is not None

    def trust_spam_sender(self, sender_email: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO spam_trusted_senders (sender_email, added_at) VALUES (?,?)",
            (sender_email, datetime.now().isoformat(timespec="seconds")))

    def spam_rows_for_sender(self, sender_email: str) -> list[sqlite3.Row]:
        """Already-tracked spam_review rows for this sender that haven't been
        rescued or confirmed-junk yet -- for catching up a newly-trusted
        sender's previously-flagged-or-left mail."""
        return list(self.conn.execute(
            "SELECT * FROM spam_review WHERE sender_email=? AND status NOT IN ('rescued', 'protected')",
            (sender_email,)))

    # -- events -------------------------------------------------------------
    def add_event(self, ev: EventCandidate) -> None:
        _, sender_email = parseaddr(ev.source_sender)
        self.conn.execute(
            """INSERT OR IGNORE INTO events
               (source_message_id, source_subject, source_sender, source_sender_email,
                title, start, end, location, confidence, all_day, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'new',?)""",
            (ev.source_message_id, ev.source_subject, ev.source_sender, sender_email.lower(),
             ev.title, ev.start, ev.end, ev.location, ev.confidence, int(ev.all_day),
             datetime.now().isoformat(timespec="seconds")))

    def events_by_status(self, status: str = "new") -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM events WHERE status = ? ORDER BY start", (status,)))

    def is_event_sender_protected(self, sender_email: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM event_protected_senders WHERE sender_email=?",
            (sender_email,)).fetchone()
        return row is not None

    def protect_event_sender(self, sender_email: str) -> list[int]:
        """Never suggest events from this sender again, and dismiss any of
        their still-pending candidates. Returns the ids that were dismissed,
        so callers can skip them for the rest of an in-progress review."""
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM events WHERE source_sender_email=? AND status='new'",
            (sender_email,))]
        if ids:
            self.conn.execute(
                "UPDATE events SET status='dismissed' WHERE source_sender_email=? AND status='new'",
                (sender_email,))
        self.conn.execute(
            "INSERT OR IGNORE INTO event_protected_senders (sender_email, added_at) VALUES (?,?)",
            (sender_email, datetime.now().isoformat(timespec="seconds")))
        return ids

    def set_event_status(self, event_id: int, status: str) -> None:
        self.conn.execute("UPDATE events SET status=? WHERE id=?", (status, event_id))

    # -- unsubscribe queue --------------------------------------------------
    def upsert_unsub(self, msg: Message, targets: list[str]) -> None:
        row = self.conn.execute(
            "SELECT status, msg_count FROM unsub_queue WHERE sender_email=?",
            (msg.sender_email,)).fetchone()
        now = datetime.now().isoformat(timespec="seconds")
        if row is None:
            self.conn.execute(
                """INSERT INTO unsub_queue (sender_email, sender, account, msg_count,
                   last_subject, targets, one_click, status, updated_at, last_to)
                   VALUES (?,?,?,?,?,?,?, 'suggested', ?, ?)""",
                (msg.sender_email, msg.sender, msg.account, 1, msg.subject,
                 json.dumps(targets), int(msg.one_click), now, msg.to or None))
        elif row["status"] in ("suggested",):
            self.conn.execute(
                """UPDATE unsub_queue SET msg_count = msg_count + 1, last_subject = ?,
                   targets = ?, one_click = ?, updated_at = ?, last_to = ? WHERE sender_email = ?""",
                (msg.subject, json.dumps(targets), int(msg.one_click), now,
                 msg.to or None, msg.sender_email))
        # done/skipped/protected senders are left alone.

    def unsub_by_status(self, status: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM unsub_queue WHERE status=? ORDER BY msg_count DESC",
            (status,)))

    def set_unsub_status(self, sender_email: str, status: str) -> None:
        self.conn.execute(
            "UPDATE unsub_queue SET status=?, updated_at=? WHERE sender_email=?",
            (status, datetime.now().isoformat(timespec="seconds"), sender_email))

    # -- purchase review ------------------------------------------------------
    def upsert_purchase(self, key: str, order_ref: str | None, vendor: str, item: str,
                        account: str, mailbox: str, mail_id: int, order_date: str,
                        status: str, review_text: str | None) -> None:
        """Record/refresh a purchase-review candidate. Leaves the row alone
        if the human already curated it (approved/skipped/returned_excluded)
        so a rescan can't clobber their decision."""
        row = self.conn.execute(
            "SELECT status FROM purchases WHERE key=?", (key,)).fetchone()
        if row and row["status"] in ("approved", "skipped", "returned_excluded"):
            return
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT INTO purchases (key, order_ref, vendor, item, account, mailbox,
               mail_id, order_date, status, review_text, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 order_ref=excluded.order_ref, vendor=excluded.vendor, item=excluded.item,
                 account=excluded.account, mailbox=excluded.mailbox, mail_id=excluded.mail_id,
                 order_date=excluded.order_date, status=excluded.status,
                 review_text=excluded.review_text, updated_at=excluded.updated_at""",
            (key, order_ref, vendor, item, account, mailbox, mail_id, order_date,
             status, review_text, now))

    def purchases_by_status(self, status: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM purchases WHERE status=? ORDER BY order_date DESC", (status,)))

    def set_purchase_status(self, key: str, status: str, review_text: str | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        if review_text is not None:
            self.conn.execute(
                "UPDATE purchases SET status=?, review_text=?, updated_at=? WHERE key=?",
                (status, review_text, now, key))
        else:
            self.conn.execute(
                "UPDATE purchases SET status=?, updated_at=? WHERE key=?",
                (status, now, key))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
