"""Where the API keys of opt-in external providers live — never in the config.

Maestro's other service keys sit in ``server_config["services"]`` in plain JSON.
Keys that can spend the user's money must not, so this module keeps them apart
and stores each one in the most protected place the computer offers:

1. **Environment variables** (read-only).  ``REPLICATE_API_TOKEN``,
   ``CLOUDFLARE_API_TOKEN`` and ``CLOUDFLARE_ACCOUNT_ID`` are honoured first, which
   is how a server or a CI machine provides them without anything on disk.
2. **The operating system's credential store** through ``keyring`` — Windows
   Credential Manager, macOS Keychain, or the Secret Service on Linux — when the
   package is installed and a real (not plaintext / fail) backend is active.
3. **Windows without keyring:** the value is encrypted with DPAPI for the current
   Windows user before it is written.
4. **Anything else:** a JSON file created with ``0600`` permissions under
   ``app/settings/external_providers``.  This is the weakest option and the status
   says so (``storage: "file_0600"``), so nobody is told a key is safer than it is.

What this module guarantees, and the tests pin:

* A secret is **never returned** by :meth:`SecretStore.describe` — only whether it
  is set, where it came from, and its last four characters.
* :meth:`SecretStore.get` is the only reader, and it is only called by the code
  that builds the provider request.
* Deleting a secret removes it from every writable backend.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from typing import Any, Callable, Mapping, Optional

__all__ = ["SecretStore", "ENV_OVERRIDES", "KEYRING_SERVICE"]

KEYRING_SERVICE = "maestro-external-providers"
SECRETS_FILENAME = "secrets.json"

# provider field -> environment variable that overrides it.
ENV_OVERRIDES: dict[str, str] = {
    "replicate.api_token": "REPLICATE_API_TOKEN",
    "cloudflare.api_token": "CLOUDFLARE_API_TOKEN",
    "cloudflare.account_id": "CLOUDFLARE_ACCOUNT_ID",
}


def _load_keyring() -> Any:
    """The ``keyring`` module when it has a backend worth trusting, else ``None``."""
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001
        return None
    name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
    if isinstance(backend, fail.Keyring) or "plaintext" in name or "null" in name:
        return None
    return keyring


# ---------------------------------------------------------------------------
# Windows DPAPI (CryptProtectData), used only when keyring is unavailable.
# ---------------------------------------------------------------------------


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = _Blob()
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = function(ctypes.byref(source), None, None, None, None, 0x01, ctypes.byref(target))
    if not ok:
        raise OSError("DPAPI refused the operation")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)  # type: ignore[attr-defined]


class SecretStore:
    """Secrets for external providers, keyed ``"<provider>.<field>"``."""

    def __init__(
        self,
        root: str,
        *,
        environ: Optional[Mapping[str, str]] = None,
        keyring_module: Any = "auto",
        platform: Optional[str] = None,
        dpapi: Optional[Callable[..., bytes]] = None,
    ) -> None:
        self._root = root
        self._environ = environ if environ is not None else os.environ
        self._keyring = _load_keyring() if keyring_module == "auto" else keyring_module
        self._platform = platform or sys.platform
        self._dpapi = dpapi or _dpapi
        self._lock = threading.RLock()

    # -- where things go --------------------------------------------------
    @property
    def storage(self) -> str:
        if self._keyring is not None:
            return "os_keyring"
        if self._platform.startswith("win"):
            return "file_dpapi"
        return "file_0600"

    def storage_label(self) -> str:
        return {
            "os_keyring": "Operating-system credential store (keyring)",
            "file_dpapi": "File encrypted with Windows DPAPI for this Windows user",
            "file_0600": "File readable only by this user (0600). Install 'keyring' for the OS credential store.",
        }[self.storage]

    def _file(self) -> str:
        return os.path.join(self._root, SECRETS_FILENAME)

    def _read_file(self) -> dict[str, str]:
        try:
            with open(self._file(), "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def _write_file(self, values: Mapping[str, str]) -> None:
        os.makedirs(self._root, exist_ok=True)
        path = self._file()
        tmp = path + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(values), handle)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    def _encode(self, value: str) -> str:
        if self.storage == "file_dpapi":
            return "dpapi:" + base64.b64encode(self._dpapi(value.encode("utf-8"), protect=True)).decode("ascii")
        return "plain:" + value

    def _decode(self, stored: str) -> str:
        if stored.startswith("dpapi:"):
            return self._dpapi(base64.b64decode(stored[6:]), protect=False).decode("utf-8")
        if stored.startswith("plain:"):
            return stored[6:]
        return ""

    # -- public API -------------------------------------------------------
    def source(self, key: str) -> str:
        env_name = ENV_OVERRIDES.get(key)
        if env_name and str(self._environ.get(env_name) or "").strip():
            return "env"
        if self._stored(key):
            return self.storage
        return ""

    def _stored(self, key: str) -> str:
        with self._lock:
            if self._keyring is not None:
                try:
                    return str(self._keyring.get_password(KEYRING_SERVICE, key) or "")
                except Exception:  # noqa: BLE001 - a locked keychain reads as unset
                    return ""
            stored = self._read_file().get(key, "")
            if not stored:
                return ""
            try:
                return self._decode(stored)
            except (OSError, ValueError):
                return ""

    def get(self, key: str) -> str:
        """The secret itself.  Only provider request builders call this."""
        env_name = ENV_OVERRIDES.get(key)
        if env_name:
            value = str(self._environ.get(env_name) or "").strip()
            if value:
                return value
        return self._stored(key).strip()

    def set(self, key: str, value: str) -> None:
        value = str(value or "").strip()
        if not value:
            self.delete(key)
            return
        if any(ch in value for ch in "\r\n\0"):
            raise ValueError("A key cannot contain line breaks")
        with self._lock:
            if self._keyring is not None:
                self._keyring.set_password(KEYRING_SERVICE, key, value)
                return
            values = self._read_file()
            values[key] = self._encode(value)
            self._write_file(values)

    def delete(self, key: str) -> None:
        with self._lock:
            if self._keyring is not None:
                try:
                    self._keyring.delete_password(KEYRING_SERVICE, key)
                except Exception:  # noqa: BLE001 - deleting an absent key is fine
                    pass
            values = self._read_file()
            if key in values:
                values.pop(key)
                self._write_file(values)

    def describe(self, key: str) -> dict[str, Any]:
        """What the UI may know about a secret: never the secret itself."""
        value = self.get(key)
        hint = ""
        if value and len(value) >= 8:
            hint = "…" + value[-4:]
        return {
            "set": bool(value),
            "source": self.source(key) if value else "",
            "hint": hint,
            "env_var": ENV_OVERRIDES.get(key, ""),
        }
