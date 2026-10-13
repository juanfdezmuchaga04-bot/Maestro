"""Optional sign-in gate for a Maestro backend reachable beyond loopback.

Maestro ships with **no** application login: on loopback that is the right
default (the browser and the server are the same user), and it is why the app
has never needed accounts. The moment the backend is reachable from another
device - Pinokio's LAN share, or a Tailscale route so a phone can drive the
render - anything on that network can drive it, including file access and
generation. This module is the missing lock:

* **opt-in.** With no configuration the gate is disabled and every request
  passes through untouched (one dict lookup). Existing users see no change.
* **password mode.** `access_gate.json` (under `app/settings`, gitignored) holds
  a PBKDF2-HMAC-SHA256 hash - never the password. The plaintext is returned
  exactly once, when it is created, for the user to write down.
* **token mode.** `MAESTRO_ACCESS_TOKEN` fixes the secret without touching disk,
  which is what automation and tests use. In this mode an ``Authorization: Bearer
  <token>`` header is also accepted, which is how another Maestro delegates
  generation to this one (``services.remote_engine``); the header is *only* honoured
  when a token is configured, so in password mode nothing but a real session works.
* **sessions are signed cookies**, HttpOnly + SameSite=Lax, with an expiry, and
  signed with a key derived from the current secret, so changing the password
  invalidates every session that already exists.

Everything here is stdlib: no new runtime dependency, and no Maestro cloud
account - the credential lives on the machine that already holds the projects.

The HTTP surface and the middleware live in `install_access_gate()`, which is
called once from `app/launch.py` before the CORS middleware is added, so CORS
stays the outer layer and a 401 still carries CORS headers in development.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

# Imported at module level rather than inside install_access_gate(): with
# `from __future__ import annotations` in effect, FastAPI resolves annotations
# through the module globals, so a Request imported in a nested scope would be
# read as a *query parameter* named "request" and every sign-in would fail with
# a 422. The CLI (scripts/maestro_access.py) only touches AccessGate, so the
# import is optional for that path and reported clearly if it is missing.
try:  # pragma: no cover - exercised by the app, not by unit tests
    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    _FASTAPI_IMPORT_ERROR: Exception | None = None
except ImportError as _exc:  # pragma: no cover
    Request = Any  # type: ignore[assignment,misc]
    HTMLResponse = JSONResponse = RedirectResponse = None  # type: ignore[assignment]
    _FASTAPI_IMPORT_ERROR = _exc

TOKEN_ENV = "MAESTRO_ACCESS_TOKEN"
SETTINGS_FILENAME = "access_gate.json"
BEARER_PREFIX = "bearer "
AUTH_HEADER = "authorization"
COOKIE_NAME = "maestro_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
_PBKDF2_ITERATIONS = 240_000
_HASH_SCHEME = "pbkdf2_sha256"
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 300
MIN_PASSWORD_LENGTH = 8

# Reachable while signed out, or the user could never sign in and the app could
# never be installed. None of these expose user data: they are the sign-in
# endpoints themselves plus the install/offline assets.
PUBLIC_PATHS = frozenset({
    "/api/v1/access/status",
    "/api/v1/access/login",
    "/api/v1/access/logout",
    "/manifest.webmanifest",
    "/maestro-sw.js",
    "/offline.html",
    "/favicon.ico",
    "/maestro-icon.png",
    "/maestro-icon-192.png",
    "/maestro-icon-512.png",
    "/maestro-maskable-512.png",
    "/maestro-apple-touch-icon.png",
    "/maestro-home-icon-orange.png",
})


class AccessGateError(RuntimeError):
    """Raised when the gate cannot be configured as asked."""


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return (
        f"{_HASH_SCHEME}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"
    )


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != _HASH_SCHEME:
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def generate_password() -> str:
    """A high-entropy password a person can still type on a phone."""

    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return "-".join(groups)


class AccessGate:
    """Resolves the configured secret and issues/validates session cookies."""

    def __init__(
        self,
        settings_dir: str | os.PathLike[str],
        *,
        environ: Mapping[str, str] | None = None,
        now: Callable[[], float] | None = None,
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        self._path = Path(settings_dir) / SETTINGS_FILENAME
        self._environ = environ if environ is not None else os.environ
        self._now = now or time.time
        self._ttl = int(ttl_seconds)
        self._lock = threading.RLock()
        self._failures: dict[str, list[float]] = {}
        # (mtime_ns, size, payload). The gate is consulted on every request, so
        # re-reading the file each time would add disk I/O to the hot path; the
        # stat is enough to notice a change made by another process.
        self._cache: tuple[int, int, dict[str, Any]] | None = None

    # -- configuration ----------------------------------------------------

    def _settings(self) -> dict[str, Any]:
        try:
            stat = self._path.stat()
        except OSError:
            with self._lock:
                self._cache = None
            return {}
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        with self._lock:
            cached = self._cache
            if cached is not None and cached[0] == signature[0] and cached[1] == signature[1]:
                return cached[2]
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return {}
        settings = payload if isinstance(payload, dict) else {}
        with self._lock:
            self._cache = (signature[0], signature[1], settings)
        return settings

    @property
    def token(self) -> str:
        return str(self._environ.get(TOKEN_ENV) or "").strip()

    @property
    def mode(self) -> str:
        if self.token:
            return "token"
        if self._settings().get("enabled") and self._settings().get("password_hash"):
            return "password"
        return "off"

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @property
    def _secret(self) -> str:
        if self.token:
            return self.token
        return str(self._settings().get("password_hash") or "")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "cookie_name": COOKIE_NAME,
            "session_days": round(self._ttl / 86400, 2),
            "token_env_var": TOKEN_ENV,
            "settings_file": str(self._path),
        }

    def set_password(self, password: str | None = None) -> str:
        """Enable the gate and return the plaintext exactly once."""

        chosen = password or generate_password()
        if len(chosen) < MIN_PASSWORD_LENGTH:
            raise AccessGateError(
                f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        with self._lock:
            _atomic_write(self._path, {
                "version": 1,
                "enabled": True,
                "password_hash": _hash_password(chosen),
                "updated_at": int(self._now()),
                "note": "Local sign-in for Maestro. The password itself is not stored.",
            })
            self._failures.clear()
            self._cache = None
        return chosen

    def disable(self) -> None:
        with self._lock:
            _atomic_write(self._path, {
                "version": 1,
                "enabled": False,
                "password_hash": "",
                "updated_at": int(self._now()),
                "note": "Local sign-in for Maestro. The password itself is not stored.",
            })
            self._failures.clear()
            self._cache = None

    # -- credentials ------------------------------------------------------

    def verify_secret(self, candidate: str) -> bool:
        if not self.enabled:
            return True
        if self.token:
            return hmac.compare_digest(candidate, self.token)
        return _verify_password(candidate, self._secret)

    def accepts_bearer(self) -> bool:
        """Is this gate willing to authenticate a machine caller by header?

        Only token mode.  A password-mode gate signs sessions with the PBKDF2
        hash; accepting that hash as a bearer credential would turn the stored
        file into a login token, so it is deliberately not allowed.
        """
        return bool(self.enabled and self.token)

    # -- sessions ---------------------------------------------------------

    def _sign(self, expires_at: int) -> str:
        message = f"v1|{expires_at}".encode("utf-8")
        digest = hmac.new(self._secret.encode("utf-8"), message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def issue_session(self) -> str:
        expires_at = int(self._now()) + self._ttl
        return f"{expires_at}.{self._sign(expires_at)}"

    def session_valid(self, cookie_value: str | None) -> bool:
        if not cookie_value or not self._secret:
            return False
        expires_raw, _, signature = cookie_value.partition(".")
        try:
            expires_at = int(expires_raw)
        except (TypeError, ValueError):
            return False
        if expires_at <= int(self._now()):
            return False
        return hmac.compare_digest(signature, self._sign(expires_at))

    # -- brute-force protection -------------------------------------------

    def retry_after(self, client_key: str) -> float | None:
        """Seconds the caller must wait, or None when it may try now."""

        now = self._now()
        with self._lock:
            attempts = [
                stamp for stamp in self._failures.get(client_key, [])
                if now - stamp < FAILURE_WINDOW_SECONDS
            ]
            self._failures[client_key] = attempts
            if len(attempts) < MAX_FAILURES:
                return None
            return max(1.0, FAILURE_WINDOW_SECONDS - (now - attempts[0]))

    def record_failure(self, client_key: str) -> None:
        with self._lock:
            self._failures.setdefault(client_key, []).append(self._now())

    def record_success(self, client_key: str) -> None:
        with self._lock:
            self._failures.pop(client_key, None)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
<meta name="theme-color" content="#0a0a0a" />
<meta name="color-scheme" content="dark" />
<title>Maestro — sign in</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #0a0a0a; color: #e8e0d0;
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         padding: 24px; }}
  main {{ width: 100%; max-width: 360px; }}
  img {{ width: 64px; height: 64px; border-radius: 16px; display: block; margin-bottom: 18px; }}
  h1 {{ font-size: 19px; margin: 0 0 8px; }}
  p {{ font-size: 13px; line-height: 1.6; color: #b8b0a0; margin: 0 0 18px; }}
  label {{ display: block; font-size: 12px; color: #b8b0a0; margin-bottom: 6px; }}
  input[type=password] {{ width: 100%; padding: 13px 14px; font-size: 16px; border-radius: 10px;
                           border: 1px solid #333; background: #141414; color: #e8e0d0; }}
  input[type=password]:focus {{ outline: none; border-color: #d2691e; }}
  button {{ margin-top: 14px; width: 100%; border: 0; border-radius: 10px; background: #d2691e;
            color: #fff; font-size: 15px; font-weight: 600; padding: 13px 18px; cursor: pointer; }}
  .error {{ background: rgba(210, 105, 30, 0.14); border: 1px solid rgba(210, 105, 30, 0.45);
            color: #f0c9a8; border-radius: 10px; padding: 10px 12px; font-size: 12px;
            margin-bottom: 14px; }}
  .hint {{ font-size: 11px; color: #6f6858; margin-top: 18px; line-height: 1.6; }}
</style>
</head>
<body>
<main>
  <img src="/maestro-icon-192.png" alt="Maestro" />
  <h1>Sign in to Maestro</h1>
  <p>This Maestro runs on your own computer and is reachable from this device.
     Enter the access password to continue.</p>
  {error}
  <form method="post" action="/api/v1/access/login">
    <input type="hidden" name="next" value="{next_path}" />
    <label for="password">Access password</label>
    <input id="password" name="password" type="password" autocomplete="current-password"
           autofocus required inputmode="text" />
    <button type="submit">Unlock</button>
  </form>
  <p class="hint">Forgot it? Run <b>python scripts/maestro_access.py reset</b> on the computer
     that runs Maestro and use the new password it prints.</p>
</main>
</body>
</html>
"""


