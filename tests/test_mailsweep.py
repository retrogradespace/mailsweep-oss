"""Unit tests for the pure-Python parts of MailSweep."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mailsweep.bridge import parse_headers
from mailsweep.classify import (clean_html, parse_llm_response, parse_purchase_item_response,
                                parse_review_draft_response, parse_spam_audit_response,
                                _extract_json, build_feedback_block)
from mailsweep.cli import _explain_move_error, events_match
from mailsweep.config import Config
from mailsweep.digest import render_html
from mailsweep.models import Classification, EventCandidate, Message
from mailsweep.timeparse import normalize_end, normalize_start, parse_user_datetime
from mailsweep.rules import (classify_by_rules, extract_order_ref, flag_spam_audit, kind_rank,
                             maybe_event, purchase_kind, spoofed_sender, unsubscribe_targets,
                             vendor_from_sender)
from mailsweep.store import Store


def make_msg(**kw) -> Message:
    base = dict(message_id="<abc@x>", account="Personal", mailbox="INBOX",
                sender="Shop <deals@shop.com>", sender_email="deals@shop.com",
                subject="Hello", date_received="2026-07-18T09:00:00Z",
                snippet="", headers={})
    base.update(kw)
    return Message(**base)


# ---------------------------------------------------------------- header parsing
def test_parse_headers_folded_and_case():
    block = ("From: Shop <deals@shop.com>\r\n"
             "List-Unsubscribe: <mailto:u@shop.com>,\r\n"
             " <https://shop.com/unsub?id=1>\r\n"
             "LIST-UNSUBSCRIBE-POST: List-Unsubscribe=One-Click\r\n"
             "X-Ignored: whatever\n")
    h = parse_headers(block)
    assert "mailto:u@shop.com" in h["list-unsubscribe"]
    assert "https://shop.com/unsub?id=1" in h["list-unsubscribe"]
    assert "one-click" in h["list-unsubscribe-post"].lower()
    assert "x-ignored" not in h


def test_unsubscribe_targets():
    msg = make_msg(headers={"list-unsubscribe":
                            "<mailto:u@shop.com>, <https://shop.com/u?id=1>"})
    t = unsubscribe_targets(msg)
    assert t == ["mailto:u@shop.com", "https://shop.com/u?id=1"]
    assert unsubscribe_targets(make_msg()) == []


def test_one_click_property():
    msg = make_msg(headers={"list-unsubscribe-post": "List-Unsubscribe=One-Click"})
    assert msg.one_click
    assert not make_msg().one_click


# ---------------------------------------------------------------- rules
def test_rules_marketing_vs_newsletter():
    marketing = make_msg(subject="48h FLASH SALE - 30% off everything",
                         headers={"list-unsubscribe": "<https://x>"})
    assert classify_by_rules(marketing).category == "marketing"
    newsletter = make_msg(subject="Weekly digest",
                          headers={"list-id": "<news.shop.com>"})
    assert classify_by_rules(newsletter).category == "newsletter"


def test_rules_transactional_and_notification():
    assert classify_by_rules(make_msg(subject="Your order #123 has shipped")
                             ).category == "transactional"
    assert classify_by_rules(make_msg(sender="no-reply@github.com",
                                      sender_email="no-reply@github.com")
                             ).category == "notification"


def test_rules_default_personal():
    v = classify_by_rules(make_msg(subject="lunch?",
                                   sender="Amy <amy@x.com>", sender_email="amy@x.com"))
    assert v.category == "personal" and v.importance == "normal"


def test_maybe_event():
    assert maybe_event(make_msg(subject="Invitation: gallery opening RSVP"))
    assert not maybe_event(make_msg(subject="Your receipt"))


# ---------------------------------------------------------------- LLM parsing
def test_extract_json_fenced_and_noisy():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Sure! {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    assert _extract_json("no json here") is None


def test_parse_llm_response_full():
    text = ('{"category":"personal","importance":"high","reason":"invite",'
            '"event":{"title":"Studio visit","start":"2026-07-25T14:00",'
            '"end":"","location":"SLC","all_day":false,"confidence":0.9}}')
    v = parse_llm_response(text, make_msg())
    assert v.category == "personal" and v.importance == "high"
    assert v.event.title == "Studio visit"
    assert v.event.confidence == 0.9
    assert v.used_llm


def test_parse_llm_response_bad_values_clamped():
    text = ('{"category":"spam???","importance":"URGENT","reason":"x",'
            '"event":{"title":"T","start":"2026-08-01","confidence":"9"}}')
    v = parse_llm_response(text, make_msg())
    assert v.category == "personal"      # unknown -> safe default
    assert v.importance == "normal"
    assert v.event.confidence == 1.0     # clamped


def test_parse_llm_response_event_needs_title_and_start():
    text = '{"category":"work","importance":"normal","reason":"x","event":{"title":"T"}}'
    assert parse_llm_response(text, make_msg()).event is None


# ---------------------------------------------------------------- event matching
def test_events_match():
    assert events_match("Gallery opening reception", "2026-07-25T18:00",
                        "Opening reception — gallery", "2026-07-25T18:30:00Z")
    assert not events_match("Gallery opening", "2026-07-25",
                            "Dentist", "2026-07-25T10:00")
    assert not events_match("Gallery opening", "2026-07-25",
                            "Gallery opening", "2026-07-26T10:00")
    assert not events_match("X", "", "X", "2026-07-25")


# ---------------------------------------------------------------- event date/time handling
def test_normalize_start_formats():
    assert normalize_start("2026-09-20T19:00") == ("2026-09-20T19:00", False)
    assert normalize_start("2026-09-22") == ("2026-09-22", True)
    # midnight means "no time given", with or without a timezone suffix
    assert normalize_start("2026-09-22T00:00") == ("2026-09-22", True)
    assert normalize_start("2027-10-01T00:00:00+00:00") == ("2027-10-01", True)
    assert normalize_start("2026-08-31T") == ("2026-08-31", True)
    # the model's habitual UTC suffix is dropped, not converted: the wall clock is what was written
    assert normalize_start("2026-08-05T11:30:00.000Z") == ("2026-08-05T11:30", False)
    assert normalize_start("2026-08-04T10:15:00+00:00") == ("2026-08-04T10:15", False)
    # a real zone is converted to local time (expected value computed so this holds on any machine)
    from datetime import timedelta, timezone
    want = datetime(2026, 8, 11, 10, 0, tzinfo=timezone(timedelta(hours=-7))).astimezone()
    assert normalize_start("2026-08-11T10:00:00-07:00") == (want.strftime("%Y-%m-%dT%H:%M"), False)
    assert normalize_start("2026-08-11T00:00:00-07:00") == ("2026-08-11", True)   # midnight stays date-only
    # formats Calendar's parser rejects outright
    assert normalize_start("2026-08-04T12:00pm") == ("2026-08-04T12:00", False)
    assert normalize_start("2026-08-01T11:45A") == ("2026-08-01T11:45", False)
    assert normalize_start("2026-09-16T0800") == ("2026-09-16T08:00", False)
    assert normalize_start("2026-09-16T8pm") == ("2026-09-16T20:00", False)
    # no usable date
    assert normalize_start("2026-02-30T10:00") is None
    assert normalize_start("next Thursday") is None
    assert normalize_start("") is None


def test_normalize_end():
    assert normalize_end("2026-09-20T19:00", "2026-09-20T21:00:00Z") == "2026-09-20T21:00"
    assert normalize_end("2026-09-20T19:00", "2026-09-20T18:00") == ""      # not after start
    assert normalize_end("2026-09-20T19:00", "2026-09-20") == ""            # timed start, no end time
    assert normalize_end("2026-09-20", "2026-09-22") == "2026-09-22"        # multi-day all-day
    assert normalize_end("2026-09-20", "2026-09-20") == ""
    assert normalize_end("2026-09-20T19:00", "") == ""


def test_parse_user_datetime():
    from datetime import date
    today = date(2026, 9, 19)
    assert parse_user_datetime("2026-09-20", today) == ("2026-09-20", True)
    assert parse_user_datetime("2026-09-20 14:30", today) == ("2026-09-20T14:30", False)
    assert parse_user_datetime("9/20 2:30pm", today) == ("2026-09-20T14:30", False)
    assert parse_user_datetime("9/20/27", today) == ("2027-09-20", True)
    assert parse_user_datetime("sometime soon", today) is None


def test_parse_llm_response_normalizes_event_times():
    def parse(start, end="", all_day=False):
        text = ('{"category":"personal","importance":"normal","reason":"x","event":{"title":"T",'
                f'"start":"{start}","end":"{end}","all_day":{str(all_day).lower()},"confidence":0.9}}}}')
        return parse_llm_response(text, make_msg()).event

    ev = parse("2026-09-22T00:00:00+00:00")           # UTC midnight would land on the 21st in Calendar
    assert ev.start == "2026-09-22" and ev.all_day
    ev = parse("2026-08-05T11:30:00.000Z", "2026-08-05T12:30:00.000Z")
    assert (ev.start, ev.end, ev.all_day) == ("2026-08-05T11:30", "2026-08-05T12:30", False)
    assert parse("2026-13-45T10:00") is None           # no real date -> no candidate to review


def test_flag_event_time_keeps_original_and_feeds_prompt(tmp_path):
    store = Store(tmp_path / "t.db")
    msg = make_msg()
    store.record_message(msg, Classification("personal", "normal", "t"))
    store.add_event(EventCandidate("Gala", "2026-09-20T19:00", "", "", 0.9,
                                   msg.message_id, "Gala invite", msg.sender))
    ev_id = store.events_by_status("new")[0]["id"]

    assert store.flag_event_time(ev_id, "2026-09-20T14:30", "2026-09-20T16:00", False)
    row = store.event_by_id(ev_id)
    assert (row["start"], row["end"], row["time_flagged"]) == ("2026-09-20T14:30", "2026-09-20T16:00", 1)
    assert row["original_start"] == "2026-09-20T19:00"

    # a second fix keeps the *first* extraction as the "wrong" value
    assert store.flag_event_time(ev_id, "2026-09-20", "", True)
    assert store.event_by_id(ev_id)["original_start"] == "2026-09-20T19:00"

    block = build_feedback_block(store)
    assert "extracted 2026-09-20T19:00, correct was 2026-09-20" in block

    # correcting onto a time already saved for the same email+title is refused, not crashed
    store.add_event(EventCandidate("Gala", "2026-09-21T10:00", "", "", 0.9,
                                   msg.message_id, "Gala invite", msg.sender))
    other = [r for r in store.events_by_status("new") if r["id"] != ev_id][0]
    assert not store.flag_event_time(other["id"], "2026-09-20", "", True)
    assert store.event_by_id(other["id"])["start"] == "2026-09-21T10:00"
    store.close()


def test_store_repairs_pending_event_times(tmp_path):
    db = tmp_path / "t.db"
    store = Store(db)
    cols = ("source_message_id, source_subject, source_sender, source_sender_email, title, "
            "start, end, location, confidence, all_day, status")
    rows = [
        ("<a>", "s", "S <s@x.com>", "s@x.com", "Midnight UTC", "2027-10-01T00:00:00+00:00", "", "", 0.9, 0, "new"),
        ("<b>", "s", "S <s@x.com>", "s@x.com", "Odd format", "2026-08-04T12:00pm", "", "", 0.9, 0, "new"),
        ("<c>", "s", "S <s@x.com>", "s@x.com", "Dup", "2026-09-10T12:00:00", "", "", 0.9, 0, "new"),
        ("<c>", "s", "S <s@x.com>", "s@x.com", "Dup", "2026-09-10T12:00", "", "", 0.9, 0, "new"),
        ("<d>", "s", "S <s@x.com>", "s@x.com", "Already created", "2026-09-01T00:00", "", "", 0.9, 0, "created"),
    ]
    for r in rows:
        store.conn.execute(f"INSERT INTO events ({cols}, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'x')", r)
    store.commit()
    store.close()

    store = Store(db)                                   # opening the store runs the repair
    by_title = {}
    for r in store.conn.execute("SELECT * FROM events ORDER BY id"):
        by_title.setdefault(r["title"], []).append(r)
    assert (by_title["Midnight UTC"][0]["start"], by_title["Midnight UTC"][0]["all_day"]) == ("2027-10-01", 1)
    assert by_title["Odd format"][0]["start"] == "2026-08-04T12:00"
    assert [r["status"] for r in by_title["Dup"]] == ["dismissed", "new"]      # duplicate dropped, not crashed
    assert by_title["Already created"][0]["start"] == "2026-09-01T00:00"      # history is left alone
    store.close()


# ---------------------------------------------------------------- store + digest
def test_store_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    msg = make_msg(headers={"list-unsubscribe": "<https://shop.com/u>"})
    store.record_message(msg, Classification("marketing", "low", "test"))
    store.upsert_unsub(msg, ["https://shop.com/u"])
    store.upsert_unsub(msg, ["https://shop.com/u"])
    # Digest hides already-passed event candidates, so this needs to stay in
    # the future relative to whenever the test actually runs.
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT19:00")
    ev = EventCandidate("Show", tomorrow, "", "SLC", 0.8,
                        msg.message_id, msg.subject, msg.sender)
    store.add_event(ev)
    store.add_event(ev)  # duplicate ignored
    store.commit()

    assert msg.message_id in store.known_message_ids()
    q = store.unsub_by_status("suggested")
    assert len(q) == 1 and q[0]["msg_count"] == 2
    assert len(store.events_by_status("new")) == 1

    store.set_unsub_status(msg.sender_email, "skipped")
    store.upsert_unsub(msg, ["https://shop.com/u"])   # must not resurrect
    assert store.unsub_by_status("suggested") == []

    html = render_html(store, {"scanned": 2, "accounts": 1, "llm": "test"})
    assert "Show" in html and "shop.com" not in html.lower().replace("deals@shop.com", "")
    assert "<script" not in html.lower()
    store.close()


def test_store_trash_refs_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    msg = make_msg(mail_id=555, headers={"to": "Jamie Test <you@example.com>"})
    store.record_message(msg, Classification("marketing", "low", "test"))
    ev = EventCandidate("Show", "2026-08-01T19:00", "", "SLC", 0.8,
                        msg.message_id, msg.subject, msg.sender)
    store.add_event(ev)
    store.commit()

    refs = store.messages_by_sender(msg.sender_email)
    assert len(refs) == 1 and refs[0]["mail_id"] == 555 and refs[0]["account"] == "Personal"

    ref = store.message_ref(msg.message_id)
    assert ref is not None and ref["mail_id"] == 555
    assert ref["to_addr"] == "Jamie Test <you@example.com>"

    assert store.message_ref("<no-such-id>") is None
    assert store.messages_by_sender("nobody@nowhere.com") == []
    store.close()


def test_noise_stats_reports_most_recent_subject_and_date(tmp_path):
    """last_subject/last_received should track the most recently received
    message, not the alphabetically-max subject."""
    store = Store(tmp_path / "t.db")
    older = make_msg(message_id="<1@x>", subject="Zzz early newsletter",
                     date_received="2026-07-01T09:00:00Z")
    newer = make_msg(message_id="<2@x>", subject="Aaa later newsletter",
                     date_received="2026-07-18T09:00:00Z")
    store.record_message(older, Classification("newsletter", "low", "test"))
    store.record_message(newer, Classification("newsletter", "low", "test"))
    store.commit()

    rows = store.noise_stats(days=30)
    assert len(rows) == 1
    assert rows[0]["n"] == 2
    assert rows[0]["last_subject"] == "Aaa later newsletter"
    assert rows[0]["last_received"] == "2026-07-18T09:00:00Z"
    store.close()


def test_classify_feedback_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    msg = make_msg(message_id="<fb1@x>")
    store.record_message(msg, Classification("marketing", "low", "test", used_llm=True))
    store.commit()

    pending = store.unreviewed_llm_messages()
    assert len(pending) == 1 and pending[0]["message_id"] == "<fb1@x>"

    # A confirmed-correct review carries no correction signal.
    store.record_classify_feedback("<fb1@x>", "Hello", "Shop <deals@shop.com>",
                                   "marketing", "low", "marketing", "low")
    store.commit()
    assert store.unreviewed_llm_messages() == []
    assert store.classify_corrections() == []

    # An actual correction shows up as a few-shot example.
    msg2 = make_msg(message_id="<fb2@x>")
    store.record_message(msg2, Classification("marketing", "low", "test", used_llm=True))
    store.record_classify_feedback("<fb2@x>", "Invoice due", "Shop <deals@shop.com>",
                                   "marketing", "low", "transactional", "high")
    store.commit()
    corrections = store.classify_corrections()
    assert len(corrections) == 1
    assert corrections[0]["correct_category"] == "transactional"
    assert corrections[0]["correct_importance"] == "high"
    store.close()


def test_unreviewed_llm_messages_filters_by_category(tmp_path):
    store = Store(tmp_path / "t.db")
    store.record_message(make_msg(message_id="<c1@x>", subject="Sale"),
                         Classification("marketing", "low", "t", used_llm=True))
    store.record_message(make_msg(message_id="<c2@x>", subject="Newsletter"),
                         Classification("newsletter", "low", "t", used_llm=True))
    store.commit()

    marketing_only = store.unreviewed_llm_messages(category="marketing")
    assert [r["message_id"] for r in marketing_only] == ["<c1@x>"]

    everything = store.unreviewed_llm_messages()
    assert {r["message_id"] for r in everything} == {"<c1@x>", "<c2@x>"}
    store.close()


def test_unreviewed_llm_messages_hides_already_actioned_by_default(tmp_path):
    store = Store(tmp_path / "t.db")

    # Sender already dealt with in unsub review -> hidden by default.
    unsub_msg = make_msg(message_id="<u1@x>", sender="Shop <deals@shop.com>",
                         sender_email="deals@shop.com")
    store.record_message(unsub_msg, Classification("marketing", "low", "t", used_llm=True))
    store.upsert_unsub(unsub_msg, ["https://shop.com/u"])
    store.set_unsub_status("deals@shop.com", "done")

    # Message whose extracted event was already resolved -> hidden by default.
    event_msg = make_msg(message_id="<e1@x>", sender="Venue <v@venue.com>",
                         sender_email="v@venue.com")
    store.record_message(event_msg, Classification("personal", "normal", "t", used_llm=True))
    ev = EventCandidate("Show", "2026-08-01T19:00", "", "", 0.8,
                        "<e1@x>", event_msg.subject, event_msg.sender)
    store.add_event(ev)
    store.set_event_status(store.events_by_status("new")[0]["id"], "dismissed")

    # Untouched message -> still shows up.
    plain_msg = make_msg(message_id="<p1@x>", sender="Amy <amy@x.com>", sender_email="amy@x.com")
    store.record_message(plain_msg, Classification("personal", "normal", "t", used_llm=True))
    store.commit()

    default = store.unreviewed_llm_messages()
    assert {r["message_id"] for r in default} == {"<p1@x>"}

    everything = store.unreviewed_llm_messages(include_actioned=True)
    assert {r["message_id"] for r in everything} == {"<u1@x>", "<e1@x>", "<p1@x>"}
    store.close()


def test_event_feedback_examples_buckets_by_status(tmp_path):
    store = Store(tmp_path / "t.db")
    dismissed = EventCandidate("Package delivery", "2026-08-01T09:00", "", "", 0.6,
                               "<e1@x>", "Your package is on its way", "UPS <n@ups.com>")
    confirmed = EventCandidate("Dentist", "2026-08-02T14:00", "", "Office", 0.9,
                               "<e2@x>", "Appointment reminder", "Dr. Smith <d@clinic.com>")
    store.add_event(dismissed)
    store.add_event(confirmed)
    store.set_event_status(store.events_by_status("new")[0]["id"], "dismissed")
    ids = [r["id"] for r in store.events_by_status("new")]
    store.set_event_status(ids[0], "created")
    store.commit()

    positive, negative = store.event_feedback_examples()
    assert len(positive) == 1 and positive[0]["title"] == "Dentist"
    assert len(negative) == 1 and negative[0]["source_subject"] == "Your package is on its way"
    store.close()


def test_feedback_block_empty_without_store():
    assert build_feedback_block(None) == ""


def test_feedback_block_renders_corrections_and_events(tmp_path):
    store = Store(tmp_path / "t.db")
    store.record_message(make_msg(message_id="<fb3@x>"),
                         Classification("marketing", "low", "test", used_llm=True))
    store.record_classify_feedback("<fb3@x>", "Invoice due", "Shop <deals@shop.com>",
                                   "marketing", "low", "transactional", "high")
    ev = EventCandidate("Package delivery", "2026-08-01T09:00", "", "", 0.6,
                        "<e3@x>", "Your package is on its way", "UPS <n@ups.com>")
    store.add_event(ev)
    store.set_event_status(store.events_by_status("new")[0]["id"], "dismissed")
    store.commit()

    block = build_feedback_block(store)
    assert "transactional" in block and "Invoice due" in block
    assert "NOT a real event" in block and "package is on its way" in block
    store.close()


def test_store_message_ref_without_mail_id_still_shows_display_fields(tmp_path):
    """A message scanned before mail_id tracking existed still shows account/to
    for display, it just can't be trashed (mail_id stays NULL)."""
    store = Store(tmp_path / "t.db")
    msg = make_msg(headers={"to": "you@example.com"})  # mail_id defaults to 0
    store.record_message(msg, Classification("marketing", "low", "test"))
    store.commit()

    ref = store.message_ref(msg.message_id)
    assert ref is not None
    assert ref["mail_id"] is None
    assert ref["to_addr"] == "you@example.com"
    assert store.messages_by_sender(msg.sender_email) == []  # excluded: no mail_id
    store.close()


