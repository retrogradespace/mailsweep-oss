"""LLM classification via a local Ollama model, with rule fallback."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime

import requests

from . import rules
from .config import ModelCfg
from .models import CATEGORIES, IMPORTANCE, Classification, EventCandidate, Message
from .timeparse import normalize_end, normalize_start

_PROMPT = """You are an email triage assistant. Today is {today}.
Classify the email below and extract any real-world event the recipient might
need on their calendar (meetings, appointments, invitations, deadlines,
performances, webinars they registered for). Marketing "events" like sales
do NOT count.
{feedback}
Category notes: spam_phishing = unsolicited scams, fake security/account alerts,
phishing attempts. junk = low-value bulk mail not worth reading, distinct from a
newsletter/marketing list the recipient actually signed up for -- this includes
repeated delivery-ETA/status pings from an order you already placed (e.g. a
revised arrival-time estimate), since only the original order confirmation and
the final delivery notice carry lasting value. trash = disposable, safe to
discard outright (e.g. a stale automated notice).

Event times: the recipient's local timezone is {tz}. Write "start" and "end" as local
wall-clock time with NO timezone suffix (no Z, no +00:00). If the email lists several
time zones, use the recipient's. If the email states no clock time, give the date only
(YYYY-MM-DD) and set all_day to true -- never invent a time like 00:00 or 12:00. Resolve
words like "Thursday" or "tomorrow" from the email's Date line, not from today.

Reply with ONLY a JSON object, no other text:
{{
  "category": "personal|work|transactional|newsletter|marketing|notification|spam_phishing|junk|trash",
  "importance": "high|normal|low",
  "reason": "<one short phrase>",
  "event": null or {{
    "title": "...",
    "start": "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD" if time unknown,
    "end": "" or "YYYY-MM-DDTHH:MM",
    "location": "",
    "all_day": false,
    "confidence": 0.0-1.0
  }}
}}