def _safe_next_path(candidate: str | None) -> str:
    """Only ever redirect back into this app (no open redirects)."""

    value = (candidate or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if value.startswith("/api/v1/access/login"):
        return "/"
    return value


def _client_key(request: Any) -> str:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client else None
    return str(host or "unknown")


def _login_page(next_path: str, error: str = "") -> str:
    error_block = f'<div class="error">{error}</div>' if error else ""
    return _LOGIN_PAGE.format(
        error=error_block,
        next_path=next_path.replace('"', "&quot;"),
    )


def install_access_gate(app: Any, gate: AccessGate) -> None:
    """Register the sign-in endpoints and the enforcing middleware.

    Call this *before* adding CORS middleware: Starlette applies middleware
    last-added-first, so the order below keeps CORS outermost and a 401 keeps
    its CORS headers for the Vite dev server.
    """

    if _FASTAPI_IMPORT_ERROR is not None:  # pragma: no cover - dependency guard
        raise AccessGateError(
            "The sign-in gate needs FastAPI, which this environment does not have: "
            f"{_FASTAPI_IMPORT_ERROR}"
        )

    def _bearer_ok(request: Request) -> bool:
        """Machine-to-machine call, authenticated by the configured token itself."""
        if not gate.accepts_bearer():
            return False
        header = request.headers.get(AUTH_HEADER) or ""
        if header[: len(BEARER_PREFIX)].lower() != BEARER_PREFIX:
            return False
        candidate = header[len(BEARER_PREFIX):].strip()
        return bool(candidate) and hmac.compare_digest(candidate, gate.token)

    def _wants_html(request: Request) -> bool:
        accept = request.headers.get("accept") or ""
        return "text/html" in accept

    def _session_from_request(request: Request) -> str | None:
        return request.cookies.get(COOKIE_NAME)

    def _authenticated(request: Request) -> bool:
        return gate.session_valid(_session_from_request(request)) or _bearer_ok(request)

    @app.get("/api/v1/access/status", include_in_schema=False)
    def access_status(request: Request):
        return {**gate.status(), "authenticated": (not gate.enabled) or _authenticated(request)}

    @app.post("/api/v1/access/login", include_in_schema=False)
    async def access_login(request: Request):
        client_key = _client_key(request)
        wait = gate.retry_after(client_key)
        content_type = (request.headers.get("content-type") or "").lower()
        payload: dict[str, Any] = {}
        if "application/json" in content_type:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
        else:
            form = await request.form()
            payload = {key: form.get(key) for key in form.keys()}
        candidate = str(payload.get("password") or "")
        next_path = _safe_next_path(payload.get("next"))
        wants_html = _wants_html(request)

        if wait is not None:
            message = (
                f"Too many attempts. Wait {int(wait)} seconds and try again."
            )
            if wants_html:
                return HTMLResponse(_login_page(next_path, message), status_code=429)
            return JSONResponse({"detail": message}, status_code=429)

        if not gate.enabled or not gate.verify_secret(candidate):
            gate.record_failure(client_key)
            message = "Incorrect password." if gate.enabled else "Sign-in is disabled."
            if wants_html:
                return HTMLResponse(_login_page(next_path, message), status_code=401)
            return JSONResponse({"detail": message}, status_code=401)

        gate.record_success(client_key)
        secure = request.url.scheme == "https" or (
            (request.headers.get("x-forwarded-proto") or "").lower() == "https"
        )
        response: Any
        if wants_html:
            response = RedirectResponse(next_path, status_code=303)
        else:
            response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE_NAME,
            gate.issue_session(),
            max_age=gate.ttl_seconds,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
        )
        return response

    @app.api_route("/api/v1/access/logout", methods=["GET", "POST"], include_in_schema=False)
    def access_logout(request: Request):
        response: Any = (
            RedirectResponse("/", status_code=303)
            if _wants_html(request)
            else JSONResponse({"ok": True})
        )
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.middleware("http")
    async def _access_gate_middleware(request: Request, call_next):
        if not gate.enabled:
            return await call_next(request)
        path = request.url.path
        if path in PUBLIC_PATHS or request.method == "OPTIONS":
            return await call_next(request)
        if _authenticated(request):
            return await call_next(request)
        if path.startswith("/api/") or not _wants_html(request):
            return JSONResponse(
                {
                    "detail": "Sign in required. Maestro is reachable from this "
                              "device; enter the access password to continue.",
                    "sign_in_required": True,
                    "login_url": "/api/v1/access/login",
                },
                status_code=401,
            )
        return HTMLResponse(_login_page(_safe_next_path(path)), status_code=200)


__all__ = [
    "AccessGate",
    "AccessGateError",
    "COOKIE_NAME",
    "MAX_FAILURES",
    "MIN_PASSWORD_LENGTH",
    "PUBLIC_PATHS",
    "SESSION_TTL_SECONDS",
    "TOKEN_ENV",
    "generate_password",
    "install_access_gate",
]