def test_protect_event_sender_dismisses_pending_and_blocks_future(tmp_path):
    store = Store(tmp_path / "t.db")
    msg = make_msg(sender="UPS <notify@ups.com>", sender_email="notify@ups.com")
    store.record_message(msg, Classification("transactional", "low", "test"))

    ev1 = EventCandidate("Package arriving", "2026-08-02T14:00", "", "", 0.6,
                         msg.message_id, msg.subject, msg.sender)
    ev2 = EventCandidate("Package delivered", "2026-08-03T09:00", "", "", 0.5,
                         "<other@ups.com>", "Delivered", msg.sender)
    store.add_event(ev1)
    store.add_event(ev2)
    store.commit()
    pending = store.events_by_status("new")
    assert len(pending) == 2
    assert pending[0]["source_sender_email"] == "notify@ups.com"

    assert store.is_event_sender_protected("notify@ups.com") is False
    dismissed_ids = store.protect_event_sender("notify@ups.com")
    store.commit()

    assert set(dismissed_ids) == {r["id"] for r in pending}
    assert store.events_by_status("new") == []
    assert len(store.events_by_status("dismissed")) == 2
    assert store.is_event_sender_protected("notify@ups.com") is True
    store.close()


def test_store_backfills_blank_event_sender_email_on_open(tmp_path):
    """Rows written before source_sender_email existed (or by any other gap
    that leaves it blank) get repaired on the next Store() open, so `p`
    (protect sender) works on them instead of silently degrading to a
    single dismiss."""
    db_path = tmp_path / "t.db"
    store = Store(db_path)
    store.conn.execute(
        """INSERT INTO events (source_message_id, source_subject, source_sender,
           source_sender_email, title, start, status, created_at)
           VALUES ('<old@x>', 'Old event', 'UPS <notify@ups.com>', '', 'Old event',
                   '2026-08-02T14:00', 'new', '2026-07-01T00:00:00')""")
    store.commit()
    store.close()

    reopened = Store(db_path)
    row = reopened.conn.execute(
        "SELECT source_sender_email FROM events WHERE source_message_id='<old@x>'").fetchone()
    assert row["source_sender_email"] == "notify@ups.com"
    reopened.close()


