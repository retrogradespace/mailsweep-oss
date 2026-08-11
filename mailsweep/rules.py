"""Cheap rule-based triage that runs before (and as a fallback for) the LLM."""
from __future__ import annotations

import re
from email.utils import parseaddr

from .models import Classification, Message

_MARKETING_SUBJECT = re.compile(
    r"(%\s?off|sale|deal|discount|coupon|last chance|limited time|free shipping"
    r"|black friday|don.t miss|act now|expires|flash sale)", re.I)
_NOTIFICATION_SENDERS = re.compile(
    r"(no-?reply|do-?not-?reply|notifications?@|alerts?@|updates?@|mailer-daemon)", re.I)
_TRANSACTIONAL_SUBJECT = re.compile(
    r"(receipt|invoice|order (confirm|#|no)|payment|shipped|delivery|statement"
    r"|password|verification code|security alert|sign-?in)", re.I)
_EVENTISH = re.compile(
    r"(invit(e|ation)|rsvp|webinar|meeting|appointment|reschedul|event|register"
    r"|save the date|reminder:|starts at|join us|conference|deadline)", re.I)
_SPAM_AUDIT_SUBJECT = re.compile(
    r"(receipt|invoice|order (confirm|#|no)|payment|shipped|deliver|statement"
    r"|security alert|sign-?in|verification code|confirmation|purchase|billing"
    r"|account (alert|notice)|password|two-factor|2fa|refund|credit report"
    r"|new inquiry|identity verification|new letter)", re.I)

# Best-effort phishing hint: a display name that claims a well-known brand
# whose real domain doesn't match where the mail actually came from. This is
# a signal to show the human reviewer, never a verdict -- legitimate mail
# sometimes ships from third-party domains (e.g. a gift-card receipt sent via
# a white-label platform), so nothing here is auto-acted on.
_KNOWN_BRAND_DOMAINS = {
    "amazon": "amazon.com", "paypal": "paypal.com", "apple": "apple.com",
    "bank of america": "bankofamerica.com", "chase": "chase.com",
    "wells fargo": "wellsfargo.com", "citi": "citi.com", "discover": "discover.com",
    "american express": "americanexpress.com", "amex": "americanexpress.com",
    "google": "google.com", "microsoft": "microsoft.com", "irs": "irs.gov",
    "usps": "usps.com", "fedex": "fedex.com", "ups": "ups.com", "netflix": "netflix.com",
    "venmo": "venmo.com", "zelle": "zellepay.com", "proton": "proton.me",
}
_SENDER_RE = re.compile(r'^"?([^"<]*)"?\s*<([^>]+)>')


def unsubscribe_targets(msg: Message) -> list[str]:
    """Extract mailto:/https: targets from the List-Unsubscribe header."""
    raw = msg.list_unsubscribe or ""
    return re.findall(r"<\s*([^<>\s]+)\s*>", raw)


def classify_by_rules(msg: Message) -> Classification:
    subj = msg.subject or ""
    sender = f"{msg.sender} {msg.sender_email}"
    is_list = bool(msg.list_unsubscribe or msg.headers.get("list-id"))
    is_bulk = msg.headers.get("precedence", "").lower() in {"bulk", "list", "junk"}

    if _TRANSACTIONAL_SUBJECT.search(subj):
        return Classification("transactional", "normal", "receipt/security-style subject")
    if is_list and _MARKETING_SUBJECT.search(subj):
        return Classification("marketing", "low", "mailing list + promotional subject")
    if is_list or is_bulk:
        return Classification("newsletter", "low", "mailing-list headers present")
    if _NOTIFICATION_SENDERS.search(sender):
        return Classification("notification", "low", "no-reply style sender")
    # Unknown: default to personal/normal so nothing real gets buried by rules alone.
    return Classification("personal", "normal", "no bulk-mail signals")


def maybe_event(msg: Message) -> bool:
    """Should this message be shown to the LLM for event extraction?"""
    return bool(_EVENTISH.search(f"{msg.subject} {msg.snippet[:500]}"))


