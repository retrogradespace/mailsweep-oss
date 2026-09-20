"""MailSweep command-line interface."""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from . import bridge, brief, classify as clf, config as cfgmod, digest, rules, unsub
from .models import CATEGORIES, IMPORTANCE
from .store import Store
from .timeparse import normalize_end, parse_user_datetime


# --------------------------------------------------------------------------- helpers
def _norm_tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 2}


def events_match(candidate_title: str, candidate_start: str,
                 cal_title: str, cal_start: str) -> bool:
    """Fuzzy match: same calendar date + meaningful title overlap."""
    if not candidate_start or not cal_start:
        return False
    if candidate_start[:10] != cal_start[:10]:
        return False
    a, b = _norm_tokens(candidate_title), _norm_tokens(cal_title)
    if not a or not b:
        return False
    overlap = len(a & b) / min(len(a), len(b))
    return overlap >= 0.5


def _ask(prompt: str, choices: str = "ynq") -> str:
    while True:
        ans = input(f"{prompt} [{'/'.join(choices)}] ").strip().lower()
        if ans and ans[0] in choices:
            return ans[0]


def _ask_from(prompt: str, choices: set[str], default: str | None = None) -> str:
    """Like _ask, but for an open-ended set of full-word choices (category/
    importance names) rather than single letters."""
    suffix = f" [{default}]" if default else ""
    options = "/".join(sorted(choices))
    while True:
        ans = input(f"{prompt} ({options}){suffix}: ").strip().lower()
        if not ans and default:
            return default
        if ans in choices:
            return ans


def _rescue_message(store: Store, m: dict, sender_email: str, reason: str) -> bool:
    """Record and immediately move a Junk-folder message to the Inbox (the
    trusted-sender auto-rescue path). Returns True if the move succeeded --
    on failure the row is left as 'suggested' so a later scan retries it."""
    store.upsert_spam_flag(
        m["message_id"], m["account"], m["mailbox"], m["mail_id"],
        m.get("sender", ""), sender_email, m.get("subject", ""),
        m.get("date_received", ""), reason, False)
    try:
        bridge.move_message(m["account"], m["mailbox"], m["mail_id"], "INBOX")
        store.set_spam_status(m["message_id"], "rescued")
        return True
    except bridge.BridgeError:
        return False


def _run_spam_audit(cfg, store: Store, llm_up: bool) -> tuple[int, int, int]:
    """Scan each account's Junk/Spam folder for likely false-positives, and
    auto-rescue mail from trusted senders. Returns (scanned_count,
    flagged_count, rescued_count). Raises bridge.BridgeError on Mail access
    failure."""
    raw = bridge.fetch_junk_messages(
        cfg.mail.lookback_days, cfg.mail.max_messages, cfg.mail.accounts,
        store.known_spam_message_ids())
    flagged = 0
    rescued = 0
    llm_calls = 0
    for m in raw:
        _, addr = parseaddr(m.get("sender", ""))
        sender_email = addr.lower()
        if store.is_spam_sender_protected(sender_email):
            continue
        if store.is_spam_sender_trusted(sender_email):
            if _rescue_message(store, m, sender_email, "trusted sender"):
                rescued += 1
            continue
        should_flag, reason = rules.flag_spam_audit(m.get("subject", ""), m.get("sender", ""))
        if not should_flag and llm_up and cfg.spam_audit.llm_review \
                and llm_calls < cfg.spam_audit.llm_max_per_scan:
            llm_calls += 1
            should_flag, reason = clf.spam_audit_with_llm(
                m.get("subject", ""), m.get("sender", ""), cfg.model)
        if not should_flag:
            continue
        phishing = rules.spoofed_sender(m.get("sender", ""))
        store.upsert_spam_flag(
            m["message_id"], m["account"], m["mailbox"], m["mail_id"],
            m.get("sender", ""), sender_email, m.get("subject", ""),
            m.get("date_received", ""), reason, phishing)
        flagged += 1
    return len(raw), flagged, rescued


_STALE_MOVE_HINT = (" (the message likely moved or was removed -- by another "
                    "mail client, a server-side rule, or manually -- since the "
                    "last `mailsweep scan`; re-scan to refresh, or just skip/"
                    "dismiss this one)")


def _explain_move_error(err: str) -> str:
    """"Can't get object" is JXA's generic can't-resolve-this-reference
    error; for a per-message move it almost always means the cached mail_id
    no longer points at anything (message moved/deleted since the scan that
    recorded it), not a real bug. Say so instead of showing a bare
    AppleScript error."""
    if "Can't get object" in err:
        return err + _STALE_MOVE_HINT
    return err


def _bulk_move_sender(store: Store, sender_email: str, dest_mailbox: str) -> bool:
    """Move every scanned message on file from sender_email to dest_mailbox
    (e.g. "Trash" or "Archive"), printing a summary. Returns True if at
    least one message was actually moved."""
    refs = store.messages_by_sender(sender_email)
    if not refs:
        print("  -> no scanned messages on file for this sender to move")
        return False
    targets = [{"account": x["account"], "mailbox": "INBOX", "id": x["mail_id"]} for x in refs]
    try:
        result = bridge.move_messages(targets, dest_mailbox)
        print(f"  -> moved {result.get('moved', 0)}/{result.get('total', 0)} "
              f"message(s) to {dest_mailbox}")
        for e in result.get("errors") or []:
            print(f"     ! {_explain_move_error(e)}")
        return result.get("moved", 0) > 0
    except bridge.BridgeError as e:
        print(f"  -> failed: {_explain_move_error(str(e))}")
        return False


