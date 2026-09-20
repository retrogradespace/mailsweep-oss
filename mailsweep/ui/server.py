"""HTTP front for the web UI. Stdlib only; binds 127.0.0.1 and nothing else.

A page on the open web can talk to localhost too, so the server doesn't rely
on the bind address alone:
  * Host must be exactly 127.0.0.1:<port> or localhost:<port>  (DNS rebinding)
  * every /api call needs a per-launch token, which only the served page knows
  * POSTs must be JSON, and any Origin header must be our own
  * a strict CSP: no external scripts/styles/fonts/images, no inline script
"""
from __future__ import annotations

import hmac
import json
import secrets
import shutil
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import ApiError, Service

STATIC = Path(__file__).parent / "static"
TYPES = {"/app.css": "text/css; charset=utf-8", "/app.js": "text/javascript; charset=utf-8"}
MAX_BODY = 64 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _int(q: dict, key: str, default: int) -> int:
    try:
        return int(q.get(key, [default])[0])
    except (TypeError, ValueError):
        raise ApiError(400, f"'{key}' must be an integer")


def make_routes(svc: Service):
    """(method, path) -> callable(query, body) -> JSON-able."""
    return {
        ("GET", "/api/summary"): lambda q, b: svc.summary(),
        ("GET", "/api/digest"): lambda q, b: svc.digest(),
        ("GET", "/api/unsub"): lambda q, b: svc.unsub_list(),
        ("GET", "/api/events"): lambda q, b: svc.events_list(_int(q, "past", 0) == 1),
        ("GET", "/api/spam"): lambda q, b: svc.spam_list(),
        ("GET", "/api/leaderboard"): lambda q, b: svc.leaderboard(_int(q, "days", 30)),
        ("GET", "/api/reviews"): lambda q, b: svc.reviews_list(q.get("status", ["ready"])[0]),
        ("POST", "/api/unsub"): lambda q, b: svc.unsub_act(b.get("action"), b.get("emails")),
        ("POST", "/api/events"): lambda q, b: svc.events_act(b.get("action"), b.get("id"), b.get("move")),
        ("POST", "/api/spam"): lambda q, b: svc.spam_act(b.get("action"), b.get("id")),
        ("POST", "/api/leaderboard"): lambda q, b: svc.leaderboard_act(
            b.get("action"), b.get("sender_email"), bool(b.get("unsub"))),
        ("POST", "/api/reviews"): lambda q, b: svc.reviews_act(b.get("action"), b.get("key"), b.get("text")),
        ("POST", "/api/reviews/export"): lambda q, b: svc.reviews_export(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MailSweepUI"
    sys_version = ""

    # -- plumbing --------------------------------------------------------
    def log_message(self, fmt, *args):      # method + path only; never bodies
        sys.stderr.write(f"  {self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "unexpected Host header"})
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            return self._api(method, url.path, parse_qs(url.query))
        if method != "GET":
            return self._json(405, {"error": "method not allowed"})
        if url.path in ("/", "/index.html"):
            page = (STATIC / "index.html").read_text(encoding="utf-8")
            return self._send(200, page.replace("__TOKEN__", self.server.token).encode(),
                              "text/html; charset=utf-8")
        if url.path in TYPES:
            return self._send(200, (STATIC / url.path.lstrip("/")).read_bytes(), TYPES[url.path])
        self._json(404, {"error": "not found"})

    def _api(self, method: str, path: str, query: dict) -> None:
        if not hmac.compare_digest(self.headers.get("X-MailSweep-Token", ""), self.server.token):
            return self._json(403, {"error": "missing or bad token -- reload the page"})
        route = self.server.routes.get((method, path))
        if route is None:
            return self._json(404, {"error": "no such endpoint"})
        body = {}
        if method == "POST":
            origin = self.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] not in self.server.allowed_hosts:
                return self._json(403, {"error": "cross-origin request refused"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "JSON body required"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._json(400, {"error": "bad Content-Length"})
            if n > MAX_BODY:
                return self._json(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "body must be a JSON object"})
        try:
            self._json(200, route(query, body))
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except sqlite3.OperationalError as e:
            self._json(503, {"error": f"database busy or unavailable ({e}) -- is a scan running?"})
        except Exception as e:                      # local tool: say what broke
            self._json(500, {"error": f"{e.__class__.__name__}: {e}"})


def make_server(svc: Service, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual = httpd.server_address[1]
    httpd.allowed_hosts = {f"127.0.0.1:{actual}", f"localhost:{actual}"}
    httpd.token = secrets.token_urlsafe(24)
    httpd.routes = make_routes(svc)
    httpd.daemon_threads = True
    return httpd


def serve(svc: Service, port: int = 8765, open_browser: bool = True, cleanup: Path | None = None) -> int:
    try:
        httpd = make_server(svc, port)
    except OSError as e:
        print(f"Can't listen on 127.0.0.1:{port}: {e}. Try --port <other>.", file=sys.stderr)
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
        return 1
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"MailSweep UI{' (DEMO -- sample data, nothing touches Mail/Calendar/network)' if svc.demo else ''}")
    print(f"  {url}   (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    return 0