def ambiguous(msg: Message, rule_result: Classification) -> bool:
    """Does this message deserve an LLM look beyond the rule verdict?"""
    if rule_result.category == "personal":
        return True                       # rules couldn't place it
    if maybe_event(msg):
        return True                       # possible buried event
    return False


def spoofed_sender(sender: str) -> bool:
    """Heuristic phishing hint: display name claims a known brand but the
    actual sending domain doesn't match. A hint for the human reviewer only."""
    m = _SENDER_RE.match((sender or "").strip())
    if not m:
        return False
    display, email = m.group(1).lower(), m.group(2).lower()
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1]
    for brand, expected_domain in _KNOWN_BRAND_DOMAINS.items():
        if brand in display and not domain.endswith(expected_domain):
            return True
    return False


def flag_spam_audit(subject: str, sender: str) -> tuple[bool, str]:
    """Should this Junk/Spam-folder message be surfaced for a false-positive
    check? True when it looks like a receipt/security/financial notice --
    exactly the kind of mail nobody wants a spam filter silently eating."""
    if _SPAM_AUDIT_SUBJECT.search(subject or ""):
        return True, "receipt/security/financial-style subject"
    return False, ""


# ---------------------------------------------------------------- purchase review
_ORDER_PLACED_SUBJECT = re.compile(
    r"(order confirm|thank you for your order|your order (has been placed|is confirmed)"
    r"|order #|order no\.?|order number|receipt for (your )?order)", re.I)
_SHIPPED_SUBJECT = re.compile(
    r"(has shipped|shipping confirmation|shipment confirmation|tracking (number|info)"
    r"|on its way|on it.s way)", re.I)
_DELIVERED_SUBJECT = re.compile(
    r"\b(delivered|delivery complete|delivery confirmation)\b", re.I)
_RETURN_SUBJECT = re.compile(
    r"(return (label|confirmation|received)|refund (issued|processed|confirmation)"
    r"|we.ve received your return)", re.I)
_ORDER_REF_RE = re.compile(
    r"\b(\d{3}-\d{7}-\d{7})\b"                                    # Amazon-style
    r"|order[^\d#:]{0,20}[#:]\s*(\d{5,})", re.I)                  # "Order Confirmation #123456" etc.

# Highest-priority kind wins when several emails share an order ref.
_KIND_PRIORITY = {"returned": 3, "delivered": 2, "shipped": 1, "ordered": 0}


def purchase_kind(subject: str) -> str | None:
    """Classify a message's likely role in an order's lifecycle (returned >
    delivered > shipped > ordered, checked in that order since e.g. a
    delivery-confirmation subject can also contain "shipped"), or None if it
    doesn't look purchase-related at all."""
    subject = subject or ""
    if _RETURN_SUBJECT.search(subject):
        return "returned"
    if _DELIVERED_SUBJECT.search(subject):
        return "delivered"
    if _SHIPPED_SUBJECT.search(subject):
        return "shipped"
    if _ORDER_PLACED_SUBJECT.search(subject):
        return "ordered"
    return None


def kind_rank(kind: str) -> int:
    """Ordering used to pick the most-advanced status among an order's
    associated emails (returned > delivered > shipped > ordered)."""
    return _KIND_PRIORITY.get(kind, -1)


def extract_order_ref(text: str) -> str | None:
    m = _ORDER_REF_RE.search(text or "")
    if not m:
        return None
    return m.group(1) or m.group(2)


def vendor_from_sender(sender: str) -> str:
    """Best-effort store/brand name from a sender's domain, e.g.
    'no-reply@wayfair.com' -> 'Wayfair'."""
    _, addr = parseaddr(sender or "")
    domain = addr.split("@")[-1].lower() if "@" in addr else ""
    parts = [p for p in domain.split(".") if p]
    base = parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")
    return base.capitalize() if base else "Unknown"
