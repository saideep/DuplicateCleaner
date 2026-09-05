"""Generic OAuth 2.0 + PKCE loopback flow — stdlib-only, reused by both providers.

Both Google and Microsoft OAuth clients register ``http://127.0.0.1`` (any
port) as a permitted redirect URI for desktop apps.  We bind a random port,
launch the browser at the provider's authorization endpoint with a fresh
PKCE verifier + state token, receive the callback, exchange the code for
tokens, and return the parsed JSON.  Nothing here is Google-specific — the
same runner will be used by :mod:`sources.onedrive` in sub-phase 4.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 300.0

_SUCCESS_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'>"
    b"<title>DuplicateCleaner</title></head><body style='font-family:sans-serif;"
    b"padding:2rem'><h2>Authorization complete</h2>"
    b"<p>You may close this window and return to DuplicateCleaner.</p>"
    b"<script>setTimeout(()=>window.close(),500)</script></body></html>"
)


class OAuthFlowError(RuntimeError):
    """Raised when the localhost flow times out or the provider returns an error."""


def _pkce_pair() -> tuple[str, str]:
    """Return a fresh (code_verifier, code_challenge) PKCE pair.

    Verifier is 64 URL-safe bytes; challenge is base64url(SHA256(verifier)),
    which is the S256 method both Google and Microsoft support.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _reserve_localhost_port(port_hint: int) -> int:
    """Bind ``port_hint`` (or 0 for random) on 127.0.0.1 and return the actual port.

    The socket is closed immediately after; the ``HTTPServer`` binds again on
    the same port.  This is a documented tolerance-of-race pattern for
    getting an OS-chosen loopback port before starting the server.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port_hint))
        port = int(s.getsockname()[1])
    finally:
        s.close()
    return port


class _CallbackServer(HTTPServer):
    """HTTPServer with a slot for the received query params."""

    received: dict[str, str] | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler that stashes the callback query on the server."""

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        flat = {k: v[0] for k, v in query.items() if v}
        server = self.server
        if isinstance(server, _CallbackServer):
            server.received = flat
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_SUCCESS_HTML)))
        self.end_headers()
        self.wfile.write(_SUCCESS_HTML)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence stdlib default logging into stderr."""
        log.debug("callback %s", format % args)


def _wait_for_callback(
    port: int, timeout_seconds: float
) -> dict[str, str]:
    """Serve a single request on 127.0.0.1:port and return its parsed query."""
    server = _CallbackServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1.0

    stop = threading.Event()

    def serve() -> None:
        deadline = time.monotonic() + timeout_seconds
        while not stop.is_set() and time.monotonic() < deadline:
            server.handle_request()
            if server.received is not None:
                return

    thread = threading.Thread(target=serve, name="dc-oauth-callback", daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    stop.set()
    server.server_close()
    if server.received is None:
        raise OAuthFlowError(
            f"OAuth callback did not arrive within {timeout_seconds:.0f}s."
        )
    return server.received


def run_localhost_flow(
    auth_url_base: str,
    client_id: str,
    client_secret: str | None,
    scopes: list[str],
    *,
    token_url: str,
    port_hint: int = 0,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    extra_auth_params: dict[str, str] | None = None,
    extra_token_params: dict[str, str] | None = None,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Run the PKCE loopback flow end-to-end, returning the parsed token JSON.

    The port defaults to random (``port_hint=0``); pass a fixed port only for
    tests.  ``client_secret=None`` yields a pure-PKCE public-client exchange
    (Google Desktop apps require a non-empty secret, Microsoft public
    clients do not).  ``extra_auth_params`` and ``extra_token_params`` are
    passed through so provider-specific quirks (``access_type=offline`` for
    Google, ``tenant`` for Microsoft) can be layered on top.
    """
    port = _reserve_localhost_port(port_hint)
    redirect_uri = f"http://127.0.0.1:{port}/"
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if extra_auth_params:
        auth_params.update(extra_auth_params)

    auth_url = f"{auth_url_base}?{urllib.parse.urlencode(auth_params)}"
    log.info("Opening browser for OAuth: %s", auth_url_base)
    if open_browser:
        webbrowser.open(auth_url)

    received = _wait_for_callback(port, timeout_seconds)
    if "error" in received:
        raise OAuthFlowError(
            f"OAuth provider returned error: {received.get('error')} "
            f"({received.get('error_description', 'no description')})"
        )
    # B6: constant-time comparison — realistic attack surface for a loopback
    # OAuth flow is thin, but ``hmac.compare_digest`` is the canonical spelling
    # for opaque-value equality checks and rules out timing side-channels.
    if not hmac.compare_digest(received.get("state", ""), state):
        raise OAuthFlowError(
            "OAuth state mismatch — possible CSRF; aborting the flow."
        )
    code = received.get("code")
    if not code:
        raise OAuthFlowError("OAuth callback missing 'code' parameter.")

    token_params: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client_secret:
        token_params["client_secret"] = client_secret
    if extra_token_params:
        token_params.update(extra_token_params)

    body = urllib.parse.urlencode(token_params).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    data: Any = json.loads(raw)
    if not isinstance(data, dict):
        raise OAuthFlowError(f"Token endpoint returned non-object body: {raw!r}")
    if "access_token" not in data:
        raise OAuthFlowError(
            f"Token endpoint response missing access_token: {data.get('error', data)!r}"
        )
    data.setdefault("scopes", list(scopes))
    if "expires_in" in data:
        with contextlib.suppress(TypeError, ValueError):
            data["expires_at"] = float(time.time()) + float(data["expires_in"])
    return data


def revoke_token(revoke_url: str, token: str) -> bool:
    """POST a revoke to the provider; return True on 2xx, False on any error.

    Best-effort: callers proceed with local file removal even if this fails.
    """
    body = urllib.parse.urlencode({"token": token}).encode("utf-8")
    req = urllib.request.Request(
        revoke_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = int(resp.status)
            return 200 <= status < 300
    except OSError as exc:
        log.debug("revoke_token failed for %s: %s", revoke_url, exc)
        return False


def _redact(secret: str) -> str:
    """Return a log-safe token repr (last 4 chars only)."""
    if len(secret) <= 4:
        return "***"
    return f"***{secret[-4:]}"
