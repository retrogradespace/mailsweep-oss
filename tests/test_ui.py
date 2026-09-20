"""Tests for the web UI: service actions (against seeded demo data + a fake I/O
layer) and the HTTP server's localhost hardening."""
import http.client
import json
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mailsweep import bridge
from mailsweep.ui import demo
from mailsweep.ui.server import make_server
from mailsweep.ui.service import ApiError, Service, approval_method


@pytest.fixture
def env():
    cfg, io, db, tmp = demo.build()
    svc = Service(cfg, io, db, demo=True)
    yield svc, io, cfg
    shutil.rmtree(tmp, ignore_errors=True)


def first(res):
    return res["results"][0]


# ---------------------------------------------------------------- reads
def test_summary_counts_match_seed(env):
    svc, _, _ = env
    s = svc.summary()
    assert (s["unsub"], s["events"], s["events_past"], s["spam"], s["reviews"]) == (19, 5, 1, 3, 2)
    assert s["unsub_mode"] == "one_click"


def test_unsub_list_hides_targets_and_labels_method(env):
    svc, _, _ = env
    items = svc.unsub_list()["items"]
    assert len(items) == 19 and all("targets" not in i for i in items)
    by = {i["sender_email"]: i for i in items}
    assert by["deals@wayfair.example"]["method"] == "one_click"
    assert by["deals@wayfair.example"]["host"] == "wayfair.example"
    assert by["no-reply@instagram.example"]["method"] == "browser"   # not one-click, has an https target


def test_approval_method_follows_mode_never(env):
    svc, _, cfg = env
    cfg.unsubscribe.mode = "never"
    assert {i["method"] for i in svc.unsub_list()["items"]} == {"none"}


def test_events_hide_past_unless_asked(env):
    svc, _, _ = env
    d = svc.events_list()
    assert d["total"] == 5 and d["hidden_past"] == 1
    assert svc.events_list(include_past=True)["total"] == 6


# ---------------------------------------------------------------- unsubscribe
def test_approve_sends_once_even_if_clicked_twice(env):
    svc, io, _ = env
    email = "deals@wayfair.example"
    assert first(svc.unsub_act("approve", [email]))["ok"] is True
    again = first(svc.unsub_act("approve", [email]))
    assert again["ok"] is False and "already done" in again["note"]
    assert [c for c in io.calls if c[0] == "unsub"] == [("unsub", email)]
    assert svc.summary()["unsub"] == 18


def test_approve_refuses_protected_sender(env):
    svc, io, cfg = env
    cfg.unsubscribe.protected = ["wayfair"]
    r = first(svc.unsub_act("approve", ["deals@wayfair.example"]))
    assert r["ok"] is False and "protected" in r["note"]
    assert not [c for c in io.calls if c[0] == "unsub"]


def test_approve_failure_is_recorded_not_lost(env):
    svc, io, _ = env
    io.unsub = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down"))
    r = first(svc.unsub_act("approve", ["deals@wayfair.example"]))
    assert r["ok"] is False and "network down" in r["note"]
    with svc.store() as s:
        assert s.unsub_row("deals@wayfair.example")["status"] == "approved"   # same as the CLI


def test_skip_and_protect_change_status(env):
    svc, io, _ = env
    svc.unsub_act("skip", ["deals@wayfair.example"])
    svc.unsub_act("protect", ["no-reply@instagram.example"])
    with svc.store() as s:
        assert s.unsub_row("deals@wayfair.example")["status"] == "skipped"
        assert s.unsub_row("no-reply@instagram.example")["status"] == "protected"
    assert not [c for c in io.calls if c[0] == "unsub"]


def test_trash_existing_moves_scanned_mail(env):
    svc, io, _ = env
    r = first(svc.unsub_act("trash", ["deals@wayfair.example"]))
    assert r["ok"] and "moved 88/88" in r["note"]
    assert io.calls[-1] == ("move_messages", 88, "Trash")


def test_unknown_action_and_empty_batch_rejected(env):
    svc, _, _ = env
    with pytest.raises(ApiError) as e:
        svc.unsub_act("delete-everything", ["a@b.c"])
    assert e.value.status == 400
    with pytest.raises(ApiError):
        svc.unsub_act("approve", [])