def _when(r) -> str:
    """Display form of an event's time; date-only starts are all-day events."""
    all_day = bool(r["all_day"]) or len(r["start"]) <= 10
    return (r["start"] + (" (all day)" if all_day else "")
            + (f" – {r['end']}" if r["end"] else ""))


def _flag_event_time(store: Store, r) -> bool:
    """Fix a real event whose extracted date/time is wrong. Saves the correction
    (keeping the original) so the next scan's prompt can learn from it."""
    print(f"  extracted: {_when(r)}")
    while True:
        raw = input("  correct start (2026-09-20, 2026-09-20 14:30, or 9/20 2:30pm; "
                    "blank to cancel): ").strip()
        if not raw:
            return False
        parsed = parse_user_datetime(raw)
        if parsed:
            break
        print("  -> couldn't read that; try 2026-09-20 or 2026-09-20 14:30")
    start, date_only = parsed
    end = ""
    if not date_only:
        raw_end = input("  correct end (a time, or date + time; blank = none): ").strip()
        if raw_end:
            p = parse_user_datetime(raw_end) or parse_user_datetime(f"{start[:10]} {raw_end}")
            end = normalize_end(start, p[0]) if p else ""
            if not end:
                print("  -> end ignored (unreadable, or not after the start)")
    if not store.flag_event_time(r["id"], start, end, date_only):
        print("  -> not saved: an event with that title and time is already on file for this email")
        return False
    store.commit()
    print(f"  -> saved {start}{' (all day)' if date_only else ''}. The original is kept and shown "
          "to the model on the next scan so it stops repeating this mistake.")
    return True


def _move_source(ctx, dest_mailbox: str) -> bool:
    """Move a single already-resolved source message (a store.message_ref()
    row) to dest_mailbox (e.g. "Trash" or "Archive"). Returns True on
    success."""
    if not ctx or ctx["mail_id"] is None:
        print("  -> source message not on file (predates trash/archive support, "
              "or aged out) — can't move")
        return False
    try:
        bridge.move_message(ctx["account"], "INBOX", ctx["mail_id"], dest_mailbox)
        print(f"  -> source email moved to {dest_mailbox}")
        return True
    except bridge.BridgeError as e:
        print(f"  -> failed: {_explain_move_error(str(e))}")
        return False


def _resolve_purchase_items(cfg, llm_up: bool, msg: dict,
                            order_ref: str | None) -> tuple[list[str] | None, str, float, str | None]:
    """Ask the LLM to name the item(s) in a representative order-related
    message (and its order ref, if not already known from the subject),
    falling back to raw source if the plain-text content doesn't confidently
    name anything (some vendors' plain-text rendering drops item names that
    only appear in the raw HTML). Returns (items, vendor, confidence,
    order_ref); items is None and confidence 0.0 if unresolved."""
    vendor_guess = rules.vendor_from_sender(msg["sender"])
    if not llm_up:
        return None, vendor_guess, 0.0, order_ref
    target = [{"account": msg["account"], "mailbox": msg["mailbox"], "id": msg["mail_id"]}]
    try:
        content = bridge.fetch_message_content(target)
    except bridge.BridgeError:
        content = []
    body = content[0].get("content", "") if content else ""
    if not order_ref:
        order_ref = rules.extract_order_ref(body)
    result = clf.extract_purchase_item(msg["subject"], msg["sender"], body, cfg.model)
    if not result or result["confidence"] < 0.5:
        try:
            src = bridge.fetch_message_source(target)
        except bridge.BridgeError:
            src = []
        raw = clf.clean_html(src[0].get("source", "")) if src else ""
        if not order_ref:
            order_ref = rules.extract_order_ref(raw)
        if raw:
            retry = clf.extract_purchase_item(msg["subject"], msg["sender"], raw, cfg.model)
            if retry and (not result or retry["confidence"] > result["confidence"]):
                result = retry
    if not order_ref and result:
        order_ref = result.get("order_ref")
    if not result:
        return None, vendor_guess, 0.0, order_ref
    return result["items"], (result["vendor"] or vendor_guess), result["confidence"], order_ref


def _unsub_now(store: Store, cfg, row) -> str:
    """Approve one queue row from a CLI review: POST first; if that doesn't work, open the page
    and ask whether it worked. Same rules as the web UI (shared in unsub.py). Opening a page is
    only recorded as done if you say so. Returns the row's resulting status."""
    email = row["sender_email"]
    if unsub.is_protected(email, cfg.unsubscribe):
        store.set_unsub_status(email, "protected")
        print("  -> sender is on your protected list -- nothing sent")
        return "protected"
    peer = unsub.blocked_by(store, row, cfg.unsubscribe)
    if peer:
        print(f"  -> {unsub.blocked_note(peer)}")
        print("     left in the queue; use n=skip or p=protect")
        return "suggested"
    state, note = unsub.attempt(row["targets"], bool(row["one_click"]), cfg.unsubscribe)
    print(f"  -> {note}")
    if state == "opened" and _ask("  did it work? y=yes, mark it done  n=not yet (it waits on the web UI's "
                                  "'did it work?' list)", "yn") == "y":
        state, note = "done", "confirmed done by you"
    store.record_unsub_attempt(email, state, note)
    return "done" if state == "done" else "approved"