def test_unsub_queue_tracks_last_to(tmp_path):
    store = Store(tmp_path / "t.db")
    msg = make_msg(headers={"to": "you@example.com"}, date_received="2026-07-01T09:00:00Z")
    store.upsert_unsub(msg, ["https://shop.com/u"])
    store.commit()
    row = store.unsub_by_status("suggested")[0]
    assert row["last_to"] == "you@example.com"
    assert row["last_received"] == "2026-07-01T09:00:00Z"

    msg2 = make_msg(headers={"to": "second@example.com"}, date_received="2026-07-18T09:00:00Z")
    store.upsert_unsub(msg2, ["https://shop.com/u"])
    store.commit()
    row = store.unsub_by_status("suggested")[0]
    assert row["last_to"] == "second@example.com"
    assert row["last_received"] == "2026-07-18T09:00:00Z"
    store.close()


def test_digest_escapes_html(tmp_path):
    store = Store(tmp_path / "t.db")
    msg = make_msg(subject="<script>alert(1)</script>",
                   sender="Evil <evil@x.com>", sender_email="evil@x.com",
                   headers={"list-unsubscribe": "<https://x/u>"})
    store.record_message(msg, Classification("marketing", "low", "t"))
    store.upsert_unsub(msg, ["https://x/u"])
    store.commit()
    html = render_html(store, {"scanned": 1, "accounts": 1, "llm": "off"})
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    store.close()


