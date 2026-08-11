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

_PROMPT = """You are an email triage assistant. Today is {today}.
Classify the email below and extract any real-world event the recipient might
need on their calendar (meetings, appointments, invitations, deadlines,
performances, webinars they registered for). Marketing "events" like sales
do NOT count.

Reply with ONLY a JSON object, no other text:
{{
  "category": "personal|work|transactional|newsletter|marketing|notification",
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
        try:
            confidence = float(ev.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        event = EventCandidate(
            title=str(ev["title"])[:200],
            start=str(ev["start"]),
            end=str(ev.get("end", "") or ""),
            location=str(ev.get("location", "") or "")[:200],
            confidence=max(0.0, min(1.0, confidence)),
            source_message_id=msg.message_id,
            source_subject=msg.subject,
            source_sender=msg.sender,
            all_day=bool(ev.get("all_day", False)),
        )
    return Classification(category, importance,
                          str(data.get("reason", ""))[:200], event, used_llm=True)


def classify_with_llm(msg: Message, cfg: ModelCfg) -> Classification | None:
    prompt = _PROMPT.format(
        today=datetime.now().strftime("%A, %Y-%m-%d"),
        account=msg.account, sender=msg.sender, subject=msg.subject,
        date=msg.date_received, snippet=msg.snippet[:1500],
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


def classify(msg: Message, cfg: ModelCfg, llm_up: bool) -> Classification:
    """Hybrid pipeline: rules first, LLM only for the ambiguous residue."""
    verdict = rules.classify_by_rules(msg)
    if llm_up and rules.ambiguous(msg, verdict):
        llm_verdict = classify_with_llm(msg, cfg)
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