def _print_link_warnings(store: Store, cfg, row) -> None:
    """Review-time warnings that come from headers nobody has authenticated."""
    if row["auth"] == "unverified":
        print(f"  !  sender not verified: {row['auth_detail']}")
    odd = [p for p in unsub.link_peers(store, row, cfg.unsubscribe) if p["protected"] or not p["same_org"]]
    if odd:
        print("  !  this exact unsubscribe link is also on: "
              + ", ".join(f"{p['sender_email']} ({p['status']})" for p in odd))


# --------------------------------------------------------------------------- commands
def cmd_init(args) -> int:
    cfg_path = cfgmod.CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        example = Path(__file__).parent.parent / "config.example.toml"
        if example.exists():
            shutil.copy(example, cfg_path)
            print(f"Wrote starter config: {cfg_path}")
        else:
            cfg_path.write_text("", encoding="utf-8")
            print(f"Created empty config: {cfg_path}")
    else:
        print(f"Config already exists: {cfg_path}")

    ok = True
    if shutil.which("osascript") is None:
        print("!! osascript not found — are you on macOS? Mail/Calendar access won't work.")
        ok = False
    else:
        print("ok osascript found (Mail/Calendar bridge available)")

    cfg = cfgmod.load()
    if clf.ollama_available(cfg.model):
        print(f"ok Ollama reachable at {cfg.model.host} (model: {cfg.model.name})")
    else:
        print(f"-- Ollama not reachable at {cfg.model.host}. Install from https://ollama.com,")
        print(f"   then: ollama pull {cfg.model.name}. Until then, rules-only mode is used.")

    print("\nNext steps:")
    print(f"  1. Edit {cfg_path} (account names, protected senders)")
    print("  2. Run: mailsweep scan   (first run triggers macOS Automation prompts — allow Mail & Calendar)")
    print("  3. See launchd/README section to schedule the daily digest")
    return 0 if ok else 1