def test_config_defaults():
    cfg = Config()
    assert cfg.mail.lookback_days == 7
    assert cfg.unsubscribe.mode == "one_click"


# ---------------------------------------------------------------- duplicate detection
def test_dupes_detects_cross_account_same_subject(tmp_path):
    store = Store(tmp_path / "t.db")
    original = make_msg(message_id="<orig@x>", account="Personal",
                        sender="Alice <alice@x.com>", sender_email="alice@x.com",
                        subject="Trip itinerary", date_received="2026-07-20T09:00:00Z")
    forwarded = make_msg(message_id="<fwd@x>", account="Work",
                         sender="Alice <alice@x.com>", sender_email="alice@x.com",
                         subject="Fwd: Trip itinerary", date_received="2026-07-20T09:05:00Z")
    store.record_message(original, Classification("personal", "normal", "t"))
    store.record_message(forwarded, Classification("personal", "normal", "t"))
    store.commit()

    dupes = store.likely_cross_account_duplicates(days=30, window_hours=72)
    assert len(dupes) == 1
    assert dupes[0]["count"] == 2
    assert dupes[0]["accounts"] == ["Personal", "Work"]
    assert dupes[0]["subject"] == "trip itinerary"
    store.close()


def test_dupes_ignores_same_account_and_far_apart_and_different_sender(tmp_path):
    store = Store(tmp_path / "t.db")
    a = make_msg(message_id="<a@x>", account="Personal", subject="Hi",
                sender_email="alice@x.com", date_received="2026-07-01T09:00:00Z")
    b = make_msg(message_id="<b@x>", account="Personal", subject="Hi",  # same account
                sender_email="alice@x.com", date_received="2026-07-01T09:05:00Z")
    c = make_msg(message_id="<c@x>", account="Work", subject="Hi",  # too far apart
                sender_email="alice@x.com", date_received="2026-07-10T09:00:00Z")
    d = make_msg(message_id="<d@x>", account="Work", subject="Hi",  # different sender
                sender_email="bob@x.com", date_received="2026-07-01T09:02:00Z")
    for m in (a, b, c, d):
        store.record_message(m, Classification("personal", "normal", "t"))
    store.commit()

    assert store.likely_cross_account_duplicates(days=30, window_hours=72) == []
    store.close()


