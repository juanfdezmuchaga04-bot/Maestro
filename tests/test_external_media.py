"""Opt-in external providers: configuration, consent, errors, cancellation.

No test touches the network: a fake transport answers exactly as the documented
Replicate and Cloudflare Workers AI endpoints do.  What is protected:

* nothing is sent unless the provider is enabled, its key is stored and the
  request's cost/data disclosure was confirmed (the confirm token is bound to the
  request, so changing the request invalidates the confirmation);
* keys are never returned by any status call, and are stored outside the config;
* every provider failure surfaces with its own code (missing key, rejected key,
  no credit, spent free allowance, rate limit, invalid input, provider down,
  timeout, network) — and a job is only ``succeeded`` once a real media file of
  the requested type was written;
* cancellation reaches Replicate's cancel endpoint, and a timeout cancels too.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib import error as urlerror

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import external_media as em  # noqa: E402
from services.external_secrets import SecretStore  # noqa: E402
from services.public_cloud import HttpResponse  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64
ACCOUNT = "0123456789abcdef0123456789abcdef"


def ok_json(payload, status=200, headers=None):
    return HttpResponse(status, {"content-type": "application/json", **(headers or {})},
                        json.dumps(payload).encode())


class FakeTransport:
    """Routes ``(method, url)`` to scripted answers and records every call."""

    def __init__(self):
        self.calls = []
        self.routes = []
        self.lock = threading.Lock()

    def on(self, method, contains, *answers):
        self.routes.append([method, contains, list(answers)])
        return self

    def __call__(self, method, url, *, data=None, headers=None, timeout=0):
        with self.lock:
            self.calls.append({"method": method, "url": url, "data": data, "headers": dict(headers or {})})
            for route in self.routes:
                if route[0] == method and route[1] in url:
                    answers = route[2]
                    answer = answers.pop(0) if len(answers) > 1 else answers[0]
                    if isinstance(answer, BaseException):
                        raise answer
                    return answer(url) if callable(answer) else answer
        raise AssertionError(f"unexpected call {method} {url}")

    def called(self, method, contains):
        return [c for c in self.calls if c["method"] == method and contains in c["url"]]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "settings")
        self.out = os.path.join(self.tmp.name, "outputs")
        self.secrets = SecretStore(self.root, environ={}, keyring_module=None, platform="linux")
        self.transport = FakeTransport()
        self._poll = mock.patch.dict(em.PROVIDERS["replicate"]["models"]["image"], {"poll_s": 0.01})
        self._poll_v = mock.patch.dict(em.PROVIDERS["replicate"]["models"]["video"], {"poll_s": 0.01})
        self._poll.start()
        self._poll_v.start()

    def tearDown(self):
        self._poll.stop()
        self._poll_v.stop()
        self.tmp.cleanup()

    def manager(self, **kwargs):
        kwargs.setdefault("transport", self.transport)
        return em.ExternalMediaManager(self.root, output_dir=lambda: ("default", self.out),
                                       secrets=self.secrets, **kwargs)

    def ready(self, m, provider):
        if provider == "replicate":
            m.set_credentials("replicate", {"api_token": "r8_secret_token_1234"})
        else:
            m.set_credentials("cloudflare", {"api_token": "cf_secret_token_9876", "account_id": ACCOUNT})
        m.set_enabled(provider, True)

    def submit(self, m, params):
        quote = m.quote(params)
        job = m.submit(params, quote["confirm_token"])
        return m.wait(job["id"], 10)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


class SecretStoreTests(unittest.TestCase):
    def test_file_fallback_is_private_and_never_described(self):
        with tempfile.TemporaryDirectory() as root:
            store = SecretStore(root, environ={}, keyring_module=None, platform="linux")
            store.set("replicate.api_token", "r8_abcdefgh12345678")
            info = store.describe("replicate.api_token")
            self.assertEqual(info["set"], True)
            self.assertEqual(info["hint"], "…5678")
            self.assertNotIn("r8_abcdefgh12345678", json.dumps(info))
            self.assertEqual(store.get("replicate.api_token"), "r8_abcdefgh12345678")
            mode = stat.S_IMODE(os.stat(os.path.join(root, "secrets.json")).st_mode)
            if os.name == "posix":
                self.assertEqual(mode, 0o600)
            self.assertEqual(store.storage, "file_0600")
            store.delete("replicate.api_token")
            self.assertFalse(store.describe("replicate.api_token")["set"])

    def test_environment_overrides_and_reports_source(self):
        with tempfile.TemporaryDirectory() as root:
            store = SecretStore(root, environ={"REPLICATE_API_TOKEN": "r8_from_env_0000"},
                                keyring_module=None, platform="linux")
            self.assertEqual(store.get("replicate.api_token"), "r8_from_env_0000")
            self.assertEqual(store.describe("replicate.api_token")["source"], "env")

    def test_keyring_backend_is_used_when_present(self):
        class FakeKeyring:
            def __init__(self):
                self.data = {}

            def get_password(self, service, key):
                return self.data.get((service, key))

            def set_password(self, service, key, value):
                self.data[(service, key)] = value

            def delete_password(self, service, key):
                self.data.pop((service, key), None)

        with tempfile.TemporaryDirectory() as root:
            ring = FakeKeyring()
            store = SecretStore(root, environ={}, keyring_module=ring)
            store.set("cloudflare.api_token", "cf_token_abcdef")
            self.assertEqual(store.storage, "os_keyring")
            self.assertFalse(os.path.exists(os.path.join(root, "secrets.json")))
            self.assertEqual(store.get("cloudflare.api_token"), "cf_token_abcdef")

    def test_windows_without_keyring_encrypts_with_dpapi(self):
        def fake_dpapi(data, *, protect):
            return bytes(b ^ 0x5A for b in data)

        with tempfile.TemporaryDirectory() as root:
            store = SecretStore(root, environ={}, keyring_module=None, platform="win32", dpapi=fake_dpapi)
            store.set("replicate.api_token", "r8_plain_value_9999")
            with open(os.path.join(root, "secrets.json"), encoding="utf-8") as handle:
                raw = handle.read()
            self.assertNotIn("r8_plain_value_9999", raw)
            self.assertIn("dpapi:", raw)
            self.assertEqual(store.get("replicate.api_token"), "r8_plain_value_9999")

    def test_line_breaks_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = SecretStore(root, environ={}, keyring_module=None, platform="linux")
            with self.assertRaises(ValueError):
                store.set("replicate.api_token", "abc\ndef")


# ---------------------------------------------------------------------------
# Configuration and consent
# ---------------------------------------------------------------------------


class ConfigurationTests(Base):
    def test_everything_is_off_by_default_and_status_never_leaks_keys(self):
        m = self.manager(start_threads=False)
        status = m.providers()
        self.assertTrue(all(p["enabled"] is False for p in status))
        m.set_credentials("replicate", {"api_token": "r8_very_secret_value"})
        dumped = json.dumps(m.providers())
        self.assertNotIn("r8_very_secret_value", dumped)
        m.set_enabled("replicate", True)
        m.submit({"provider": "replicate", "modality": "image", "prompt": "x"},
                 m.quote({"provider": "replicate", "modality": "image", "prompt": "x"})["confirm_token"])
        for name in ("config.json", "jobs.json"):
            with open(os.path.join(self.root, name), encoding="utf-8") as handle:
                self.assertNotIn("r8_very", handle.read())

    def test_disabled_provider_refuses_and_sends_nothing(self):
        m = self.manager(start_threads=False)
        m.set_credentials("replicate", {"api_token": "r8_token_abcdefgh"})
        params = {"provider": "replicate", "modality": "image", "prompt": "a red fox"}
        token = m.quote(params)["confirm_token"]
        with self.assertRaises(em.ExternalMediaError) as ctx:
            m.submit(params, token)
        self.assertEqual(ctx.exception.code, "provider_disabled")
        self.assertEqual(self.transport.calls, [])

    def test_missing_credentials_named(self):
        m = self.manager(start_threads=False)
        m.set_enabled("cloudflare", True)
        params = {"provider": "cloudflare", "modality": "image", "prompt": "a lighthouse"}
        with self.assertRaises(em.ExternalMediaError) as ctx:
            m.submit(params, m.quote(params)["confirm_token"])
        self.assertEqual(ctx.exception.code, "missing_credentials")

    def test_confirmation_required_and_bound_to_request(self):
        m = self.manager(start_threads=False)
        self.ready(m, "replicate")
        params = {"provider": "replicate", "modality": "video", "prompt": "waves", "resolution": "480p"}
        token = m.quote(params)["confirm_token"]
        with self.assertRaises(em.ExternalMediaError) as ctx:
            m.submit(params, "")
        self.assertEqual(ctx.exception.code, "confirmation_required")
        changed = dict(params, resolution="720p")
        with self.assertRaises(em.ExternalMediaError) as ctx:
            m.submit(changed, token)
        self.assertEqual(ctx.exception.code, "confirmation_required")
        self.assertEqual(self.transport.calls, [])

    def test_quote_discloses_cost_data_and_limits(self):
        m = self.manager(start_threads=False)
        quote = m.quote({"provider": "replicate", "modality": "video", "prompt": "a city at night",
                         "resolution": "720p", "num_frames": 81, "frames_per_second": 16})
        d = quote["disclosure"]
        self.assertEqual(d["cost"]["amount_usd"], 0.1)
        self.assertFalse(d["cost"]["free"])
        self.assertIn("Description text", d["data_sent"][0])
        self.assertEqual(d["endpoint_host"], "api.replicate.com")
        self.assertAlmostEqual(d["duration_s"], 5.06, places=2)
        self.assertEqual(d["cancel"], "remote")
        cf = m.quote({"provider": "cloudflare", "modality": "image", "prompt": "x", "steps": 4})["disclosure"]
        self.assertTrue(cf["cost"]["free"])
        self.assertEqual(cf["cost"]["neurons"], 57.6)
        self.assertEqual(cf["cancel"], "local_only")

    def test_invalid_inputs(self):
        m = self.manager(start_threads=False)
        cases = [
            {"provider": "nope", "modality": "image", "prompt": "x"},
            {"provider": "cloudflare", "modality": "video", "prompt": "x"},
            {"provider": "replicate", "modality": "image", "prompt": "   "},
            {"provider": "replicate", "modality": "image", "prompt": "x" * 2001},
            {"provider": "replicate", "modality": "image", "prompt": "x", "aspect_ratio": "5:1"},
            {"provider": "replicate", "modality": "video", "prompt": "x", "num_frames": 300},
            {"provider": "replicate", "modality": "video", "prompt": "x", "resolution": "4k"},
            {"provider": "cloudflare", "modality": "image", "prompt": "x", "steps": 20},
            {"provider": "replicate", "modality": "image", "prompt": "x", "seed": "abc"},
        ]
        for params in cases:
            with self.subTest(params=params), self.assertRaises(em.ExternalMediaError):
                m.quote(params)

    def test_account_id_format_checked(self):
        m = self.manager(start_threads=False)
        with self.assertRaises(em.ExternalMediaError):
            m.set_credentials("cloudflare", {"account_id": "not-an-id"})


# ---------------------------------------------------------------------------
# Cloudflare Workers AI
# ---------------------------------------------------------------------------


class CloudflareTests(Base):
    PARAMS = {"provider": "cloudflare", "modality": "image", "prompt": "a lighthouse at dusk", "steps": 4}

    def test_success_writes_image_and_metadata(self):
        self.transport.on("POST", "/ai/run/@cf/black-forest-labs/flux-1-schnell",
                          ok_json({"success": True, "result": {"image": base64.b64encode(JPEG).decode()}}))
        m = self.manager()
        self.ready(m, "cloudflare")
        job = self.submit(m, self.PARAMS)
        self.assertEqual(job["state"], "succeeded", job)
        call = self.transport.calls[0]
        self.assertIn(f"/accounts/{ACCOUNT}/ai/run/", call["url"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer cf_secret_token_9876")
        self.assertEqual(json.loads(call["data"]), {"prompt": "a lighthouse at dusk", "steps": 4})
        path = os.path.join(self.out, job["result"]["filename"])
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), JPEG)
        self.assertTrue(path.endswith(".jpg"))
        with open(os.path.splitext(path)[0] + ".meta.json", encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual(meta["generation_mode"], "external_image")
        self.assertEqual(meta["external"]["provider"], "cloudflare")
        self.assertIn("workspace=default", job["result"]["url"])

    def test_free_allowance_exhausted(self):
        self.transport.on("POST", "/ai/run/", ok_json(
            {"success": False, "errors": [{"code": 4006, "message":
                                           "you have used up your daily free allocation of 10,000 neurons"}]},
            status=429))
        m = self.manager()
        self.ready(m, "cloudflare")
        job = self.submit(m, self.PARAMS)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "quota_exhausted")
        self.assertIn("4006", job["error"]["message"])
        self.assertEqual(os.listdir(self.out) if os.path.isdir(self.out) else [], [])

    def test_rejected_token(self):
        self.transport.on("POST", "/ai/run/", ok_json(
            {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}, status=401))
        m = self.manager()
        self.ready(m, "cloudflare")
        job = self.submit(m, self.PARAMS)
        self.assertEqual(job["error"]["code"], "auth_failed")

    def test_answer_that_is_not_an_image_fails(self):
        self.transport.on("POST", "/ai/run/", ok_json(
            {"success": True, "result": {"image": base64.b64encode(b"<html>oops</html>").decode()}}))
        m = self.manager()
        self.ready(m, "cloudflare")
        job = self.submit(m, self.PARAMS)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "invalid_output")
        self.assertIsNone(job["result"])

    def test_network_error_and_timeout(self):
        for exc, code in ((urlerror.URLError("Name or service not known"), "network_error"),
                          (TimeoutError("timed out"), "timeout")):
            with self.subTest(code=code):
                self.transport.routes.clear()
                self.transport.on("POST", "/ai/run/", exc)
                m = self.manager()
                self.ready(m, "cloudflare")
                job = self.submit(m, self.PARAMS)
                self.assertEqual(job["error"]["code"], code)

    def test_server_error_is_provider_unavailable(self):
        self.transport.on("POST", "/ai/run/", HttpResponse(503, {}, b"Service Unavailable"))
        m = self.manager()
        self.ready(m, "cloudflare")
        job = self.submit(m, self.PARAMS)
        self.assertEqual(job["error"]["code"], "provider_unavailable")

    def test_cancel_while_waiting_discards_result_and_says_quota_may_be_used(self):
        gate = threading.Event()

        def slow(url):
            gate.wait(5)
            return ok_json({"success": True, "result": {"image": base64.b64encode(PNG).decode()}})

        self.transport.on("POST", "/ai/run/", slow)
        m = self.manager()
        self.ready(m, "cloudflare")
        quote = m.quote(self.PARAMS)
        job = m.submit(self.PARAMS, quote["confirm_token"])
        for _ in range(200):
            if m.get(job["id"])["state"] == "running":
                break
            time.sleep(0.01)
        self.assertEqual(m.cancel(job["id"])["state"], "cancelling")
        gate.set()
        done = m.wait(job["id"], 5)
        self.assertEqual(done["state"], "cancelled")
        self.assertIn("may have consumed quota", done["progress"])
        self.assertIsNone(done["result"])

    def test_credentials_check_uses_token_verify(self):
        self.transport.on("GET", "/user/tokens/verify", ok_json({"success": True, "result": {"status": "active"}}))
        m = self.manager(start_threads=False)
        self.ready(m, "cloudflare")
        self.assertTrue(m.test_credentials("cloudflare")["ok"])


# ---------------------------------------------------------------------------
# Replicate
# ---------------------------------------------------------------------------


def prediction(status, output=None, error=None, pid="pred123abc"):
    return ok_json({"id": pid, "status": status, "output": output, "error": error, "logs": "",
                    "metrics": {"predict_time": 1.5} if status == "succeeded" else {}},
                   status=201 if status == "starting" else 200)


class ReplicateTests(Base):
    IMAGE = {"provider": "replicate", "modality": "image", "prompt": "a red fox", "aspect_ratio": "16:9", "seed": 7}
    VIDEO = {"provider": "replicate", "modality": "video", "prompt": "ocean waves", "resolution": "480p"}

    def test_image_success_polls_and_downloads(self):
        out_url = "https://replicate.delivery/xezq/abc/out-0.png"
        (self.transport
         .on("POST", "/models/black-forest-labs/flux-schnell/predictions", prediction("starting"))
         .on("GET", "/predictions/pred123abc", prediction("processing"), prediction("succeeded", [out_url]))
         .on("GET", "replicate.delivery", HttpResponse(200, {"content-type": "image/png"}, PNG)))
        m = self.manager()
        self.ready(m, "replicate")
        job = self.submit(m, self.IMAGE)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(job["remote_id"], "pred123abc")
        body = json.loads(self.transport.calls[0]["data"])
        self.assertEqual(body["input"], {"prompt": "a red fox", "aspect_ratio": "16:9", "num_outputs": 1,
                                         "output_format": "png", "seed": 7})
        self.assertEqual(self.transport.calls[0]["headers"]["Authorization"], "Bearer r8_secret_token_1234")
        self.assertNotIn("Authorization", self.transport.called("GET", "replicate.delivery")[0]["headers"])
        self.assertTrue(job["result"]["filename"].endswith(".png"))

    def test_video_success(self):
        (self.transport
         .on("POST", "/models/wan-video/wan-2.2-t2v-fast/predictions", prediction("starting"))
         .on("GET", "/predictions/pred123abc", prediction("succeeded", "https://replicate.delivery/v.mp4"))
         .on("GET", "replicate.delivery", HttpResponse(200, {}, MP4)))
        m = self.manager()
        self.ready(m, "replicate")
        job = self.submit(m, self.VIDEO)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(job["result"]["kind"], "video")
        sent = json.loads(self.transport.calls[0]["data"])["input"]
        self.assertEqual(sent["num_frames"], 81)
        self.assertEqual(sent["frames_per_second"], 16)
        self.assertEqual(sent["resolution"], "480p")

    def test_http_errors_are_classified(self):
        cases = [
            (HttpResponse(402, {}, json.dumps({"title": "Insufficient credit", "detail":
                          "You have insufficient credit to run this model."}).encode()), "insufficient_credit"),
            (HttpResponse(401, {}, json.dumps({"detail": "Invalid token."}).encode()), "auth_failed"),
            (HttpResponse(429, {"retry-after": "8"}, json.dumps({"detail": "Request was throttled."}).encode()),
             "rate_limited"),
            (HttpResponse(422, {}, json.dumps({"detail": "num_frames must be <= 100"}).encode()), "invalid_input"),
            (HttpResponse(404, {}, json.dumps({"detail": "Not found."}).encode()), "not_found"),
            (HttpResponse(500, {}, b"boom"), "provider_unavailable"),
        ]
        for answer, code in cases:
            with self.subTest(code=code):
                self.transport.routes.clear()
                self.transport.on("POST", "/predictions", answer)
                m = self.manager()
                self.ready(m, "replicate")
                job = self.submit(m, self.IMAGE)
                self.assertEqual(job["state"], "failed")
                self.assertEqual(job["error"]["code"], code)
        self.assertEqual(job["error"]["http_status"], 500)

    def test_prediction_failure_reports_provider_reason(self):
        (self.transport.on("POST", "/predictions", prediction("starting"))
         .on("GET", "/predictions/pred123abc", prediction("failed", error="NSFW content detected")))
        m = self.manager()
        self.ready(m, "replicate")
        job = self.submit(m, self.IMAGE)
        self.assertEqual(job["error"]["code"], "provider_failed")
        self.assertIn("NSFW content detected", job["error"]["message"])

    def test_wrong_media_type_and_html_never_succeed(self):
        for body in (PNG, b"<html>error page</html>"):
            with self.subTest(body=body[:6]):
                self.transport.routes.clear()
                (self.transport.on("POST", "/predictions", prediction("starting"))
                 .on("GET", "/predictions/pred123abc", prediction("succeeded", "https://replicate.delivery/x"))
                 .on("GET", "replicate.delivery", HttpResponse(200, {}, body)))
                m = self.manager()
                self.ready(m, "replicate")
                job = self.submit(m, self.VIDEO)
                self.assertEqual(job["state"], "failed")
                self.assertEqual(job["error"]["code"], "invalid_output")

    def test_success_without_output_fails(self):
        (self.transport.on("POST", "/predictions", prediction("starting"))
         .on("GET", "/predictions/pred123abc", prediction("succeeded", None)))
        m = self.manager()
        self.ready(m, "replicate")
        job = self.submit(m, self.IMAGE)
        self.assertEqual(job["error"]["code"], "invalid_output")

    def test_cancel_reaches_replicate(self):
        (self.transport.on("POST", "/models/", prediction("starting"))
         .on("GET", "/predictions/pred123abc", prediction("processing"))
         .on("POST", "/predictions/pred123abc/cancel", prediction("canceled")))
        m = self.manager()
        self.ready(m, "replicate")
        params = self.VIDEO
        job = m.submit(params, m.quote(params)["confirm_token"])
        for _ in range(300):
            if self.transport.called("GET", "/predictions/pred123abc"):
                break
            time.sleep(0.01)
        m.cancel(job["id"])
        done = m.wait(job["id"], 5)
        self.assertEqual(done["state"], "cancelled")
        self.assertEqual(len(self.transport.called("POST", "/cancel")), 1)
        self.assertIn("Cancelled on Replicate", done["progress"])

    def test_cancel_before_start_sends_nothing(self):
        m = self.manager(start_threads=False)
        self.ready(m, "replicate")
        job = m.submit(self.IMAGE, m.quote(self.IMAGE)["confirm_token"])
        self.assertEqual(m.cancel(job["id"])["state"], "cancelled")
        m.run_job(job["id"])
        self.assertEqual(self.transport.calls, [])

    def test_timeout_cancels_remotely(self):
        ticks = iter(range(0, 10_000_000, 400))
        (self.transport.on("POST", "/models/", prediction("starting"))
         .on("GET", "/predictions/pred123abc", prediction("processing"))
         .on("POST", "/cancel", prediction("canceled")))
        m = self.manager(clock=lambda: float(next(ticks)))
        self.ready(m, "replicate")
        job = self.submit(m, self.VIDEO)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "timeout")
        self.assertEqual(len(self.transport.called("POST", "/cancel")), 1)

    def test_restart_resumes_replicate_and_closes_sync_jobs(self):
        m = self.manager(start_threads=False)
        self.ready(m, "replicate")
        self.ready(m, "cloudflare")
        r = m.submit(self.IMAGE, m.quote(self.IMAGE)["confirm_token"])
        c = m.submit(CloudflareTests.PARAMS, m.quote(CloudflareTests.PARAMS)["confirm_token"])
        m._jobs[r["id"]].update(state="running", remote_id="pred123abc")
        m._jobs[c["id"]].update(state="running")
        m._save_jobs()
        (self.transport.on("GET", "/predictions/pred123abc", prediction("succeeded", ["https://replicate.delivery/a.png"]))
         .on("GET", "replicate.delivery", HttpResponse(200, {}, PNG)))
        again = self.manager()
        self.assertEqual(again.wait(r["id"], 5)["state"], "succeeded")
        closed = again.get(c["id"])
        self.assertEqual(closed["state"], "failed")
        self.assertEqual(closed["error"]["code"], "interrupted")
        self.assertEqual(self.transport.called("POST", "/predictions"), [])

    def test_credentials_check_uses_account_endpoint(self):
        self.transport.on("GET", "/v1/account", ok_json({"username": "someone", "type": "user"}))
        m = self.manager(start_threads=False)
        self.ready(m, "replicate")
        result = m.test_credentials("replicate")
        self.assertIn("someone", result["message"])
        self.transport.routes.clear()
        self.transport.on("GET", "/v1/account", HttpResponse(401, {}, b'{"detail":"Invalid token."}'))
        with self.assertRaises(em.ExternalMediaError) as ctx:
            m.test_credentials("replicate")
        self.assertEqual(ctx.exception.code, "auth_failed")


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services import external_media_api
except Exception:  # pragma: no cover - fastapi missing
    FastAPI = None


@unittest.skipIf(FastAPI is None, "fastapi not installed")
class ApiTests(Base):
    def client(self):
        m = self.manager(start_threads=False)
        app = FastAPI()
        app.include_router(external_media_api.create_router(lambda: m))
        return TestClient(app), m

    def test_flow_and_error_shapes(self):
        client, m = self.client()
        r = client.put("/api/v1/external/providers/replicate",
                       json={"credentials": {"api_token": "r8_api_secret_5555"}, "enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("r8_api_secret_5555", r.text)
        replicate = next(p for p in r.json()["providers"] if p["id"] == "replicate")
        self.assertTrue(replicate["enabled"])
        self.assertTrue(replicate["configured"])
        params = {"provider": "replicate", "modality": "image", "prompt": "a fox"}
        q = client.post("/api/v1/external/quote", json=params).json()
        refused = client.post("/api/v1/external/jobs", json={"params": params, "confirm_token": "wrong"})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["detail"]["code"], "confirmation_required")
        accepted = client.post("/api/v1/external/jobs", json={"params": params, "confirm_token": q["confirm_token"]})
        self.assertEqual(accepted.status_code, 200)
        job_id = accepted.json()["id"]
        cancelled = client.post(f"/api/v1/external/jobs/{job_id}/cancel").json()
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(client.get("/api/v1/external/jobs/nope").status_code, 404)
        bad = client.post("/api/v1/external/quote", json={"provider": "replicate", "modality": "image"})
        self.assertEqual(bad.status_code, 400)
        cleared = client.delete("/api/v1/external/providers/replicate/credentials").json()
        replicate = next(p for p in cleared["providers"] if p["id"] == "replicate")
        self.assertFalse(replicate["enabled"])
        self.assertFalse(replicate["configured"])

    def test_missing_credentials_status(self):
        client, m = self.client()
        m.set_enabled("cloudflare", True)
        params = {"provider": "cloudflare", "modality": "image", "prompt": "x"}
        token = client.post("/api/v1/external/quote", json=params).json()["confirm_token"]
        r = client.post("/api/v1/external/jobs", json={"params": params, "confirm_token": token})
        self.assertEqual(r.status_code, 412)
        self.assertEqual(r.json()["detail"]["code"], "missing_credentials")


if __name__ == "__main__":
    unittest.main()
