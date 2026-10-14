#!/usr/bin/env python3
"""Read-only GPU / backend diagnosis for one specific computer (e.g. a Lenovo IdeaPad S145).

Run it ON the machine being evaluated:

    python scripts/diagnose_gpu.py                 # inventory only, changes nothing
    python scripts/diagnose_gpu.py --probe         # also runs a tiny tensor op on each
                                                   # backend that is already installed
    python scripts/diagnose_gpu.py --json out.json # save the full report

It answers three questions with measurements, never with assumptions:

1. **What exactly is this machine?**  Manufacturer/model (Lenovo machine type such as
   ``81N3`` identifies the S145 variant), CPU, RAM, operating system and build, and
   every display adapter with its PCI vendor/device ID and driver version.
2. **Which GPU family is it?**  The PCI ID is matched against a small table of the
   GPUs that S145 variants ship with.  An unknown ID is reported as unknown.
3. **Which backends are candidates, and which were actually exercised?**  ROCm,
   DirectML, Vulkan, OpenCL and CPU are each reported as ``not_applicable``,
   ``unsupported_by_vendor``, ``candidate_untested``, ``installed_untested``,
   ``probe_passed`` or ``probe_failed``.  Only ``--probe`` can produce
   ``probe_passed``, and only for a backend that really ran a computation here.

Nothing is installed, downloaded or modified.  External tools are run as argument
lists with a timeout; a missing tool is recorded, not an error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Optional, Sequence

Runner = Callable[[Sequence[str], float], tuple[int, str]]

# PCI IDs of the GPUs found in Lenovo IdeaPad S145 configurations, plus the
# families a user is likely to meet.  "rocm" is AMD's own support statement for
# the architecture (ROCm officially targets CDNA and RDNA2+ parts; GCN APUs such
# as Raven/Picasso and Stoney Ridge are not on its support list).
KNOWN_GPUS: dict[str, dict[str, Any]] = {
    "1002:15d8": {"name": "AMD Radeon Vega 3/6/8/10 (Picasso APU, Ryzen 3000U)", "arch": "GCN5 / gfx902-family",
                  "s145": "S145-14API / S145-15API", "rocm": False, "directx12": True, "vulkan": True},
    "1002:15dd": {"name": "AMD Radeon Vega (Raven Ridge APU, Ryzen 2000U)", "arch": "GCN5 / gfx902",
                  "s145": "", "rocm": False, "directx12": True, "vulkan": True},
    "1002:98e4": {"name": "AMD Radeon R2/R3/R4/R5 (Stoney Ridge APU, A6-9225 / A9-9425)", "arch": "GCN3 / gfx810",
                  "s145": "S145-14AST / S145-15AST", "rocm": False, "directx12": True, "vulkan": True},
    "1002:1636": {"name": "AMD Radeon (Renoir APU, Ryzen 4000U)", "arch": "GCN5 / gfx90c",
                  "s145": "", "rocm": False, "directx12": True, "vulkan": True},
    "1002:164c": {"name": "AMD Radeon (Lucienne/Cezanne APU, Ryzen 5000U)", "arch": "GCN5 / gfx90c",
                  "s145": "", "rocm": False, "directx12": True, "vulkan": True},
    "8086:3ea0": {"name": "Intel UHD Graphics 620 (Whiskey Lake)", "arch": "Gen9.5",
                  "s145": "S145-14IWL / S145-15IWL", "rocm": False, "directx12": True, "vulkan": True},
    "8086:3185": {"name": "Intel UHD Graphics 600 (Gemini Lake)", "arch": "Gen9.5",
                  "s145": "S145-14IGM / S145-15IGM", "rocm": False, "directx12": True, "vulkan": True},
    "8086:3184": {"name": "Intel UHD Graphics 605 (Gemini Lake)", "arch": "Gen9.5",
                  "s145": "S145-14IGM / S145-15IGM", "rocm": False, "directx12": True, "vulkan": True},
    "8086:8a56": {"name": "Intel UHD Graphics (Ice Lake)", "arch": "Gen11",
                  "s145": "S145-14IIL / S145-15IIL", "rocm": False, "directx12": True, "vulkan": True},
    "8086:8a52": {"name": "Intel Iris Plus Graphics (Ice Lake)", "arch": "Gen11",
                  "s145": "S145-14IIL / S145-15IIL", "rocm": False, "directx12": True, "vulkan": True},
}


def _default_runner(command: Sequence[str], timeout: float) -> tuple[int, str]:
    if not shutil.which(command[0]):
        return 127, ""
    try:
        done = subprocess.run(list(command), capture_output=True, text=True, timeout=timeout,
                              errors="replace")  # noqa: S603 - fixed argument list
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _powershell_json(runner: Runner, script: str) -> Any:
    code, out = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"{script} | ConvertTo-Json -Depth 3"], 30)
    if code != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def pci_id_from_pnp(pnp: str) -> str:
    """``PCI\\VEN_1002&DEV_15D8&...`` → ``1002:15d8``."""
    match = re.search(r"VEN_([0-9A-Fa-f]{4}).*?DEV_([0-9A-Fa-f]{4})", pnp or "")
    return f"{match.group(1)}:{match.group(2)}".lower() if match else ""


def parse_lspci(output: str) -> list[dict[str, Any]]:
    """Display controllers from ``lspci -nn``."""
    gpus = []
    for line in output.splitlines():
        if not re.search(r"VGA compatible controller|3D controller|Display controller", line):
            continue
        ids = re.findall(r"\[([0-9a-f]{4}):([0-9a-f]{4})\]", line)
        pci = f"{ids[-1][0]}:{ids[-1][1]}" if ids else ""
        name = line.split(": ", 1)[-1]
        gpus.append({"name": re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", "", name).strip(), "pci_id": pci,
                     "driver_version": "", "adapter_ram_mb": None})
    return gpus


def identify(gpu: dict[str, Any]) -> dict[str, Any]:
    known = KNOWN_GPUS.get(gpu.get("pci_id", ""))
    return {**gpu, "known": known or None}


def collect(runner: Runner = _default_runner, system: Optional[str] = None) -> dict[str, Any]:
    system = system or platform.system()
    report: dict[str, Any] = {
        "tool": "maestro scripts/diagnose_gpu.py",
        "os": {"system": system, "release": platform.release(), "version": platform.version(),
               "machine": platform.machine(), "python": sys.version.split()[0]},
        "machine": {}, "cpu": "", "ram_mb": None, "gpus": [], "tools": {}, "notes": [],
    }
    if system == "Windows":
        cs = _powershell_json(runner, "Get-CimInstance Win32_ComputerSystem | "
                                      "Select-Object Manufacturer,Model,SystemFamily,TotalPhysicalMemory")
        cs = (_as_list(cs) or [{}])[0]
        report["machine"] = {"manufacturer": cs.get("Manufacturer"), "model": cs.get("Model"),
                             "family": cs.get("SystemFamily")}
        if cs.get("TotalPhysicalMemory"):
            report["ram_mb"] = int(int(cs["TotalPhysicalMemory"]) / 1048576)
        osinfo = (_as_list(_powershell_json(runner, "Get-CimInstance Win32_OperatingSystem | "
                                                    "Select-Object Caption,Version,BuildNumber")) or [{}])[0]
        report["os"].update(caption=osinfo.get("Caption"), build=osinfo.get("BuildNumber"))
        cpu = (_as_list(_powershell_json(runner, "Get-CimInstance Win32_Processor | Select-Object Name")) or [{}])[0]
        report["cpu"] = str(cpu.get("Name") or "").strip()
        for item in _as_list(_powershell_json(runner, "Get-CimInstance Win32_VideoController | Select-Object "
                                                      "Name,PNPDeviceID,DriverVersion,AdapterRAM")):
            ram = item.get("AdapterRAM")
            report["gpus"].append({"name": item.get("Name"), "pci_id": pci_id_from_pnp(item.get("PNPDeviceID", "")),
                                   "driver_version": item.get("DriverVersion"),
                                   "adapter_ram_mb": int(ram / 1048576) if isinstance(ram, int) and ram > 0 else None})
    else:
        for key, path in (("manufacturer", "/sys/class/dmi/id/sys_vendor"), ("model", "/sys/class/dmi/id/product_name"),
                          ("family", "/sys/class/dmi/id/product_version")):
            try:
                with open(path, encoding="utf-8") as handle:
                    report["machine"][key] = handle.read().strip()
            except OSError:
                report["machine"][key] = None
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as handle:
                names = re.findall(r"^model name\s*:\s*(.+)$", handle.read(), re.M)
            report["cpu"] = names[0].strip() if names else ""
            with open("/proc/meminfo", encoding="utf-8") as handle:
                kb = re.search(r"MemTotal:\s+(\d+)", handle.read())
            report["ram_mb"] = int(kb.group(1)) // 1024 if kb else None
        except OSError:
            pass
        try:
            with open("/etc/os-release", encoding="utf-8") as handle:
                pretty = re.search(r'^PRETTY_NAME="?([^"\n]+)', handle.read(), re.M)
            report["os"]["caption"] = pretty.group(1) if pretty else None
        except OSError:
            pass
        code, out = runner(["lspci", "-nn"], 15)
        report["tools"]["lspci"] = code == 0
        report["gpus"] = parse_lspci(out) if code == 0 else []
        if code != 0:
            report["notes"].append("lspci is not available (install pciutils) — GPU could not be identified.")
        code, out = runner(["rocminfo"], 30)
        report["tools"]["rocminfo"] = code == 0
        report["rocm_agents"] = sorted(set(re.findall(r"\bgfx[0-9a-f]+\b", out))) if code == 0 else []
        code, out = runner(["clinfo", "-l"], 30)
        report["tools"]["clinfo"] = code == 0
        report["opencl_devices"] = out.strip().splitlines()[:10] if code == 0 else []
    code, out = runner(["vulkaninfo", "--summary"], 30)
    report["tools"]["vulkaninfo"] = code == 0
    report["vulkan_devices"] = re.findall(r"deviceName\s*=\s*(.+)", out) if code == 0 else []
    report["gpus"] = [identify(g) for g in report["gpus"]]
    report["python_packages"] = {name: importlib.util.find_spec(name) is not None
                                 for name in ("torch", "torch_directml", "onnxruntime")}
    report["backends"] = assess(report)
    return report


def assess(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    system = report["os"]["system"]
    gpus = report["gpus"]
    amd = [g for g in gpus if g.get("pci_id", "").startswith("1002:")]
    packages = report.get("python_packages", {})
    out: dict[str, dict[str, str]] = {}
    if not amd:
        out["rocm"] = {"status": "not_applicable", "why": "No AMD GPU was detected."}
    elif system != "Linux":
        out["rocm"] = {"status": "not_applicable", "why": "PyTorch ROCm wheels used here are Linux-only."}
    elif all(g.get("known") and g["known"]["rocm"] is False for g in amd):
        out["rocm"] = {"status": "unsupported_by_vendor",
                       "why": "AMD's ROCm support list does not include this APU architecture; overriding "
                              "HSA_OVERRIDE_GFX_VERSION is unsupported and is not recommended."}
    else:
        out["rocm"] = {"status": "candidate_untested",
                       "why": f"rocminfo agents: {report.get('rocm_agents') or 'none'}; check AMD's support matrix."}
    dx12 = any((g.get("known") or {}).get("directx12") for g in gpus)
    if system != "Windows":
        out["directml"] = {"status": "not_applicable", "why": "DirectML is a Windows API."}
    elif packages.get("torch_directml") or packages.get("onnxruntime"):
        out["directml"] = {"status": "installed_untested", "why": "A DirectML-capable package is installed; run --probe."}
    else:
        out["directml"] = {"status": "candidate_untested" if dx12 or not gpus else "unknown",
                           "why": "Needs a DirectX 12 GPU and driver; install torch-directml or onnxruntime-directml "
                                  "in a separate environment to test."}
    out["vulkan"] = ({"status": "candidate_untested",
                      "why": f"Vulkan devices: {', '.join(report['vulkan_devices'])} (usable by stable-diffusion.cpp / "
                             "llama.cpp Vulkan builds; untested here)."}
                     if report.get("vulkan_devices") else
                     {"status": "unknown", "why": "vulkaninfo not found or no Vulkan device reported."})
    out["cpu"] = {"status": "candidate_untested", "why": "Always available; slow for diffusion models."}
    return out


def probe(report: dict[str, Any]) -> None:
    """Run a tiny computation on each backend that is already installed."""
    results = report.setdefault("probes", {})
    if report["python_packages"].get("torch"):
        try:
            import torch  # type: ignore
            a = torch.randn(256, 256)
            results["torch_cpu"] = {"status": "probe_passed", "value": float((a @ a).sum())}
            hip = getattr(torch.version, "hip", None)
            if hip and torch.cuda.is_available():
                b = torch.randn(256, 256, device="cuda")
                results["torch_rocm"] = {"status": "probe_passed", "device": torch.cuda.get_device_name(0),
                                         "value": float((b @ b).sum().cpu())}
                report["backends"]["rocm"]["status"] = "probe_passed"
        except Exception as exc:  # noqa: BLE001
            results["torch"] = {"status": "probe_failed", "error": str(exc)[:300]}
        else:
            report["backends"]["cpu"]["status"] = "probe_passed"
    if report["python_packages"].get("torch_directml"):
        try:
            import torch  # type: ignore
            import torch_directml  # type: ignore
            device = torch_directml.device()
            b = torch.randn(256, 256).to(device)
            results["torch_directml"] = {"status": "probe_passed", "device": torch_directml.device_name(0),
                                         "value": float((b @ b).sum().cpu())}
            report["backends"]["directml"]["status"] = "probe_passed"
        except Exception as exc:  # noqa: BLE001
            results["torch_directml"] = {"status": "probe_failed", "error": str(exc)[:300]}
            report["backends"]["directml"]["status"] = "probe_failed"
    if report["python_packages"].get("onnxruntime"):
        try:
            import onnxruntime  # type: ignore
            results["onnxruntime"] = {"status": "providers_listed",
                                      "providers": onnxruntime.get_available_providers()}
        except Exception as exc:  # noqa: BLE001
            results["onnxruntime"] = {"status": "probe_failed", "error": str(exc)[:300]}


def summary(report: dict[str, Any]) -> str:
    m = report["machine"]
    lines = [f"Machine : {m.get('manufacturer')} {m.get('model')} ({m.get('family')})",
             f"OS      : {report['os'].get('caption') or report['os']['system']} {report['os'].get('build') or report['os']['release']}",
             f"CPU     : {report['cpu']}", f"RAM     : {report['ram_mb']} MB"]
    if not report["gpus"]:
        lines.append("GPU     : none identified")
    for gpu in report["gpus"]:
        known = gpu.get("known")
        lines.append(f"GPU     : {gpu.get('name')} [{gpu.get('pci_id') or 'no PCI id'}] driver {gpu.get('driver_version') or '?'}")
        lines.append(f"          → {known['name']} · {known['arch']}" + (f" · S145 {known['s145']}" if known and known['s145'] else "")
                     if known else "          → not in Maestro's table: report this PCI id")
    lines.append("Backends (nothing here is a claim that generation works):")
    for name, info in report["backends"].items():
        lines.append(f"  {name:9s} {info['status']:22s} {info['why']}")
    for name, info in report.get("probes", {}).items():
        lines.append(f"  probe {name}: {json.dumps(info)}")
    return "\n".join(lines + report["notes"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--probe", action="store_true", help="run a tiny computation on installed backends")
    parser.add_argument("--json", help="write the full report to this file")
    args = parser.parse_args(argv)
    report = collect()
    if args.probe:
        probe(report)
    print(summary(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(f"\nFull report: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
