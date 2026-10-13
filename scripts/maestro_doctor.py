"""Maestro doctor — what actually works on this computer right now.

Run this before you expose Maestro to a phone, and again whenever something
stops working:

    python scripts/maestro_doctor.py

It is read-only: it starts nothing, downloads nothing, changes no file. Every
line is either measured on this machine or reported as unknown with the reason,
so the report never claims a capability it did not observe.

Exit codes: 0 = nothing blocking, 1 = at least one blocking problem.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_DIR = os.path.join(_REPO_ROOT, "app")
sys.path.insert(0, _APP_DIR)

from services import access_gate as access_gate_service  # noqa: E402
from services import comfy_bootstrap, comfy_models, local_hardware  # noqa: E402
from services.comfyui_adapter import (  # noqa: E402
    default_templates_dir,
    list_templates,
    template_supported,
)

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
_LABEL = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[fail]", UNKNOWN: "[?]   "}


class Report:
    def __init__(self) -> None:
        self.sections: list[tuple[str, list[tuple[str, str, str]]]] = []
        self.actions: list[str] = []
        self.blocking = 0

    def section(self, title: str, rows: list[tuple[str, str, str]]) -> None:
        self.sections.append((title, rows))
        for status, _, _ in rows:
            if status == FAIL:
                self.blocking += 1

    def action(self, text: str) -> None:
        if text not in self.actions:
            self.actions.append(text)

    def render(self) -> str:
        lines = ["Maestro doctor — production preflight", "=" * 38, ""]
        for title, rows in self.sections:
            lines.append(title)
            for status, summary, detail in rows:
                prefix = _LABEL.get(status, "[?]   ")
                lines.append(f"  {prefix} {summary}")
                if detail:
                    for chunk in detail.splitlines():
                        lines.append(f"          {chunk}")
            lines.append("")
        if self.actions:
            lines.append("Next steps")
            for index, action in enumerate(self.actions, 1):
                lines.append(f"  {index}. {action}")
            lines.append("")
        lines.append(
            "Blocking problems: none."
            if not self.blocking
            else f"Blocking problems: {self.blocking} (see [fail] rows above)."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def _hardware() -> dict[str, Any]:
    return local_hardware.detect_hardware()


def check_computer(hardware: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    os_info = hardware.get("os") or {}
    rows.append((OK,
                 f"Operating system: {os_info.get('system')} {os_info.get('release')} "
                 f"({os_info.get('machine')})",
                 f"Python {os_info.get('python')} at {os_info.get('executable')}"))

    cores = (hardware.get("cpu") or {}).get("logical_cores")
    cpu_model = (hardware.get("cpu") or {}).get("model") or ""
    rows.append((OK, f"CPU cores: {cores} ({cpu_model})", "") if cores else
                (UNKNOWN, "CPU cores: could not be read", ""))

    ram_gb = (hardware.get("memory") or {}).get("total_gb")
    if ram_gb:
        rows.append((OK if ram_gb >= 16 else WARN,
                     f"System RAM: {ram_gb} GB",
                     "" if ram_gb >= 16 else
                     "16 GB or more is recommended: the video models offload to RAM "
                     "when VRAM runs short."))
    else:
        rows.append((UNKNOWN, "System RAM: could not be read", ""))

    disk = hardware.get("disk") or {}
    free = disk.get("free_gb")
    needed = comfy_models.required_disk_gb(comfy_models.MODELS["wan22_ti2v_5b"])
    if isinstance(free, (int, float)):
        rows.append((OK if free >= needed else FAIL,
                     f"Free disk at {disk.get('path')}: {free:.0f} GB",
                     "" if free >= needed else
                     f"The local engine needs about {needed:.0f} GB for its weights, "
                     "plus room for your renders."))
    else:
        rows.append((UNKNOWN, "Free disk: could not be read", ""))
    for problem in hardware.get("problems") or []:
        rows.append((WARN, f"Hardware probe note: {problem}", ""))
    return rows


def check_gpu(hardware: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    gpus = hardware.get("gpus") or []
    if gpus:
        for gpu in gpus:
            vram = round((gpu.get("vram_total_mb") or 0) / 1024)
            rows.append((OK, f"GPU: {gpu.get('name')} ({vram} GB VRAM, driver "
                             f"{gpu.get('driver_version') or 'unknown'})", ""))
        selection = comfy_models.select_video_model(hardware, preferred=("wan22_ti2v_5b",))
        model = selection.get("model")
        if model:
            rows.append((OK,
                         f"Local engine model: {model.get('label')} — fits this machine",
                         f"Licence: {model.get('licence')}. {selection.get('detail') or ''}"))
        else:
            rows.append((WARN, "No catalogue model fits the GPU that was measured",
                         (selection.get("detail") or "") + "\n" +
                         "\n".join("- " + b for b in selection.get("blockers") or [])))
    else:
        reason = (hardware.get("cuda") or {}).get("reason") or "nvidia-smi is not on PATH"
        rows.append((FAIL, "No usable NVIDIA GPU detected", reason))
        rows.append((WARN, "Video generation is unavailable on this machine",
                     "Importing media, the storyboard, audio analysis and assembly "
                     "still work. Generation needs a CUDA GPU with 8 GB+ VRAM."))

    try:
        import torch  # type: ignore
        cuda = bool(torch.cuda.is_available())
        rows.append((OK if cuda else FAIL,
                     f"PyTorch {torch.__version__}: CUDA {'available' if cuda else 'NOT available'}",
                     "" if cuda else
                     "Install the CUDA build of PyTorch for the Python environment "
                     "Maestro uses (Pinokio's Update does this)."))
    except Exception as exc:  # noqa: BLE001 - report, never crash the doctor
        rows.append((FAIL, "PyTorch is not importable in this interpreter",
                     f"{type(exc).__name__}: {exc}"))
    return rows


def check_media_tools(hardware: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    tools = hardware.get("tools") or local_hardware.tool_paths()
    ffmpeg_hint = (
        "Assembly, trimming, thumbnails and MP4 export need it. Install it and put "
        "its bin folder on PATH — on Windows the builds Maestro documents are at "
        "https://github.com/BtbN/FFmpeg-Builds/releases (unzip, add bin to PATH); "
        "on Linux `sudo apt install ffmpeg`."
    )
    for tool in ("ffmpeg", "ffprobe"):
        path = tools.get(tool)
        rows.append((OK, f"{tool}: {path}", "") if path else
                    (FAIL, f"{tool}: not found on PATH", ffmpeg_hint))
    git = tools.get("git")
    rows.append((OK, f"git: {git}", "") if git else
                (WARN, "git: not found on PATH",
                 "Only needed to install the local engine from source."))
    return rows


def check_ui_bundle() -> list[tuple[str, str, str]]:
    dist = os.path.join(_REPO_ROOT, "ui", "dist")
    index = os.path.join(dist, "index.html")
    if not os.path.isfile(index):
        return [(FAIL, "The web UI has not been built",
                 "The backend would serve the API only. Build it once:\n"
                 "cd ui && npm install && npm run build")]
    newest_source = 0.0
    src = os.path.join(_REPO_ROOT, "ui", "src")
    for root, _dirs, files in os.walk(src):
        for name in files:
            path = os.path.join(root, name)
            try:
                newest_source = max(newest_source, os.path.getmtime(path))
            except OSError:
                continue
    try:
        built_at = os.path.getmtime(index)
    except OSError:
        built_at = 0.0
    age_days = (time.time() - built_at) / 86400
    if newest_source > built_at:
        return [(WARN, "The built UI is older than its source",
                 "Rebuild so the browser loads the current interface:\n"
                 "cd ui && npm run build")]
    return [(OK, f"UI bundle present and current (built {age_days:.1f} days ago)", "")]


def _http_json(url: str, timeout: float = 4.0) -> tuple[int, Any]:
    try:
        with urlrequest.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local only
            body = response.read().decode("utf-8", "replace")
        try:
            return response.status, json.loads(body)
        except ValueError:
            return response.status, body[:200]
    except urlerror.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc)) from exc


def check_backend(port: int) -> tuple[list[tuple[str, str, str]], dict[str, Any] | None]:  # noqa: D401
    base = f"http://127.0.0.1:{port}"
    try:
        status, payload = _http_json(f"{base}/api/v1/access/status")
    except RuntimeError as exc:
        return ([(WARN, f"Maestro is not answering on {base}",
                  f"{exc}\nStart it (Pinokio → Start, or `python app/launch.py`) and "
                  "run the doctor again to check the engine handshake.")], None)
    if status != 200 or not isinstance(payload, dict):
        return ([(WARN, f"Maestro answered {status} on {base}", "")], None)
    mode = payload.get("mode") or "off"
    rows = [(OK, f"Backend answering on {base}", f"Sign-in gate: {mode}")]
    return rows, payload


def check_local_engine(hardware: dict[str, Any]) -> list[tuple[str, str, str]]:
    base_url = os.environ.get("MAESTRO_COMFY_URL", "http://127.0.0.1:8188")
    rows: list[tuple[str, str, str]] = []
    model = comfy_models.MODELS["wan22_ti2v_5b"]
    blockers = comfy_bootstrap.hardware_blockers(hardware, model)
    if blockers:
        rows.append((FAIL, "This computer cannot run the local engine yet",
                     "\n".join(f"- {blocker}" for blocker in blockers)))
        return rows
    try:
        from services import comfyui_adapter
        client = comfyui_adapter.ComfyUIClient(base_url)
        health = client.check_connection()
    except Exception as exc:  # noqa: BLE001
        rows.append((WARN, f"ComfyUI is not reachable at {base_url}",
                     f"{exc}\nStart ComfyUI on the GPU machine, then use Studio → "
                     "Compute & transfer → Local engine → Run the test."))
        return rows
    rows.append((OK, f"ComfyUI reachable at {base_url}", ""))
    try:
        object_info = client.object_info()
    except Exception as exc:  # noqa: BLE001
        rows.append((WARN, "Could not read ComfyUI's node list", str(exc)))
        return rows
    for template in list_templates(default_templates_dir()):
        supported, missing = template_supported(template, object_info)
        if supported:
            rows.append((OK, f"Workflow ready: {template.label}", ""))
        else:
            rows.append((FAIL, f"Workflow {template.template_id} needs a newer ComfyUI",
                         "Missing node classes: " + ", ".join(sorted(missing))))
    missing_files = comfy_bootstrap.missing_model_files(model, object_info)
    if missing_files:
        rows.append((FAIL, f"{model.label} is missing {len(missing_files)} weight file(s)",
                     "\n".join(
                         f"- {item['filename']} -> {item['directory']} "
                         f"({item['size_gb']} GB)"
                         for item in missing_files
                     )))
    else:
        rows.append((OK, f"{model.label} weights are present", ""))
    devices = health.get("devices") or []
    rows.append((OK if devices else WARN,
                 "ComfyUI reported its device(s)",
                 json.dumps(devices, ensure_ascii=False) if devices else
                 "ComfyUI did not list a GPU device."))
    return rows


def check_access_gate(bound_beyond_loopback: bool) -> list[tuple[str, str, str]]:
    settings_dir = os.path.join(_APP_DIR, "settings")
    gate = access_gate_service.AccessGate(settings_dir)
    status = gate.status()
    rows: list[tuple[str, str, str]] = []
    if not status["enabled"]:
        detail = (
            "Anyone who can reach this port can drive Maestro: generation, media "
            "access and file writes. That is fine on one computer."
        )
        rows.append((WARN if bound_beyond_loopback else OK,
                     f"Sign-in gate: off (mode {status['mode']})",
                     (detail + "\nTurn it on:  python scripts/maestro_access.py enable")
                     if bound_beyond_loopback else detail))
    else:
        rows.append((OK, f"Sign-in gate: on (mode {status['mode']})",
                     f"Sessions last {status['session_days']} days. Sign out on a "
                     "device by opening /api/v1/access/logout."))
    rows.append((OK, "No API key or credit balance is required",
                 "Generation runs on this computer. Optional external providers "
                 "stay unused unless you configure them yourself."))
    return rows


def check_remote_access() -> tuple[list[tuple[str, str, str]], str | None]:
    rows: list[tuple[str, str, str]] = []
    https_url: str | None = None
    try:
        from services.remote_access import TailscaleManager
        manager = TailscaleManager(os.path.join(_APP_DIR, "settings"), 7860)
        status = manager.status()
    except Exception as exc:  # noqa: BLE001
        return ([(UNKNOWN, "Tailscale status could not be read", str(exc))], None)
    if not status.get("installed"):
        rows.append((WARN, "Tailscale is not installed on this computer",
                     "It is the free, private way to reach Maestro from your phone "
                     "without exposing it: https://tailscale.com/download"))
        return rows, None
    rows.append((OK if status.get("connected") else WARN,
                 f"Tailscale: {'connected' if status.get('connected') else 'installed, not signed in'}",
                 ""))
    if status.get("configured") and status.get("https_url"):
        https_url = str(status["https_url"]).rstrip("/")
        rows.append((OK, f"Private HTTPS address for your phone: {https_url}", ""))
    else:
        rows.append((WARN, "No private HTTPS route is configured yet",
                     "In Pinokio choose Secure Remote Access (Tailscale), or in the "
                     "app use Settings → Notifications → Private HTTPS access."))
    return rows, https_url


def check_comfyui_exposure() -> list[tuple[str, str, str]]:
    addresses = _lan_addresses()
    for address in addresses:
        try:
            with socket.create_connection((address, 8188), timeout=0.6):
                return [(FAIL, f"ComfyUI appears reachable at http://{address}:8188",
                         "ComfyUI has no authentication of its own. Keep it on "
                         "127.0.0.1 (its default) so only Maestro on this computer "
                         "can use it.")]
        except OSError:
            continue
    return [(OK, "ComfyUI is not exposed on the local network", "")]


def _lan_addresses() -> list[str]:
    addresses: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(("8.8.8.8", 80))
        addresses.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in addresses and not address.startswith("127."):
                addresses.append(address)
    except OSError:
        pass
    return addresses


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def build_report(port: int) -> Report:
    hardware = _hardware()
    report = Report()
    report.section("This computer", check_computer(hardware))
    report.section("GPU and CUDA", check_gpu(hardware))
    report.section("Media tools", check_media_tools(hardware))
    report.section("Web interface", check_ui_bundle())

    backend_rows, gate_status = check_backend(port)
    report.section("Backend", backend_rows)

    lan = _lan_addresses()
    bind_name = (os.environ.get("SERVER_NAME") or "").strip()
    share = (os.environ.get("PINOKIO_SHARE_LOCAL") or "").strip().lower() == "true"
    bound_beyond_loopback = share or (bool(bind_name) and bind_name != "127.0.0.1")
    report.section("Sign-in gate", check_access_gate(bound_beyond_loopback))
    remote_rows, https_url = check_remote_access()
    report.section("Reach it from your phone", remote_rows)
    report.section("Local engine (ComfyUI)", check_local_engine(hardware))
    report.section("Safety checks", check_comfyui_exposure())

    report.section("Addresses for this computer", (
        [(OK, f"On this computer: http://127.0.0.1:{port}", "")] +
        [(OK, f"Same Wi-Fi: http://{address}:{port}",
          "" if share else "Only reachable if Pinokio's LAN share is on "
                           "(PINOKIO_SHARE_LOCAL=true).") for address in lan] +
        ([(OK, f"From anywhere you are signed in: {https_url}",
           "Tailscale serves it over private HTTPS to your own devices only.")]
         if https_url else
         [(WARN, "No private HTTPS address yet",
           "Install Tailscale on this computer and your phone, sign both into the "
           "same account, then run the Secure Remote Access action once.")])
    ))

    gate = access_gate_service.AccessGate(os.path.join(_APP_DIR, "settings")).status()
    if not gate["enabled"]:
        report.action(
            "python scripts/maestro_access.py enable   (create the access password "
            "before this port is reachable from another device)"
        )
    if not os.path.isfile(os.path.join(_REPO_ROOT, "ui", "dist", "index.html")):
        report.action("cd ui && npm install && npm run build")
    report.action(f"Start Maestro, then open it on the computer: http://127.0.0.1:{port}")
    report.action("In Studio → Compute & transfer → Local engine: Re-detect, Install, "
                  "Run the test, Register engine.")
    report.action("On the phone: open the private HTTPS address, Add to Home screen, "
                  "sign in with the access password.")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maestro production preflight")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SERVER_PORT") or 7860),
        help="Port the backend listens on (default: $SERVER_PORT or 7860).",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    args = parser.parse_args(argv)

    report = build_report(args.port)
    if args.json:
        print(json.dumps(
            {
                "sections": [
                    {"title": title, "rows": [
                        {"status": status, "summary": summary, "detail": detail}
                        for status, summary, detail in rows
                    ]}
                    for title, rows in report.sections
                ],
                "actions": report.actions,
                "blocking": report.blocking,
            },
            indent=2, ensure_ascii=False,
        ))
    else:
        print(report.render())
    return 1 if report.blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
