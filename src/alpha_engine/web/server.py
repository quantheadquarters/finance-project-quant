"""The web server: three sections and one API.

    python -m web.server                    # http://127.0.0.1:8000

Three sections, deliberately separate because they answer different questions:

    /research    dated cached evidence and user-strategy replays; Apple first.
    /dashboard   what the engine has already decided — the recorded signal log,
                 hit rates, regime and risk. Read-only, no keys, no AI.
    /terminal    a chat where an AI drives the engine's tools for you. You bring
                 your own OpenAI/Anthropic/OpenRouter key; it is used for the
                 request and never stored.

And one API, so this repo is usable without either page:

    GET  /api/v1/tools              self-describing tool catalogue
    GET  /api/v1/tools/<name>?...   call a tool with query params
    POST /api/v1/tools/<name>       call a tool with a JSON body
    POST /api/v1/mcp                MCP JSON-RPC over HTTP (remote MCP clients)
    POST /api/v1/agent              one AI terminal turn (caller supplies the key)
    GET  /api/v1/providers          which LLM providers the terminal accepts

Every route above dispatches into `alpha_engine.toolkit`, the same table the
stdio MCP server uses, so the two can never disagree about what a tool does.

Policy — auth, rate limits, the write gate — lives in `web/api.py` so it can be
tested without a socket. This file is sockets, routing and headers.

Still no framework. `ThreadingHTTPServer` plus a routing table is ~200 lines for
what Flask would do in 80, and those 120 lines cost less than a dependency tree
in a repo whose entire pitch is that you can read it and re-run it yourself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from alpha_engine.dashboard.service import build_asset_history, build_dashboard_payload
from alpha_engine.web.api import (
    MAX_BODY_BYTES,
    ApiConfig,
    ApiState,
    RequestLog,
    authorize,
    catalogue,
    coerce_query_args,
    dispatch_mcp,
    dispatch_tool,
)

STATIC_ROOT = Path(__file__).resolve().parent / "static"

# Asset symbols are short tickers (BTC, AAPL, NIFTY, RELIANCE.NS). Anything
# else in the URL segment is rejected before touching the filesystem or log.
_ASSET_RE = re.compile(r"^[A-Za-z0-9._&-]{1,24}$")
_TOOL_RE = re.compile(r"^[a-z_]{1,40}$")

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

#: Pages, so a URL like /terminal serves a file without exposing the layout.
_PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/dashboard": "dashboard.html",
    "/research": "research.html",
    "/terminal": "terminal.html",
}


class AppHandler(BaseHTTPRequestHandler):
    server_version = "AlphaEngine/1.0"
    protocol_version = "HTTP/1.1"

    #: Set on the class at startup by `main`.
    state: ApiState = ApiState()

    # -- headers ------------------------------------------------------------

    def _send_security_headers(self, json_response: bool = False) -> None:
        """Baseline hardening: no MIME sniffing, no framing, and scripts/styles
        only from this origin (the frontend is fully self-hosted, so a strict
        CSP costs nothing).

        `connect-src 'self'` matters more than it looks: it means a script
        injected into one of these pages cannot POST the user's pasted API key
        to an attacker's host.
        """
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'none'",
        )
        if json_response:
            # A pasted key must never be cached by a proxy or the browser.
            self.send_header("Cache-Control", "no-store")
        origin = self.state.config.cors_origin
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def handle_one_request(self) -> None:
        """Stamp a start time so the response can report its own duration."""
        self._started = time.time()
        super().handle_one_request()

    def log_message(self, format: str, *args) -> None:
        """Silence stdlib's access log.

        Not noise reduction: stdlib's logger is one careless change away from
        printing a body, and `POST /api/v1/agent` carries the caller's API key in
        its. Structured logging goes through `ApiState.log`, which can only see a
        route template and a status — see `web/api.py::RequestLog`.
        """
        return

    def _log(self, status: int, size: int) -> None:
        self.state.log.write(
            self.command or "?",
            self.path,
            status,
            getattr(self, "_started", time.time()),
            self._client(),
            size,
        )

    # -- request plumbing ---------------------------------------------------

    def _client(self) -> str:
        return self.client_address[0] if self.client_address else "unknown"

    def _guard(self) -> bool:
        """Auth + rate limit. Replies and returns False when the request must
        not proceed."""
        ok, message = authorize(self.state, self.headers.get("Authorization"))
        if not ok:
            self._send_json({"error": message}, status=HTTPStatus.UNAUTHORIZED)
            return False
        if not self.state.limiter.allow(self._client()):
            self._send_json(
                {
                    "error": "rate limit exceeded",
                    "limit_per_minute": self.state.config.rate_limit_per_min,
                },
                status=HTTPStatus.TOO_MANY_REQUESTS,
            )
            return False
        return True

    def _read_json_body(self) -> tuple[dict[str, Any] | None, str]:
        """Read and parse a JSON body under a hard size cap. Returns
        `(payload, error_message)`."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "invalid Content-Length"
        if length <= 0:
            return {}, ""
        if length > MAX_BODY_BYTES:
            return None, f"request body too large (max {MAX_BODY_BYTES} bytes)"
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            return None, f"invalid JSON body: {e}"
        if not isinstance(payload, dict):
            return None, "body must be a JSON object"
        return payload, ""

    # -- routes -------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_security_headers(json_response=True)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        split = urlsplit(self.path)
        path = unquote(split.path)

        if path == "/api/dashboard":
            self._send_json(build_dashboard_payload())
            return

        if path.startswith("/api/asset/"):
            symbol = path.removeprefix("/api/asset/")
            if not _ASSET_RE.match(symbol):
                self._send_json({"error": "invalid asset symbol"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(build_asset_history(symbol))
            return

        if path == "/api/v1/tools":
            self._send_json(catalogue())
            return

        if path == "/api/v1/providers":
            from alpha_engine.narrative.providers import list_providers

            self._send_json({"providers": list_providers()})
            return

        if path.startswith("/api/v1/tools/"):
            name = path.removeprefix("/api/v1/tools/")
            if not _TOOL_RE.match(name):
                self._send_json({"error": "invalid tool name"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not self._guard():
                return
            args = coerce_query_args(parse_qs(split.query))
            status, payload = dispatch_tool(self.state, name, args)
            self._send_json(payload, status=status)
            return

        if path in _PAGES:
            self._send_static(_PAGES[path])
            return
        if path.startswith("/static/"):
            self._send_static(path.removeprefix("/static/"))
            return

        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)

        if not path.startswith("/api/v1/"):
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        if not self._guard():
            return

        body, error = self._read_json_body()
        if error:
            self._send_json({"error": error}, status=HTTPStatus.BAD_REQUEST)
            return
        assert body is not None

        if path == "/api/v1/mcp":
            response = dispatch_mcp(self.state, body)
            # A JSON-RPC notification gets no reply body, only an accepted status.
            if response is None:
                self.send_response(HTTPStatus.ACCEPTED)
                self._send_security_headers(json_response=True)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(response)
            return

        if path == "/api/v1/agent":
            self._handle_agent(body)
            return

        if path.startswith("/api/v1/tools/"):
            name = path.removeprefix("/api/v1/tools/")
            if not _TOOL_RE.match(name):
                self._send_json({"error": "invalid tool name"}, status=HTTPStatus.BAD_REQUEST)
                return
            status, payload = dispatch_tool(self.state, name, body)
            self._send_json(payload, status=status)
            return

        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def _handle_agent(self, body: dict[str, Any]) -> None:
        """One AI terminal turn.

        The caller's API key arrives in the body, is handed to the provider, and
        is dropped when this function returns. It is never written to disk, never
        put in a log line, and never included in the response.
        """
        from alpha_engine.narrative.agent import ask

        question = (body.get("question") or "").strip()
        api_key = body.get("api_key") or ""
        if not question:
            self._send_json({"error": "question is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not api_key:
            self._send_json(
                {
                    "error": "api_key is required — the terminal uses your own LLM key",
                    "hint": "GET /api/v1/providers lists supported providers and where to get a key",
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        reply = ask(
            question,
            api_key=api_key,
            provider_key=body.get("provider") or "openai",
            model=body.get("model") or None,
            history=body.get("history") or None,
        )
        payload = reply.to_dict()
        # Not a blanket 502. A mistyped provider or model name is the caller's
        # to fix, and answering "Bad Gateway" sends them looking for an outage
        # that is not happening. `error_status` carries the upstream's own
        # classification through; 502 is reserved for a genuine gateway failure.
        self._send_json(payload, status=HTTPStatus.OK if not reply.error else reply.error_status)

    # -- responses ----------------------------------------------------------

    def _send_static(self, relative: str) -> None:
        """Serve one file from STATIC_ROOT, refusing anything that resolves
        outside it (e.g. /static/../../.env traversal attempts)."""
        target = (STATIC_ROOT / relative).resolve()
        if not target.is_relative_to(STATIC_ROOT) or not target.is_file():
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)
        self._log(int(HTTPStatus.OK), len(body))

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers(json_response=True)
        self.end_headers()
        self.wfile.write(body)
        self._log(int(status), len(body))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alpha Engine dashboard, terminal and API.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on")
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="permit tools to append to the signal log (default: read-only)",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=60,
        help="max API requests per minute per client IP (0 disables)",
    )
    parser.add_argument(
        "--cors",
        metavar="ORIGIN",
        default=None,
        help="send CORS headers for this origin (default: none, same-origin only)",
    )
    parser.add_argument(
        "--allow-public",
        action="store_true",
        help="bind a non-loopback address without an API key (containers; see below)",
    )
    parser.add_argument(
        "--log-requests",
        action="store_true",
        help="emit one JSON line per request (route template + status + duration; "
        "never bodies, headers, query values or keys)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config = ApiConfig.from_env(
        allow_writes=args.allow_writes,
        rate_limit_per_min=args.rate_limit,
        cors_origin=args.cors,
    )

    # Refuse, do not warn. Binding to a network interface without a key exposes
    # a CPU-expensive API to anyone who can reach the host, and a warning
    # printed to a terminal nobody is watching is not a security control.
    #
    # `--allow-public` is the container escape hatch, and it is a flag rather
    # than an exception for containers because auto-detecting one would be
    # guessing. Inside a container 0.0.0.0 is the ONLY way to be reachable at
    # all — the isolating boundary there is the port mapping, not the bind
    # address — so refusing it outright breaks `docker compose up` for a setup
    # that was never unsafe. Making the operator say so keeps the intent
    # explicit and the default safe.
    if args.host not in _LOOPBACK and not config.requires_auth() and not args.allow_public:
        print(
            f"refusing to bind {args.host} without an API key.\n"
            "  Set ALPHA_API_KEY=<something long and random> to require auth, or\n"
            "  pass --allow-public if this is a container whose port mapping is\n"
            "  the real boundary, or bind 127.0.0.1 (the default) to stay local.",
            file=sys.stderr,
        )
        return 2

    AppHandler.state = ApiState(config=config, log=RequestLog(enabled=args.log_requests))

    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    base = f"http://{args.host}:{args.port}"
    print(f"Alpha Engine listening on {base}", flush=True)
    print(f"  dashboard  {base}/dashboard", flush=True)
    print(f"  research   {base}/research", flush=True)
    print(f"  terminal   {base}/terminal", flush=True)
    print(f"  api        {base}/api/v1/tools", flush=True)
    if config.requires_auth():
        print("  auth       required (ALPHA_API_KEY is set)", flush=True)
    if config.allow_writes:
        print("  writes     ENABLED — tools may append to the signal log", flush=True)
    if args.log_requests:
        print("  logging    one JSON line per request (no bodies, no keys)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
