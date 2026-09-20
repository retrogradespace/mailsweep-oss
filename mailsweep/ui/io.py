"""Side effects the UI can trigger, behind one small interface.

LiveIO calls the same bridge/unsub code the CLI review flows use. The demo
build swaps in a stand-in (see demo.py) so the UI can be tried, and tested,
without touching Mail, Calendar, or the network.
"""
from __future__ import annotations

from .. import bridge, classify as clf, unsub


class LiveIO:
    def __init__(self, cfg):
        self.cfg = cfg

    def ollama_up(self) -> bool:
        return clf.ollama_available(self.cfg.model)

    def unsub(self, targets_json: str, one_click: bool, sender_email: str,
              allow_open: bool = True) -> tuple[str, str]:
        """POST first, then open the page if that fails. See unsub.attempt()."""
        return unsub.attempt(targets_json, one_click, self.cfg.unsubscribe, allow_open)

    def open_url(self, url: str) -> None:
        unsub._open(url)

    def move_messages(self, targets: list[dict], dest: str) -> dict:
        return bridge.move_messages(targets, dest)

    def move_message(self, account: str, mailbox: str, mail_id: int, dest: str) -> None:
        bridge.move_message(account, mailbox, mail_id, dest)

    def create_event(self, calendar: str, title: str, start: str, end: str,
                     location: str, all_day: bool) -> None:
        bridge.create_calendar_event(calendar, title, start, end, location, all_day)
