"""Unsubscribe execution: RFC 8058 one-click POST, plus browser fallback."""
from __future__ import annotations

import json
import subprocess
import webbrowser

import requests

from .config import UnsubCfg


def is_protected(sender_email: str, cfg: UnsubCfg) -> bool:
    return any(p.lower() in sender_email.lower() for p in cfg.protected)


def _http_targets(targets: list[str]) -> list[str]:
    return [t for t in targets if t.lower().startswith("http")]


def _mailto_targets(targets: list[str]) -> list[str]:
    return [t for t in targets if t.lower().startswith("mailto:")]


def execute(sender_email: str, targets_json: str, one_click: bool,
            cfg: UnsubCfg) -> tuple[bool, str]:
    """Attempt unsubscribe for an approved sender. Returns (done, note)."""
    if cfg.mode == "never":
        return False, "unsubscribe mode is 'never' — nothing sent"
    try:
        targets = json.loads(targets_json or "[]")
    except json.JSONDecodeError:
        targets = []
    if not targets:
        return False, "no List-Unsubscribe targets recorded"

    http = _http_targets(targets)

    # RFC 8058: POST "List-Unsubscribe=One-Click" to the https target.
    if one_click and http and cfg.mode == "one_click":
        url = http[0]
        try:
            r = requests.post(url, data={"List-Unsubscribe": "One-Click"},
                              timeout=15, allow_redirects=True)
            if r.status_code < 400:
                return True, f"one-click POST accepted ({r.status_code})"
            return False, f"one-click POST returned {r.status_code}; try the link manually: {url}"
        except requests.RequestException as e:
            return False, f"one-click POST failed ({e.__class__.__name__}); link: {url}"

    # Fallback: open the unsubscribe page for the user to finish.
    if http:
        _open(http[0])
        return True, f"opened in browser: {http[0]}"

    mailto = _mailto_targets(targets)
    if mailto:
        _open(mailto[0])
        return True, f"opened mail compose: {mailto[0]}"
    return False, "no usable target"


def _open(url: str) -> None:
    try:
        subprocess.run(["open", url], check=False, timeout=10)  # macOS
    except (FileNotFoundError, subprocess.TimeoutExpired):
        webbrowser.open(url)
