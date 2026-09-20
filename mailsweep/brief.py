"""Render the morning briefing: today's Calendar + mail still needing a reply.

Distinct from digest.py's clutter-focused digest (buried events, unsubscribe
queue, noise stats) -- this is meant to be read in ten seconds: what's
actually on the calendar today, and what's high-importance and unanswered.
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .digest import _CSS, _write_atomic
from .store import Store


def _esc(s) -> str:
    return html.escape(str(s or ""))


def render_html(store: Store, events: list[dict], needs_response_days: int) -> str:
    now = datetime.now().strftime("%A %B %d, %Y %H:%M")
    pending = store.needs_response(days=needs_response_days)

    parts = [f"<!doctype html><html><head><meta charset='utf-8'>"
             f"<title>MailSweep briefing</title><style>{_CSS}</style></head><body>"]
    parts.append(f"<h1>Morning briefing</h1><p class='muted'>{now}</p>")

    parts.append("<h2>Today's calendar</h2>")
    if events:
        ordered = sorted(events, key=lambda e: e.get("start", ""))
        parts.append("<table><tr><th>When</th><th>What</th><th>Where</th></tr>")
        for e in ordered:
            when = (e.get("start", "") or "")[11:16] or e.get("start", "")
            parts.append(
                f"<tr class='event'><td>{_esc(when)}</td><td>{_esc(e.get('title'))}</td>"
                f"<td>{_esc(e.get('location'))}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='muted'>Nothing on the calendar today.</p>")

    parts.append("<h2>Needs a response</h2>")
    if pending:
        parts.append("<table><tr><th>From</th><th>Subject</th><th>Account</th><th>Received</th></tr>")
        for m in pending:
            parts.append(
                f"<tr class='high'><td>{_esc(m['sender'])}</td><td>{_esc(m['subject'])}</td>"
                f"<td>{_esc(m['account'])}</td><td>{_esc((m['date_received'] or '')[:16])}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='muted'>Nothing urgent waiting on a reply.</p>")

    parts.append("</body></html>")
    return "".join(parts)


def write_html(store: Store, events: list[dict], needs_response_days: int, out_dir: Path) -> Path:
    path = out_dir / "brief-latest.html"
    _write_atomic(path, render_html(store, events, needs_response_days))
    return path


def print_terminal(store: Store, events: list[dict], needs_response_days: int) -> None:
    pending = store.needs_response(days=needs_response_days)

    print(f"\n=== Morning briefing: {datetime.now().strftime('%A %B %d')} ===")
    print(f"\nToday's calendar ({len(events)}):")
    if events:
        for e in sorted(events, key=lambda e: e.get("start", "")):
            when = (e.get("start", "") or "")[11:16] or "?"
            loc = f"  @ {e['location']}" if e.get("location") else ""
            print(f"  {when}  {e.get('title')}{loc}")
    else:
        print("  (nothing scheduled)")

    print(f"\nNeeds a response ({len(pending)}):")
    if pending:
        for m in pending:
            print(f"  {m['sender']} — {m['subject']}  [{m['account']}]")
    else:
        print("  (nothing urgent waiting)")
