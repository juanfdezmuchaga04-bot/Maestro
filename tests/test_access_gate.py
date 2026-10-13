"""The optional sign-in gate, exercised over HTTP.

These tests drive a real FastAPI app through the gate the same way a phone does:
an anonymous request, the sign-in form, the session cookie, then an authenticated
call. They also pin the two properties that keep the feature safe to ship — the
gate is invisible when it is not configured, and it can never lock the user out
of signing in.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import access_gate as gate_module  # noqa: E402


def _build_app(settings_dir: str, *, environ=None, now=None, ttl=gate_module.SESSION_TTL_SECONDS):
    app = FastAPI()

    @app.get("/api/v1/private")
    def private():
        return {"secret": "generated-media"}

    @app.get("/")
    def index():
        return {"app": "maestro"}

    gate = gate_module.AccessGate(
        settings_dir, environ=environ or {}, now=now, ttl_seconds=ttl
    )
    gate_module.install_access_gate(app, gate)
    return app, gate


class GateTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings_dir = self._tmp.name

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _client(self, app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://maestro.test"
        )
        self.addAsyncCleanup(client.aclose)
        return client


class DisabledGateTest(GateTestCase):
    async def test_unconfigured_gate_is_invisible(self):
        app, gate = _build_app(self.settings_dir)
        self.assertFalse(gate.enabled)
        client = await self._client(app)
        response = await client.get("/api/v1/private")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"secret": "generated-media"})
        # A navigation must not be replaced by a sign-in page either.
        page = await client.get("/", headers={"accept": "text/html"})
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Sign in to Maestro", page.text)

    async def test_status_reports_mode_without_secrets(self):
        app, _ = _build_app(self.settings_dir)
        client = await self._client(app)
        body = (await client.get("/api/v1/access/status")).json()
        self.assertEqual(body["mode"], "off")
        self.assertTrue(body["authenticated"])
        self.assertNotIn("password", json.dumps(body).lower())


class PasswordGateTest(GateTestCase):
    async def _enabled_app(self, **kwargs):
        app, gate = _build_app(self.settings_dir, **kwargs)
        self.password = gate.set_password("correct-horse-battery")
        return app, gate

    async def test_api_is_refused_while_the_browser_gets_a_form(self):
        app, _ = await self._enabled_app()
        client = await self._client(app)
        refused = await client.get("/api/v1/private")
        self.assertEqual(refused.status_code, 401)
        self.assertTrue(refused.json()["sign_in_required"])

        page = await client.get("/", headers={"accept": "text/html"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("Sign in to Maestro", page.text)
        self.assertIn('action="/api/v1/access/login"', page.text)

    async def test_install_and_offline_assets_stay_reachable(self):
        app, _ = await self._enabled_app()
        client = await self._client(app)
        for path in ("/manifest.webmanifest", "/maestro-sw.js", "/offline.html",
                     "/maestro-icon-192.png", "/maestro-apple-touch-icon.png",
                     "/api/v1/access/status"):
            with self.subTest(path=path):
                response = await client.get(path)
                self.assertNotEqual(response.status_code, 401)

    async def test_wrong_password_is_refused_then_correct_one_signs_in(self):
        app, _ = await self._enabled_app()
        client = await self._client(app)
        bad = await client.post(
            "/api/v1/access/login",
            json={"password": "nope"},
            headers={"accept": "application/json"},
        )
        self.assertEqual(bad.status_code, 401)

        ok = await client.post(
            "/api/v1/access/login",
            json={"password": self.password},
            headers={"accept": "application/json"},
        )
        self.assertEqual(ok.status_code, 200)
        cookie = ok.cookies.get(gate_module.COOKIE_NAME)
        self.assertTrue(cookie)

        allowed = await client.get("/api/v1/private")
        self.assertEqual(allowed.status_code, 200)

    async def test_form_sign_in_redirects_into_the_app(self):
        app, _ = await self._enabled_app()
        client = await self._client(app)
        response = await client.post(
            "/api/v1/access/login",
            data={"password": self.password, "next": "/some/page"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/some/page")
        self.assertIn(gate_module.COOKIE_NAME, response.headers.get("set-cookie", ""))

    async def test_login_refuses_an_open_redirect(self):
        app, _ = await self._enabled_app()
        client = await self._client(app)
        for target in ("//evil.example", "https://evil.example", "/\\evil"):
            with self.subTest(target=target):
                response = await client.post(
                    "/api/v1/access/login",
                    data={"password": self.password, "next": target},
                    headers={"accept": "text/html"},
                    follow_redirects=False,
                )
                self.assertEqual(response.headers["location"], "/")

    async def test_session_cookie_is_httponly_and_scoped_to_the_site(self):
        app, _ = await self._enabled_app()
        client = await self._client(app)
        response = await client.post(
            "/api/v1/access/login",
            json={"password": self.password},
            headers={"accept": "application/json"},
        )
        header = response.headers.get("set-cookie", "").lower()
        self.assertIn("httponly", header)
        self.assertIn("samesite=lax", header)
        self.assertIn("path=/", header)
        # No HTTPS in this test, so the cookie must not be marked Secure —
        # otherwise a plain-HTTP local session would silently never stick.
        self.assertNotIn("secure", header)

    async def test_session_expires(self):
        clock = {"now": 1_000_000.0}
        app, gate = _build_app(self.settings_dir, now=lambda: clock["now"], ttl=60)
        password = gate.set_password("correct-horse-battery")
        client = await self._client(app)
        login = await client.post(
            "/api/v1/access/login",
            json={"password": password},
            headers={"accept": "application/json"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual((await client.get("/api/v1/private")).status_code, 200)
        clock["now"] += 61
        self.assertEqual((await client.get("/api/v1/private")).status_code, 401)

    async def test_a_tampered_cookie_is_rejected(self):
        app, gate = await self._enabled_app()
        client = await self._client(app)
        good = gate.issue_session()
        forged = f"{good.split('.')[0]}.{'A' * len(good.split('.')[1])}"
        for value in (forged, "nonsense", "", "9999999999.deadbeef"):
            with self.subTest(cookie=value):
                client.cookies.clear()
                client.cookies.set(gate_module.COOKIE_NAME, value)
                response = await client.get("/api/v1/private")
                self.assertEqual(response.status_code, 401)

    async def test_changing_the_password_ends_existing_sessions(self):
        app, gate = await self._enabled_app()
        client = await self._client(app)
        old_session = gate.issue_session()
        gate.set_password("a-different-password")
        client.cookies.clear()
        client.cookies.set(gate_module.COOKIE_NAME, old_session)
        response = await client.get("/api/v1/private")
        self.assertEqual(response.status_code, 401)

    async def test_logout_clears_the_session(self):
        app, _ = await self._enabled_app()
        client = await self._client(app)
        await client.post(
            "/api/v1/access/login",
            json={"password": self.password},
            headers={"accept": "application/json"},
        )
        self.assertEqual((await client.get("/api/v1/private")).status_code, 200)
        await client.post("/api/v1/access/logout")
        self.assertEqual((await client.get("/api/v1/private")).status_code, 401)

    async def test_repeated_failures_are_throttled(self):
        app, _ = await self._enabled_app()
        client = await self._client(app)
        for _ in range(gate_module.MAX_FAILURES):
            response = await client.post("/api/v1/access/login", json={"password": "wrong"})
            self.assertEqual(response.status_code, 401)
        blocked = await client.post("/api/v1/access/login", json={"password": "wrong"})
        self.assertEqual(blocked.status_code, 429)
        # The real password is throttled too: that is the point of the limit.
        still_blocked = await client.post(
            "/api/v1/access/login", json={"password": self.password}
        )
        self.assertEqual(still_blocked.status_code, 429)

    async def test_a_successful_sign_in_clears_the_failure_count(self):
        app, _ = await self._enabled_app()
        client = await self._client(app)
        for _ in range(gate_module.MAX_FAILURES - 1):
            await client.post("/api/v1/access/login", json={"password": "wrong"})
        ok = await client.post("/api/v1/access/login", json={"password": self.password})
        self.assertEqual(ok.status_code, 200)
        for _ in range(gate_module.MAX_FAILURES - 1):
            response = await client.post("/api/v1/access/login", json={"password": "wrong"})
            self.assertEqual(response.status_code, 401)


class StorageTest(GateTestCase):
    async def test_the_password_is_never_stored_in_plaintext(self):
        app, gate = _build_app(self.settings_dir)
        password = gate.set_password("correct-horse-battery")
        raw = Path(self.settings_dir, gate_module.SETTINGS_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn(password, raw)
        self.assertIn("pbkdf2_sha256", raw)
        payload = json.loads(raw)
        self.assertTrue(payload["enabled"])
        self.assertNotIn("password", payload)

    async def test_generated_passwords_are_long_and_unique(self):
        first = gate_module.generate_password()
        second = gate_module.generate_password()
        self.assertGreaterEqual(len(first), gate_module.MIN_PASSWORD_LENGTH)
        self.assertNotEqual(first, second)

    async def test_short_passwords_are_refused(self):
        app, gate = _build_app(self.settings_dir)
        with self.assertRaises(gate_module.AccessGateError):
            gate.set_password("short")

    async def test_token_mode_uses_the_environment_and_writes_nothing(self):
        app, gate = _build_app(self.settings_dir, environ={gate_module.TOKEN_ENV: "s3cret-token"})
        self.assertEqual(gate.mode, "token")
        self.assertFalse(Path(self.settings_dir, gate_module.SETTINGS_FILENAME).exists())
        client = await self._client(app)
        self.assertEqual((await client.get("/api/v1/private")).status_code, 401)
        response = await client.post(
            "/api/v1/access/login", json={"password": "s3cret-token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await client.get("/api/v1/private")).status_code, 200)

    async def test_a_machine_caller_can_present_the_token_as_a_bearer_header(self):
        """How another Maestro delegates work here: token mode, header, no password.

        The header is only ever honoured in token mode, because in password mode
        the signing secret is the stored PBKDF2 hash — accepting that as a bearer
        credential would turn the settings file into a login token.
        """
        app, gate = _build_app(self.settings_dir, environ={gate_module.TOKEN_ENV: "s3cret-token"})
        client = await self._client(app)
        self.assertEqual((await client.get("/api/v1/private")).status_code, 401)
        nosec = await client.get(
            "/api/v1/private", headers={"Authorization": "Bearer not-the-token"}
        )
        self.assertEqual(nosec.status_code, 401)
        awful = await client.get("/api/v1/private", headers={"Authorization": "s3cret-token"})
        self.assertEqual(awful.status_code, 401)
        allowed = await client.get(
            "/api/v1/private", headers={"Authorization": "Bearer s3cret-token"}
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json(), {"secret": "generated-media"})
        # A bearer call does not mint a browser session.
        self.assertEqual((await client.get("/api/v1/private")).status_code, 401)

    async def test_bearer_is_never_accepted_in_password_mode(self):
        app, gate = _build_app(self.settings_dir)
        gate.set_password("correct-horse-battery")
        self.assertFalse(gate.accepts_bearer())
        client = await self._client(app)
        for header in (gate._secret, "correct-horse-battery"):
            response = await client.get(
                "/api/v1/private", headers={"Authorization": f"Bearer {header}"}
            )
            self.assertEqual(response.status_code, 401)

    async def test_disable_opens_the_app_again(self):
        app, gate = _build_app(self.settings_dir)
        gate.set_password("correct-horse-battery")
        client = await self._client(app)
        self.assertEqual((await client.get("/api/v1/private")).status_code, 401)
        gate.disable()
        self.assertFalse(gate.enabled)
        self.assertEqual((await client.get("/api/v1/private")).status_code, 200)


class WiringTest(unittest.TestCase):
    """The gate must be installed in launch.py, before CORS."""

    def test_launch_installs_the_gate_before_cors(self):
        source = Path(
            Path(__file__).resolve().parents[1] / "app" / "launch.py"
        ).read_text(encoding="utf-8")
        install_at = source.find("install_access_gate(api, _maestro_access_gate)")
        cors_at = source.find("api.add_middleware(\n    CORSMiddleware,")
        self.assertNotEqual(install_at, -1, "launch.py does not install the access gate")
        self.assertNotEqual(cors_at, -1, "CORS middleware not found in launch.py")
        self.assertLess(
            install_at, cors_at,
            "the access gate must be added before CORS so CORS stays outermost",
        )
        self.assertIn("_AccessGate(_maestro_settings_dir)", source)

    def test_public_paths_cover_every_route_the_login_page_needs(self):
        # If one of these stopped being public, a signed-out device could never
        # sign in or install the app.
        for path in ("/api/v1/access/login", "/api/v1/access/status",
                     "/manifest.webmanifest", "/maestro-sw.js", "/offline.html"):
            with self.subTest(path=path):
                self.assertIn(path, gate_module.PUBLIC_PATHS)

    def test_every_icon_the_page_references_is_reachable_when_signed_out(self):
        # iOS and Android fetch the Home Screen icon and the manifest before any
        # session exists. An icon the gate answers with 401 makes the installed
        # app fall back to a screenshot or a monogram, so the public list must
        # keep up with what ui/index.html and the manifest actually reference.
        root = Path(__file__).resolve().parents[1]
        index = (root / "ui" / "index.html").read_text(encoding="utf-8")
        manifest = json.loads(
            (root / "ui" / "public" / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        referenced = set(re.findall(r'<link[^>]+href="(/[^"]+)"', index))
        referenced.update(icon["src"] for icon in manifest["icons"])
        self.assertIn("/maestro-apple-touch-icon.png", referenced)
        # Served by a backend route from the canonical artwork, not from
        # ui/public (see launch.py).
        backend_routes = {"/maestro-icon.png"}
        for path in sorted(referenced):
            with self.subTest(path=path):
                self.assertIn(path, gate_module.PUBLIC_PATHS)
                if path in backend_routes:
                    continue
                self.assertTrue(
                    (root / "ui" / "public" / path.lstrip("/")).is_file(),
                    f"{path} is referenced by the UI but missing from ui/public",
                )


if __name__ == "__main__":
    unittest.main()
