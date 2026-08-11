"""Configuration loading with sane defaults."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

CONFIG_PATH = Path("~/.config/mailsweep/config.toml").expanduser()
DATA_DIR = Path("~/.local/share/mailsweep").expanduser()


@dataclass
class MailCfg:
    accounts: list[str] = field(default_factory=list)
    lookback_days: int = 7
    max_messages: int = 400


@dataclass
class ModelCfg:
    host: str = "http://localhost:11434"
    name: str = "llama3.2:3b"
    timeout: int = 30
    enabled: bool = True


@dataclass
class CalendarCfg:
    calendars: list[str] = field(default_factory=list)
    target_calendar: str = "Home"
    horizon_days: int = 60


@dataclass
class UnsubCfg:
    mode: str = "one_click"   # never | http_get | one_click
    protected: list[str] = field(default_factory=list)


@dataclass
class DigestCfg:
    output_dir: str = "~/MailSweep"
    terminal: bool = True


@dataclass
class SpamAuditCfg:
    enabled: bool = True
    llm_review: bool = True
    llm_max_per_scan: int = 40


@dataclass
class PurchasesCfg:
    enabled: bool = True
    lookback_days: int = 60
    mailboxes: list[str] = field(default_factory=lambda: ["INBOX", "Trash"])


@dataclass
class Config:
    mail: MailCfg = field(default_factory=MailCfg)
    model: ModelCfg = field(default_factory=ModelCfg)
    calendar: CalendarCfg = field(default_factory=CalendarCfg)
    unsubscribe: UnsubCfg = field(default_factory=UnsubCfg)
    digest: DigestCfg = field(default_factory=DigestCfg)
    spam_audit: SpamAuditCfg = field(default_factory=SpamAuditCfg)
    purchases: PurchasesCfg = field(default_factory=PurchasesCfg)

    @property
    def db_path(self) -> Path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR / "mailsweep.db"

    @property
    def digest_dir(self) -> Path:
        p = Path(os.path.expanduser(self.digest.output_dir))
        p.mkdir(parents=True, exist_ok=True)
        return p


def _apply(section_obj, data: dict) -> None:
    for key, value in data.items():
        if hasattr(section_obj, key):
            setattr(section_obj, key, value)


def load(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or CONFIG_PATH
    if path.exists():
        if tomllib is None:
            raise RuntimeError("Python < 3.11 needs the 'tomli' package to read config")
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        for section in ("mail", "model", "calendar", "unsubscribe", "digest", "spam_audit",
                        "purchases"):
            if section in raw:
                _apply(getattr(cfg, section), raw[section])
    return cfg