# ---------------------------------------------------------------- spam audit
def test_spam_audit_subject_matches():
    assert flag_spam_audit("Here's your receipt for Same Day Delivery", "x")[0]
    assert flag_spam_audit("Security alert: new sign-in", "x")[0]
    assert flag_spam_audit("New Year, New Workshops", "x")[0] is False


def test_parse_spam_audit_response_legitimate_and_junk():
    legit, reason = parse_spam_audit_response(
        '{"legitimate": true, "reason": "personal note from a friend"}')
    assert legit and reason == "LLM: personal note from a friend"

    not_legit, reason = parse_spam_audit_response(
        '{"legitimate": false, "reason": "promotional blast"}')
    assert not_legit is False and reason == ""


def test_parse_spam_audit_response_bad_input():
    assert parse_spam_audit_response("not json") == (False, "")
    assert parse_spam_audit_response("{}") == (False, "")


def test_spoofed_sender_detects_domain_mismatch():
    assert spoofed_sender('"proton.me" <support@areareservada.fhenus.com>')
    assert spoofed_sender('"Support Wordpress" <amy@amypthompson.com>') is False  # no brand keyword
    assert spoofed_sender('Amazon.com <account-update@amazon.com>') is False  # genuine domain
    assert spoofed_sender('"Amazon" <security@totally-not-amazon.ru>')


