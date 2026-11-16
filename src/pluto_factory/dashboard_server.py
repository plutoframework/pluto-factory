"""Loopback HTTP boundary for the local factory dashboard.

Everything is read-only except one narrow route: POST
``/api/factories/<id>/decisions/<decision>/answer`` records an owner's answer
in the factory ledger. The server never runs git, gh or workers; the
coordinator acts on the answer. The server only binds 127.0.0.1. Optional tailnet access goes through a
foreground ``tailscale serve`` proxy (see ``tailnet``)."""

import hmac
import json
import re
import secrets
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from .attention import AttentionError, DecisionConflict
from .dashboard_data import DashboardData
from .tailnet import (
    TailnetAccess,
    TailnetProxy,
    discover,
    unwind_on_termination,
)
from .visibility import VisibilityError

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/fonts/manrope.ttf": ("fonts/manrope.ttf", "font/ttf"),
    "/fonts/bricolage-grotesque.ttf": (
        "fonts/bricolage-grotesque.ttf",
        "font/ttf",
    ),
    "/fonts/ibm-plex-mono.ttf": ("fonts/ibm-plex-mono.ttf", "font/ttf"),
}
PAGES = {"/", "/index.html"}
DETAIL = re.compile(
    r"^/api/factories/([a-f0-9]{20})/work/([A-Za-z0-9_-]{1,100})(/terminal)?$"
)
ANSWER = re.compile(
    r"^/api/factories/([a-f0-9]{20})/decisions/(decision-[a-f0-9]{12})/answer$"
)
MAX_RESPONSE = 8 * 1024 * 1024
MAX_BODY = 16 * 1024
CSRF_HEADER = "X-Pluto-CSRF"


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        data: DashboardData,
        port=8765,
        tailnet: TailnetAccess | None = None,
    ):
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("dashboard port must be between 0 and 65535")
        self.data = data
        self.tailnet = tailnet
        # Per-process secret, delivered with /api/state, required on writes.
        self.csrf = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "PlutoDashboard"
    sys_version = ""

    def log_message(self, _format, *_arguments):
        # Do not log URLs or browser-supplied request strings.
        pass

    def _refusal(self):
        """Why this request is refused, or None when it is trusted.

        Reasons name the failed check and the header values involved (never
        the URL), so a refused browser can be diagnosed.
        """
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            return "exactly one Host header is required"
        host = values[0]
        scheme = "http://"
        origin_host = host
        tailnet = self.server.tailnet
        if host not in hosts:
            # Tailnet requests arrive through tailscale serve, which names
            # the requesting tailnet user. Only allowed logins get through.
            if tailnet is None:
                return f"host {host!r} is not loopback"
            if host not in tailnet.hosts:
                expected = ", ".join(sorted(tailnet.hosts))
                return f"host {host!r} is not this tailnet name ({expected})"
            logins = self.headers.get_all("Tailscale-User-Login", [])
            if len(logins) != 1:
                return (
                    "no single Tailscale-User-Login (tagged or shared devices "
                    "carry no user identity)"
                )
            if logins[0] not in tailnet.logins:
                return (
                    f"tailnet login {logins[0]!r} is not allowed; restart "
                    f"with --tailscale-allow {logins[0]}"
                )
            scheme = "https://"
            if tailnet.https_port == 443:
                origin_host = tailnet.dns_name
        self._expected_origin = scheme + origin_host
        # Only a tailnet-proxied request vouches for a user identity.
        self._login = (
            self.headers.get("Tailscale-User-Login")
            if host not in hosts
            else None
        )
        origins = self.headers.get_all("Origin", [])
        if origins and (
            len(origins) != 1 or origins[0] != scheme + origin_host
        ):
            return f"origin {origins!r} does not match {scheme + origin_host}"
        if (
            self.headers.get("Sec-Fetch-Site") == "cross-site"
            and not self._page_navigation()
        ):
            return "cross-site request"
        return None

    def _page_navigation(self):
        """A top-level visit to the dashboard page itself, e.g. from a link
        in another app. Its data requests are then same-origin; cross-site
        fetches, embeds and links to API or terminal routes stay refused."""
        return (
            self.headers.get("Sec-Fetch-Mode") == "navigate"
            and self.headers.get("Sec-Fetch-Dest") == "document"
            and self.path in PAGES
        )

    def _send(
        self, status, content, content_type="application/json; charset=utf-8"
    ):
        if len(content) > MAX_RESPONSE:
            status, content = (
                503,
                b'{"error":"Dashboard response limit exceeded."}',
            )
            content_type = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, value):
        encoded = (
            json.dumps(value, ensure_ascii=True, allow_nan=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .encode()
        )
        self._send(status, encoded)

    def do_GET(self):
        refusal = self._refusal()
        if refusal:
            print(f"dashboard: refused request: {refusal}", file=sys.stderr)
            return self._json(
                403,
                {
                    "error": "Loopback or allowed tailnet same-origin "
                    f"requests are required: {refusal}."
                },
            )
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return self._json(404, {"error": "Unknown dashboard route."})
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return self._json(404, {"error": "Unknown dashboard route."})
        path = parsed.path
        try:
            if path == "/api/state":
                return self._json(
                    200, {**self.server.data.state(), "csrf": self.server.csrf}
                )
            match = DETAIL.fullmatch(path)
            if match:
                factory, work, terminal = match.groups()
                operation = (
                    self.server.data.terminal
                    if terminal
                    else self.server.data.detail
                )
                return self._json(200, operation(factory, work))
            if path in STATIC:
                filename, content_type = STATIC[path]
                resource = files("pluto_factory").joinpath(
                    "dashboard", filename
                )
                if not resource.is_file():
                    return self._json(
                        503, {"error": "Dashboard assets are not installed."}
                    )
                return self._send(200, resource.read_bytes(), content_type)
            return self._json(404, {"error": "Unknown dashboard route."})
        except KeyError:
            return self._json(
                404, {"error": "Factory or work item was not found."}
            )
        except VisibilityError as error:
            return self._json(409, {"error": str(error)})
        except (OSError, ValueError, RuntimeError, TypeError, sqlite3.Error):
            return self._json(
                503, {"error": "Factory data is temporarily unavailable."}
            )

    def _unsupported(self):
        self._json(
            405,
            {
                "error": "The dashboard supports GET requests and decision "
                "answers only."
            },
        )
        self.close_connection = True

    def _reject(self, status, message):
        self._json(status, {"error": message})
        self.close_connection = True

    def _write_refusal(self):
        """Extra checks every state-changing request must pass."""
        origins = self.headers.get_all("Origin", [])
        if len(origins) != 1 or origins[0] != self._expected_origin:
            return 403, "A matching Origin header is required."
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            return 403, "Same-origin requests are required."
        token = self.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(token.encode(), self.server.csrf.encode()):
            return 403, "A valid CSRF token is required."
        kind = self.headers.get("Content-Type", "").split(";")[0].strip()
        if kind.lower() != "application/json":
            return 415, "Content-Type must be application/json."
        if self.headers.get("Transfer-Encoding"):
            return 411, "A Content-Length body is required."
        return None

    def do_POST(self):
        refusal = self._refusal()
        if refusal:
            print(f"dashboard: refused request: {refusal}", file=sys.stderr)
            return self._reject(
                403,
                "Loopback or allowed tailnet same-origin requests are "
                f"required: {refusal}.",
            )
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            return self._reject(404, "Unknown dashboard route.")
        match = (
            None
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            else ANSWER.fullmatch(parsed.path)
        )
        if not match:
            return self._unsupported()
        refused = self._write_refusal()
        if refused:
            return self._reject(*refused)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reject(411, "A Content-Length body is required.")
        if length < 1:
            return self._reject(400, "A JSON body is required.")
        if length > MAX_BODY:
            return self._reject(413, "The request body is too large.")
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self._reject(400, "The body must be valid JSON.")
        if (
            not isinstance(body, dict)
            or set(body) - {"answer", "note"}
            or not isinstance(body.get("answer"), str)
            or not isinstance(body.get("note", ""), str)
        ):
            return self._reject(
                400, "Send an object with a text 'answer' and optional 'note'."
            )
        factory, decision = match.groups()
        try:
            result = self.server.data.answer(
                factory,
                decision,
                body["answer"],
                body.get("note", ""),
                by=self._login or "owner",
            )
        except KeyError:
            return self._reject(404, "Factory or decision was not found.")
        except AttentionError as error:
            return self._reject(400, str(error))
        except DecisionConflict as error:
            return self._reject(409, str(error))
        except (OSError, RuntimeError, TypeError, sqlite3.Error):
            return self._reject(503, "Factory data is temporarily unavailable.")
        self._json(
            200,
            {"id": result["id"], "state": result["state"]},
        )
        self.close_connection = True

    do_PUT = _unsupported
    do_PATCH = _unsupported
    do_DELETE = _unsupported
    do_HEAD = _unsupported
    do_OPTIONS = _unsupported


def serve(
    root: Path,
    *,
    extra_home: Path | None = None,
    port=8765,
    tailscale: bool = False,
    tailscale_port: int = 443,
    tailscale_allow: list[str] | None = None,
):
    if not 1 <= port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    access = (
        discover(tailscale_port, tailscale_allow or []) if tailscale else None
    )
    data = DashboardData(root, extra_home)
    with (
        unwind_on_termination(),
        DashboardServer(data, port, tailnet=access) as server,
    ):
        proxy = None
        try:
            if access:
                proxy = TailnetProxy(access, server.server_address[1])
                proxy.start()
            print(
                f"Factory dashboard: http://127.0.0.1:{server.server_address[1]}",
                file=sys.stderr,
                flush=True,
            )
            if access:
                print(
                    f"Tailnet: {access.url} (allowed: "
                    f"{', '.join(sorted(access.logins))})",
                    file=sys.stderr,
                    flush=True,
                )
            print(
                "Read-only except answering decisions. Press Ctrl-C to stop.",
                file=sys.stderr,
                flush=True,
            )
            server.serve_forever(poll_interval=0.25)
        finally:
            if proxy:
                proxy.stop()
    return {"stopped": True}
