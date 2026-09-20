"""Data and actions behind the web UI.

Every action here mirrors an existing CLI review flow: same store methods,
same bridge calls, same status transitions. This module returns JSON where
the CLI prints. Actions re-check row status inside a lock, so a double-click
or a second browser tab can't fire an unsubscribe (or a Calendar write) twice.
"""
from __future__ import annotations

import csv
import json
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlparse

from .. import auth, bridge
from .. import unsub as unsubmod
from ..cli import _explain_move_error
from ..store import Store

MAX_ROWS = 500          # rows returned per list; the page pages through them client-side
MAX_BATCH = 500
MAX_TABS = 5             # pages one action may open in the browser; the rest stay queued
PURCHASE_STATUSES = ("ready", "pending_receipt", "approved", "skipped",
                     "returned_excluded", "name_unresolvable")
MOVE_DEST = {"trash": "Trash", "archive": "Archive", "junk": "Junk"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _row(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _targets(raw: str | None) -> list[str]:
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def approval_method(row, mode: str) -> str:
    """What approving this queue row would actually do -- kept in step with
    unsub.attempt so the confirm dialog tells the truth."""
    targets = _targets(row["targets"])
    http = [t for t in targets if t.lower().startswith(("http://", "https://"))]
    mailto = [t for t in targets if t.lower().startswith("mailto:")]
    if mode == "never" or not targets:
        return "none"
    if row["one_click"] and http and mode == "one_click":
        return "one_click"
    if http:
        return "browser"
    if mailto:
        return "mailto"
    return "none"


_URL_IN_TEXT = re.compile(r"https?://([^\s/?#]+)[^\s]*")


def _redact(text):
    """Unsubscribe links carry a per-recipient token. Show where a link goes, never the token."""
    return _URL_IN_TEXT.sub(r"https://\1/…", text) if text else text


def _domain(email: str) -> str:
    return (email or "").rpartition("@")[2].lower()


def _openable(row) -> str | None:
    """First http(s) or mailto target on file, for the 'open the page myself' action.
    Anything else in a List-Unsubscribe header (javascript:, file:, ...) is ignored."""
    ts = _targets(row["targets"])
    for prefix in ("http://", "https://", "mailto:"):
        for t in ts:
            if t.lower().startswith(prefix):
                return t
    return None


def _host(row) -> str:
    for t in _targets(row["targets"]):
        if t.lower().startswith(("http://", "https://")):
            return urlparse(t).hostname or ""
    return ""


class Service:
    def __init__(self, cfg, io, db_path, *, demo: bool = False):
        self.cfg = cfg
        self.io = io
        self.db_path = db_path
        self.demo = demo
        self._lock = threading.Lock()
        self._llm: tuple[float, bool] = (0.0, False)

    @contextmanager
    def store(self):
        s = Store(self.db_path)
        try:
            yield s
        finally:
            s.close()

    # ------------------------------------------------------------------ reads
    def _llm_up(self) -> bool:
        at, up = self._llm
        if time.monotonic() - at > 30:        # ollama_available() can block up to 3s
            up = self.io.ollama_up()
            self._llm = (time.monotonic(), up)
        return up

    def summary(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        with self.store() as s:
            q = lambda sql, *a: s.conn.execute(sql, a).fetchone()[0]
            out = {
                "unsub": q("SELECT COUNT(*) FROM unsub_queue WHERE status='suggested'"),
                "events": q("SELECT COUNT(*) FROM events WHERE status='new' AND start >= ?", today),
                "events_past": q("SELECT COUNT(*) FROM events WHERE status='new' AND start < ?", today),
                "spam": q("SELECT COUNT(*) FROM spam_review WHERE status='suggested'"),
                "reviews": q("SELECT COUNT(*) FROM purchases WHERE status='ready'"),
                "last_scan": q("SELECT MAX(processed_at) FROM messages"),
            }
            out["unsub_failed"] = q("SELECT COUNT(*) FROM unsub_queue WHERE status='approved' AND opened_at IS NULL")
            out["unsub_awaiting"] = q("SELECT COUNT(*) FROM unsub_queue WHERE status='approved' AND opened_at IS NOT NULL")
            out["unsub_repeat"] = len(s.unsub_still_sending())
            out["forged"] = len(s.forged_recent())
        out["unsub_todo"] = out["unsub"] + out["unsub_failed"] + out["unsub_awaiting"] + out["unsub_repeat"]
        out["llm"] = {"up": self._llm_up(), "model": self.cfg.model.name}
        out["unsub_mode"] = self.cfg.unsubscribe.mode
        out["demo"] = self.demo
        return out

    def digest(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        with self.store() as s:
            unsubs = s.unsub_by_status("suggested")
            events = [e for e in s.events_by_status("new") if e["start"] >= today]
            spam = s.spam_by_status("suggested")
            noise = s.noise_stats(days=30)[:8]
            important = s.important_recent(days=7)
            mix = s.category_counts(days=7)
            forged = s.forged_recent()
        return {
            "summary": self.summary(),
            "forged": {"total": len(forged), "top": [
                {"sender": f["sender"], "subject": f["subject"], "date": f["date_received"]} for f in forged[:5]]},
            "mix": mix,
            "unsub": {"total": len(unsubs), "top": [
                {"sender": u["sender"], "sender_email": u["sender_email"],
                 "msg_count": u["msg_count"], "one_click": bool(u["one_click"])}
                for u in unsubs[:5]]},
            "events": {"total": len(events), "next": [
                {k: e[k] for k in ("id", "title", "start", "location", "confidence",
                                   "source_sender", "all_day")}
                for e in events[:8]]},
            "attention": [_row(m) for m in important[:10]],
            "spam": {"total": len(spam), "phishing": sum(1 for x in spam if x["phishing_risk"]),
                     "top": [{"sender": x["sender"], "subject": x["subject"],
                              "phishing_risk": bool(x["phishing_risk"])} for x in spam[:5]]},
            "noise": [{"sender": r["sender"], "n": r["n"], "category": r["category"]}
                      for r in noise],
        }

    def _link_peers(self, s: Store, r) -> list[dict]:
        return unsubmod.link_peers(s, r, self.cfg.unsubscribe)

    def _blocked_by(self, s: Store, r) -> str | None:
        return unsubmod.blocked_by(s, r, self.cfg.unsubscribe)

    def unsub_list(self) -> dict:
        mode = self.cfg.unsubscribe.mode
        with self.store() as s:
            rows = s.unsub_by_status("suggested")
            approved = s.unsub_by_status("approved")
            failed_rows = [r for r in approved if not r["opened_at"]]
            awaiting_rows = sorted((r for r in approved if r["opened_at"]),
                                   key=lambda r: r["opened_at"], reverse=True)
            repeat = s.unsub_still_sending()
            prior = s.unsubscribed_domains()
            repeat_rows = {d["sender_email"]: s.unsub_row(d["sender_email"]) for d in repeat[:MAX_ROWS]}
            shown = rows[:MAX_ROWS] + failed_rows[:MAX_ROWS] + awaiting_rows[:MAX_ROWS] + list(repeat_rows.values())
            peers = {r["sender_email"]: self._link_peers(s, r) for r in shown}

        def link_info(email: str) -> dict:
            ps = peers[email]
            return {"peers": ps, "blocked_by": next((p["sender_email"] for p in ps if p["protected"]), None),
                    "link_conflict": any(not p["same_org"] for p in ps)}

        def shape(r) -> dict:
            d = _row(r)
            d.pop("targets", None)
            d["one_click"] = bool(d["one_click"])
            d["method"] = approval_method(r, mode)
            d["host"] = _host(r)
            d["can_open"] = _openable(r) is not None
            d["last_note"] = _redact(d.get("last_note"))
            d.update(link_info(r["sender_email"]))
            return d

        items = []
        for r in rows[:MAX_ROWS]:
            d = shape(r)
            other = prior.get(_domain(r["sender_email"]))
            if other and other[0] != r["sender_email"]:     # a different address, same domain
                d["prior"] = {"sender_email": other[0], "when": other[1]}
            items.append(d)
        failed = [shape(r) for r in failed_rows[:MAX_ROWS]]
        awaiting = [shape(r) for r in awaiting_rows[:MAX_ROWS]]
        repeat_items = repeat[:MAX_ROWS]
        for d in repeat_items:
            r = repeat_rows[d["sender_email"]]
            d["method"] = approval_method(r, mode)
            d["host"] = _host(r)
            d["can_open"] = _openable(r) is not None
            d["last_note"] = _redact(r["last_note"])
            d["auth"], d["auth_detail"] = r["auth"], r["auth_detail"]
            d.update(link_info(d["sender_email"]))
        return {"items": items, "total": len(rows), "mode": mode,
                "failed": failed, "failed_total": len(failed_rows),
                "awaiting": awaiting, "awaiting_total": len(awaiting_rows),
                "repeat": repeat_items, "repeat_total": len(repeat)}

    def events_list(self, include_past: bool = False) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        with self.store() as s:
            rows = s.events_by_status("new")
            items, hidden = [], 0
            for r in rows:
                if r["start"] < today and not include_past:
                    hidden += 1
                    continue
                ref = s.message_ref(r["source_message_id"])
                d = _row(r)
                d["inbox"] = ref["account"] if ref else None
                d["to"] = (ref["to_addr"] if ref else None) or None
                d["can_move"] = bool(ref and ref["mail_id"] is not None)
                items.append(d)
        return {"items": items[:MAX_ROWS], "total": len(items), "hidden_past": hidden}

    def spam_list(self) -> dict:
        with self.store() as s:
            rows = s.spam_by_status("suggested")
        items = []
        for r in rows[:MAX_ROWS]:
            d = _row(r)
            d["phishing_risk"] = bool(d["phishing_risk"])
            items.append(d)
        return {"items": items, "total": len(rows)}

    def leaderboard(self, days: int = 30) -> dict:
        days = max(1, min(int(days), 365))
        with self.store() as s:
            rows = s.noise_stats(days=days)[:25]
            items = []
            for r in rows:
                email = r["sender_email"]
                u = s.unsub_row(email)
                items.append({
                    "sender": r["sender"], "sender_email": email, "account": r["account"],
                    "category": r["category"], "n": r["n"],
                    "last_subject": r["last_subject"], "last_received": r["last_received"],
                    "movable": len(s.messages_by_sender(email)),
                    "can_unsub": bool(u and u["status"] == "suggested" and _targets(u["targets"])),
                })
        return {"items": items, "days": days}

    def reviews_list(self, status: str = "ready") -> dict:
        if status not in PURCHASE_STATUSES:
            raise ApiError(400, f"unknown status '{status}'")
        with self.store() as s:
            rows = s.purchases_by_status(status)
            counts = {r["status"]: r["n"] for r in s.conn.execute(
                "SELECT status, COUNT(*) AS n FROM purchases GROUP BY status")}
        return {"items": [_row(r) for r in rows[:MAX_ROWS]], "total": len(rows),
                "status": status, "counts": counts}

    # ---------------------------------------------------------------- actions
    @staticmethod
    def _one_of(value, allowed, what="action"):
        if value not in allowed:
            raise ApiError(400, f"unknown {what} '{value}'")
        return value

    def _move_sender(self, s: Store, sender_email: str, dest: str) -> dict:
        refs = s.messages_by_sender(sender_email)
        if not refs:
            return {"ok": False, "note": "no scanned messages on file for this sender to move"}
        targets = [{"account": x["account"], "mailbox": "INBOX", "id": x["mail_id"]} for x in refs]
        try:
            res = self.io.move_messages(targets, dest)
        except bridge.BridgeError as e:
            return {"ok": False, "note": f"failed: {_explain_move_error(str(e))}"}
        moved, total = res.get("moved", 0), res.get("total", 0)
        out = {"ok": moved > 0, "note": f"moved {moved}/{total} message(s) to {dest}"}
        errs = [_explain_move_error(e) for e in (res.get("errors") or [])]
        if errs:
            out["errors"] = errs[:5]
        return out

    # which queue statuses each action may start from; anything else is reported, not done
    _UNSUB_FROM = {
        "approve": {"suggested"}, "skip": {"suggested", "approved"}, "protect": {"suggested", "approved"},
        "retry": {"approved", "done"}, "markdone": {"approved"}, "ack": {"done"},
    }

    def _attempt(self, s: Store, r, email: str, tabs: list[int]) -> dict:
        """Try the unsubscribe for one queue row (POST, then open the page if that fails)
        and record what happened. `tabs` is a one-item list holding how many more pages
        this action may open, so a big batch can't bury the browser in tabs."""
        if unsubmod.is_protected(email, self.cfg.unsubscribe):
            s.set_unsub_status(email, "protected")
            return {"ok": False, "state": "failed", "note": "sender is on your protected list -- nothing sent"}
        peer = self._blocked_by(s, r)
        if peer:                    # acting on this link would unsubscribe you from a sender you protected
            return {"ok": False, "state": "blocked", "note": unsubmod.blocked_note(peer)}
        try:
            state, note = self.io.unsub(r["targets"], bool(r["one_click"]), email, allow_open=tabs[0] > 0)
        except Exception as e:      # never leave the batch half-recorded
            state, note = "failed", f"failed: {e.__class__.__name__}: {e}"
        if state == "not_opened":   # nothing was tried; leave the row exactly as it was
            return {"ok": False, "state": state, "note": note}
        if state == "opened":
            tabs[0] -= 1
        s.record_unsub_attempt(email, state, note)
        return {"ok": state in ("done", "opened"), "state": state, "note": note}

    def unsub_act(self, action: str, emails) -> dict:
        self._one_of(action, ("approve", "skip", "protect", "trash", "archive",
                              "retry", "open", "markdone", "ack"))
        emails = list(dict.fromkeys(e for e in (emails or []) if isinstance(e, str) and e))
        if not emails:
            raise ApiError(400, "no senders given")
        if len(emails) > MAX_BATCH:
            raise ApiError(400, f"at most {MAX_BATCH} senders per request")
        results = []
        tabs = [MAX_TABS]
        with self._lock, self.store() as s:
            for email in emails:
                r = s.unsub_row(email)
                if r is None:
                    results.append({"id": email, "ok": False, "note": "not in the queue"})
                    continue
                if action in MOVE_DEST:
                    results.append({"id": email, **self._move_sender(s, email, MOVE_DEST[action])})
                    continue
                if action == "open":
                    url = _openable(r)
                    if url is None:
                        results.append({"id": email, "ok": False, "note": "no usable unsubscribe link on file"})
                        continue
                    peer = self._blocked_by(s, r)
                    if peer:
                        results.append({"id": email, "ok": False, "state": "blocked", "note": unsubmod.blocked_note(peer)})
                        continue
                    try:
                        self.io.open_url(url)
                        where = _host(r) or "mail compose"
                        if r["status"] == "approved":     # a failed/pending row: now waiting on the user
                            s.mark_unsub_opened(email, f"opened {where} by you")
                            s.commit()
                        results.append({"id": email, "ok": True, "state": "opened",
                                        "note": f"opened {where} -- come back and confirm"})
                    except Exception as e:
                        results.append({"id": email, "ok": False, "note": f"couldn't open it: {e}"})
                    continue
                if r["status"] not in self._UNSUB_FROM[action]:
                    results.append({"id": email, "ok": False,
                                    "note": f"already {r['status']} -- nothing done"})
                    continue
                if action in ("approve", "retry"):
                    results.append({"id": email, **self._attempt(s, r, email, tabs)})
                elif action == "skip":
                    s.set_unsub_status(email, "skipped")
                    results.append({"id": email, "ok": True, "note": "skipped"})
                elif action == "protect":
                    s.set_unsub_status(email, "protected")
                    results.append({"id": email, "ok": True, "note": "protected -- won't be suggested again"})
                elif action == "markdone":
                    s.confirm_unsub(email, "confirmed done by you" if r["opened_at"] else "marked done by hand")
                    results.append({"id": email, "ok": True, "note": "marked done"})
                else:  # ack
                    s.ack_unsub(email)
                    results.append({"id": email, "ok": True,
                                    "note": "ignored -- it'll only be flagged again if it sends more"})
                s.commit()
        for r in results:
            r["note"] = _redact(r.get("note"))
        return {"results": results}

    def events_act(self, action: str, event_id, move: str | None = None) -> dict:
        self._one_of(action, ("add", "dismiss", "protect", "trash", "archive"))
        if not isinstance(event_id, int):
            raise ApiError(400, "event id must be an integer")
        with self._lock, self.store() as s:
            r = s.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if r is None or r["status"] != "new":
                return {"results": [{"id": event_id, "ok": False, "note": "not pending (already handled)"}]}
            if action == "add":
                start = r["start"]
                if not start:
                    res = {"ok": False, "note": "no start time on this candidate -- dismiss it or add it by hand"}
                else:
                    all_day = bool(r["all_day"]) or len(start) <= 10
                    cal = self.cfg.calendar.target_calendar
                    try:
                        self.io.create_event(cal, r["title"], start, r["end"], r["location"], all_day)
                        s.set_event_status(event_id, "created")
                        res = {"ok": True, "note": f"added to '{cal}'"}
                    except bridge.BridgeError as e:
                        res = {"ok": False, "note": f"failed: {e}"}
            elif action == "dismiss":
                s.set_event_status(event_id, "dismissed")
                res = {"ok": True, "note": "dismissed"}
            elif action == "protect":
                email = r["source_sender_email"]
                if not email:
                    s.set_event_status(event_id, "dismissed")
                    res = {"ok": True, "note": "couldn't parse a sender address; dismissed this one only"}
                else:
                    ids = s.protect_event_sender(email)
                    res = {"ok": True, "note": f"{email} won't be suggested as an event source again "
                                               f"({len(ids)} pending candidate(s) dismissed)"}
                    if move in ("Trash", "Archive"):
                        mv = self._move_sender(s, email, move)
                        res["note"] += f"; {mv['note']}"
            else:  # trash / archive the source email
                ctx = s.message_ref(r["source_message_id"])
                if not ctx or ctx["mail_id"] is None:
                    res = {"ok": False, "note": "source message not on file (predates trash/archive "
                                                "support, or aged out) -- can't move"}
                else:
                    dest = MOVE_DEST[action]
                    try:
                        self.io.move_message(ctx["account"], "INBOX", ctx["mail_id"], dest)
                        s.set_event_status(event_id, "dismissed")
                        res = {"ok": True, "note": f"source email moved to {dest}; candidate dismissed"}
                    except bridge.BridgeError as e:
                        res = {"ok": False, "note": f"failed: {_explain_move_error(str(e))}"}
            s.commit()
        return {"results": [{"id": event_id, **res}]}

    def spam_act(self, action: str, message_id) -> dict:
        self._one_of(action, ("rescue", "trust", "leave", "protect"))
        if not isinstance(message_id, str) or not message_id:
            raise ApiError(400, "message id required")
        with self._lock, self.store() as s:
            r = s.conn.execute("SELECT * FROM spam_review WHERE message_id=?", (message_id,)).fetchone()
            if r is None or r["status"] != "suggested":
                return {"results": [{"id": message_id, "ok": False, "note": "not pending (already handled)"}]}
            if action == "leave":
                s.set_spam_status(message_id, "left")
                res = {"ok": True, "note": "left in spam"}
            elif action == "protect":
                s.set_spam_status(message_id, "protected")
                res = {"ok": True, "note": "confirmed junk -- sender won't be flagged again"}
            else:
                try:
                    self.io.move_message(r["account"], r["mailbox"], r["mail_id"], "INBOX")
                    s.set_spam_status(message_id, "rescued")
                    res = {"ok": True, "note": "moved to Inbox"}
                    if action == "trust":
                        s.trust_spam_sender(r["sender_email"])
                        res["note"] += f"; {r['sender_email']} will always be rescued from now on"
                except bridge.BridgeError as e:
                    res = {"ok": False, "note": f"failed: {_explain_move_error(str(e))}"}
            s.commit()
        return {"results": [{"id": message_id, **res}]}

    def leaderboard_act(self, action: str, sender_email, and_unsub: bool = False) -> dict:
        self._one_of(action, ("trash", "junk", "archive"))
        if not isinstance(sender_email, str) or not sender_email:
            raise ApiError(400, "sender required")
        results = []
        with self._lock, self.store() as s:
            results.append({"id": sender_email, **self._move_sender(s, sender_email, MOVE_DEST[action])})
            if and_unsub:
                u = s.unsub_row(sender_email)
                if not (u and u["status"] == "suggested" and _targets(u["targets"])):
                    results.append({"id": sender_email, "ok": False, "note": "no unsubscribe target pending"})
                else:
                    results.append({"id": sender_email, **self._attempt(s, u, sender_email, [MAX_TABS])})
            s.commit()
        return {"results": results}

    def reviews_act(self, action: str, key, text: str | None = None) -> dict:
        new_status = {"approve": "approved", "skip": "skipped", "notreceived": "pending_receipt",
                      "exclude": "returned_excluded"}[self._one_of(action, ("approve", "skip", "notreceived", "exclude"))]
        if not isinstance(key, str) or not key:
            raise ApiError(400, "key required")
        if text is not None:
            if not isinstance(text, str) or len(text) > 4000:
                raise ApiError(400, "review text must be a string under 4000 characters")
            text = text.strip() or None
        with self._lock, self.store() as s:
            if s.conn.execute("SELECT 1 FROM purchases WHERE key=?", (key,)).fetchone() is None:
                return {"results": [{"id": key, "ok": False, "note": "not found"}]}
            s.set_purchase_status(key, new_status, review_text=text if action == "approve" else None)
            s.commit()
        return {"results": [{"id": key, "ok": True, "note": new_status.replace("_", " ")}]}

    def reviews_export(self) -> dict:
        with self._lock, self.store() as s:
            rows = s.purchases_by_status("approved")
        if not rows:
            return {"results": [{"id": "export", "ok": False, "note": "no approved reviews to export"}]}
        path = self.cfg.digest_dir / f"purchase_reviews_{datetime.now():%Y-%m-%d}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["vendor", "item", "order_ref", "review_text", "status"])
            for r in rows:
                w.writerow([r["vendor"], r["item"], r["order_ref"] or "", r["review_text"] or "", r["status"]])
        return {"results": [{"id": "export", "ok": True, "note": f"exported {len(rows)} review(s) to {path}"}]}