def test_spoofed_sender_handles_malformed_input():
    assert spoofed_sender("") is False
    assert spoofed_sender("no angle brackets here") is False


def test_store_spam_review_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_spam_flag(
        "<spam1@x>", "Personal", "Spam", 42, "Amex <noreply@amex-alerts.com>",
        "noreply@amex-alerts.com", "Security alert on your account",
        "2026-07-25T00:00:00Z", "receipt/security-style subject", False)
    store.commit()

    pending = store.spam_by_status("suggested")
    assert len(pending) == 1 and pending[0]["mail_id"] == 42
    assert "<spam1@x>" in store.known_spam_message_ids()

    store.set_spam_status("<spam1@x>", "protected")
    store.commit()
    assert store.spam_by_status("suggested") == []
    assert store.is_spam_sender_protected("noreply@amex-alerts.com")

    # protected sender's future messages should be skippable by the caller
    # (upsert itself doesn't dedupe by sender, but the scan loop checks first)
    assert not store.is_spam_sender_protected("someone-else@x.com")
    store.close()


def test_spam_trusted_senders_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    assert not store.is_spam_sender_trusted("friend@example.com")

    store.trust_spam_sender("friend@example.com")
    store.commit()
    assert store.is_spam_sender_trusted("friend@example.com")
    assert not store.is_spam_sender_trusted("someone-else@x.com")

    # trusting the same sender twice shouldn't error (INSERT OR IGNORE)
    store.trust_spam_sender("friend@example.com")
    store.commit()

    store.upsert_spam_flag(
        "<f1@x>", "Personal", "Spam", 1, "Friend <friend@example.com>",
        "friend@example.com", "Hey, long time no talk", "2026-07-25T00:00:00Z", "test", False)
    store.upsert_spam_flag(
        "<f2@x>", "Personal", "Spam", 2, "Friend <friend@example.com>",
        "friend@example.com", "Lunch next week?", "2026-07-26T00:00:00Z", "test", False)
    store.set_spam_status("<f2@x>", "rescued")
    store.commit()

    pending = store.spam_rows_for_sender("friend@example.com")
    assert {r["message_id"] for r in pending} == {"<f1@x>"}  # already-rescued row excluded
    store.close()