# ---------------------------------------------------------------- second look: failed + still sending
def _repeat(svc):
    return {r["sender_email"]: r for r in svc.unsub_list()["repeat"]}


def test_rejected_post_opens_the_page_and_waits_for_confirmation(env):
    svc, io, _ = env
    email = "promo@flakyshop.example"                       # demo IO: its first POST is rejected (500)
    r = first(svc.unsub_act("approve", [email]))
    assert r["state"] == "opened" and "returned 500" in r["note"] and r["ok"] is True
    assert ("open_url", "https://flakyshop.example/unsubscribe?u=demo") in io.calls
    d = svc.unsub_list()
    assert email in {a["sender_email"] for a in d["awaiting"]}
    assert email not in {f["sender_email"] for f in d["failed"]}            # not a failure: it's waiting on you
    with svc.store() as s:
        row = s.unsub_row(email)
        assert row["status"] == "approved" and row["opened_at"]             # NOT done until you say so


def test_confirming_an_opened_page_marks_it_done(env):
    svc, _, _ = env
    email = "promo@flakyshop.example"
    svc.unsub_act("approve", [email])
    assert first(svc.unsub_act("markdone", [email]))["ok"]
    with svc.store() as s:
        row = s.unsub_row(email)
        assert row["status"] == "done" and row["opened_at"] is None and "confirmed" in row["last_note"]
    assert email not in {a["sender_email"] for a in svc.unsub_list()["awaiting"]}


def test_retry_after_a_rejected_post_can_succeed(env):
    svc, _, _ = env
    email = "promo@flakyshop.example"
    svc.unsub_act("approve", [email])                        # POST rejected, page opened
    r = first(svc.unsub_act("retry", [email]))               # demo IO accepts the POST this time
    assert r["state"] == "done"
    with svc.store() as s:
        assert s.unsub_row(email)["status"] == "done"


def test_browser_only_sender_is_never_marked_done_on_open(env):
    svc, io, _ = env
    email = "no-reply@instagram.example"                     # no one-click support
    r = first(svc.unsub_act("approve", [email]))
    assert r["state"] == "opened"
    with svc.store() as s:
        assert s.unsub_row(email)["status"] == "approved"


def test_a_successful_post_never_opens_a_page(env):
    svc, io, _ = env
    first(svc.unsub_act("approve", ["deals@wayfair.example"]))
    assert not [c for c in io.calls if c[0] == "open_url"]


def test_one_action_opens_at_most_five_pages_and_leaves_the_rest_queued(env):
    from mailsweep.ui.service import MAX_TABS
    svc, io, _ = env
    emails = [i["sender_email"] for i in svc.unsub_list()["items"]]
    res = svc.unsub_act("approve", emails)["results"]
    opened = [r for r in res if r.get("state") == "opened"]
    assert len(opened) == MAX_TABS == len([c for c in io.calls if c[0] == "open_url"])
    held = [r for r in res if r.get("state") == "not_opened"]
    assert held, "the demo has more browser-only senders than the cap"
    with svc.store() as s:
        assert all(s.unsub_row(r["id"])["status"] == "suggested" for r in held)     # untouched


def test_post_rejected_over_the_cap_is_recorded_failed_not_lost(env):
    svc, io, _ = env
    io.fail_once = {"promo@flakyshop.example"}
    emails = ["no-reply@instagram.example", "hello@duolingo.example", "recs@etsy.example",
              "offers@airbnb.example", "email@homedepot.example", "promo@flakyshop.example"]
    res = {r["id"]: r for r in svc.unsub_act("approve", emails)["results"]}
    assert res["promo@flakyshop.example"]["state"] == "failed"
    assert "tab limit" in res["promo@flakyshop.example"]["note"]
    assert "promo@flakyshop.example" in {f["sender_email"] for f in svc.unsub_list()["failed"]}


def test_opening_a_failed_rows_page_moves_it_to_awaiting(env):
    svc, io, _ = env
    assert first(svc.unsub_act("open", ["promo@chewy.example"]))["ok"]
    d = svc.unsub_list()
    assert "promo@chewy.example" in {a["sender_email"] for a in d["awaiting"]}
    assert "promo@chewy.example" not in {f["sender_email"] for f in d["failed"]}


