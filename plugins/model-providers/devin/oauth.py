"""Devin OAuth login — PKCE against app.devin.ai.

Flow:

1. PKCE (S256) + random state.
2. Local callback server on ``127.0.0.1:59653/callback`` (port fallback
   allowed — Devin accepts any loopback redirect).
3. Browser opens ``https://app.devin.ai/auth/cli/continue?...``.
4. The returned ``code`` is exchanged at
   ``POST https://api.devin.ai/auth/cli/token`` for a session JWT.
5. The JWT is stored in Hermes' credential pool for provider ``devin`` so the
   standard api_key resolution path (env var → pool) finds it.

No Devin CLI, no ACP.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple

DEVIN_WEBAPP_URL = "https://app.devin.ai"
DEVIN_API_URL = "https://api.devin.ai"
CALLBACK_PORT = 59653
CALLBACK_PATH = "/callback"
TOKEN_PATH = "/auth/cli/token"
FALLBACK_EXPIRES_MS = 365 * 24 * 60 * 60 * 1000
LOGIN_TIMEOUT_S = 300

PROVIDER = "devin"
SESSION_TOKEN_PREFIX = "devin-session-token$"
# Marker so logout can distinguish OAuth-managed entries from keys the user
# added manually via `hermes auth add devin`.
OAUTH_MARKER_KEY = "devin_oauth"


class DevinLoginError(RuntimeError):
    pass


def normalize_session_token(token: str) -> str:
    """Apply the ``devin-session-token$`` prefix the Cascade API expects."""
    token = (token or "").strip()
    if not token:
        return ""
    return token if token.startswith(SESSION_TOKEN_PREFIX) else SESSION_TOKEN_PREFIX + token


def _generate_pkce() -> Tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def token_expiry_ms(token: str) -> int:
    """JWT ``exp`` (ms, minus a 5-minute skew buffer); conservative fallback."""
    try:
        payload = token.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = decoded.get("exp")
        if isinstance(exp, (int, float)):
            return int(exp * 1000) - 5 * 60 * 1000
    except Exception:
        pass
    return int(time.time() * 1000) + FALLBACK_EXPIRES_MS


def token_subject(token: str) -> str:
    """Best-effort account label (email/sub) for display."""
    try:
        payload = token.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        for key in ("email", "sub", "name"):
            value = decoded.get(key)
            if isinstance(value, str) and value:
                return value
    except Exception:
        pass
    return ""


def exchange_code(code: str, verifier: str, timeout: float = 30.0) -> str:
    """POST the authorization code for a session token."""
    body = json.dumps({"code": code, "code_verifier": verifier}).encode()
    req = urllib.request.Request(
        DEVIN_API_URL + TOKEN_PATH,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise DevinLoginError(f"Devin token exchange failed: HTTP {exc.code} {detail}".strip())
    except Exception as exc:
        raise DevinLoginError(f"Devin token exchange failed: {exc}")
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise DevinLoginError("Devin token exchange returned an empty token")
    return token


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        error = (params.get("error") or [""])[0]
        self.server.callback_result = {"code": code, "state": state, "error": error}  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;text-align:center;padding:4em'>"
            b"<h2>Devin sign-in complete</h2>"
            b"<p>You can close this tab and return to Hermes.</p>"
            b"</body></html>"
        )

    def log_message(self, *args):  # silence stderr noise
        pass


def run_login_flow(*, open_browser: bool = True, timeout: float = LOGIN_TIMEOUT_S,
                   on_url=None) -> str:
    """Run the full browser OAuth flow; returns the raw session token.

    ``on_url`` (optional) is called with the authorization URL once known so
    callers can display it for manual copy/paste.
    """
    verifier, challenge = _generate_pkce()
    state = str(uuid.uuid4())

    server: Optional[HTTPServer] = None
    port = CALLBACK_PORT
    try:
        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    except OSError:
        # Port fallback — Devin accepts any loopback redirect URI.
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        port = server.server_address[1]
    server.callback_result = None  # type: ignore[attr-defined]
    server.timeout = 0.5

    redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    params = urllib.parse.urlencode({
        "redirect_uri": redirect_uri,
        "state": state,
        "prompt": "select_account",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"{DEVIN_WEBAPP_URL}/auth/cli/continue?{params}"

    if on_url:
        on_url(auth_url)
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            server.handle_request()
            result = server.callback_result  # type: ignore[attr-defined]
            if result is not None:
                if result["error"]:
                    raise DevinLoginError(f"Devin sign-in failed: {result['error']}")
                if result["state"] != state:
                    raise DevinLoginError("Devin sign-in failed: state mismatch (CSRF check)")
                if not result["code"]:
                    raise DevinLoginError("Devin sign-in failed: no authorization code returned")
                return exchange_code(result["code"], verifier)
        raise DevinLoginError("Devin sign-in timed out waiting for the browser callback")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# Credential pool persistence
# ---------------------------------------------------------------------------

def save_session_token(token: str) -> None:
    """Persist the session token into Hermes' credential pool for ``devin``.

    ``source="manual"`` is required: any other source is treated as a borrowed
    credential and its ``access_token`` is stripped on write. OAuth-managed
    entries are marked in ``extra`` so ``logout`` can remove only those.
    """
    from agent.credential_pool import AUTH_TYPE_OAUTH
    from hermes_cli.auth import write_credential_pool
    from hermes_cli.config import get_hermes_home
    import json as _json

    auth_path = get_hermes_home() / "auth.json"
    removed = []
    try:
        existing = _json.loads(auth_path.read_text())
        for entry in existing.get("credential_pool", {}).get(PROVIDER, []):
            if isinstance(entry, dict) and (entry.get("extra") or {}).get(OAUTH_MARKER_KEY):
                removed.append(entry.get("id"))
    except Exception:
        pass

    label = token_subject(token) or "devin-oauth"
    write_credential_pool(PROVIDER, [{
        "id": "devin-oauth",
        "auth_type": AUTH_TYPE_OAUTH,
        "provider": PROVIDER,
        "access_token": token,
        "refresh_token": token,
        "expires_at_ms": token_expiry_ms(token),
        "label": label,
        "source": "manual",
        "base_url": "https://server.codeium.com",
        "extra": {OAUTH_MARKER_KEY: True},
    }], removed_ids=[r for r in removed if r])


def clear_oauth_credentials() -> int:
    """Remove OAuth-managed pool entries; returns how many were removed."""
    from hermes_cli.auth import write_credential_pool
    from hermes_cli.config import get_hermes_home
    import json as _json

    auth_path = get_hermes_home() / "auth.json"
    try:
        existing = _json.loads(auth_path.read_text())
    except Exception:
        return 0
    removed = [
        e.get("id")
        for e in existing.get("credential_pool", {}).get(PROVIDER, [])
        if isinstance(e, dict) and (e.get("extra") or {}).get(OAUTH_MARKER_KEY) and e.get("id")
    ]
    if removed:
        write_credential_pool(PROVIDER, [], removed_ids=removed)
    return len(removed)


def load_session_token() -> Optional[str]:
    """Resolve the active Devin credential the same way the runtime does:
    ``DEVIN_API_KEY`` env var first, then the credential pool."""
    import os
    env = (os.environ.get("DEVIN_API_KEY") or "").strip()
    if env:
        return env
    try:
        from agent.credential_pool import load_pool
        pool = load_pool(PROVIDER)
        entry = pool.peek()
        if entry is not None:
            return (entry.runtime_api_key or entry.access_token or "").strip() or None
    except Exception:
        pass
    return None