def cmd_scan(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    llm_up = clf.ollama_available(cfg.model)
    started = datetime.now()
    run_errors: list[dict] = []

    lookback = args.lookback or cfg.mail.lookback_days
    print(f"Scanning Mail.app inboxes (last {lookback} days, "
          f"LLM {'on: ' + cfg.model.name if llm_up else 'off — rules only'})...")
    try:
        messages = bridge.fetch_messages(
            lookback, cfg.mail.max_messages, cfg.mail.accounts,
            store.known_message_ids())
    except bridge.BridgeError as e:
        print(f"Mail bridge error: {e}", file=sys.stderr)
        run_errors.append({"stage": "fetch_messages", "message": str(e)})
        store.record_run("scan", started, datetime.now(), ok=False,
                         messages_scanned=0, accounts=0, llm_up=llm_up, errors=run_errors)
        store.commit()
        store.close()
        return 1

    accounts = sorted({m.account for m in messages})
    print(f"  {len(messages)} new message(s) from {len(accounts) or '?'} account(s)")

    # Built once per scan, not per message -- the underlying corrections/
    # events don't change mid-scan, so re-querying per ambiguous message
    # would just be repeated identical work.
    feedback = clf.build_feedback_block(store) if llm_up else ""

    n_events = 0
    n_forged = 0
    for i, msg in enumerate(messages, 1):
        verdict = clf.classify(msg, cfg.model, llm_up, feedback)
        store.record_message(msg, verdict)
        if verdict.category in ("newsletter", "marketing", "notification", "junk", "trash"):
            # spam_phishing deliberately excluded -- auto-suggesting "unsubscribe" on
            # suspected phishing risks confirming a live address to whoever sent it,
            # even off a real List-Unsubscribe header.
            targets = rules.unsubscribe_targets(msg)
            if targets and not unsub.is_protected(msg.sender_email, cfg.unsubscribe):
                if not store.upsert_unsub(msg, targets):
                    n_forged += 1       # DMARC failed: forged From line, not queued
        if verdict.event and verdict.event.confidence >= 0.4:
            if not store.is_event_sender_protected(msg.sender_email):
                store.add_event(verdict.event)
                n_events += 1
        if i % 25 == 0:
            store.commit()
            print(f"  ...{i}/{len(messages)}")
    store.commit()
    if n_forged:
        print(f"  {n_forged} message(s) failed DMARC (forged From line): not queued for unsubscribe")

    # Cross-check event candidates against Calendar so we don't nag about
    # things already scheduled.
    new_events = store.events_by_status("new")
    if new_events:
        try:
            cal = bridge.fetch_calendar_events(cfg.calendar.horizon_days,
                                               cfg.calendar.calendars)
            for ev in new_events:
                for existing in cal:
                    if events_match(ev["title"], ev["start"],
                                    existing["title"], existing["start"]):
                        store.set_event_status(ev["id"], "on_calendar")
                        break
        except bridge.BridgeError as e:
            print(f"  (calendar cross-check skipped: {e})")
            run_errors.append({"stage": "calendar_cross_check", "message": str(e)})
    store.commit()

    n_spam_flagged = 0
    if cfg.spam_audit.enabled:
        print("Auditing Junk/Spam folders for possible false positives...")
        try:
            n_spam_scanned, n_spam_flagged, n_spam_rescued = _run_spam_audit(cfg, store, llm_up)
            print(f"  {n_spam_scanned} spam-folder message(s) scanned, "
                  f"{n_spam_flagged} flagged for review, {n_spam_rescued} auto-rescued (trusted senders)")
            if n_spam_flagged:
                print("  -> mailsweep spam review")
        except bridge.BridgeError as e:
            print(f"  (spam audit skipped: {e})")
            run_errors.append({"stage": "spam_audit", "message": str(e)})
    store.commit()

    run_info = {"scanned": len(messages), "accounts": len(accounts) or "?",
                "llm": cfg.model.name if llm_up else "off",
                "spam_flagged": n_spam_flagged}
    path = digest.write_html(store, run_info, cfg.digest_dir)
    if cfg.digest.terminal:
        digest.print_terminal(store, run_info)
    print(f"Digest written: {path}")
    n_pruned = digest.prune_old(cfg.digest_dir, cfg.digest.keep_days)
    if n_pruned:
        print(f"  pruned {n_pruned} digest file(s) older than {cfg.digest.keep_days} days")

    store.record_run("scan", started, datetime.now(), ok=True,
                     messages_scanned=len(messages), accounts=len(accounts),
                     llm_up=llm_up, errors=run_errors)
    store.commit()
    store.close()
    return 0


def cmd_digest(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    path = digest.write_html(store, {"scanned": 0, "accounts": "-", "llm": "-"},
                             cfg.digest_dir)
    store.close()
    print(f"Digest written: {path}")
    if args.open:
        subprocess.run(["open", str(path)], check=False)
    return 0


def cmd_brief(args) -> int:
    """Morning briefing: today's Calendar + high-importance mail still
    unanswered. Reads whatever `scan` last classified rather than
    re-scanning, so it's meant to run right after (or is chained after it
    in launchd)."""
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    days = args.days if args.days is not None else cfg.brief.needs_response_days
    started = datetime.now()
    run_errors: list[dict] = []

    try:
        events = bridge.fetch_calendar_events(cfg.calendar.horizon_days, cfg.calendar.calendars,
                                              today_only=True)
    except bridge.BridgeError as e:
        print(f"  (calendar fetch skipped: {e})")
        run_errors.append({"stage": "calendar_fetch", "message": str(e)})
        events = []

    path = brief.write_html(store, events, days, cfg.digest_dir)
    brief.print_terminal(store, events, days)
    print(f"\nBriefing written: {path}")

    store.record_run("brief", started, datetime.now(), ok=True,
                     messages_scanned=None, accounts=None, llm_up=None, errors=run_errors)
    store.commit()
    store.close()
    if args.open:
        subprocess.run(["open", str(path)], check=False)
    return 0


def cmd_runs(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    rows = store.recent_runs(limit=args.limit)
    if not rows:
        print("No run history yet.")
    for r in rows:
        errs = json.loads(r["errors"] or "[]")
        status = "ok" if r["ok"] else "FAILED"
        scanned = f"scanned={r['messages_scanned']}" if r["messages_scanned"] is not None else ""
        err_note = ("  -- " + ", ".join(e["stage"] for e in errs)) if errs else ""
        print(f"  {r['started_at']}  {r['command']:<6} {status:<6} "
              f"{r['duration_s']:.1f}s  {scanned}{err_note}")

    summary = store.run_error_summary(days=args.days)
    if summary["stage_failures"]:
        print(f"\nLast {args.days} days ({summary['total_runs']} run(s)) -- recurring failures:")
        for stage, n in sorted(summary["stage_failures"].items(), key=lambda kv: -kv[1]):
            print(f"  {stage}: failed {n}x")
    store.close()
    return 0


def cmd_gc(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    days = args.days if args.days is not None else cfg.gc.keep_days

    if args.dry_run:
        n = store.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE processed_at < datetime('now', ?)",
            (f"-{days} days",)).fetchone()["n"]
        print(f"Would delete {n} message row(s) older than {days} days (dry run, nothing changed).")
        store.close()
        return 0

    n = store.purge_old_messages(days)
    store.commit()
    print(f"Deleted {n} message row(s) older than {days} days.")
    if n:
        before = cfg.db_path.stat().st_size
        store.vacuum()
        after = cfg.db_path.stat().st_size
        print(f"Vacuumed database: {before / 1e6:.1f}MB -> {after / 1e6:.1f}MB")
    store.close()
    return 0


def cmd_unsub(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    if args.action == "list":
        rows = store.unsub_by_status("suggested")
        if not rows:
            print("No unsubscribe suggestions pending.")
        for r in rows:
            print(f"  {r['msg_count']:>3}x  {r['sender']}  <{r['sender_email']}>"
                  f"{'  [one-click]' if r['one_click'] else ''}")
            print(f"        inbox: {r['account']} / INBOX    to: {r['last_to'] or '(unknown)'}"
                  f"    last: {(r['last_received'] or '')[:10] or '(unknown)'}")
    elif args.action == "review":
        rows = store.unsub_by_status("suggested")
        if not rows:
            print("No unsubscribe suggestions pending.")
        for r in rows:
            print(f"\n{r['sender']}  <{r['sender_email']}>  ({r['msg_count']} msgs"
                  f"{', one-click' if r['one_click'] else ''})")
            print(f"  inbox: {r['account']} / INBOX")
            print(f"  to   : {r['last_to'] or '(unknown)'}")
            print(f"  date : {(r['last_received'] or '')[:10] or '(unknown)'}")
            print(f"  last : {r['last_subject']}")
            _print_link_warnings(store, cfg, r)
            while True:
                ans = _ask("  unsubscribe? y=yes n=skip p=protect t=trash existing mail "
                           "a=archive existing mail q=quit", "ynptaq")
                if ans in ("t", "a"):
                    _bulk_move_sender(store, r["sender_email"], "Trash" if ans == "t" else "Archive")
                    continue
                break
            if ans == "q":
                break
            if ans == "n":
                store.set_unsub_status(r["sender_email"], "skipped")
            elif ans == "p":
                store.set_unsub_status(r["sender_email"], "protected")
            elif ans == "y":
                _unsub_now(store, cfg, r)
            store.commit()
    store.close()
    return 0


def cmd_events(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    today = datetime.now().strftime("%Y-%m-%d")
    if args.action == "list":
        rows = store.events_by_status("new")
        if not args.include_past:
            rows = [r for r in rows if r["start"] >= today]
        if not rows:
            print("No pending event candidates.")
        for r in rows:
            ctx = store.message_ref(r["source_message_id"])
            where = f"  @ {r['location']}" if r["location"] else ""
            print(f"  #{r['id']}  {r['start']}  {r['title']}{where}  "
                  f"({r['confidence']:.0%}, from {r['source_sender']})")
            acct = ctx["account"] if ctx else "?"
            to = (ctx["to_addr"] if ctx else "") or "(unknown)"
            print(f"        inbox: {acct} / INBOX    to: {to}")
    elif args.action == "review":
        rows = store.events_by_status("new")
        if not args.include_past:
            rows = [r for r in rows if r["start"] >= today]
        if not rows:
            print("No pending event candidates.")
        skip_ids = set()  # already dismissed via an earlier protect in this pass
        for r in rows:
            if r["id"] in skip_ids:
                continue
            ctx = store.message_ref(r["source_message_id"])
            print(f"\n#{r['id']}  {r['title']}")
            print(f"  when : {_when(r)}")
            print(f"  where: {r['location'] or '(not specified)'}")
            print(f"  inbox: {ctx['account'] if ctx else '?'} / INBOX")
            print(f"  to   : {(ctx['to_addr'] if ctx else '') or '(unknown)'}")
            print(f"  from : {r['source_sender']} — {r['source_subject']} "
                  f"({r['confidence']:.0%} confidence)")
            while True:
                ans = _ask("  add to Calendar? y=yes n=dismiss s=skip f=flag wrong date/time "
                           "t=trash source email a=archive source email p=protect sender "
                           "(never suggest events again) q=quit", "ynsftapq")
                if ans == "f":
                    if _flag_event_time(store, r):
                        r = store.event_by_id(r["id"])
                        print(f"  when : {_when(r)}")
                    continue
                if ans in ("t", "a"):
                    if _move_source(ctx, "Trash" if ans == "t" else "Archive"):
                        store.set_event_status(r["id"], "dismissed")
                        store.commit()
                    break
                if ans == "p":
                    sender_email = r["source_sender_email"]
                    if not sender_email:
                        store.set_event_status(r["id"], "dismissed")
                        print("  -> couldn't parse a sender address; dismissed this one only")
                        break
                    dismissed_ids = store.protect_event_sender(sender_email)
                    skip_ids.update(dismissed_ids)
                    print(f"  -> {sender_email} won't be suggested as an event source again "
                          f"({len(dismissed_ids)} pending candidate(s) dismissed)")
                    move_ans = _ask("  also move this sender's scanned messages? "
                                    "t=trash a=archive n=leave alone", "tan")
                    if move_ans in ("t", "a"):
                        _bulk_move_sender(store, sender_email, "Trash" if move_ans == "t" else "Archive")
                    store.commit()
                    break
                break
            if ans == "q":
                break
            if ans == "n":
                store.set_event_status(r["id"], "dismissed")
            elif ans == "y":
                start = r["start"]
                all_day = bool(r["all_day"]) or len(start) <= 10
                try:
                    bridge.create_calendar_event(
                        cfg.calendar.target_calendar, r["title"], start,
                        r["end"], r["location"], all_day)
                    store.set_event_status(r["id"], "created")
                    print(f"  -> added to '{cfg.calendar.target_calendar}'")
                except bridge.BridgeError as e:
                    print(f"  -> failed: {e}")
            store.commit()
    store.close()
    return 0


def cmd_spam(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    llm_up = clf.ollama_available(cfg.model)

    if args.action == "scan":
        print("Scanning Junk/Spam folders for possible false positives...")
        try:
            n_scanned, n_flagged, n_rescued = _run_spam_audit(cfg, store, llm_up)
        except bridge.BridgeError as e:
            print(f"Mail bridge error: {e}", file=sys.stderr)
            store.close()
            return 1
        store.commit()
        print(f"  {n_scanned} spam-folder message(s) scanned, {n_flagged} flagged for review, "
              f"{n_rescued} auto-rescued (trusted senders)")
    elif args.action == "list":
        rows = store.spam_by_status("suggested")
        if not rows:
            print("No spam false-positive candidates pending.")
        for r in rows:
            risk = "  [POSSIBLE PHISHING]" if r["phishing_risk"] else ""
            print(f"  {r['account']:25s} {r['sender']}{risk}")
            print(f"      {r['subject']}    ({(r['date_received'] or '')[:10] or '?'})")
    elif args.action == "review":
        rows = store.spam_by_status("suggested")
        if not rows:
            print("No spam false-positive candidates pending.")
        for r in rows:
            print(f"\n{r['sender']}  (account: {r['account']} / {r['mailbox']})")
            print(f"  subject: {r['subject']}")
            print(f"  date   : {(r['date_received'] or '')[:10] or '(unknown)'}")
            print(f"  reason : {r['reason']}")
            if r["phishing_risk"]:
                print("  ⚠  possible phishing -- sender display name doesn't match its domain")
            ans = _ask("  rescue to Inbox? y=yes (this one) a=yes, always rescue this sender "
                       "n=leave in spam p=protect sender (confirm this really is junk, stop "
                       "flagging) q=quit", "ynapq")
            if ans == "q":
                break
            if ans == "n":
                store.set_spam_status(r["message_id"], "left")
            elif ans == "p":
                store.set_spam_status(r["message_id"], "protected")
            elif ans in ("y", "a"):
                try:
                    bridge.move_message(r["account"], r["mailbox"], r["mail_id"], "INBOX")
                    store.set_spam_status(r["message_id"], "rescued")
                    if ans == "a":
                        store.trust_spam_sender(r["sender_email"])
                        print(f"  -> moved to Inbox, and {r['sender_email']} will always be "
                              "rescued from now on")
                    else:
                        print("  -> moved to Inbox")
                except bridge.BridgeError as e:
                    print(f"  -> failed: {e}")
            store.commit()
    elif args.action == "trust":
        sender_email = (args.sender or "").strip().lower()
        if not sender_email:
            print("Usage: mailsweep spam trust <sender-email>", file=sys.stderr)
            store.close()
            return 1
        store.trust_spam_sender(sender_email)
        store.commit()
        print(f"{sender_email} marked as trusted -- their Junk mail will always be rescued.")

        n_rescued = 0
        for r in store.spam_rows_for_sender(sender_email):
            try:
                bridge.move_message(r["account"], r["mailbox"], r["mail_id"], "INBOX")
                store.set_spam_status(r["message_id"], "rescued")
                n_rescued += 1
            except bridge.BridgeError as e:
                print(f"  ! failed to move a previously-tracked message: {_explain_move_error(str(e))}")
        store.commit()

        n_swept = 0
        try:
            raw = bridge.fetch_junk_messages(cfg.mail.lookback_days, cfg.mail.max_messages,
                                             cfg.mail.accounts, store.known_spam_message_ids())
        except bridge.BridgeError as e:
            print(f"  (live Junk sweep skipped: {e})")
            raw = []
        for m in raw:
            _, addr = parseaddr(m.get("sender", ""))
            if addr.lower() != sender_email:
                continue
            if _rescue_message(store, m, sender_email, "trusted sender"):
                n_swept += 1
        store.commit()
        print(f"  -> rescued {n_rescued + n_swept} message(s) total "
              f"({n_rescued} previously tracked, {n_swept} newly found in Junk)")
    store.close()
    return 0


def cmd_purchases(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    llm_up = clf.ollama_available(cfg.model)

    if args.action == "scan":
        lookback = args.lookback or cfg.purchases.lookback_days
        print(f"Scanning purchase-related mail (last {lookback} days across "
              f"{', '.join(cfg.purchases.mailboxes)}, "
              f"LLM {'on: ' + cfg.model.name if llm_up else 'off — item names/reviews will be skipped'})...")
        try:
            raw = bridge.fetch_purchase_candidates(lookback, cfg.purchases.mailboxes, cfg.mail.accounts)
        except bridge.BridgeError as e:
            print(f"Mail bridge error: {e}", file=sys.stderr)
            store.close()
            return 1

        candidates = [m for m in raw if rules.purchase_kind(m.get("subject", ""))]
        print(f"  {len(raw)} message(s) scanned, {len(candidates)} look purchase-related")

        groups: dict[str, list[dict]] = {}
        for m in candidates:
            ref = rules.extract_order_ref(m["subject"])
            key = f"ref:{ref}" if ref else f"msg:{m['account']}:{m['mailbox']}:{m['mail_id']}"
            groups.setdefault(key, []).append(m)

        n_items = 0
        for key, msgs in groups.items():
            best = max(msgs, key=lambda m: rules.kind_rank(rules.purchase_kind(m["subject"])))
            kind = rules.purchase_kind(best["subject"])
            status = {"returned": "returned_excluded", "delivered": "ready",
                     "shipped": "pending_receipt", "ordered": "pending_receipt"}[kind]
            order_ref = rules.extract_order_ref(best["subject"])
            # Order confirmations usually list items most fully; prefer one for extraction.
            source_msg = next((m for m in msgs if rules.purchase_kind(m["subject"]) == "ordered"), best)

            items, vendor, confidence, order_ref = _resolve_purchase_items(
                cfg, llm_up, source_msg, order_ref)
            if not items or confidence < 0.4:
                status = "name_unresolvable"
                label = f"{len(msgs)} item(s) (item name unknown -- {best['date_received'][:10]})"
                store.upsert_purchase(key, order_ref, vendor, label, source_msg["account"],
                                      source_msg["mailbox"], source_msg["mail_id"],
                                      best["date_received"], status, None)
                n_items += 1
                continue

            for idx, item in enumerate(items):
                review_text = None
                if status == "ready" and llm_up:
                    review_text = clf.draft_purchase_review(item, vendor, "", cfg.model)
                item_key = key if len(items) == 1 else f"{key}::{idx}"
                store.upsert_purchase(item_key, order_ref, vendor, item, source_msg["account"],
                                      source_msg["mailbox"], source_msg["mail_id"],
                                      best["date_received"], status, review_text)
                n_items += 1
        store.commit()
        print(f"  {len(groups)} order group(s), {n_items} item row(s) recorded — "
              f"see `mailsweep purchases list --status ready`")

    elif args.action == "list":
        status = args.status or "ready"
        rows = store.purchases_by_status(status)
        if not rows:
            print(f"No purchase candidates with status '{status}'.")
        for r in rows:
            date = (r["order_date"] or "")[:10] or "?"
            print(f"  [{r['status']}]  {r['vendor']:<15} {r['item']}  "
                  f"(order {r['order_ref'] or '?'}, {date})")
            if r["review_text"]:
                print(f"      draft: {r['review_text']}")

    elif args.action == "review":
        rows = store.purchases_by_status("ready")
        if not rows:
            print("No purchase-review drafts pending.")
        for r in rows:
            print(f"\n{r['vendor']}  —  {r['item']}")
            print(f"  order   : {r['order_ref'] or '(no ref found)'}")
            print(f"  received: {(r['order_date'] or '')[:10] or '?'}")
            print(f"  draft   : {r['review_text'] or '(no draft — LLM was off or found nothing to say)'}")
            ans = _ask("  approve for export? y=yes e=edit text n=skip r=not received yet "
                       "x=return/exclude q=quit", "yenrxq")
            if ans == "q":
                break
            if ans == "y":
                store.set_purchase_status(r["key"], "approved")
            elif ans == "e":
                text = input("  new review text: ").strip()
                store.set_purchase_status(r["key"], "approved", review_text=text or r["review_text"])
            elif ans == "n":
                store.set_purchase_status(r["key"], "skipped")
            elif ans == "r":
                store.set_purchase_status(r["key"], "pending_receipt")
            elif ans == "x":
                store.set_purchase_status(r["key"], "returned_excluded")
            store.commit()

    elif args.action == "export":
        rows = store.purchases_by_status("approved")
        if not rows:
            print("No approved purchase reviews to export. Run `mailsweep purchases review` first.")
            store.close()
            return 0
        path = cfg.digest_dir / f"purchase_reviews_{datetime.now().strftime('%Y-%m-%d')}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["vendor", "item", "order_ref", "review_text", "status"])
            for r in rows:
                w.writerow([r["vendor"], r["item"], r["order_ref"] or "",
                           r["review_text"] or "", r["status"]])
        print(f"Exported {len(rows)} approved review(s) to {path}")

    store.close()
    return 0


def cmd_dupes(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    groups = store.likely_cross_account_duplicates(days=args.days, window_hours=args.window_hours)
    if not groups:
        print(f"No likely cross-account duplicates found in the last {args.days} days.")
    else:
        print(f"{len(groups)} likely duplicate group(s) across accounts "
              f"(last {args.days} days, within {args.window_hours}h of each other):\n")
        for g in groups:
            print(f"  {g['count']}x  {g['sender']}  <{g['sender_email']}>")
            print(f"       \"{g['subject']}\"")
            print(f"       accounts: {', '.join(g['accounts'])}")
    store.close()
    return 0


def cmd_classify(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    if args.action == "review":
        if args.category and args.category not in CATEGORIES:
            print(f"Unknown category '{args.category}' -- choose from: {', '.join(sorted(CATEGORIES))}",
                  file=sys.stderr)
            store.close()
            return 1
        rows = store.unreviewed_llm_messages(limit=args.limit, category=args.category,
                                             include_actioned=args.include_actioned)
        if not rows:
            scope = f" predicted '{args.category}'" if args.category else ""
            print(f"No unreviewed LLM classifications{scope} pending.")
        for r in rows:
            print(f"\n{r['sender']}  ({(r['date_received'] or '')[:10] or '?'})")
            print(f"  subject   : {r['subject']}")
            print(f"  predicted : {r['category']} / {r['importance']}  ({r['reason']})")
            ans = _ask("  correct? y=yes n=no, fix it s=skip for now q=quit", "ynsq")
            if ans == "q":
                break
            if ans == "s":
                continue
            if ans == "y":
                store.record_classify_feedback(
                    r["message_id"], r["subject"], r["sender"],
                    r["category"], r["importance"], r["category"], r["importance"])
            elif ans == "n":
                cat = _ask_from("  correct category?", CATEGORIES)
                imp = _ask_from("  correct importance?", IMPORTANCE, default=r["importance"])
                store.record_classify_feedback(
                    r["message_id"], r["subject"], r["sender"],
                    r["category"], r["importance"], cat, imp)
            store.commit()
        print("\nCorrections feed back into the next `scan`'s LLM prompt as few-shot examples.")
    store.close()
    return 0


def cmd_stats(args) -> int:
    cfg = cfgmod.load()
    store = Store(cfg.db_path)
    counts = store.category_counts(days=args.days)
    total = sum(counts.values())
    print(f"Last {args.days} days — {total} messages processed")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        bar = "#" * max(1, round(40 * v / total)) if total else ""
        print(f"  {k:<14}{v:>5}  {bar}")
    noise = store.noise_stats(days=args.days, include_actioned=args.include_actioned)
    top_noise = noise[:15]
    if noise:
        print("\nNoisiest senders:")
        for r in top_noise:
            received = (r["last_received"] or "")[:10] or "?"
            print(f"  {r['n']:>4}x  {r['sender']}  [{r['category']}]  (last received {received})")

    if args.review and top_noise:
        print("\nReviewing noisiest senders for bulk trash/junk/archive...")
        for r in top_noise:
            unsub_row = store.unsub_row(r["sender_email"])
            can_unsub = bool(unsub_row and unsub_row["status"] == "suggested"
                              and json.loads(unsub_row["targets"] or "[]"))
            print(f"\n{r['sender']}  <{r['sender_email']}>  ({r['n']} msgs, "
                  f"account {r['account']}, {r['category']})")
            print(f"  last received: {(r['last_received'] or '')[:10] or '?'}")
            print(f"  last subject : {r['last_subject']}")
            if can_unsub:
                print("  unsubscribe target on file")
            ans = _ask("  move all of this sender's scanned messages? "
                       "d=delete (Trash) j=junk (report spam) a=archive n=skip q=quit", "djanq")
            if ans == "q":
                break
            if ans in ("d", "j", "a"):
                dest = {"d": "Trash", "j": "Junk", "a": "Archive"}[ans]
                _bulk_move_sender(store, r["sender_email"], dest)
                if can_unsub:
                    u_ans = _ask("  also unsubscribe from this sender now?", "yn")
                    if u_ans == "y":
                        _unsub_now(store, cfg, unsub_row)
                store.commit()
    store.close()
    return 0


def cmd_ui(args) -> int:
    # Imported here so the CLI (and launchd's daily scan) never loads the web stack.
    from .ui import server
    from .ui.service import Service

    if args.demo:
        from .ui import demo
        cfg, io, db_path, tmp = demo.build()
        return server.serve(Service(cfg, io, db_path, demo=True), args.port,
                            open_browser=not args.no_open, cleanup=tmp)
    from .ui.io import LiveIO
    cfg = cfgmod.load()
    return server.serve(Service(cfg, LiveIO(cfg), cfg.db_path), args.port,
                        open_browser=not args.no_open)


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mailsweep",
                                description="Local mail triage for Apple Mail.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="write starter config and check dependencies")

    sp = sub.add_parser("scan", help="scan inboxes, classify, update digest")
    sp.add_argument("--lookback", type=int, default=None,
                    help="override lookback days for this run")

    sp = sub.add_parser("digest", help="re-render the HTML digest from the database")
    sp.add_argument("--open", action="store_true", help="open it after writing")

    sp = sub.add_parser("brief", help="morning briefing: today's Calendar + mail needing a reply")
    sp.add_argument("--days", type=int, default=None,
                    help="override how far back to look for unanswered high-importance mail")
    sp.add_argument("--open", action="store_true", help="open it after writing")

    sp = sub.add_parser("runs", help="scan/brief run history and recurring error patterns")
    sp.add_argument("--limit", type=int, default=20, help="how many recent runs to list")
    sp.add_argument("--days", type=int, default=30, help="window for the recurring-failure summary")

    sp = sub.add_parser("gc", help="delete old message rows from the database and reclaim space")
    sp.add_argument("--days", type=int, default=None,
                    help="override retention window (default: gc.keep_days in config, 180)")
    sp.add_argument("--dry-run", action="store_true", help="show what would be deleted, change nothing")

    sp = sub.add_parser("unsub", help="unsubscribe queue")
    sp.add_argument("action", choices=["list", "review"])

    sp = sub.add_parser("events", help="detected event candidates")
    sp.add_argument("action", choices=["list", "review"])
    sp.add_argument("--include-past", action="store_true",
                    help="also show candidates whose start date has already passed "
                    "(hidden by default -- a past date can't usefully be added to Calendar)")

    sp = sub.add_parser("classify", help="review recent LLM classifications and correct them "
                        "(feeds few-shot examples into future scans)")
    sp.add_argument("action", choices=["review"])
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--category", default=None,
                    help="only review messages predicted this category (e.g. marketing) -- "
                         "for skimming a backlog bucket-by-bucket instead of raw date order")
    sp.add_argument("--include-actioned", action="store_true",
                    help="also show messages already resolved in `unsub review`/`events review` "
                         "(hidden by default, since you already dealt with them there)")

    sp = sub.add_parser("stats", help="inbox noise statistics")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--review", action="store_true",
                    help="after listing, interactively offer to bulk-trash/junk/archive each "
                    "noisy sender's messages, and unsubscribe if a target is on file")
    sp.add_argument("--include-actioned", action="store_true",
                    help="also show senders already resolved via unsub review or a prior "
                    "stats --review (done/protected/skipped/approved) -- hidden by default")

    sp = sub.add_parser("dupes", help="likely cross-account duplicates (e.g. from old forwarding rules)")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--window-hours", type=int, default=72,
                    help="max time gap between copies to still count as a duplicate")

    sp = sub.add_parser("spam", help="audit Junk/Spam folders for false positives")
    sp.add_argument("action", choices=["scan", "list", "review", "trust"])
    sp.add_argument("sender", nargs="?", default=None,
                    help="sender email address, required for 'trust'")

    sp = sub.add_parser("purchases", help="purchase-review sweep: draft product reviews from order emails")
    sp.add_argument("action", choices=["scan", "list", "review", "export"])
    sp.add_argument("--lookback", type=int, default=None, help="override lookback days for scan")
    sp.add_argument("--status", default="ready",
                    help="status to show for `list` (default: ready; also try pending_receipt, "
                         "approved, returned_excluded, name_unresolvable, skipped)")

    sp = sub.add_parser("ui", help="local web UI: the same review flows in a browser (127.0.0.1 only)")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    sp.add_argument("--demo", action="store_true",
                    help="run on throwaway sample data; nothing touches Mail, Calendar, or the network")

    args = p.parse_args(argv)
    handlers = {"init": cmd_init, "scan": cmd_scan, "digest": cmd_digest, "brief": cmd_brief,
                "runs": cmd_runs, "gc": cmd_gc, "unsub": cmd_unsub, "events": cmd_events,
                "classify": cmd_classify, "stats": cmd_stats, "dupes": cmd_dupes, "spam": cmd_spam,
                "purchases": cmd_purchases, "ui": cmd_ui}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