def test_opening_a_repeat_senders_page_changes_nothing(env):
    svc, io, _ = env
    first(svc.unsub_act("open", ["deals@grubhub.example"]))
    with svc.store() as s:
        assert s.unsub_row("deals@grubhub.example")["status"] == "done"


# ---- unsub.attempt(): the real POST-then-open logic, with the network and `open` stubbed out
class _Resp:
    def __init__(self, code): self.status_code = code


def _attempt_env(monkeypatch, post):
    from mailsweep import unsub
    opened = []
    monkeypatch.setattr(unsub.requests, "post", post)
    monkeypatch.setattr(unsub, "_open", opened.append)
    return unsub, opened


def _cfg(mode="one_click"):
    from mailsweep.config import UnsubCfg
    return UnsubCfg(mode=mode)


T_HTTP = json.dumps(["https://news.example/u?id=1", "mailto:u@news.example"])


def test_attempt_post_accepted_opens_nothing(monkeypatch):
    unsub, opened = _attempt_env(monkeypatch, lambda *a, **k: _Resp(200))
    assert unsub.attempt(T_HTTP, True, _cfg()) == ("done", "one-click POST accepted (200)")
    assert opened == []


@pytest.mark.parametrize("code", [400, 404, 500, 503])
def test_attempt_rejected_post_falls_back_to_opening_the_page(monkeypatch, code):
    unsub, opened = _attempt_env(monkeypatch, lambda *a, **k: _Resp(code))
    state, note = unsub.attempt(T_HTTP, True, _cfg())
    assert state == "opened" and f"returned {code}" in note and "news.example" in note
    assert opened == ["https://news.example/u?id=1"]


def test_attempt_post_exception_falls_back_too(monkeypatch):
    def boom(*a, **k): raise unsub_mod.requests.ConnectionError("down")
    from mailsweep import unsub as unsub_mod
    unsub, opened = _attempt_env(monkeypatch, boom)
    state, note = unsub.attempt(T_HTTP, True, _cfg())
    assert state == "opened" and "ConnectionError" in note and opened


def test_attempt_without_one_click_just_opens_and_never_posts(monkeypatch):
    def no_post(*a, **k): raise AssertionError("must not POST without List-Unsubscribe-Post")
    unsub, opened = _attempt_env(monkeypatch, no_post)
    state, _ = unsub.attempt(T_HTTP, False, _cfg())
    assert state == "opened" and opened == ["https://news.example/u?id=1"]


def test_attempt_mailto_only_opens_compose(monkeypatch):
    unsub, opened = _attempt_env(monkeypatch, lambda *a, **k: _Resp(200))
    state, note = unsub.attempt(json.dumps(["mailto:u@news.example?subject=unsubscribe"]), False, _cfg())
    assert state == "opened" and "u@news.example" in note and opened == ["mailto:u@news.example?subject=unsubscribe"]


def test_attempt_honours_the_tab_limit(monkeypatch):
    unsub, opened = _attempt_env(monkeypatch, lambda *a, **k: _Resp(500))
    state, note = unsub.attempt(T_HTTP, True, _cfg(), allow_open=False)
    assert state == "failed" and "tab limit" in note and opened == []
    state, _ = unsub.attempt(T_HTTP, False, _cfg(), allow_open=False)
    assert state == "not_opened" and opened == []


def test_attempt_refuses_non_web_schemes_and_respects_mode_never(monkeypatch):
    unsub, opened = _attempt_env(monkeypatch, lambda *a, **k: _Resp(200))
    bad = json.dumps(["javascript:alert(1)", "httpx://evil.example/", "file:///etc/passwd"])
    assert unsub.attempt(bad, True, _cfg())[0] == "failed" and opened == []
    assert unsub.attempt(T_HTTP, True, _cfg("never"))[0] == "failed" and opened == []


def test_legacy_approved_rows_show_as_failed_without_a_note(env):
    svc, _, _ = env
    chewy = next(f for f in svc.unsub_list()["failed"] if f["sender_email"] == "promo@chewy.example")
    assert chewy["method"] == "one_click" and chewy["can_open"]


def test_retry_only_from_approved_or_done(env):
    svc, io, _ = env
    r = first(svc.unsub_act("retry", ["deals@wayfair.example"]))          # still 'suggested'
    assert r["ok"] is False and "already suggested" in r["note"]
    assert not [c for c in io.calls if c[0] == "unsub"]


