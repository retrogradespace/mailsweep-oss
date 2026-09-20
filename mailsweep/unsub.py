"""Unsubscribe execution: RFC 8058 one-click POST, falling back to opening the page.

Used by both the CLI review flows and the web UI, so they behave the same way.
"""
from __future__ import annotations

import json
import subprocess
import webbrowser
from urllib.parse import urlparse

import requests

from . import auth
from .config import UnsubCfg


def is_protected(sender_email: str, cfg: UnsubCfg) -> bool:
    return any(p.lower() in sender_email.lower() for p in cfg.protected)


def _mailto_targets(targets: list[str]) -> list[str]:
    return [t for t in targets if t.lower().startswith("mailto:")]


def _web_targets(targets: list[str]) -> list[str]:
    """http(s) only. Anything we hand to `open` on the user's behalf must be a real web URL,
    not whatever scheme a header happens to name."""
    return [t for t in targets if t.lower().startswith(("http://", "https://"))]


def _open(url: str) -> None:
    try:
        subprocess.run(["open", url], check=False, timeout=10)  # macOS
    except (FileNotFoundError, subprocess.TimeoutExpired):
        webbrowser.open(url)


# --------------------------------------------------------------------------- shared-link guard
def link_peers(store, row, cfg: UnsubCfg) -> list[dict]:
    """Other queue rows carrying an identical unsubscribe link (same URL, token and all).

    Two addresses of one organisation sharing a link is normal. Two unrelated senders sharing
    one is how a spoofed From line files somebody else's link under a real sender, and acting
    on it would unsubscribe you from that other sender."""
    email, found = row["sender_email"], {}
    try:
        targets = json.loads(row["targets"] or "[]")
    except json.JSONDecodeError:
        targets = []
    for target in targets:
        for p in store.conn.execute(
                "SELECT sender_email, status FROM unsub_queue "
                "WHERE sender_email != ? AND instr(targets, ?) > 0", (email, json.dumps(target))):
            found[p["sender_email"]] = p["status"]
    me = auth.site(email.rpartition("@")[2])
    return [{"sender_email": e, "status": st, "same_org": auth.site(e.rpartition("@")[2]) == me,
             "protected": st == "protected" or is_protected(e, cfg)}
            for e, st in found.items()]


def blocked_by(store, row, cfg: UnsubCfg) -> str | None:
    """A protected sender whose exact link this row carries, if any: acting on it would
    unsubscribe you from someone you asked to keep."""
    return next((p["sender_email"] for p in link_peers(store, row, cfg) if p["protected"]), None)


def blocked_note(peer: str) -> str:
    return (f"refused: this is the same unsubscribe link {peer} uses, and you protected {peer}, "
            "so it would unsubscribe you from them. Nothing sent or opened")


# --------------------------------------------------------------------------- attempt
def attempt(targets_json: str, one_click: bool, cfg: UnsubCfg,
            allow_open: bool = True) -> tuple[str, str]:
    """POST first; if the sender rejects it (or it errors), open the unsubscribe page
    for the user to finish. Returns (state, note):

      done        the one-click POST was accepted (HTTP status < 400)
      opened      a page / compose window was opened; only the user can say if it worked
      failed      nothing worked, or nothing could be tried
      not_opened  a page needed opening but allow_open was False; nothing was recorded

    Opening a page is never reported as done: many pages unsubscribe on load, others
    still want a click, and we can't tell which from here.
    """
    if cfg.mode == "never":
        return "failed", "unsubscribe mode is 'never' — nothing sent"
    try:
        targets = json.loads(targets_json or "[]")
    except json.JSONDecodeError:
        targets = []
    web = _web_targets(targets)
    mailto = _mailto_targets(targets)
    if not web and not mailto:
        return "failed", "no usable unsubscribe target on file"

    post_note = ""
    if one_click and web and cfg.mode == "one_click":
        try:
            r = requests.post(web[0], data={"List-Unsubscribe": "One-Click"},
                              timeout=15, allow_redirects=True)
            if r.status_code < 400:
                return "done", f"one-click POST accepted ({r.status_code})"
            post_note = f"one-click POST returned {r.status_code}"
        except requests.RequestException as e:
            post_note = f"one-click POST failed ({e.__class__.__name__})"

    lead = post_note + "; " if post_note else ""
    if web:
        host = urlparse(web[0]).hostname or "the sender's site"
        if not allow_open:
            if post_note:
                return "failed", f"{post_note}; page not opened (tab limit for one action) — use Open page"
            return "not_opened", "left in the queue: tab limit for one action reached"
        _open(web[0])
        return "opened", f"{lead}opened {host} in your browser — come back and confirm"
    if not allow_open:
        return "not_opened", "left in the queue: tab limit for one action reached"
    addr = mailto[0][len("mailto:"):].split("?", 1)[0]
    _open(mailto[0])
    return "opened", f"{lead}opened a mail compose window to {addr} — send it, then confirm"
