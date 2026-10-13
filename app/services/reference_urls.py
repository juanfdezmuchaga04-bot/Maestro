"""Fetch a reference URL safely (spec §5.3).

Importing a public reference URL is the one place where the studio reaches out
to a host the user chose, which makes it the classic SSRF surface.  This module
is built so that the guard fails closed:

* **Scheme allow-list.**  Only ``http`` and ``https``.  ``file://``, ``gopher://``,
  ``data:``, ``ftp://`` and friends are refused by name, not by omission.
* **Network allow-list.**  The host is resolved *before* any request, and **every**
  address it resolves to must be public.  Loopback, RFC1918, link-local,
  unique-local IPv6, multicast, reserved and unspecified addresses are refused,
  along with the cloud metadata endpoints (``169.254.169.254``,
  ``metadata.google.internal`` …) that turn an SSRF into credential theft.
* **Per-hop validation.**  Redirects are re-validated, so a public URL cannot
  bounce you into ``http://localhost:8000``.  Redirect chains are capped.
* **Bounded.**  A byte cap enforced while streaming (not from a trusted
  ``Content-Length``), a content-type allow-list, and a hard timeout.
* **No execution, no bypass.**  Bytes are written to a file and never executed.
  Known platforms that gate their media behind authentication are *reported*, not
  circumvented: we do not strip protections or forge sessions.

One residual risk is stated rather than hidden: validation resolves the name, and
the HTTP client resolves it again when it connects, so a hostile DNS server could
answer differently the second time (DNS rebinding).  Resolving every address and
requiring *all* of them to be public makes that a narrow window, and the
documented remedy for a hardened deployment is to run the backend behind the
existing authenticated network path (``services.remote_access``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import tempfile
import time
from typing import Any, Iterable, Mapping, Optional
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse, urlunparse

__all__ = [
    "ReferenceUrlError",
    "BLOCKED_IP_REASONS",
    "ALLOWED_CONTENT_TYPES",
    "validate_reference_url",
    "describe_blocked_target",
    "fetch_reference_url",
]

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = (80, 443)
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 512 * 1024 * 1024  # 512 MB, same order as the upload cap
DEFAULT_USER_AGENT = "MaestroFilmStudio/1.0 (reference import)"

BLOCKED_SCHEMES = (
    "file",
    "ftp",
    "ftps",
    "gopher",
    "data",
    "javascript",
    "vbscript",
    "dict",
    "ldap",
    "ldaps",
    "tftp",
    "telnet",
    "smb",
    "ws",
    "wss",
    "blob",
    "about",
)

# Cloud instance metadata services.  Reaching one of these from a server-side
# fetch is how an SSRF escalates into cloud credential theft.
CLOUD_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "metadata.azure.com",
        "metadata.azure.net",
        "metadata.oraclecloud.com",
        "metadata.internal",
        "compute.internal",
    }
)
CLOUD_METADATA_IPS = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / GCP / OpenStack IMDS
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IMDS over IPv6
    }
)

# Only media, documents and subtitle formats.  HTML is refused on purpose: the
# studio imports files, it does not scrape pages.
ALLOWED_CONTENT_TYPES = (
    "image/",
    "video/",
    "audio/",
    "application/pdf",
    "application/json",
    "application/x-subrip",
    "text/plain",
    "text/markdown",
    "text/vtt",
    "text/csv",
)

# Platforms whose media is not at the URL you paste.  We say so plainly instead
# of pretending a download will work.
GUARDED_PLATFORMS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "twitter.com": "X/Twitter",
    "x.com": "X/Twitter",
    "netflix.com": "Netflix",
    "spotify.com": "Spotify",
    "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion",
}

# A host name, a dotted-quad IPv4 literal, or an IPv6 literal (urlparse strips
# the brackets, so '::1' and '::ffff:127.0.0.1' arrive with colons).
_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


class ReferenceUrlError(ValueError):
    """The URL is not importable, with a message safe to show the user."""


def _split_host_port(parsed) -> tuple[str, int]:
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ReferenceUrlError("The URL has no host")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ReferenceUrlError(f"Invalid port in URL: {exc}") from exc
    return host, int(port)


def validate_reference_url(
    url: str,
    *,
    allowed_ports: Iterable[int] = DEFAULT_PORTS,
    allow_private: bool = False,
) -> dict[str, Any]:
    """Validate the *shape* of a URL and its network target.  No request is made.

    Returns ``{"url", "host", "port", "scheme", "addresses"}``.  Raises
    :class:`ReferenceUrlError` with a user-safe message otherwise.
    """
    text = str(url or "").strip()
    if not text:
        raise ReferenceUrlError("Paste a URL to import")
    if len(text) > 4000:
        raise ReferenceUrlError("That URL is unreasonably long")
    if any(ch in text for ch in ("\n", "\r", "\t", "\x00")):
        raise ReferenceUrlError("That URL contains control characters")

    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise ReferenceUrlError("The URL must start with http:// or https://")
    if scheme in BLOCKED_SCHEMES:
        raise ReferenceUrlError(
            f"{scheme}:// addresses are not importable; only http and https are allowed"
        )
    if scheme not in ALLOWED_SCHEMES:
        raise ReferenceUrlError(
            f"Unsupported scheme {scheme!r}; only http and https are allowed"
        )
    if parsed.username or parsed.password:
        raise ReferenceUrlError("URLs with embedded credentials are not accepted")

    host, port = _split_host_port(parsed)
    if not _HOST_RE.match(host):
        raise ReferenceUrlError("That host name is not valid")
    if host in CLOUD_METADATA_HOSTS:
        raise ReferenceUrlError(
            "That host is a cloud instance-metadata service; the studio will not request it"
        )

    # Refused by host name, before any DNS lookup: these platforms never serve
    # the media at the page URL, so there is nothing to fetch and nothing to
    # obtain legitimately here.
    platform = _guarded_platform(host)
    if platform:
        raise ReferenceUrlError(
            f"{platform} serves its media through an authenticated player, not at the "
            f"page URL. Export or download the file from {platform} and import it "
            "directly — the studio does not circumvent a platform's access controls."
        )

    allowed = {int(value) for value in allowed_ports}
    if port not in allowed:
        raise ReferenceUrlError(
            f"Port {port} is not allowed; permitted ports are "
            + ", ".join(str(value) for value in sorted(allowed))
        )

    addresses: list[str] = []
    literal: Optional[str] = None
    try:
        literal = str(ipaddress.ip_address(host))
    except ValueError:
        literal = None

    if literal is not None:
        addresses = [literal]
    else:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise ReferenceUrlError(f"Could not resolve {host}: {exc}") from exc
        for info in infos:
            sockaddr = info[4]
            if sockaddr and sockaddr[0] not in addresses:
                addresses.append(str(sockaddr[0]))
    if not addresses:
        raise ReferenceUrlError(f"Could not resolve {host} to any address")

    if not allow_private:
        for address in addresses:
            blocked = describe_blocked_target(address)
            if blocked:
                raise ReferenceUrlError(
                    f"{host} resolves to {address}, which is {blocked}. "
                    "Private, loopback and metadata addresses are refused."
                )

    normalized = urlunparse(
        (scheme, parsed.netloc, parsed.path or "/", parsed.params, parsed.query, "")
    )
    return {
        "url": normalized,
        "host": host,
        "port": port,
        "scheme": scheme,
        "addresses": addresses,
    }


def describe_blocked_target(address: str) -> str:
    """Return a human reason if ``address`` must not be fetched, else ``""``."""
    raw = str(address or "").strip()
    if not raw:
        return "not a usable address"
    if raw in CLOUD_METADATA_IPS:
        return "a cloud instance-metadata address"
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return "not a valid IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 is still loopback.
        embedded = describe_blocked_target(str(ip.ipv4_mapped))
        if embedded:
            return f"an IPv4-mapped {embedded}"
    # Order matters for the *message*: in Python's tables 0.0.0.0 and 240.0.0.0/4
    # are both "private", so the more specific reason has to come first.
    if ip.is_unspecified:
        return "the unspecified address"
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_private:
        return "a private address"
    if ip.is_reserved:
        return "a reserved address"
    return ""


BLOCKED_IP_REASONS = describe_blocked_target


def _guarded_platform(host: str) -> Optional[str]:
    for suffix, name in GUARDED_PLATFORMS.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return None


def _resolve_content_kind(content_type: str, filename: str) -> str:
    lowered = (content_type or "").split(";")[0].strip().lower()
    if lowered.startswith("image/"):
        return "image"
    if lowered.startswith("video/"):
        return "video"
    if lowered.startswith("audio/"):
        return "audio"
    if lowered.startswith("text/") or lowered in {"application/json", "application/x-subrip"}:
        return "document"
    if lowered == "application/pdf":
        return "document"
    # Fall back to the extension the server gave us.
    from .film_projects import infer_media_kind

    return infer_media_kind(filename)


def _check_content_type(content_type: str, url: str, host: str) -> None:
    lowered = (content_type or "").split(";")[0].strip().lower()
    if not lowered:
        return
    if any(lowered.startswith(prefix) for prefix in ALLOWED_CONTENT_TYPES):
        return
    platform = _guarded_platform(host)
    if lowered == "text/html" or lowered.startswith("text/html"):
        if platform:
            raise ReferenceUrlError(
                f"That is a {platform} page, not a media file. {platform} serves its "
                "media through an authenticated player; the studio does not bypass that. "
                "Download the file from the platform (or export it) and import it directly."
            )
        raise ReferenceUrlError(
            "That URL serves a web page, not a media or document file. The studio "
            "imports files, it does not scrape pages — use the file's own URL."
        )
    raise ReferenceUrlError(
        f"That URL returns {lowered}, which is not an image, video, audio or document."
    )


class _SafeRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Re-validates every redirect hop against the same rules."""

    def __init__(self, *, allowed_ports: Iterable[int], allow_private: bool, max_redirects: int) -> None:
        super().__init__()
        self.allowed_ports = tuple(allowed_ports)
        self.allow_private = allow_private
        self.max_redirects = int(max_redirects)
        self.hops = 0
        self.chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        self.hops += 1
        if self.hops > self.max_redirects:
            raise ReferenceUrlError(
                f"That URL redirected more than {self.max_redirects} times; refusing to follow"
            )
        validate_reference_url(
            newurl,
            allowed_ports=self.allowed_ports,
            allow_private=self.allow_private,
        )
        self.chain.append(str(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_reference_url(
    url: str,
    *,
    dest_dir: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
    allowed_ports: Iterable[int] = DEFAULT_PORTS,
    extra_headers: Optional[Mapping[str, str]] = None,
    allow_private: bool = False,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    """Download a reference file with the guard described in the module docstring.

    ``allow_private`` exists for local/trusted use (and for the test suite); it
    must not be switched on for a server reachable by other people.
    """
    if not dest_dir:
        raise ReferenceUrlError("A destination directory is required")
    if max_bytes <= 0:
        raise ReferenceUrlError("max_bytes must be positive")
    checked = validate_reference_url(
        url, allowed_ports=allowed_ports, allow_private=allow_private
    )
    host = checked["host"]

    os.makedirs(dest_dir, exist_ok=True)
    redirect_handler = _SafeRedirectHandler(
        allowed_ports=allowed_ports, allow_private=allow_private, max_redirects=MAX_REDIRECTS
    )
    opener = urlrequest.build_opener(redirect_handler)

    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    if extra_headers:
        for key, value in extra_headers.items():
            name = str(key)
            if name.lower() in {"host", "content-length", "transfer-encoding"}:
                continue  # let the transport own these
            headers[name] = str(value)[:1000]

    request = urlrequest.Request(checked["url"], headers=headers, method="GET")
    started = time.time()
    fd, partial = tempfile.mkstemp(prefix=".reference-", suffix=".part", dir=dest_dir)
    digest = hashlib.sha256()
    written = 0
    try:
        try:
            response = opener.open(request, timeout=float(timeout))
        except urlerror.HTTPError as exc:
            if 300 <= int(getattr(exc, "code", 0)) < 400:
                # urllib refuses a redirect to any scheme outside http/https
                # before our own per-hop check runs; report that clearly instead
                # of as a bare status code.
                raise ReferenceUrlError(
                    "That URL redirected somewhere the studio will not follow "
                    f"({getattr(exc, 'reason', None) or 'refused redirect'})."
                ) from exc
            raise ReferenceUrlError(
                f"The server refused the request (HTTP {exc.code}). "
                "If the file needs a signed-in session, download it yourself and import it."
            ) from exc
        except urlerror.URLError as exc:
            raise ReferenceUrlError(f"Could not reach {host}: {exc.reason}") from exc
        except ReferenceUrlError:
            raise
        except OSError as exc:
            raise ReferenceUrlError(f"Could not reach {host}: {exc}") from exc

        with response:
            content_type = str(response.headers.get("Content-Type") or "")
            _check_content_type(content_type, checked["url"], host)
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ReferenceUrlError(
                    f"That file is {int(declared) / 1e6:.0f} MB, over the "
                    f"{max_bytes / 1e6:.0f} MB import limit"
                )
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise ReferenceUrlError(
                            f"That file is larger than the "
                            f"{max_bytes / 1e6:.0f} MB import limit"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            final_url = str(getattr(response, "url", checked["url"]))
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(partial):
            try:
                os.unlink(partial)
            except OSError:
                pass
        raise

    if written == 0:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise ReferenceUrlError("That URL returned an empty file")

    sha256 = digest.hexdigest()
    base = os.path.basename(urlparse(final_url).path) or f"reference-{sha256[:8]}"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:120] or f"reference-{sha256[:8]}"
    extension = os.path.splitext(safe_name)[1].lower()
    if not extension:
        guessed = (content_type or "").split(";")[0].strip().lower()
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "application/pdf": ".pdf",
            "text/plain": ".txt",
            "text/vtt": ".vtt",
        }.get(guessed, "")
        safe_name = f"{safe_name}{extension}"
    stored_name = f"{sha256[:16]}{extension or ''}"
    final_path = os.path.join(dest_dir, stored_name)

    if os.path.exists(final_path):
        # Identical content already imported: reuse it instead of storing twice.
        try:
            os.unlink(partial)
        except OSError:
            pass
        deduplicated = True
    else:
        os.replace(partial, final_path)
        deduplicated = False

    return {
        "source_url": checked["url"],
        "final_url": final_url,
        "redirect_chain": list(redirect_handler.chain),
        "host": host,
        "content_type": content_type,
        "kind": _resolve_content_kind(content_type, safe_name),
        "filename": safe_name,
        "stored_name": stored_name,
        "path": final_path,
        "bytes": written,
        "sha256": sha256,
        "deduplicated": deduplicated,
        "fetched_at": time.time(),
        "elapsed_seconds": round(time.time() - started, 3),
        "note": "Downloaded as a reference asset. The file is stored, never executed.",
    }
