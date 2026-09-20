"""`mailsweep ui --demo`: throwaway sample data and a stand-in for Mail/Calendar.

Nothing here reads your mailbox, writes to Calendar, moves messages, or makes
a network request -- the fake I/O just reports what it *would* have done.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from ..config import Config
from ..models import Classification, EventCandidate, Message
from ..store import Store
from .io import LiveIO  # noqa: F401  (documents the interface FakeIO mirrors)

# sender, email, msgs seen, one-click, category, last subject
_NOISE = [
    ("Wayfair Deals", "deals@wayfair.example", 88, True, "marketing", "Save 60% on outdoor furniture, today only"),
    ("LinkedIn Notifications", "notifications@linkedin.example", 61, True, "notification", "You appeared in 9 searches this week"),
    ("Instagram", "no-reply@instagram.example", 52, False, "notification", "You have new notifications"),
    ("Zillow Listings", "listings@zillow.example", 40, True, "marketing", "3 new homes match your search"),
    ("Nordstrom Rack", "offers@nordstromrack.example", 34, True, "marketing", "Extra 25% off clearance"),
    ("Duolingo", "hello@duolingo.example", 30, False, "notification", "Your streak is in danger"),
    ("Substack Digest", "digest@substack.example", 21, True, "newsletter", "Top stories this week"),
    ("Etsy Recommendations", "recs@etsy.example", 17, False, "marketing", "Picked for you: handmade ceramics"),
    ("Meetup", "info@meetup.example", 15, True, "notification", "Events near you this weekend"),
    ("Strava Weekly", "weekly@strava.example", 9, True, "newsletter", "Your week in review"),
    ("Airbnb Offers", "offers@airbnb.example", 8, False, "marketing", "Plan your fall getaway"),
    ("Medium Daily Digest", "noreply@medium.example", 8, True, "newsletter", "Stories for you"),
    ("Wirecutter", "newsletter@wirecutter.example", 7, True, "newsletter", "The best standing desks"),
    ("Home Depot", "email@homedepot.example", 6, False, "marketing", "Fall project savings"),
    ("Pinterest", "hello@pinterest.example", 6, True, "notification", "Pins you might like"),
    ("Grammarly Weekly", "weekly@grammarly.example", 4, True, "newsletter", "Your writing update"),
    ("Yelp", "hello@yelp.example", 3, False, "marketing", "New spots in your area"),
    ("Eventbrite", "info@eventbrite.example", 3, True, "notification", "Events you may like"),
    ("Flaky Shop", "promo@flakyshop.example", 2, True, "marketing", "Flash sale: 48 hours only"),
]


# Senders whose mail is authenticated as someone else (a mail service), not as themselves.
_UNVERIFIED = {"recs@etsy.example", "promo@flakyshop.example"}


class FakeIO:
    def __init__(self):
        self.calls: list[tuple] = []
        # First attempt fails, so the failure -> retry path can be tried end to end.
        self.fail_once = {"promo@flakyshop.example"}

    def ollama_up(self) -> bool:
        return True

    def unsub(self, targets_json: str, one_click: bool, sender_email: str, allow_open: bool = True):
        """Mirrors unsub.attempt(): POST first, then 'open' the page. Nothing is sent or opened."""
        self.calls.append(("unsub", sender_email))
        host = sender_email.split("@")[1]
        post_note = ""
        if one_click:
            if sender_email in self.fail_once:
                self.fail_once.discard(sender_email)
                post_note = "one-click POST returned 500"
            else:
                return "done", "demo: one-click POST accepted (200) -- nothing was actually sent"
        if not allow_open:
            if post_note:
                return "failed", f"{post_note}; page not opened (tab limit for one action) -- use Open page"
            return "not_opened", "left in the queue: tab limit for one action reached"
        self.calls.append(("open_url", f"https://{host}/unsubscribe?u=demo"))
        lead = post_note + "; " if post_note else ""
        return "opened", f"{lead}demo: would open {host} in your browser -- nothing opened. Come back and confirm"

    def open_url(self, url):
        self.calls.append(("open_url", url))

    def move_messages(self, targets, dest):
        self.calls.append(("move_messages", len(targets), dest))
        return {"moved": len(targets), "total": len(targets), "errors": []}

    def move_message(self, account, mailbox, mail_id, dest):
        self.calls.append(("move_message", mail_id, dest))

    def create_event(self, calendar, title, start, end, location, all_day):
        self.calls.append(("create_event", title))


def _iso(dt: datetime, date_only: bool = False) -> str:
    return dt.strftime("%Y-%m-%d") if date_only else dt.strftime("%Y-%m-%dT%H:%M:%S")


def seed(store: Store) -> None:
    now = datetime.now()
    mail_id = 1000

    def msg(sender, email, subject, ago_h, mid_tag, account="Personal", to="you@example.com"):
        nonlocal mail_id
        mail_id += 1
        return Message(
            message_id=f"<{mid_tag}-{mail_id}@demo>", account=account, mailbox="INBOX",
            sender=f"{sender} <{email}>", sender_email=email, subject=subject,
            date_received=_iso(now - timedelta(hours=ago_h)), snippet="",
            headers={"to": to, "from": f"{sender} <{email}>"}, mail_id=mail_id)

    for name, email, count, one_click, cat, last in _NOISE:
        targets = [f"https://{email.split('@')[1]}/unsubscribe?u=demo"]
        if not one_click:
            targets.append(f"mailto:unsub@{email.split('@')[1]}")
        headers = {"list-unsubscribe": ", ".join(f"<{t}>" for t in targets)}
        dom = email.split("@")[1]
        headers["authentication-results"] = (
            "mx.demo; dkim=pass header.d=sendgrid.net; spf=pass smtp.mailfrom=sendgrid.net; dmarc=none"
            if email in _UNVERIFIED else
            f"mx.demo; dkim=pass header.d={dom}; spf=pass smtp.mailfrom={dom}; dmarc=pass header.from={dom}")
        if one_click:
            headers["list-unsubscribe-post"] = "List-Unsubscribe=One-Click"
        for i in range(count):
            m = msg(name, email, last, 1 + i * 6, "n", account="iCloud" if i % 3 else "Gmail")
            m.headers.update(headers)
            store.record_message(m, Classification(cat, "low", "demo", used_llm=bool(i % 2)))
        newest = msg(name, email, last, 2, "u", account="iCloud")
        newest.headers.update(headers)
        store.upsert_unsub(newest, targets)
        # upsert_unsub only counts one message; set the seen-count directly
        store.conn.execute("UPDATE unsub_queue SET msg_count=? WHERE sender_email=?", (count, email))

    # -- unsubscribe history: what the "second look" groups and the domain flag show ----------
    def past_unsub(name, email, days_ago, before, since, status="done", note=None, attempts=1, opened=False, url=None):
        when = now - timedelta(days=days_ago)
        targets = [url or f"https://{email.split('@')[1]}/unsubscribe?u=demo"]
        first = msg(name, email, "Weekly deals", 1, "h")
        first.headers.update({"list-unsubscribe": f"<{targets[0]}>",
                              "list-unsubscribe-post": "List-Unsubscribe=One-Click"})
        store.upsert_unsub(first, targets)
        store.conn.execute(
            "UPDATE unsub_queue SET status=?, updated_at=?, last_note=?, attempts=?, opened_at=? WHERE sender_email=?",
            (status, _iso(when), note, attempts, _iso(when) if opened else None, email))
        for d in before:                       # mail from before we unsubscribed
            store.record_message(msg(name, email, "Weekly deals", d * 24, "hb"),
                                 Classification("marketing", "low", "demo"))
        for d in since:                        # mail that arrived after
            store.record_message(msg(name, email, "This week's picks", d * 24, "hs"),
                                 Classification("marketing", "low", "demo"))

    past_unsub("Wayfair Sales", "sale@wayfair.example", 30, before=[40, 38], since=[])
    past_unsub("Grubhub Promos", "deals@grubhub.example", 25, before=[30, 28], since=[10, 7, 4, 1])
    past_unsub("Care.com Digest", "digest@care.example", 5, before=[9], since=[2])
    past_unsub("Chewy", "promo@chewy.example", 3, before=[6], since=[], status="approved", attempts=1,
               note="one-click POST returned 404; try the link manually: https://chewy.example/unsubscribe?u=demo")

    past_unsub("Peloton", "hello@peloton.example", 1, before=[4], since=[], status="approved", opened=True,
               note="one-click POST returned 502; opened peloton.example in your browser -- come back and confirm")

    # A spoofed From line filing someone else's link under a real sender: the airline row carries
    # the *recreation department's* per-recipient link, and you protected the recreation sender.
    shared = "https://recreation.example/webtrac/unsubscribe.html?email=DEMOTOKEN123&Action=AutoProcess"
    past_unsub("Town Recreation", "nccary@recreation.example", 20, before=[], since=[], status="protected", url=shared)
    past_unsub("Airline Customer Care", "no-reply@airline.example", 0, before=[], since=[], status="approved", attempts=1,
               url=shared, note=f"one-click POST returned 403; try the link manually: {shared}")

    # A forged From line: the receiving server said DMARC failed. Recorded, but never queued.
    forged = msg("Chase Alerts", "security@chase-verify.example", "Your account is locked: verify now", 30, "f")
    forged.headers.update({"list-unsubscribe": "<https://chase-verify.example/u?t=1>",
                           "authentication-results": "mx.demo; dkim=none; spf=fail smtp.mailfrom=chase-verify.example; "
                                                     "dmarc=fail (p=REJECT) header.from=chase.example"})
    store.record_message(forged, Classification("marketing", "low", "demo"))
    store.upsert_unsub(forged, ["https://chase-verify.example/u?t=1"])

    important = [
        ("Dr. Alvarez's Office", "frontdesk@alvarezdental.example", "Please confirm your appointment"),
        ("Vermont State Colleges HR", "hr@vsc.example", "Action needed: benefits enrollment closes Friday"),
        ("Landlord", "landlord@example.example", "Lease renewal: please sign by the 30th"),
    ]
    for i, (n, e, s) in enumerate(important):
        store.record_message(msg(n, e, s, 5 + i * 9, "i"),
                             Classification("personal", "high", "demo", used_llm=True))
    for i in range(9):
        store.record_message(msg("Colleague", "colleague@example.example", f"Re: project notes {i}", 3 + i * 7, "w"),
                             Classification("work", "normal", "demo", used_llm=True))
    for i in range(6):
        store.record_message(msg("Bank", "alerts@bank.example", f"Statement ready {i}", 4 + i * 20, "t"),
                             Classification("transactional", "normal", "demo"))

    def event(title, days, hour, where, conf, sender, email, subject, date_only=False):
        start = _iso((now + timedelta(days=days)).replace(hour=hour, minute=0, second=0), date_only)
        store.add_event(EventCandidate(
            title=title, start=start, end="", location=where, confidence=conf,
            source_message_id=f"<ev-{days}@demo>", source_subject=subject,
            source_sender=f"{sender} <{email}>", all_day=date_only))
        source = msg(sender, email, subject, 8 + days, "ev")
        source.message_id = f"<ev-{days}@demo>"     # what the event row points back at
        store.record_message(source, Classification("personal", "high", "demo", used_llm=True))

    event("Dentist cleaning", 4, 14, "Alvarez Dental, 12 Main St", 0.92, "Dr. Alvarez's Office",
          "frontdesk@alvarezdental.example", "Reminder: your appointment on the 22nd")
    event("Flight BTV -> DCA", 14, 6, "Burlington International", 0.81, "Airline",
          "confirm@airline.example", "Your itinerary is confirmed")
    event("Package delivery window", 1, 10, "", 0.55, "Carrier", "track@carrier.example",
          "Out for delivery tomorrow", date_only=True)
    event("Faculty meeting", 6, 15, "Room 214", 0.63, "Dean's Office", "dean@vsc.example",
          "Faculty meeting: agenda attached")
    event("Book club", 9, 19, "Community library", 0.47, "Book Club", "club@example.example",
          "This month's pick + where we're meeting")
    event("Past webinar (already happened)", -3, 12, "", 0.6, "Vendor", "news@vendor.example",
          "Recording available")

    flags = [
        ("Vermont DMV", "noreply@dmv.vermont.example", "Your registration renewal receipt", "receipt-style subject", False),
        ("Chase", "alerts@chase-secure.example", "Security alert: verify your account now", "security-style subject", True),
        ("Delta Faucet", "orders@deltafaucet.example", "Your order has shipped", "receipt-style subject", False),
    ]
    for i, (n, e, s, why, phish) in enumerate(flags):
        store.upsert_spam_flag(f"<sp-{i}@demo>", "Gmail", "Spam", 5000 + i, f"{n} <{e}>", e, s,
                               _iso(now - timedelta(days=i + 1)), why, phish)

    purchases = [
        ("ord-1", "112-4433", "Amazon", "Ceramic pour-over coffee dripper", "ready",
         "Solid dripper, heats evenly and cleans up easily. Two stars off only because the stand wobbles a little."),
        ("ord-2", "9981-22", "Wayfair", "Walnut floating shelf, 36 in", "ready",
         "Sturdy and easy to level. Hardware included and the finish matches the photos."),
        ("ord-3", "5510-77", "Etsy", "Hand-thrown mug", "pending_receipt", None),
    ]
    for key, ref, vendor, item, status, text in purchases:
        store.upsert_purchase(key, ref, vendor, item, "Gmail", "INBOX", 7000, _iso(now - timedelta(days=12)),
                              status, text)
    store.commit()


def build() -> tuple[Config, FakeIO, Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="mailsweep-demo-"))
    cfg = Config()
    cfg.digest.output_dir = str(tmp / "out")
    cfg.unsubscribe.protected = ["chase"]
    cfg.calendar.target_calendar = "Home"
    db = tmp / "demo.db"
    store = Store(db)
    seed(store)
    store.close()
    return cfg, FakeIO(), db, tmp