def test_still_sending_counts_only_mail_after_the_unsubscribe(env):
    svc, _, _ = env
    rep = _repeat(svc)
    assert rep["deals@grubhub.example"]["n"] == 4            # 2 older messages don't count
    assert rep["deals@grubhub.example"]["within_grace"] is False
    assert rep["digest@care.example"]["within_grace"] is True
    assert "sale@wayfair.example" not in rep                  # unsubscribed, nothing since


def test_only_noise_categories_count_as_still_sending(env):
    svc, _, _ = env
    with svc.store() as s:
        from mailsweep.models import Classification, Message
        m = Message(message_id="<receipt@demo>", account="Personal", mailbox="INBOX",
                    sender="Sale <sale@wayfair.example>", sender_email="sale@wayfair.example",
                    subject="Your receipt", date_received=datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    snippet="")
        s.record_message(m, Classification("transactional", "normal", "demo"))
    assert "sale@wayfair.example" not in _repeat(svc)


def test_ignore_hides_it_until_it_sends_more(env):
    svc, _, _ = env
    assert first(svc.unsub_act("ack", ["deals@grubhub.example"]))["ok"]
    assert "deals@grubhub.example" not in _repeat(svc)
    with svc.store() as s:
        from mailsweep.models import Classification, Message
        later = (datetime.now() + timedelta(hours=1)).astimezone(timezone.utc)
        m = Message(message_id="<more@demo>", account="Personal", mailbox="INBOX",
                    sender="Grubhub <deals@grubhub.example>", sender_email="deals@grubhub.example",
                    subject="Still here", date_received=later.strftime("%Y-%m-%dT%H:%M:%S.000Z"), snippet="")
        s.record_message(m, Classification("marketing", "low", "demo"))
    assert _repeat(svc)["deals@grubhub.example"]["n"] == 1     # only the new one counts now


def test_domain_flag_on_a_different_address_never_the_same_one(env):
    svc, _, _ = env
    items = {i["sender_email"]: i for i in svc.unsub_list()["items"]}
    assert items["deals@wayfair.example"]["prior"]["sender_email"] == "sale@wayfair.example"
    assert "prior" not in items["no-reply@instagram.example"]


def test_webmail_domains_never_trigger_the_domain_flag(env):
    svc, _, _ = env
    from mailsweep.models import Message
    with svc.store() as s:
        for who, status in (("old@gmail.com", "done"), ("new@gmail.com", "suggested")):
            m = Message(message_id=f"<{who}>", account="P", mailbox="INBOX", sender=who, sender_email=who,
                        subject="x", date_received="2026-09-01T00:00:00.000Z", snippet="")
            s.upsert_unsub(m, ["https://gmail.com/u"])
            s.set_unsub_status(who, status)
    assert "prior" not in {i["sender_email"]: i for i in svc.unsub_list()["items"]}["new@gmail.com"]


def test_open_action_uses_only_http_or_mailto_links(env):
    svc, io, _ = env
    assert first(svc.unsub_act("open", ["promo@chewy.example"]))["ok"]
    assert io.calls[-1] == ("open_url", "https://chewy.example/unsubscribe?u=demo")
    with svc.store() as s:      # a hostile header: only a javascript: target
        s.conn.execute("UPDATE unsub_queue SET targets=? WHERE sender_email=?",
                       (json.dumps(["javascript:alert(1)", "file:///etc/passwd"]), "promo@chewy.example"))
    n = len(io.calls)
    r = first(svc.unsub_act("open", ["promo@chewy.example"]))
    assert r["ok"] is False and len(io.calls) == n


def test_markdone_and_giveup_from_the_failed_list(env):
    svc, _, _ = env
    assert first(svc.unsub_act("markdone", ["promo@chewy.example"]))["ok"]
    with svc.store() as s:
        assert s.unsub_row("promo@chewy.example")["status"] == "done"
    assert first(svc.unsub_act("markdone", ["deals@wayfair.example"]))["ok"] is False   # not a failed row


def test_summary_todo_badge_adds_it_all_up(env):
    svc, _, _ = env
    s = svc.summary()
    assert s["unsub_todo"] == s["unsub"] + s["unsub_failed"] + s["unsub_awaiting"] + s["unsub_repeat"] == 19 + 2 + 1 + 2


