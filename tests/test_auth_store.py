"""Store behaviour that depends on sender authentication, and the CLI's shared unsubscribe step."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mailsweep import cli, unsub
from mailsweep.config import Config
from mailsweep.models import Classification, Message
from mailsweep.store import Store

VERIFIED = "mx.x; dkim=pass header.d=united.com; spf=pass smtp.mailfrom=united.com; dmarc=pass header.from=united.com"
SPOOF_UNVERIFIED = "mx.x; dkim=pass header.d=rectrac.com; spf=pass smtp.mailfrom=rectrac.com; dmarc=none"
SPOOF_FAILED = "mx.x; dkim=pass header.d=rectrac.com; dmarc=fail (p=REJECT) header.from=united.com"


def msg(ar=None, url="https://united.com/u?id=1", mid="<1>", subject="Deals"):
    headers = {"from": "United <no-reply@united.com>", "list-unsubscribe": f"<{url}>"}
    if ar:
        headers["authentication-results"] = ar
    return Message(message_id=mid, account="P", mailbox="INBOX", sender="United <no-reply@united.com>",
                   sender_email="no-reply@united.com", subject=subject, date_received="2026-09-01T00:00:00.000Z",
                   snippet="", headers=headers, mail_id=1)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def row(store):
    return store.unsub_row("no-reply@united.com")


def test_a_dmarc_failed_message_is_not_queued_but_is_still_recorded(store):
    m = msg(SPOOF_FAILED)
    assert store.upsert_unsub(m, ["https://united.com/u?id=1"]) is False
    assert row(store) is None
    store.record_message(m, Classification("marketing", "low", "x"))
    assert [f["sender"] for f in store.forged_recent()] == ["United <no-reply@united.com>"]
    assert store.conn.execute("SELECT auth FROM messages").fetchone()["auth"] == "failed"


def test_a_forged_message_cannot_overwrite_a_verified_senders_link(store):
    store.upsert_unsub(msg(VERIFIED), ["https://united.com/u?id=1"])
    store.upsert_unsub(msg(SPOOF_UNVERIFIED, mid="<2>", subject="Hi"), ["https://nccary.example/u?email=TOKEN"])
    r = row(store)
    assert json.loads(r["targets"]) == ["https://united.com/u?id=1"] and r["auth"] == "verified"
    assert r["msg_count"] == 2 and r["last_subject"] == "Hi"          # still counted as mail from them


def test_a_verified_message_replaces_an_unverified_link(store):
    store.upsert_unsub(msg(SPOOF_UNVERIFIED, url="https://nccary.example/u"), ["https://nccary.example/u"])
    store.upsert_unsub(msg(VERIFIED, mid="<2>"), ["https://united.com/u?id=1"])
    r = row(store)
    assert json.loads(r["targets"]) == ["https://united.com/u?id=1"] and r["auth"] == "verified"


def test_messages_without_the_header_behave_exactly_as_before(store):
    store.upsert_unsub(msg(None), ["https://a.example/u"])
    store.upsert_unsub(msg(None, mid="<2>"), ["https://b.example/u"])
    r = row(store)
    assert json.loads(r["targets"]) == ["https://b.example/u"] and r["auth"] == "unknown"


def test_old_databases_gain_the_auth_columns_in_place(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE messages (message_id TEXT PRIMARY KEY, account TEXT, sender TEXT, sender_email TEXT, "
                "subject TEXT, date_received TEXT, category TEXT, importance TEXT, reason TEXT, used_llm INTEGER, "
                "processed_at TEXT, mail_id INTEGER, to_addr TEXT)")
    con.commit(); con.close()
    s = Store(db)
    assert "auth" in {r["name"] for r in s.conn.execute("PRAGMA table_info(messages)")}
    assert {"auth", "auth_detail"} <= {r["name"] for r in s.conn.execute("PRAGMA table_info(unsub_queue)")}
    s.close()


# ---------------------------------------------------------------- the CLI's shared approve step
@pytest.fixture
def cli_env(store, monkeypatch):
    cfg = Config()
    store.upsert_unsub(msg(VERIFIED), ["https://united.com/u?id=1"])
    answers, calls = [], []
    monkeypatch.setattr(cli, "_ask", lambda prompt, choices="ynq": (answers.pop(0) if answers else "n"))
    monkeypatch.setattr(unsub, "_open", lambda url: calls.append(("open", url)))
    return store, cfg, answers, calls, monkeypatch


def approve(store, cfg):
    return cli._unsub_now(store, cfg, store.unsub_row("no-reply@united.com"))


def test_cli_post_accepted_is_done(cli_env):
    store, cfg, answers, calls, mp = cli_env
    mp.setattr(unsub, "attempt", lambda *a, **k: ("done", "one-click POST accepted (200)"))
    assert approve(store, cfg) == "done" and row(store)["status"] == "done"


def test_cli_opened_page_is_only_done_if_you_say_so(cli_env):
    store, cfg, answers, calls, mp = cli_env
    mp.setattr(unsub, "attempt", lambda *a, **k: ("opened", "opened united.com in your browser"))
    answers[:] = ["n"]
    assert approve(store, cfg) == "approved"
    r = row(store)
    assert r["status"] == "approved" and r["opened_at"]                    # waits on the UI's "did it work?" list
    answers[:] = ["y"]
    store.set_unsub_status("no-reply@united.com", "suggested")
    assert approve(store, cfg) == "done" and row(store)["status"] == "done"


def test_cli_failure_is_recorded_with_its_reason(cli_env):
    store, cfg, answers, calls, mp = cli_env
    mp.setattr(unsub, "attempt", lambda *a, **k: ("failed", "one-click POST returned 500"))
    assert approve(store, cfg) == "approved"
    r = row(store)
    assert r["last_note"] == "one-click POST returned 500" and not r["opened_at"]


def test_cli_refuses_a_link_a_protected_sender_also_uses(cli_env):
    store, cfg, answers, calls, mp = cli_env
    m = msg(VERIFIED, url="https://united.com/u?id=1")
    other = Message(message_id="<o>", account="P", mailbox="INBOX", sender="Cary <nccary@rectrac.com>",
                    sender_email="nccary@rectrac.com", subject="x", date_received="2026-09-01T00:00:00.000Z",
                    snippet="", headers={"list-unsubscribe": "<https://united.com/u?id=1>"})
    store.upsert_unsub(other, ["https://united.com/u?id=1"])
    store.set_unsub_status("nccary@rectrac.com", "protected")
    called = []
    mp.setattr(unsub, "attempt", lambda *a, **k: called.append(1) or ("done", ""))
    assert approve(store, cfg) == "suggested" and not called and not calls
    assert row(store)["status"] == "suggested"


def test_cli_protected_sender_is_never_contacted(cli_env):
    store, cfg, answers, calls, mp = cli_env
    cfg.unsubscribe.protected = ["united.com"]
    called = []
    mp.setattr(unsub, "attempt", lambda *a, **k: called.append(1) or ("done", ""))
    assert approve(store, cfg) == "protected" and not called