# ---------------------------------------------------------------- purchase review
def test_purchase_kind_priority_and_none():
    assert purchase_kind("Your order has shipped!") == "shipped"
    assert purchase_kind("Order Confirmation #12345") == "ordered"
    assert purchase_kind("Your package has been delivered") == "delivered"
    assert purchase_kind("We've received your return") == "returned"
    assert purchase_kind("Weekly newsletter") is None
    # return language wins over shipped/delivered when a subject has both
    assert purchase_kind("Refund issued for your delivered order") == "returned"


def test_kind_rank_orders_returned_highest():
    assert kind_rank("returned") > kind_rank("delivered") > kind_rank("shipped") > kind_rank("ordered")
    assert kind_rank("not-a-kind") < kind_rank("ordered")


def test_extract_order_ref_amazon_and_generic():
    assert extract_order_ref("Your Amazon.com order 111-6567914-4569043 has shipped") \
        == "111-6567914-4569043"
    assert extract_order_ref("Order Confirmation #2826202260") == "2826202260"
    assert extract_order_ref("Order Number: 495979408 confirmed") == "495979408"
    assert extract_order_ref("Weekly newsletter") is None


def test_vendor_from_sender():
    assert vendor_from_sender("no-reply@wayfair.com") == "Wayfair"
    assert vendor_from_sender("Amazon.com <ship-confirm@amazon.com>") == "Amazon"
    assert vendor_from_sender("not-an-email") == "Unknown"
    assert vendor_from_sender("") == "Unknown"