def test_store_migrates_an_old_unsub_queue_in_place(tmp_path):
    import sqlite3
    from mailsweep.store import Store
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE unsub_queue (sender_email TEXT PRIMARY KEY, sender TEXT, account TEXT,
                   msg_count INTEGER DEFAULT 0, last_subject TEXT, targets TEXT, one_click INTEGER DEFAULT 0,
                   status TEXT DEFAULT 'suggested', updated_at TEXT, last_to TEXT, last_received TEXT)""")
    con.execute("INSERT INTO unsub_queue (sender_email, status, updated_at) VALUES ('a@b.c','approved','2026-09-01T10:00:00')")
    con.commit(); con.close()
    s = Store(db)
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(unsub_queue)")}
    assert {"last_note", "attempts", "acked_at", "opened_at"} <= cols
    assert s.unsub_row("a@b.c")["status"] == "approved"          # existing data untouched
    s.close()


# ---------------------------------------------------------------- events
def _event(svc, title):
    return next(e for e in svc.events_list()["items"] if e["title"] == title)


def test_event_add_creates_and_marks_created(env):
    svc, io, _ = env
    ev = _event(svc, "Dentist cleaning")
    assert first(svc.events_act("add", ev["id"]))["ok"]
    assert ("create_event", "Dentist cleaning") in io.calls
    assert first(svc.events_act("add", ev["id"]))["ok"] is False     # not added twice
    assert sum(1 for c in io.calls if c[0] == "create_event") == 1


def test_event_add_failure_leaves_it_pending(env):
    svc, io, _ = env
    def boom(*a): raise bridge.BridgeError("Calendar isn't running")
    io.create_event = boom
    ev = _event(svc, "Dentist cleaning")
    r = first(svc.events_act("add", ev["id"]))
    assert r["ok"] is False and "Calendar" in r["note"]
    assert any(e["id"] == ev["id"] for e in svc.events_list()["items"])


def test_event_protect_dismisses_and_blocks_sender(env):
    svc, _, _ = env
    ev = _event(svc, "Dentist cleaning")
    assert first(svc.events_act("protect", ev["id"]))["ok"]
    assert all(e["title"] != "Dentist cleaning" for e in svc.events_list()["items"])
    with svc.store() as s:
        assert s.is_event_sender_protected("frontdesk@alvarezdental.example")


def test_event_id_must_be_int(env):
    svc, _, _ = env
    with pytest.raises(ApiError):
        svc.events_act("add", "1; DROP TABLE events")


# ---------------------------------------------------------------- spam
def test_spam_rescue_and_trust(env):
    svc, io, _ = env
    rows = {r["sender_email"]: r for r in svc.spam_list()["items"]}
    dmv = rows["noreply@dmv.vermont.example"]
    assert first(svc.spam_act("trust", dmv["message_id"]))["ok"]
    assert io.calls[-1][0] == "move_message"
    with svc.store() as s:
        assert s.is_spam_sender_trusted("noreply@dmv.vermont.example")
    assert svc.spam_list()["total"] == 2


def test_spam_rescue_failure_stays_suggested(env):
    svc, io, _ = env
    def boom(*a): raise bridge.BridgeError("Can't get object")
    io.move_message = boom
    msg = svc.spam_list()["items"][0]
    r = first(svc.spam_act("rescue", msg["message_id"]))
    assert r["ok"] is False and "moved or was removed" in r["note"]
    assert svc.spam_list()["total"] == 3


# ---------------------------------------------------------------- leaderboard / reviews
def test_leaderboard_move_and_optional_unsub(env):
    svc, io, _ = env
    email = "no-reply@instagram.example"
    res = svc.leaderboard_act("junk", email, and_unsub=True)["results"]
    assert res[0]["ok"] and ("move_messages", 52, "Junk") in io.calls
    assert res[1]["ok"] and ("unsub", email) in io.calls


def test_review_approve_keeps_edited_text_and_exports(env):
    svc, _, cfg = env
    key = svc.reviews_list()["items"][0]["key"]
    svc.reviews_act("approve", key, "  My edited review.  ")
    d = svc.reviews_list("approved")
    assert d["items"][0]["review_text"] == "My edited review."
    out = first(svc.reviews_export())
    assert out["ok"]
    csv_path = next(cfg.digest_dir.glob("purchase_reviews_*.csv"))
    assert "My edited review." in csv_path.read_text()


# ---------------------------------------------------------------- HTTP hardening
@pytest.fixture
def http_env(env):
    svc, io, _ = env
    httpd = make_server(svc, 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd, httpd.server_address[1], svc, io
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), data


def test_binds_loopback_only(http_env):
    httpd, *_ = http_env
    assert httpd.server_address[0] == "127.0.0.1"


def test_index_gets_token_and_strict_headers(http_env):
    httpd, port, *_ = http_env
    status, headers, body = call(port, "GET", "/")
    assert status == 200
    assert httpd.token.encode() in body and b"__TOKEN__" not in body
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "'unsafe-inline'" not in csp
    assert headers["Cache-Control"] == "no-store"


def test_wrong_host_header_refused(http_env):
    _, port, *_ = http_env
    status, *_ = call(port, "GET", "/", headers={"Host": f"evil.example:{port}"})
    assert status == 403
    status, *_ = call(port, "GET", "/", headers={"Host": "127.0.0.1"})   # wrong port
    assert status == 403


def test_api_requires_token(http_env):
    httpd, port, *_ = http_env
    assert call(port, "GET", "/api/summary")[0] == 403
    assert call(port, "GET", "/api/summary", headers={"X-MailSweep-Token": "nope"})[0] == 403
    assert call(port, "GET", "/api/summary", headers={"X-MailSweep-Token": httpd.token})[0] == 200


def test_post_needs_json_and_own_origin(http_env):
    httpd, port, svc, io = http_env
    tok = {"X-MailSweep-Token": httpd.token}
    body = json.dumps({"action": "approve", "emails": ["deals@wayfair.example"]})
    assert call(port, "POST", "/api/unsub", body, {**tok, "Content-Type": "text/plain"})[0] == 415
    assert call(port, "POST", "/api/unsub", body,
                {**tok, "Content-Type": "application/json", "Origin": "http://evil.example"})[0] == 403
    assert call(port, "POST", "/api/unsub", "x" * 70000, {**tok, "Content-Type": "application/json"})[0] == 413
    assert not [c for c in io.calls if c[0] == "unsub"]      # none of the refused requests acted


def test_post_happy_path_and_bad_input(http_env):
    httpd, port, svc, io = http_env
    h = {"X-MailSweep-Token": httpd.token, "Content-Type": "application/json",
         "Origin": f"http://127.0.0.1:{port}"}
    status, _, data = call(port, "POST", "/api/unsub",
                           json.dumps({"action": "skip", "emails": ["deals@wayfair.example"]}), h)
    assert status == 200 and json.loads(data)["results"][0]["ok"]
    status, _, data = call(port, "POST", "/api/unsub", json.dumps({"action": "nope", "emails": ["a@b.c"]}), h)
    assert status == 400 and "unknown action" in json.loads(data)["error"]
    assert call(port, "POST", "/api/unsub", "[1,2]", h)[0] == 400        # body must be an object
    assert call(port, "GET", "/api/events?past=abc", headers={"X-MailSweep-Token": httpd.token})[0] == 400


def test_static_serves_only_whitelisted_files(http_env):
    _, port, *_ = http_env
    assert call(port, "GET", "/app.js")[0] == 200
    assert call(port, "GET", "/app.css")[0] == 200
    for p in ("/../store.py", "/%2e%2e/store.py", "/index.py", "/static/app.js"):
        assert call(port, "GET", p)[0] == 404


def test_demo_temp_dir_is_removed_when_the_port_is_taken(env):
    import socket
    from mailsweep.ui.server import serve
    svc, _, _ = env
    tmp = Path(demo.build()[3])
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        assert serve(svc, busy.getsockname()[1], open_browser=False, cleanup=tmp) == 1
    assert not tmp.exists()


# ---------------------------------------------------------------- shared links / spoofed From lines
SPOOF = "no-reply@airline.example"          # demo: carries the protected recreation sender's exact link
PROTECTED = "nccary@recreation.example"


def _failed(svc, email):
    return next(f for f in svc.unsub_list()["failed"] if f["sender_email"] == email)


def test_link_shared_with_a_protected_sender_is_flagged_and_blocked(env):
    svc, _, _ = env
    row = _failed(svc, SPOOF)
    assert row["blocked_by"] == PROTECTED and row["link_conflict"] is True
    assert row["peers"] == [{"sender_email": PROTECTED, "status": "protected", "same_org": False, "protected": True}]


@pytest.mark.parametrize("action", ["retry", "open"])
def test_a_blocked_row_is_refused_and_nothing_is_sent_or_opened(env, action):
    svc, io, _ = env
    r = first(svc.unsub_act(action, [SPOOF]))
    assert r["ok"] is False and r["state"] == "blocked" and PROTECTED in r["note"]
    assert not [c for c in io.calls if c[0] in ("unsub", "open_url")]
    with svc.store() as s:
        assert s.unsub_row(SPOOF)["status"] == "approved"        # untouched


def test_bulk_approve_skips_only_the_blocked_sender(env):
    svc, io, _ = env
    res = {r["id"]: r for r in svc.unsub_act("retry", [SPOOF, "promo@chewy.example"])["results"]}
    assert res[SPOOF]["state"] == "blocked"
    assert res["promo@chewy.example"]["state"] in ("done", "opened")


def test_a_link_shared_by_two_addresses_of_one_organisation_is_not_blocked(env):
    svc, io, _ = env
    from mailsweep.models import Message
    url = "https://mail.news.example/u?id=ABC"
    with svc.store() as s:
        for who in ("editor@magazines.news.example", "email@news.example"):
            m = Message(message_id=f"<{who}>", account="P", mailbox="INBOX", sender=who, sender_email=who,
                        subject="x", date_received="2026-09-01T00:00:00.000Z", snippet="")
            s.upsert_unsub(m, [url])
    items = {i["sender_email"]: i for i in svc.unsub_list()["items"]}
    a = items["editor@magazines.news.example"]
    assert a["link_conflict"] is False and a["blocked_by"] is None      # same organisation: fine
    assert first(svc.unsub_act("approve", ["editor@magazines.news.example"]))["state"] in ("done", "opened")


def test_same_host_but_a_different_link_is_not_treated_as_shared(env):
    svc, _, _ = env
    from mailsweep.models import Message
    with svc.store() as s:
        m = Message(message_id="<x>", account="P", mailbox="INBOX", sender="a@other.example",
                    sender_email="a@other.example", subject="x", date_received="2026-09-01T00:00:00.000Z", snippet="")
        s.upsert_unsub(m, ["https://recreation.example/webtrac/unsubscribe.html?email=SOMEONEELSE&Action=AutoProcess"])
    item = next(i for i in svc.unsub_list()["items"] if i["sender_email"] == "a@other.example")
    assert item["peers"] == [] and item["blocked_by"] is None


def test_config_protected_pattern_also_blocks_a_shared_link(env):
    svc, _, cfg = env
    with svc.store() as s:
        s.set_unsub_status(PROTECTED, "done")                   # no longer marked protected in the DB...
    cfg.unsubscribe.protected = ["recreation.example"]          # ...but still on your config list
    assert _failed(svc, SPOOF)["blocked_by"] == PROTECTED


def test_tokens_never_reach_the_page(env):
    svc, _, _ = env
    blob = json.dumps(svc.unsub_list())
    assert "DEMOTOKEN123" not in blob and "AutoProcess" not in blob
    assert "https://recreation.example/…" in _failed(svc, SPOOF)["last_note"]
    r = first(svc.unsub_act("open", ["promo@chewy.example"]))
    assert "?u=demo" not in json.dumps(r)


# ---------------------------------------------------------------- sender authentication
def test_unverified_senders_are_tagged_and_forged_ones_never_reach_the_queue(env):
    svc, _, _ = env
    items = {i["sender_email"]: i for i in svc.unsub_list()["items"]}
    assert items["recs@etsy.example"]["auth"] == "unverified"
    assert "sendgrid.net, not etsy.example" in items["recs@etsy.example"]["auth_detail"]
    assert items["deals@wayfair.example"]["auth"] == "verified"
    assert "security@chase-verify.example" not in items                     # dmarc=fail: refused at the door


def test_forged_messages_are_recorded_and_reported_on_the_digest(env):
    svc, _, _ = env
    d = svc.digest()
    assert d["forged"]["total"] == 1 and d["forged"]["top"][0]["sender"].startswith("Chase Alerts")
    assert svc.summary()["forged"] == 1