Email:
Account: {account}
From: {sender}
Subject: {subject}
Date: {date}
Body (truncated):
{snippet}
"""


def ollama_available(cfg: ModelCfg) -> bool:
    if not cfg.enabled:
        return False
    try:
        r = requests.get(f"{cfg.host}/api/tags", timeout=3)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of model output (tolerates fencing/chatter)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_llm_response(text: str, msg: Message) -> Classification | None:
    data = _extract_json(text)
    if not data:
        return None
    category = str(data.get("category", "")).lower()
    importance = str(data.get("importance", "")).lower()
    if category not in CATEGORIES:
        category = "personal"
    if importance not in IMPORTANCE:
        importance = "normal"
    event = None
    ev = data.get("event")
    if isinstance(ev, dict) and ev.get("title") and ev.get("start"):
        norm = normalize_start(str(ev["start"]))
        if norm is not None:          # no usable date -> nothing Calendar could take
            start, date_only = norm
            try:
                confidence = float(ev.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            event = EventCandidate(
                title=str(ev["title"])[:200],
                start=start,
                end=normalize_end(start, str(ev.get("end", "") or "")),
                location=str(ev.get("location", "") or "")[:200],
                confidence=max(0.0, min(1.0, confidence)),
                source_message_id=msg.message_id,
                source_subject=msg.subject,
                source_sender=msg.sender,
                all_day=bool(ev.get("all_day", False)) or date_only,
            )
    return Classification(category, importance,
                          str(data.get("reason", ""))[:200], event, used_llm=True)


def build_feedback_block(store) -> str:
    """Recent human corrections, formatted as few-shot examples for the
    triage prompt. `store` is a mailsweep.store.Store, or None (e.g. in
    tests, or callers that don't have one) -- in which case this is a
    no-op, same as before feedback existed.

    Call this ONCE per scan (not per message) and pass the resulting string
    into classify()/classify_with_llm() -- the feedback data doesn't change
    mid-scan, so re-querying it per ambiguous message is pure waste."""
    if store is None:
        return ""
    sections = []

    corrections = store.classify_corrections(limit=5)
    if corrections:
        lines = ["Recent corrections from the user -- do not repeat these mistakes:"]
        for c in corrections:
            lines.append(
                f'- From: "{c["sender"]}"  Subject: "{c["subject"]}"\n'
                f'  -> correct answer: category={c["correct_category"]}, '
                f'importance={c["correct_importance"]} '
                f'(previously misclassified as {c["predicted_category"]}/{c["predicted_importance"]})')
        sections.append("\n".join(lines))

    positive, negative = store.event_feedback_examples(limit=3)
    if positive or negative:
        lines = ["Recent event-detection feedback from the user:"]
        for p in positive:
            lines.append(
                f'- From: "{p["source_sender"]}"  Subject: "{p["source_subject"]}"\n'
                f'  -> WAS a real event ("{p["title"]}", {p["start"]}) -- confirmed by the user')
        for n in negative:
            lines.append(
                f'- From: "{n["source_sender"]}"  Subject: "{n["source_subject"]}"\n'
                f'  -> NOT a real event -- the user dismissed this suggestion')
        sections.append("\n".join(lines))

    fixes = store.event_time_corrections(limit=3)
    if fixes:
        lines = ["Recent date/time corrections from the user -- the event was real but the "
                 "date/time extracted was wrong. Copy times exactly as written in the email; "
                 "if no clock time is stated, give the date only:"]
        for f in fixes:
            lines.append(
                f'- From: "{f["source_sender"]}"  Subject: "{f["source_subject"]}"\n'
                f'  -> extracted {f["original_start"]}, correct was {f["start"]}')
        sections.append("\n".join(lines))

    return "\n" + "\n\n".join(sections) + "\n" if sections else ""


def _local_tz_label() -> str:
    now = datetime.now().astimezone()
    off = now.strftime("%z")
    return f"{now.tzname()} (UTC{off[:3]}:{off[3:]})"


def _local_received(iso: str) -> str:
    """The message's received time as local weekday + wall-clock, so relative
    dates in the body ("this Thursday") resolve against what the recipient saw,
    not a UTC timestamp that can land on the wrong weekday."""
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return iso
        return dt.astimezone().strftime("%A %Y-%m-%d %H:%M")
    except ValueError:
        return iso


def classify_with_llm(msg: Message, cfg: ModelCfg, feedback: str = "") -> Classification | None:
    prompt = _PROMPT.format(
        today=datetime.now().strftime("%A, %Y-%m-%d"), tz=_local_tz_label(),
        account=msg.account, sender=msg.sender, subject=msg.subject,
        date=_local_received(msg.date_received), snippet=msg.snippet[:1500],
        feedback=feedback,
    )
    try:
        r = requests.post(f"{cfg.host}/api/generate", json={
            "model": cfg.name,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_predict": 400},
        }, timeout=cfg.timeout)
        r.raise_for_status()
        return parse_llm_response(r.json().get("response", ""), msg)
    except requests.RequestException:
        return None


def classify(msg: Message, cfg: ModelCfg, llm_up: bool, feedback: str = "") -> Classification:
    """Hybrid pipeline: rules first, LLM only for the ambiguous residue.
    `feedback`, if given (see build_feedback_block), is injected into the LLM
    prompt as few-shot examples from recent `classify review`/`events
    review` corrections."""
    verdict = rules.classify_by_rules(msg)
    if llm_up and rules.ambiguous(msg, verdict):
        llm_verdict = classify_with_llm(msg, cfg, feedback)
        if llm_verdict:
            return llm_verdict
    return verdict


# ---------------------------------------------------------------- spam audit
_SPAM_AUDIT_PROMPT = """You are reviewing an email a spam filter already placed in a \
Junk/Spam folder. Today is {today}. Decide whether this looks like legitimate mail the \
recipient would want to see (a false positive), as opposed to genuine spam/junk/marketing \
they don't want. Judge from the subject and sender alone -- the message body isn't available.

Reply with ONLY a JSON object, no other text:
{{
  "legitimate": true or false,
  "reason": "<one short phrase>"
}}

From: {sender}
Subject: {subject}
"""


def parse_spam_audit_response(text: str) -> tuple[bool, str]:
    data = _extract_json(text)
    if not data or not data.get("legitimate"):
        return False, ""
    reason = str(data.get("reason", ""))[:200]
    return True, f"LLM: {reason}" if reason else "LLM: looks legitimate"


def spam_audit_with_llm(subject: str, sender: str, cfg: ModelCfg) -> tuple[bool, str]:
    """Second opinion for Junk-folder mail that didn't match the subject regex
    in rules.flag_spam_audit -- widens recall beyond that fixed pattern list.
    Returns (should_flag, reason); (False, "") if the LLM is unreachable or
    returned nothing usable."""
    prompt = _SPAM_AUDIT_PROMPT.format(
        today=datetime.now().strftime("%A, %Y-%m-%d"), sender=sender, subject=subject)
    try:
        r = requests.post(f"{cfg.host}/api/generate", json={
            "model": cfg.name,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_predict": 100},
        }, timeout=cfg.timeout)
        r.raise_for_status()
        return parse_spam_audit_response(r.json().get("response", ""))
    except requests.RequestException:
        return False, ""


# ---------------------------------------------------------------- purchase review
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
# Bidi/format control chars HTML emails sometimes leak into extracted text
# (e.g. isolate marks around numbers) -- invisible but pollute item names.
_UNICODE_FORMAT_CHARS_RE = re.compile(r"[​-‏ -‮⁠-⁩﻿]")


def clean_html(s: str) -> str:
    """Crude tag stripper for raw message source, so it's LLM-prompt-sized
    and doesn't drown the model in markup. Not for display -- just extraction."""
    text = _HTML_TAG_RE.sub(" ", s or "")
    text = html.unescape(text)
    text = _UNICODE_FORMAT_CHARS_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


_PURCHASE_ITEM_PROMPT = """You are extracting purchase details from an order-related email so the \
recipient can write a short product review for each item afterward. Today is {today}.

Reply with ONLY a JSON object, no other text:
{{
  "items": ["<short product name>", "..."],
  "order_ref": "<the order/confirmation number if you see one, else \\"\\">",
  "vendor": "<store/brand name, or \\"\\" if unclear>",
  "confidence": 0.0-1.0
}}
"items" should have one entry per distinct product in this order (just one entry if
that's all there is). "confidence" is how sure you are the "items" list names real,
specific product(s) rather than a placeholder like "3 items".

Email:
Subject: {subject}
From: {sender}
Body (may be truncated, may include raw HTML/markup):
{content}
"""


def _clean_item_text(s: str) -> str:
    """Strip stray bidi/format marks that leak into item names from either
    Mail.app's plain-text content() rendering or raw HTML source."""
    return _UNICODE_FORMAT_CHARS_RE.sub("", s or "").strip()


def parse_purchase_item_response(text: str) -> dict | None:
    data = _extract_json(text)
    if not data:
        return None
    items = data.get("items")
    if isinstance(items, str):
        items = [items]
    items = [_clean_item_text(str(i))[:200] for i in (items or []) if _clean_item_text(str(i or ""))]
    if not items:
        return None
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "items": items,
        "order_ref": _clean_item_text(str(data.get("order_ref", "") or ""))[:60] or None,
        "vendor": _clean_item_text(str(data.get("vendor", "") or ""))[:80],
        "confidence": max(0.0, min(1.0, confidence)),
    }


