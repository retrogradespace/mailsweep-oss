"""Render the daily digest: terminal summary + standalone HTML file."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from pathlib import Path

from .store import Store

_DATED_NAME = re.compile(r"^digest-(\d{4}-\d{2}-\d{2})\.html$")

_CSS = """
body{font-family:-apple-system,Helvetica,sans-serif;max-width:820px;margin:2rem auto;
     padding:0 1rem;color:#1a1a1a;line-height:1.45}
h1{font-size:1.4rem} h2{font-size:1.05rem;margin-top:2rem;border-bottom:1px solid #ddd;
     padding-bottom:.3rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
td,th{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #eee;vertical-align:top}
th{color:#666;font-weight:600}
.badge{display:inline-block;padding:.05rem .45rem;border-radius:.6rem;font-size:.75rem;
     background:#eee}
.high{background:#ffe3e3}.event{background:#e3f0ff}.muted{color:#888}
code{background:#f4f4f4;padding:.1rem .3rem;border-radius:.25rem;font-size:.85em}
"""


def _esc(s) -> str:
    return html.escape(str(s or ""))


def render_html(store: Store, run_info: dict) -> str:
    now = datetime.now().strftime("%A %B %d, %Y %H:%M")
    counts = store.category_counts(days=7)
    important = store.important_recent(days=7)
    events = store.events_by_status("new")
    unsubs = store.unsub_by_status("suggested")
    spam_flags = store.spam_by_status("suggested")
    noise = store.noise_stats(days=30)[:15]

    parts = [f"<!doctype html><html><head><meta charset='utf-8'>"
             f"<title>MailSweep digest</title><style>{_CSS}</style></head><body>"]
    parts.append(f"<h1>MailSweep digest</h1><p class='muted'>{now} &middot; "
                 f"{run_info.get('scanned', 0)} new messages scanned across "
                 f"{run_info.get('accounts', '?')} account(s)"
                 f"{' &middot; LLM: ' + run_info.get('llm', 'off')}</p>")

    if counts:
        chips = " ".join(f"<span class='badge'>{_esc(k)}: {v}</span>"
                         for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        parts.append(f"<p>Last 7 days: {chips}</p>")

    parts.append("<h2>Events that might get buried</h2>")
    today = datetime.now().strftime("%Y-%m-%d")
    upcoming = sorted((e for e in events if e["start"] >= today),
                      key=lambda e: e["created_at"], reverse=True)
    past = len(events) - len(upcoming)
    if upcoming:
        shown, extra = upcoming[:20], max(0, len(upcoming) - 20)
        parts.append("<table><tr><th>When</th><th>What</th><th>From</th><th>Conf.</th></tr>")
        for e in shown:
            parts.append(
                f"<tr class='event'><td>{_esc(e['start'])}</td>"
                f"<td>{_esc(e['title'])}"
                + (f"<br><span class='muted'>{_esc(e['location'])}</span>" if e['location'] else "")
                + f"</td><td>{_esc(e['source_sender'])}<br>"
                f"<span class='muted'>{_esc(e['source_subject'])}</span></td>"
                f"<td>{e['confidence']:.0%}</td></tr>")
        parts.append("</table>")
        note = "Review with <code>mailsweep events review</code> to add these to Calendar."
        if extra:
            note = f"+{extra} more upcoming candidate(s) not shown. " + note
        if past:
            note += f" ({past} more pending candidate(s) have already passed and are hidden here.)"
        parts.append(f"<p class='muted'>{note}</p>")
    elif past:
        parts.append(f"<p class='muted'>Nothing upcoming, but {past} pending candidate(s) have "
                     "already passed -- clean up the backlog with "
                     "<code>mailsweep events review</code>.</p>")
    else:
        parts.append("<p class='muted'>Nothing new detected.</p>")

    parts.append("<h2>Needs your attention</h2>")
    if important:
        parts.append("<table><tr><th>From</th><th>Subject</th><th>Account</th></tr>")
        for m in important:
            parts.append(f"<tr class='high'><td>{_esc(m['sender'])}</td>"
                         f"<td>{_esc(m['subject'])}</td><td>{_esc(m['account'])}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='muted'>No high-importance mail flagged this week.</p>")

    parts.append("<h2>Unsubscribe candidates</h2>")
    if unsubs:
        parts.append("<table><tr><th>Sender</th><th>Msgs</th><th>Last subject</th>"
                     "<th>One-click</th></tr>")
        for u in unsubs[:20]:
            parts.append(f"<tr><td>{_esc(u['sender'])}<br>"
                         f"<span class='muted'>{_esc(u['sender_email'])}</span></td>"
                         f"<td>{u['msg_count']}</td><td>{_esc(u['last_subject'])}</td>"
                         f"<td>{'yes' if u['one_click'] else 'no'}</td></tr>")
        parts.append("</table><p class='muted'>Approve or skip with "
                     "<code>mailsweep unsub review</code>. Nothing is sent without you.</p>")
    else:
        parts.append("<p class='muted'>Queue is empty.</p>")

    parts.append("<h2>Possible spam false positives</h2>")
    if spam_flags:
        parts.append("<table><tr><th>Sender</th><th>Subject</th><th>Account</th><th>Flag</th></tr>")
        for s in spam_flags[:20]:
            flag = "<span class='badge high'>possible phishing</span>" if s["phishing_risk"] else ""
            parts.append(f"<tr><td>{_esc(s['sender'])}</td><td>{_esc(s['subject'])}</td>"
                         f"<td>{_esc(s['account'])}</td><td>{flag}</td></tr>")
        parts.append("</table><p class='muted'>Review with <code>mailsweep spam review</code> "
                     "to rescue to Inbox, leave, or protect the sender. Nothing moves without you.</p>")
    else:
        parts.append("<p class='muted'>No receipt/security-style mail found in Junk/Spam this run.</p>")

    parts.append("<h2>Noisiest senders (30 days)</h2>")
    if noise:
        parts.append("<table><tr><th>Sender</th><th>Msgs</th><th>Category</th></tr>")
        for r in noise:
            parts.append(f"<tr><td>{_esc(r['sender'])}</td><td>{r['n']}</td>"
                         f"<td><span class='badge'>{_esc(r['category'])}</span></td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='muted'>No noise recorded yet — run a scan first.</p>")

    parts.append("</body></html>")
    return "".join(parts)


def _write_atomic(dest: Path, text: str) -> None:
    """Write via a temp file + rename instead of truncating dest in place.
    OneDrive's sync engine holds a lock on digest-latest.html often enough
    (it's the file re-read/re-opened on every run) to raise EDEADLK on a
    direct write_text; replace() doesn't open dest for writing, so it
    doesn't race that lock."""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)


def write_html(store: Store, run_info: dict, out_dir: Path) -> Path:
    path = out_dir / f"digest-{datetime.now():%Y-%m-%d}.html"
    html = render_html(store, run_info)
    _write_atomic(path, html)
    _write_atomic(out_dir / "digest-latest.html", html)
    return path


def prune_old(out_dir: Path, keep_days: int) -> int:
    """Delete dated digest-YYYY-MM-DD.html files older than keep_days.
    Parses the date from the filename rather than mtime, so a re-render of
    an old digest can't accidentally save it from pruning. Never touches
    digest-latest.html. Returns the count removed."""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    for f in out_dir.glob("digest-*.html"):
        m = _DATED_NAME.match(f.name)
        if m and m.group(1) < cutoff:
            f.unlink()
            removed += 1
    return removed


def print_terminal(store: Store, run_info: dict) -> None:
    counts = store.category_counts(days=7)
    events = store.events_by_status("new")
    unsubs = store.unsub_by_status("suggested")
    spam_flags = store.spam_by_status("suggested")
    important = store.important_recent(days=7)

    print(f"\nMailSweep — {run_info.get('scanned', 0)} new messages scanned "
          f"(LLM: {run_info.get('llm', 'off')})")
    if counts:
        print("  7-day mix : " + ", ".join(f"{k} {v}" for k, v in
                                           sorted(counts.items(), key=lambda kv: -kv[1])))
    print(f"  events    : {len(events)} candidate(s) pending  -> mailsweep events review")
    print(f"  unsub     : {len(unsubs)} suggestion(s) pending -> mailsweep unsub review")
    print(f"  spam audit: {len(spam_flags)} possible false positive(s) -> mailsweep spam review")
    if important:
        print("  attention :")
        for m in important[:5]:
            print(f"    - {m['sender']}: {m['subject']}  [{m['account']}]")
    print()
