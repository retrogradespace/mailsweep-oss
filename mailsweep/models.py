"""Shared dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Message:
    message_id: str
    account: str
    mailbox: str
    sender: str          # raw sender string, e.g. 'Jane Doe <jane@example.com>'
    sender_email: str    # normalized address, lowercase
    subject: str
    date_received: str   # ISO 8601
    snippet: str         # first N chars of plain-text content
    headers: dict[str, str] = field(default_factory=dict)
    mail_id: int = 0     # Mail.app's numeric message id, 0 if unknown (needed to move/trash)

    @property
    def list_unsubscribe(self) -> str | None:
        return self.headers.get("list-unsubscribe")

    @property
    def one_click(self) -> bool:
        v = self.headers.get("list-unsubscribe-post", "")
        return "one-click" in v.lower()

    @property
    def to(self) -> str:
        return self.headers.get("to", "")


@dataclass
class EventCandidate:
    title: str
    start: str            # ISO 8601, or "" if the model couldn't pin a time
    end: str              # ISO 8601 or ""
    location: str
    confidence: float
    source_message_id: str
    source_subject: str
    source_sender: str
    all_day: bool = False


@dataclass
class Classification:
    category: str         # personal | work | transactional | newsletter | marketing | notification
    importance: str       # high | normal | low
    reason: str
    event: EventCandidate | None = None
    used_llm: bool = False


CATEGORIES = {"personal", "work", "transactional", "newsletter", "marketing", "notification"}
IMPORTANCE = {"high", "normal", "low"}