def extract_purchase_item(subject: str, sender: str, content: str, cfg: ModelCfg) -> dict | None:
    """Ask the LLM to name the purchased item(s) and vendor from an order
    email's subject/content. Returns {item, vendor, confidence} or None if
    the LLM is unreachable or returned nothing usable."""
    prompt = _PURCHASE_ITEM_PROMPT.format(
        today=datetime.now().strftime("%A, %Y-%m-%d"),
        subject=subject, sender=sender, content=content[:3000])
    try:
        r = requests.post(f"{cfg.host}/api/generate", json={
            "model": cfg.name,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_predict": 200},
        }, timeout=cfg.timeout)
        r.raise_for_status()
        return parse_purchase_item_response(r.json().get("response", ""))
    except requests.RequestException:
        return None


_REVIEW_DRAFT_PROMPT = """Write a short, casual, first-person product review (1-2 sentences, \
under 40 words) for the item below, as if the buyer is reasonably happy with it. Be specific \
and plausible based on the product name/context given -- do not invent brand claims or details \
that aren't implied by it. If you truly have nothing specific to say, write one honest, brief \
generic sentence instead of padding.
Reply with ONLY the review text -- no quotes, no preamble, no other commentary.

Item: {item}
Vendor: {vendor}
Extra context (may be empty): {content}
"""


def parse_review_draft_response(text: str) -> str | None:
    cleaned = _clean_item_text((text or "").strip().strip('"').strip())
    return cleaned[:400] or None


def draft_purchase_review(item: str, vendor: str, content: str, cfg: ModelCfg) -> str | None:
    """Ask the LLM for a short draft review for an item that's likely been
    delivered. Returns None if the LLM is unreachable or gave nothing usable
    -- the review_text field is just left blank for the human to fill in."""
    prompt = _REVIEW_DRAFT_PROMPT.format(item=item, vendor=vendor or "unknown", content=content[:1000])
    try:
        r = requests.post(f"{cfg.host}/api/generate", json={
            "model": cfg.name,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.4, "num_predict": 120},
        }, timeout=cfg.timeout)
        r.raise_for_status()
        return parse_review_draft_response(r.json().get("response", ""))
    except requests.RequestException:
        return None