def test_clean_html_strips_tags_entities_and_bidi_marks():
    raw = "<p>Bunkie&nbsp;Board⁩, 8-piece</p><br/>Full   size"
    cleaned = clean_html(raw)
    assert "<" not in cleaned and ">" not in cleaned
    assert "⁩" not in cleaned
    assert "Bunkie" in cleaned and "Full" in cleaned


def test_parse_purchase_item_response_variants():
    single = parse_purchase_item_response(
        '{"items": "Skylar Area Rug", "vendor": "Wayfair", "confidence": 0.9}')
    assert single == {"items": ["Skylar Area Rug"], "order_ref": None,
                      "vendor": "Wayfair", "confidence": 0.9}

    multi = parse_purchase_item_response(
        '{"items": ["Rug", "Bed Frame"], "vendor": "Wayfair", "order_ref": "2826202260", '
        '"confidence": 1.4}')
    assert multi["items"] == ["Rug", "Bed Frame"]
    assert multi["order_ref"] == "2826202260"
    assert multi["confidence"] == 1.0  # clamped

    assert parse_purchase_item_response('{"items": [], "confidence": 0.9}') is None
    assert parse_purchase_item_response("not json") is None


def test_parse_review_draft_response_trims_and_caps():
    assert parse_review_draft_response('  "Great product, would buy again."  ') \
        == "Great product, would buy again."
    assert parse_review_draft_response("") is None
    assert parse_review_draft_response("   ") is None
    assert len(parse_review_draft_response("x" * 500)) == 400


def test_store_purchase_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_purchase("ref:12345", "12345", "Wayfair", "Area Rug",
                          "Personal", "INBOX", 101, "2026-07-28T10:00:00Z",
                          "ready", "Love this rug, holds up well outdoors.")
    store.commit()

    ready = store.purchases_by_status("ready")
    assert len(ready) == 1
    assert ready[0]["vendor"] == "Wayfair" and ready[0]["review_text"].startswith("Love")

    # approve it, then rescanning the same key must not clobber the decision
    store.set_purchase_status("ref:12345", "approved")
    store.commit()
    store.upsert_purchase("ref:12345", "12345", "Wayfair", "Area Rug (rescanned)",
                          "Personal", "INBOX", 101, "2026-07-28T10:00:00Z",
                          "ready", "different draft")
    store.commit()
    row = store.purchases_by_status("approved")[0]
    assert row["item"] == "Area Rug"  # untouched by the rescan
    assert store.purchases_by_status("ready") == []
    store.close()


def test_upsert_purchase_returned_excluded_not_clobbered(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_purchase("ref:99", "99", "Amazon", "Lamp", "Personal", "INBOX", 9,
                          "2026-07-28", "ready", "Nice lamp.")
    store.commit()
    store.set_purchase_status("ref:99", "returned_excluded")
    store.commit()

    # a later rescan (e.g. within the lookback window) must not resurrect it
    store.upsert_purchase("ref:99", "99", "Amazon", "Lamp", "Personal", "INBOX", 9,
                          "2026-07-28", "ready", "different draft")
    store.commit()
    row = store.purchases_by_status("returned_excluded")[0]
    assert row["status"] == "returned_excluded"
    assert store.purchases_by_status("ready") == []
    store.close()


def test_store_purchase_export_only_returns_approved(tmp_path):
    store = Store(tmp_path / "t.db")
    store.upsert_purchase("ref:1", "1", "Amazon", "Widget", "Personal", "INBOX", 1,
                          "2026-07-28", "ready", "Nice widget.")
    store.upsert_purchase("ref:2", "2", "Amazon", "Gadget", "Personal", "INBOX", 2,
                          "2026-07-27", "ready", "Nice gadget.")
    store.commit()
    store.set_purchase_status("ref:1", "approved")
    store.commit()

    approved = store.purchases_by_status("approved")
    assert len(approved) == 1 and approved[0]["item"] == "Widget"
    store.close()


def test_explain_move_error_adds_stale_hint_only_for_cant_get_object():
    explained = _explain_move_error("hxe00570/INBOX/201533: Can't get object.")
    assert explained.startswith("hxe00570/INBOX/201533: Can't get object.")
    assert "likely moved or was removed" in explained

    other = _explain_move_error("hxe00570/INBOX/201533: some other AppleScript error.")
    assert other == "hxe00570/INBOX/201533: some other AppleScript error."
