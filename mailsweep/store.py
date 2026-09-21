"""SQLite persistence: processed messages, event candidates, unsubscribe queue."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from . import auth
from .models import Classification, EventCandidate, Message
from .timeparse import normalize_end, normalize_start

# Same categories cmd_scan uses to decide a sender is worth queueing for unsubscribe.
NOISE_CATEGORIES = ("newsletter", "marketing", "notification", "junk", "trash")
# Shared domains: an unsubscribe at one address says nothing about another at the same domain.
SHARED_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
                  "live.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com"}
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

def _local_naive(d: datetime) -> datetime:
    """date_received is UTC ('...000Z'); our own timestamps are naive local time."""
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,
    account      TEXT, sender TEXT, sender_email TEXT,
    subject      TEXT, date_received TEXT,
    category     TEXT, importance TEXT, reason TEXT,
    used_llm     INTEGER DEFAULT 0,
    processed_at TEXT,
    mail_id      INTEGER,           -- Mail.app numeric id, for move/trash; NULL if unknown
    to_addr      TEXT,              -- raw To: header, so review shows which of your addresses it hit
    auth         TEXT               -- verified | unverified | failed | unknown (auth.py); 'failed' = forged From line
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
    last_to TEXT,                        -- raw To: header of the most recent message from this sender
    last_received TEXT,                  -- date_received of the most recent message from this sender
    last_note TEXT,                      -- outcome text of the latest unsubscribe attempt made from the UI
    attempts INTEGER DEFAULT 0,          -- unsubscribe attempts made from the UI
    acked_at TEXT,                       -- 'ignore until it sends more': mail before this doesn't count as still-sending
    opened_at TEXT,                      -- page opened for the user to finish; set while status='approved' = awaiting their confirmation
    auth TEXT,                           -- verified | unverified | unknown: was the message that supplied `targets` really from this sender (see auth.py)
    auth_detail TEXT
);
CREATE TABLE IF NOT EXISTS classify_feedback (
    message_id           TEXT PRIMARY KEY,
    subject              TEXT, sender TEXT,
    predicted_category   TEXT, predicted_importance TEXT,
    correct_category     TEXT, correct_importance TEXT,
    created_at           TEXT
);
CREATE TABLE IF NOT EXISTS noise_reviewed (
    sender_email TEXT PRIMARY KEY,   -- decided in `stats --review`: trashed/junked/archived/skipped
    action       TEXT,               -- trash | junk | archive | skip
    reviewed_at  TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    command           TEXT,          -- 'scan' | 'brief'
    started_at        TEXT,
    finished_at       TEXT,
    duration_s        REAL,
    ok                INTEGER DEFAULT 1,   -- 0 if a fatal error stopped the run early
    messages_scanned  INTEGER,
    accounts          INTEGER,
    llm_up            INTEGER,
    errors            TEXT           -- JSON list of {stage, message}, '[]' if none
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
            "ALTER TABLE messages ADD COLUMN auth TEXT",
            "ALTER TABLE unsub_queue ADD COLUMN last_to TEXT",
            "ALTER TABLE unsub_queue ADD COLUMN last_received TEXT",
            "ALTER TABLE events ADD COLUMN source_sender_email TEXT",
            "ALTER TABLE events ADD COLUMN original_start TEXT",
            "ALTER TABLE events ADD COLUMN original_end TEXT",
            "ALTER TABLE events ADD COLUMN time_flagged INTEGER DEFAULT 0",
            "ALTER TABLE unsub_queue ADD COLUMN last_note TEXT",
            "ALTER TABLE unsub_queue ADD COLUMN attempts INTEGER DEFAULT 0",
            "ALTER TABLE unsub_queue ADD COLUMN acked_at TEXT",
            "ALTER TABLE unsub_queue ADD COLUMN opened_at TEXT",
            "ALTER TABLE unsub_queue ADD COLUMN auth TEXT",
            "ALTER TABLE unsub_queue ADD COLUMN auth_detail TEXT",
            "ALTER TABLE messages ADD COLUMN replied_status INTEGER DEFAULT 0",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # already present (fresh DB, or migrated in a previous run)
        self._backfill_event_sender_emails()
        self._backfill_event_times()
        self._backfill_unsub_last_seen()

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

    def _backfill_event_times(self) -> None:
        """One-time repair for pending candidates saved before start times were
        normalized: timezone suffixes that shifted events by the UTC offset,
        midnight stamped on date-only events (created as 12:00 AM timed events
        instead of all-day), and formats Calendar can't parse at all. Only
        touches status 'new' -- resolved rows are history. Idempotent."""
        changed = False
        for r in self.conn.execute(
                "SELECT id, start, end, all_day FROM events WHERE status='new'").fetchall():
            norm = normalize_start(r["start"])
            if norm is None:
                continue
            start, date_only = norm
            end = normalize_end(start, r["end"] or "")
            all_day = 1 if (date_only or r["all_day"]) else 0
            if (start, end, all_day) == (r["start"], r["end"] or "", r["all_day"]):
                continue
            try:
                self.conn.execute("UPDATE events SET start=?, end=?, all_day=? WHERE id=?",
                                  (start, end, all_day, r["id"]))
            except sqlite3.IntegrityError:
                # Normalizing made it identical to a row already saved for this
                # message and title -- it's a duplicate, so drop it from review.
                self.conn.execute("UPDATE events SET status='dismissed' WHERE id=?", (r["id"],))
            changed = True
        if changed:
            self.conn.commit()

    def _backfill_unsub_last_seen(self) -> None:
        """One-time repair for unsub_queue rows written before last_to/
        last_received existed: those rows show '(unknown)' for to/date in
        `unsub review` forever, since upsert_unsub only refreshes them when
        the sender's next message scans in while the row is still
        'suggested'. Idempotent: only touches rows still blank."""
        rows = self.conn.execute(
            "SELECT sender_email FROM unsub_queue "
            "WHERE last_received IS NULL OR last_received = '' "
            "OR last_to IS NULL OR last_to = ''").fetchall()
        for r in rows:
            msg = self.conn.execute(
                """SELECT to_addr, date_received FROM messages
                   WHERE sender_email = ? AND date_received IS NOT NULL AND date_received != ''
                   ORDER BY date_received DESC LIMIT 1""",
                (r["sender_email"],)).fetchone()
            if msg:
                self.conn.execute(
                    """UPDATE unsub_queue SET
                         last_to = COALESCE(NULLIF(last_to, ''), ?),
                         last_received = COALESCE(NULLIF(last_received, ''), ?)
                       WHERE sender_email = ?""",
                    (msg["to_addr"], msg["date_received"], r["sender_email"]))
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
                category, importance, reason, used_llm, processed_at, mail_id, to_addr, auth,
                replied_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (msg.message_id, msg.account, msg.sender, msg.sender_email, msg.subject,
             msg.date_received, cls.category, cls.importance, cls.reason,
             int(cls.used_llm), datetime.now().isoformat(timespec="seconds"),
             msg.mail_id or None, msg.to or None, auth.assess(msg.headers)[0],
             int(msg.replied_status)))

    def forged_recent(self, days: int = 7) -> list[sqlite3.Row]:
        """Messages whose receiving server said DMARC failed: the From line is very likely forged."""
        return list(self.conn.execute(
            """SELECT sender, subject, account, date_received FROM messages
               WHERE auth = 'failed' AND processed_at > datetime('now', ?)
               ORDER BY date_received DESC""", (f"-{days} days",)))

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

    # -- classify feedback (few-shot correction loop) ------------------------
    def unreviewed_llm_messages(self, limit: int = 20, category: str | None = None,
                                include_actioned: bool = False) -> list[sqlite3.Row]:
        """Recent LLM-classified messages that haven't been through `classify
        review` yet, newest first. Pass `category` to spot-check one bucket
        at a time (e.g. everything predicted "marketing") instead of raw
        date order -- useful for skimming a backlog fast.

        By default, excludes messages you've already disposed of via a
        *different* review flow: senders resolved in `unsub review`
        (done/protected/skipped/approved), and messages whose extracted
        event was resolved in `events review` (anything but 'new'). Those
        are a different question (should I unsubscribe / is this an event)
        than the one this command asks (was the category/importance
        right), but from the user's side both feel like "I already dealt
        with this" -- pass include_actioned=True to see them anyway."""
        query = """SELECT m.message_id, m.sender, m.subject, m.date_received,
                          m.category, m.importance, m.reason
                   FROM messages m
                   LEFT JOIN classify_feedback f ON f.message_id = m.message_id
                   WHERE m.used_llm = 1 AND f.message_id IS NULL"""
        params: list = []
        if category:
            query += " AND m.category = ?"
            params.append(category)
        if not include_actioned:
            query += """ AND NOT EXISTS (
                           SELECT 1 FROM unsub_queue u WHERE u.sender_email = m.sender_email
                             AND u.status IN ('done','protected','skipped','approved'))
                         AND NOT EXISTS (
                           SELECT 1 FROM events e WHERE e.source_message_id = m.message_id
                             AND e.status != 'new')"""
        query += " ORDER BY m.processed_at DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(query, params))

    def record_classify_feedback(self, message_id: str, subject: str, sender: str,
                                 predicted_category: str, predicted_importance: str,
                                 correct_category: str, correct_importance: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO classify_feedback
               (message_id, subject, sender, predicted_category, predicted_importance,
                correct_category, correct_importance, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (message_id, subject, sender, predicted_category, predicted_importance,
             correct_category, correct_importance, datetime.now().isoformat(timespec="seconds")))

    def classify_corrections(self, limit: int = 5) -> list[sqlite3.Row]:
        """Most recent cases where the human's category/importance differed
        from what was predicted -- the actual signal for few-shot prompting
        (confirmed-correct rows carry no new information for the model)."""
        return list(self.conn.execute(
            """SELECT subject, sender, predicted_category, predicted_importance,
                      correct_category, correct_importance
               FROM classify_feedback
               WHERE correct_category != predicted_category
                  OR correct_importance != predicted_importance
               ORDER BY created_at DESC LIMIT ?""", (limit,)))

    def noise_stats(self, days: int = 30, include_actioned: bool = False) -> list[sqlite3.Row]:
        """Per-sender noise counts, with the subject/date of their most
        recently received message (not alphabetically-max subject). By
        default excludes senders already resolved in `unsub review`
        (done/protected/skipped/approved) or a prior `stats --review`
        (noise_reviewed) -- trashing/archiving/skipping a sender in `stats
        --review` used to leave no record at all unless you also
        unsubscribed, so a resolved sender kept reappearing here every run.
        Pass include_actioned=True to see them anyway."""
        exclude = "" if include_actioned else (
            """ AND NOT EXISTS (
                  SELECT 1 FROM unsub_queue u WHERE u.sender_email = messages.sender_email
                    AND u.status IN ('done','protected','skipped','approved'))
                AND NOT EXISTS (
                  SELECT 1 FROM noise_reviewed nr WHERE nr.sender_email = messages.sender_email)"""
        )
        return list(self.conn.execute(
            f"""WITH noisy AS (
                 SELECT sender_email, sender, account, category, subject, date_received,
                        ROW_NUMBER() OVER (
                          PARTITION BY sender_email ORDER BY date_received DESC
                        ) AS rn
                 FROM messages
                 WHERE category IN ('newsletter','marketing','notification')
                   AND processed_at > datetime('now', ?){exclude}
               )
               SELECT sender_email, sender, account, category, COUNT(*) AS n,
                      MAX(CASE WHEN rn = 1 THEN subject END) AS last_subject,
                      MAX(CASE WHEN rn = 1 THEN date_received END) AS last_received
               FROM noisy
               GROUP BY sender_email ORDER BY n DESC""", (f"-{days} days",)))

    def mark_noise_reviewed(self, sender_email: str, action: str) -> None:
        self.conn.execute(
            """INSERT INTO noise_reviewed (sender_email, action, reviewed_at)
               VALUES (?,?,?)
               ON CONFLICT(sender_email) DO UPDATE SET
                 action=excluded.action, reviewed_at=excluded.reviewed_at""",
            (sender_email, action, datetime.now().isoformat(timespec="seconds")))

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

    def needs_response(self, days: int = 3) -> list[sqlite3.Row]:
        """High-importance mail that hasn't been replied to yet -- the
        briefing's 'needs a response' list. Restricted to categories a
        reply is actually plausible for: 'high' importance alone isn't
        enough, since marketing/notification mail can land high importance
        too (e.g. a real event invite from a newsletter sender)."""
        return list(self.conn.execute(
            """SELECT sender, subject, account, date_received FROM messages
               WHERE importance = 'high' AND COALESCE(replied_status, 0) = 0
                 AND category IN ('personal', 'work', 'transactional')
                 AND processed_at > datetime('now', ?)
               ORDER BY date_received DESC LIMIT 20""", (f"-{days} days",)))

    # -- run history ----------------------------------------------------------
    def record_run(self, command: str, started_at: datetime, finished_at: datetime,
                   ok: bool, messages_scanned: int | None, accounts: int | None,
                   llm_up: bool | None, errors: list[dict]) -> None:
        self.conn.execute(
            """INSERT INTO runs (command, started_at, finished_at, duration_s, ok,
                                 messages_scanned, accounts, llm_up, errors)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (command, started_at.isoformat(timespec="seconds"),
             finished_at.isoformat(timespec="seconds"),
             (finished_at - started_at).total_seconds(), int(ok),
             messages_scanned, accounts,
             int(llm_up) if llm_up is not None else None, json.dumps(errors)))

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)))

    def run_error_summary(self, days: int = 30) -> dict:
        """Count of runs and per-stage failure counts in the last N days --
        turns a one-off error print into something that can catch a
        recurring problem (e.g. 'calendar_cross_check failed 6 of the last
        7 scans') instead of it going unnoticed in a log nobody rereads."""
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE started_at > datetime('now', ?)",
            (f"-{days} days",)).fetchone()["n"]
        rows = self.conn.execute(
            "SELECT errors FROM runs WHERE started_at > datetime('now', ?) AND errors != '[]'",
            (f"-{days} days",)).fetchall()
        stage_failures: dict[str, int] = {}
        for r in rows:
            for e in json.loads(r["errors"]):
                stage_failures[e["stage"]] = stage_failures.get(e["stage"], 0) + 1
        return {"total_runs": total, "stage_failures": stage_failures}

    def purge_old_messages(self, keep_days: int) -> int:
        """Delete `messages` rows older than keep_days. Doesn't touch
        classify_feedback or event-correction tables -- those are the
        classifier's few-shot learning history, not scan output, and stay
        small regardless of how long the mailbox history gets. A dangling
        events.source_message_id after this is harmless: message_ref()
        returns None and callers already handle that (shows '?' instead of
        the account)."""
        cur = self.conn.execute(
            "DELETE FROM messages WHERE processed_at < datetime('now', ?)",
            (f"-{keep_days} days",))
        return cur.rowcount

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

    def event_by_id(self, event_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()

    def flag_event_time(self, event_id: int, start: str, end: str, all_day: bool) -> bool:
        """Record a user correction to a real event's date/time. The first
        extracted values are kept (original_*) so the correction can be shown
        to the model as a before/after example. Returns False if the corrected
        time collides with an event already saved for the same message+title."""
        try:
            self.conn.execute(
                """UPDATE events SET original_start = COALESCE(original_start, start),
                                     original_end   = COALESCE(original_end, end),
                                     start=?, end=?, all_day=?, time_flagged=1
                   WHERE id=?""", (start, end, int(all_day), event_id))
        except sqlite3.IntegrityError:
            return False
        return True

    def event_time_corrections(self, limit: int = 3) -> list[sqlite3.Row]:
        """Most recent events the user flagged as real-but-wrong-time."""
        return list(self.conn.execute(
            """SELECT source_subject, source_sender, original_start, start FROM events
               WHERE time_flagged = 1 AND original_start IS NOT NULL AND original_start != start
               ORDER BY id DESC LIMIT ?""", (limit,)))

    def event_feedback_examples(self, limit: int = 3) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """Most recent confirmed-real (created, or already on_calendar) vs.
        confirmed-not-real (dismissed) event candidates, for few-shot
        steering of event extraction. Message bodies aren't retained, so
        examples are subject/sender only -- same signal `spam_audit_with_llm`
        already relies on for its judgment calls."""
        positive = list(self.conn.execute(
            """SELECT source_subject, source_sender, title, start FROM events
               WHERE status IN ('created', 'on_calendar')
               ORDER BY id DESC LIMIT ?""", (limit,)))
        negative = list(self.conn.execute(
            """SELECT source_subject, source_sender FROM events
               WHERE status = 'dismissed'
               ORDER BY id DESC LIMIT ?""", (limit,)))
        return positive, negative

    # -- unsubscribe queue --------------------------------------------------
    def upsert_unsub(self, msg: Message, targets: list[str]) -> bool:
        """Queue (or refresh) a sender's unsubscribe suggestion. Returns False if the message
        was refused because its receiving server said DMARC *failed*: the From line is almost
        certainly forged, and a forged message must not file its link under the real sender
        (nor, like suspected phishing, prompt us to confirm a live address to whoever sent it).

        A later, less trustworthy message can't replace the link a more trustworthy one
        supplied: verified beats unknown beats unverified. Counts and 'last seen' still update."""
        state, detail = auth.assess(msg.headers)
        if state == "failed":
            return False
        row = self.conn.execute(
            "SELECT status, msg_count, auth FROM unsub_queue WHERE sender_email=?",
            (msg.sender_email,)).fetchone()
        now = datetime.now().isoformat(timespec="seconds")
        if row is None:
            self.conn.execute(
                """INSERT INTO unsub_queue (sender_email, sender, account, msg_count,
                   last_subject, targets, one_click, status, updated_at, last_to, last_received,
                   auth, auth_detail)
                   VALUES (?,?,?,?,?,?,?, 'suggested', ?, ?, ?, ?, ?)""",
                (msg.sender_email, msg.sender, msg.account, 1, msg.subject,
                 json.dumps(targets), int(msg.one_click), now, msg.to or None,
                 msg.date_received or None, state, detail))
        elif row["status"] in ("suggested",):
            if auth.RANK[state] >= auth.RANK[row["auth"] or "unknown"]:
                self.conn.execute(
                    """UPDATE unsub_queue SET msg_count = msg_count + 1, last_subject = ?,
                       targets = ?, one_click = ?, updated_at = ?, last_to = ?, last_received = ?,
                       auth = ?, auth_detail = ?
                       WHERE sender_email = ?""",
                    (msg.subject, json.dumps(targets), int(msg.one_click), now,
                     msg.to or None, msg.date_received or None, state, detail, msg.sender_email))
            else:       # keep the better-authenticated link
                self.conn.execute(
                    """UPDATE unsub_queue SET msg_count = msg_count + 1, last_subject = ?,
                       updated_at = ?, last_to = ?, last_received = ? WHERE sender_email = ?""",
                    (msg.subject, now, msg.to or None, msg.date_received or None, msg.sender_email))
        # done/skipped/protected senders are left alone.
        return True

    def unsub_by_status(self, status: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM unsub_queue WHERE status=? ORDER BY msg_count DESC",
            (status,)))

    def unsub_row(self, sender_email: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM unsub_queue WHERE sender_email=?", (sender_email,)).fetchone()

    def set_unsub_status(self, sender_email: str, status: str) -> None:
        self.conn.execute(
            "UPDATE unsub_queue SET status=?, updated_at=? WHERE sender_email=?",
            (status, datetime.now().isoformat(timespec="seconds"), sender_email))

    def record_unsub_attempt(self, sender_email: str, state: str, note: str) -> None:
        """state: 'done' | 'opened' | 'failed'. Same status rule as the CLI (done if it
        worked, else approved), plus what happened and how many tries, so a failure isn't
        only visible in a log. 'opened' = a page was opened for the user to finish: still
        status 'approved', but opened_at marks it as awaiting their confirmation."""
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """UPDATE unsub_queue SET status=?, last_note=?, attempts=COALESCE(attempts,0)+1,
               updated_at=?, acked_at=NULL, opened_at=? WHERE sender_email=?""",
            ("done" if state == "done" else "approved", note, now,
             now if state == "opened" else None, sender_email))

    def confirm_unsub(self, sender_email: str, note: str) -> None:
        """The user says it worked (or did it by hand). Becomes the unsubscribe date the
        still-sending check counts from."""
        self.conn.execute(
            """UPDATE unsub_queue SET status='done', last_note=?, updated_at=?, acked_at=NULL,
               opened_at=NULL WHERE sender_email=?""",
            (note, datetime.now().isoformat(timespec="seconds"), sender_email))

    def mark_unsub_opened(self, sender_email: str, note: str) -> None:
        """The user opened the page themselves from a failed row: now awaiting confirmation."""
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("UPDATE unsub_queue SET last_note=?, opened_at=?, updated_at=? WHERE sender_email=?",
                          (note, now, now, sender_email))

    def ack_unsub(self, sender_email: str) -> None:
        self.conn.execute("UPDATE unsub_queue SET acked_at=? WHERE sender_email=?",
                          (datetime.now().isoformat(timespec="seconds"), sender_email))

    def unsub_still_sending(self, grace_days: int = 14) -> list[dict]:
        """Senders we unsubscribed from (status 'done') that have sent newsletter/
        marketing/notification mail since. "Since" is the unsubscribe time, or the last
        'ignore' if that is later. Mail within grace_days of the request is flagged
        within_grace: senders get ~10 business days to comply, so it isn't yet a failure."""
        marks = ",".join("?" * len(NOISE_CATEGORIES))
        rows = self.conn.execute(
            f"""SELECT m.sender_email, m.subject, m.date_received FROM messages m
                JOIN unsub_queue u ON u.sender_email = m.sender_email
                WHERE u.status = 'done' AND m.category IN ({marks})""", NOISE_CATEGORIES).fetchall()
        by_sender: dict[str, list] = {}
        for r in rows:
            by_sender.setdefault(r["sender_email"], []).append(r)
        now = datetime.now()
        out = []
        for u in self.conn.execute("SELECT * FROM unsub_queue WHERE status='done'"):
            marks_at = [d for d in (_parse_iso(u["updated_at"]), _parse_iso(u["acked_at"])) if d]
            if not marks_at:
                continue
            cutoff = max(marks_at)
            newer = []
            for m in by_sender.get(u["sender_email"], []):
                d = _parse_iso(m["date_received"])
                if d and _local_naive(d) > cutoff:
                    newer.append((_local_naive(d), m["subject"]))
            if not newer:
                continue
            newer.sort()
            unsubbed = _parse_iso(u["updated_at"])
            out.append({
                "sender_email": u["sender_email"], "sender": u["sender"], "account": u["account"],
                "since": u["updated_at"], "n": len(newer),
                "last_received": newer[-1][0].isoformat(timespec="seconds"),
                "last_subject": newer[-1][1],
                "days_since": (now - unsubbed).days if unsubbed else None,
                "within_grace": bool(unsubbed and (now - unsubbed).days < grace_days),
                "attempts": u["attempts"] or 0, "one_click": bool(u["one_click"]),
                "ignored": bool(u["acked_at"]),
            })
        out.sort(key=lambda x: (-x["n"], x["sender_email"]))
        return out

    def unsubscribed_domains(self) -> dict[str, tuple[str, str]]:
        """domain -> (sender_email, when) for the most recent unsubscribe that worked
        at that domain. Lets the queue flag a *different* address at a domain we've
        already unsubscribed from. Shared/webmail domains are left out."""
        out: dict[str, tuple[str, str]] = {}
        for r in self.conn.execute(
                "SELECT sender_email, updated_at FROM unsub_queue WHERE status='done' "
                "ORDER BY updated_at"):
            dom = r["sender_email"].rpartition("@")[2].lower()
            if dom and dom not in SHARED_DOMAINS:
                out[dom] = (r["sender_email"], r["updated_at"])
        return out

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

    def vacuum(self) -> None:
        self.conn.commit()   # VACUUM can't run inside an open transaction
        self.conn.execute("VACUUM")

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
