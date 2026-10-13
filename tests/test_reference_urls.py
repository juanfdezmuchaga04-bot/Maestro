"""Reference URL import: SSRF guard, content rules, streaming caps, cleanup.

The origin is a local stub server, which is exactly the case the guard has to
*refuse* by default — hence ``allow_private=True`` in the fetch tests, mirroring
what a trusted local deployment (or a test) does.  The guard itself is asserted
against every blocked address class without needing the network.
"""
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.reference_urls import (  # noqa: E402
    ReferenceUrlError,
    describe_blocked_target,
    fetch_reference_url,
    validate_reference_url,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    requests: list = []

    def log_message(self, *args):
        return

    @property
    def _host(self):
        return self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"

    def _send(self, body=b"", *, code=200, content_type="application/octet-stream", headers=None):
        self.send_response(code)
        if content_type:
            self.send_header("Content-Type", content_type)
        if body is not None:
            self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        type(self).requests.append(path)
        if path == "/clip.png":
            return self._send(PNG, content_type="image/png")
        if path == "/also.png":
            return self._send(PNG, content_type="image/png")
        if path == "/page.html":
            return self._send(b"<html>hi</html>", content_type="text/html; charset=utf-8")
        if path == "/archive.weird":
            return self._send(b"PK\x03\x04", content_type="application/x-msdownload")
        if path == "/empty":
            return self._send(b"", content_type="image/png")
        if path == "/missing":
            return self._send(b"gone", code=404, content_type="text/plain")
        if path == "/to-bad-port":
            return self._send(b"", code=302, content_type="",
                              headers={"Location": "http://127.0.0.1:9/x"})
        if path == "/to-file":
            return self._send(b"", code=302, content_type="",
                              headers={"Location": "file:///etc/passwd"})
        if path == "/declared-huge":
            # Declares 100 MB and sends nothing: the caller must refuse on the
            # declared size, before transferring a byte.
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", "100000000")
            self.end_headers()
            return None
        if path == "/underdeclared":
            # No Content-Length and a body far larger than the caller's cap: the
            # fetcher must stop on the bytes it actually received.
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            try:
                for _ in range(8):
                    self.wfile.write(b"\x00" * (256 * 1024))
            except (ConnectionResetError, BrokenPipeError):
                # The client stopped reading once its cap was hit — that is the
                # behaviour under test, not an error here.
                pass
            return None
        return self._send(b"nope", code=404, content_type="text/plain")


class OriginTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Origin.requests = []
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = os.path.join(self._tmp.name, "refs")

    def tearDown(self):
        self._tmp.cleanup()

    def stored(self):
        """Files left in the destination; the guard refuses before creating it."""
        return sorted(os.listdir(self.dest)) if os.path.isdir(self.dest) else []

    def fetch(self, path, **kwargs):
        kwargs.setdefault("allow_private", True)
        kwargs.setdefault("allowed_ports", [self.port])
        return fetch_reference_url(f"{self.base}{path}", dest_dir=self.dest, **kwargs)


class GuardTest(unittest.TestCase):
    """No network is needed: the guard runs before any request is made."""

    def test_only_http_and_https_are_allowed(self):
        for bad in (
            "file:///etc/passwd",
            "gopher://127.0.0.1:70/_x",
            "ftp://example.com/a.png",
            "data:text/plain;base64,aGk=",
            "javascript:alert(1)",
            "ws://example.com/socket",
        ):
            with self.assertRaises(ReferenceUrlError) as ctx:
                validate_reference_url(bad)
            self.assertTrue(
                "not importable" in str(ctx.exception) or "only http" in str(ctx.exception),
                str(ctx.exception),
            )

    def test_a_missing_scheme_is_refused(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            validate_reference_url("example.com/a.png")
        self.assertIn("http://", str(ctx.exception))

    def test_credentials_and_control_characters_are_refused(self):
        with self.assertRaises(ReferenceUrlError):
            validate_reference_url("http://user:pass@example.com/a.png")
        with self.assertRaises(ReferenceUrlError):
            validate_reference_url("http://example.com/a\n.png")

    def test_loopback_private_and_link_local_literals_are_refused(self):
        for address, reason in (
            ("127.0.0.1", "loopback"),
            ("127.1.2.3", "loopback"),
            ("0.0.0.0", "unspecified"),
            ("10.1.2.3", "private"),
            ("172.16.5.5", "private"),
            ("192.168.0.10", "private"),
            ("169.254.169.254", "cloud instance-metadata"),
            ("100.100.100.200", "cloud instance-metadata"),
            ("224.0.0.1", "multicast"),
        ):
            with self.assertRaises(ReferenceUrlError) as ctx:
                validate_reference_url(f"http://{address}/a.png")
            self.assertIn(reason, str(ctx.exception), address)

    def test_ipv6_loopback_and_ipv4_mapped_loopback_are_refused(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            validate_reference_url("http://[::1]/a.png")
        self.assertIn("loopback", str(ctx.exception))
        with self.assertRaises(ReferenceUrlError) as ctx:
            validate_reference_url("http://[::ffff:127.0.0.1]/a.png")
        self.assertIn("IPv4-mapped", str(ctx.exception))
        self.assertIn("loopback", str(ctx.exception))

    def test_a_public_literal_is_accepted_and_ports_are_restricted(self):
        checked = validate_reference_url("http://93.184.216.34/a.png")
        self.assertEqual(checked["addresses"], ["93.184.216.34"])
        with self.assertRaises(ReferenceUrlError) as ctx:
            validate_reference_url("http://93.184.216.34:8080/a.png")
        self.assertIn("Port 8080", str(ctx.exception))
        self.assertEqual(
            validate_reference_url("http://93.184.216.34:8080/a.png",
                                   allowed_ports=[8080])["port"], 8080)

    def test_cloud_metadata_hostnames_are_refused_before_resolution(self):
        for host in ("metadata.google.internal", "instance-data", "metadata.azure.com"):
            with self.assertRaises(ReferenceUrlError) as ctx:
                validate_reference_url(f"http://{host}/x")
            self.assertIn("metadata", str(ctx.exception))

    def test_platform_page_urls_are_refused_without_circumventing_anything(self):
        for url, name in (
            ("https://www.youtube.com/watch?v=abc", "YouTube"),
            ("https://youtu.be/abc", "YouTube"),
            ("https://instagram.com/p/x", "Instagram"),
            ("https://www.tiktok.com/@a/video/1", "TikTok"),
            ("https://x.com/a/status/1", "X/Twitter"),
        ):
            with self.assertRaises(ReferenceUrlError) as ctx:
                validate_reference_url(url)
            message = str(ctx.exception)
            self.assertIn(name, message)
            self.assertIn("does not circumvent", message)

    def test_blocked_target_reasons(self):
        self.assertEqual(describe_blocked_target("8.8.8.8"), "")
        self.assertEqual(describe_blocked_target("1.1.1.1"), "")
        self.assertIn("loopback", describe_blocked_target("::1"))
        self.assertIn("private", describe_blocked_target("fd00::1"))
        # 240.0.0.0/4 and 192.0.2.0/24 report as private or reserved depending on
        # the interpreter's tables; what matters is that neither is reachable.
        self.assertTrue(describe_blocked_target("240.0.0.1"))
        self.assertTrue(describe_blocked_target("192.0.2.1"))
        self.assertIn("not a valid IP", describe_blocked_target("nope"))


class FetchTest(OriginTestCase):
    def test_a_reference_file_is_stored_with_its_metadata(self):
        result = self.fetch("/clip.png")
        self.assertEqual(result["kind"], "image")
        self.assertEqual(result["bytes"], len(PNG))
        self.assertEqual(result["content_type"], "image/png")
        self.assertEqual(result["host"], "127.0.0.1")
        self.assertTrue(os.path.isfile(result["path"]))
        with open(result["path"], "rb") as handle:
            self.assertEqual(handle.read(), PNG)
        self.assertEqual(len(result["sha256"]), 64)
        self.assertFalse(result["deduplicated"])
        self.assertIn("never executed", result["note"])
        self.assertIn("/clip.png", result["final_url"])

    def test_identical_content_is_not_stored_twice(self):
        first = self.fetch("/clip.png")
        second = self.fetch("/also.png")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["path"], first["path"])
        self.assertEqual(self.stored(), [os.path.basename(first["path"])])

    def test_a_web_page_is_refused_with_an_actionable_message(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            self.fetch("/page.html")
        message = str(ctx.exception)
        self.assertIn("web page", message)
        self.assertIn("does not scrape pages", message)

    def test_an_unsupported_content_type_is_refused(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            self.fetch("/archive.weird")
        self.assertIn("application/x-msdownload", str(ctx.exception))

    def test_inaccessible_pages_are_reported_clearly(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            self.fetch("/missing")
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertIn("import it", str(ctx.exception))

    def test_an_empty_response_is_refused_and_leaves_nothing_behind(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            self.fetch("/empty")
        self.assertIn("empty file", str(ctx.exception))
        self.assertEqual(self.stored(), [])

    def test_a_declared_size_over_the_limit_is_refused_before_downloading(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            self.fetch("/declared-huge", max_bytes=1024)
        self.assertIn("import limit", str(ctx.exception))
        self.assertEqual(self.stored(), [])

    def test_the_byte_cap_is_enforced_while_streaming(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            self.fetch("/underdeclared", max_bytes=64 * 1024)
        self.assertIn("import limit", str(ctx.exception))
        # The partial file is cleaned up, never left as a usable artifact.
        self.assertEqual(self.stored(), [])

    def test_a_redirect_to_a_disallowed_port_is_refused_per_hop(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            self.fetch("/to-bad-port")
        self.assertIn("Port 9", str(ctx.exception))
        self.assertEqual(self.stored(), [])

    def test_a_redirect_to_another_scheme_is_refused_per_hop(self):
        with self.assertRaises(ReferenceUrlError) as ctx:
            self.fetch("/to-file")
        message = str(ctx.exception)
        self.assertIn("will not follow", message)
        self.assertIn("file:///etc/passwd", message)
        # Only the original request was made; the redirect was refused first.
        self.assertEqual(Origin.requests, ["/to-file"])
        self.assertEqual(self.stored(), [])

    def test_the_same_url_is_refused_when_the_guard_is_left_on(self):
        # This is the default posture: a private target is refused, and no
        # request reaches the origin at all.
        with self.assertRaises(ReferenceUrlError) as ctx:
            fetch_reference_url(
                f"{self.base}/clip.png",
                dest_dir=self.dest,
                allowed_ports=[self.port],
            )
        self.assertIn("loopback", str(ctx.exception))
        self.assertEqual(self.stored(), [])


if __name__ == "__main__":
    unittest.main()
