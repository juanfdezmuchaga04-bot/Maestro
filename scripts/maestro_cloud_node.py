#!/usr/bin/env python3
"""Turn a free NVIDIA notebook session (Kaggle, Lightning AI) into your own Maestro render node.

Run it from a clone of this repository inside the notebook:

    python scripts/maestro_cloud_node.py                  # install, start, tunnel, print the code
    python scripts/maestro_cloud_node.py --check          # only report GPU / driver / disk, change nothing

It prints a ``maestro-node:…`` connection code.  Paste it into Maestro on your own
computer (Models → Free NVIDIA GPU → Connect).  From then on Studio and Films send
renders to this session, which runs Maestro's real engine on the NVIDIA GPU, and
the finished files come back to your computer.

Steps (each one printed as it happens, each one idempotent on a re-run):
1. measure the GPU, the driver's CUDA version and the free disk;
2. create an isolated Python 3.11 environment with ``uv`` and install the PyTorch
   build that matches the driver, then ``app/requirements.txt``;
3. put the model folder (``app/ckpts``) and the Hugging Face cache on the largest disk;
4. start ``app/launch.py`` in token mode (a random ``MAESTRO_ACCESS_TOKEN``), on
   127.0.0.1 only;
5. open a TryCloudflare tunnel (HTTPS, no account) to it;
6. start the node's queue worker and print the connection code;
7. stay in the foreground, reporting GPU use, until the session ends or you stop it.

Nothing here works around a platform's quota or terms: the session is yours, runs
only while you keep it open, and stops when the platform stops it.
"""

from __future__ import annotations

import argparse
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

from services import cloud_node  # noqa: E402

VENV = os.path.join(ROOT, ".node-venv")
PORT = 7860
CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"


def log(message: str) -> None:
    print(f"[maestro-node] {message}", flush=True)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    log("$ " + " ".join(command))
    return subprocess.run(command, check=True, **kwargs)


def detect_platform() -> str:
    if os.path.isdir("/kaggle"):
        return "kaggle"
    if os.environ.get("LIGHTNING_CLOUDSPACE_HOST") or os.path.isdir("/teamspace"):
        return "lightning"
    if "COLAB_RELEASE_TAG" in os.environ:
        return "colab"
    return "other"


def measure() -> dict:
    if not shutil.which("nvidia-smi"):
        raise cloud_node.CloudNodeError("nvidia-smi not found: turn on a GPU accelerator for this notebook.")
    query = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,compute_cap",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=30)
    header = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=30)
    gpus = cloud_node.parse_gpus(query.stdout)
    if not gpus:
        raise cloud_node.CloudNodeError("No NVIDIA GPU reported by nvidia-smi.")
    cuda = cloud_node.driver_cuda(header.stdout)
    candidates = [p for p in ("/kaggle/tmp", "/tmp", "/kaggle/working", "/teamspace/studios/this_studio", ROOT)
                  if os.path.isdir(p) and os.access(p, os.W_OK)]
    free = {p: shutil.disk_usage(p).free for p in candidates}
    return {"gpus": gpus, "cuda": cuda, "free": free, "storage": cloud_node.pick_storage(candidates, free),
            "platform": detect_platform()}


def ensure_environment(torch: dict) -> str:
    python = os.path.join(VENV, "bin", "python")
    marker = os.path.join(VENV, ".maestro-node-ready")
    if os.path.isfile(marker) and open(marker, encoding="utf-8").read().strip() == torch["label"]:
        log(f"Environment ready ({torch['label']}).")
        return python
    if not shutil.which("uv"):
        run([sys.executable, "-m", "pip", "install", "--quiet", "uv"])
    uv = shutil.which("uv") or os.path.join(os.path.dirname(sys.executable), "uv")
    run([uv, "venv", "--python", "3.11", VENV])
    env = {**os.environ, "VIRTUAL_ENV": VENV}
    run([uv, "pip", "install", "--python", python, *torch["torch"].split(), "--index-url", torch["index"]], env=env)
    run([uv, "pip", "install", "--python", python, "-r", os.path.join(APP, "requirements.txt")], env=env)
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(torch["label"])
    return python


def place_storage(storage: str) -> None:
    target = os.path.join(storage, "maestro-ckpts")
    os.makedirs(target, exist_ok=True)
    link = os.path.join(APP, "ckpts")
    if os.path.islink(link) or not os.path.exists(link):
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(target, link)
        log(f"Model folder: {link} → {target}")
    else:
        log(f"Model folder {link} already exists; keeping it.")
    os.environ.setdefault("HF_HOME", os.path.join(storage, "hf-home"))


def wait_for(url: str, token: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - still starting
            pass
        time.sleep(5)
    return False


def start_tunnel(storage: str) -> tuple[subprocess.Popen, str]:
    binary = shutil.which("cloudflared") or os.path.join(storage, "cloudflared")
    if not os.path.isfile(binary):
        log("Downloading cloudflared (Cloudflare's tunnel client)…")
        urllib.request.urlretrieve(CLOUDFLARED_URL, binary)
        os.chmod(binary, 0o755)
    process = subprocess.Popen([binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 90
    while time.time() < deadline and process.stdout is not None:
        line = process.stdout.readline()
        url = cloud_node.tunnel_url(line)
        if url:
            return process, url
    process.terminate()
    raise cloud_node.CloudNodeError("The tunnel did not start (no trycloudflare.com address within 90 s). "
                                    "Is internet access enabled for this notebook?")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="only measure and report")
    parser.add_argument("--token", default=os.environ.get("MAESTRO_ACCESS_TOKEN", ""),
                        help="reuse a fixed token (default: a new random one per session)")
    parser.add_argument("--startup-timeout", type=float, default=1800)
    args = parser.parse_args(argv)

    if platform.system() != "Linux":
        log("This script is meant for a Linux notebook with an NVIDIA GPU.")
        return 2
    try:
        info = measure()
    except (cloud_node.CloudNodeError, OSError, subprocess.SubprocessError) as error:
        log(f"Cannot run a node here: {error}")
        return 2
    gpu = info["gpus"][0]
    torch = cloud_node.choose_torch(info["cuda"], gpu["compute_capability"])
    log(f"Platform: {info['platform']} · GPU: {', '.join(g['name'] for g in info['gpus'])} "
        f"({gpu['memory_mb']} MiB, compute {gpu['compute_capability']}) · driver CUDA "
        f"{'.'.join(map(str, info['cuda'] or ()))} → {torch['label']}")
    log("Free disk: " + ", ".join(f"{p} {v / 1024 ** 3:.0f} GB" for p, v in info["free"].items())
        + f" → models on {info['storage']}")
    if info["platform"] == "colab":
        log("Google Colab's free tier does not allow serving a web UI from a notebook; use Kaggle or Lightning "
            "AI, or a Colab plan with paid compute units.")
        return 2
    if args.check:
        return 0

    python = ensure_environment(torch)
    place_storage(info["storage"])
    token = args.token or secrets.token_urlsafe(32)
    env = {**os.environ, "MAESTRO_ACCESS_TOKEN": token, "SERVER_NAME": "127.0.0.1", "SERVER_PORT": str(PORT),
           "PYTHONUNBUFFERED": "1"}
    log_path = os.path.join(info["storage"], "maestro-node.log")
    log(f"Starting Maestro (log: {log_path}). The first start imports the engine and can take several minutes.")
    server = subprocess.Popen([python, "launch.py"], cwd=APP, env=env,
                              stdout=open(log_path, "a", encoding="utf-8"), stderr=subprocess.STDOUT)
    if not wait_for(f"http://127.0.0.1:{PORT}/api/v1/engines", token, args.startup_timeout):
        log(f"Maestro did not answer within {int(args.startup_timeout)} s; see the end of {log_path}.")
        server.terminate()
        return 1
    tunnel, url = start_tunnel(info["storage"])
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/v1/production/queue/worker/start", data=b"{}",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                     method="POST")
    urllib.request.urlopen(request, timeout=30).close()
    label = {"kaggle": "Kaggle", "lightning": "Lightning AI"}.get(info["platform"], "Cloud") + f" {gpu['name']}"
    code = cloud_node.make_code(url, token, label=label, gpu=f"{gpu['name']} {gpu['memory_mb']} MiB",
                                expires_hint="until this notebook session ends")
    with open(os.path.join(info["storage"], "maestro-connection-code.txt"), "w", encoding="utf-8") as handle:
        handle.write(code + "\n")
    print("\n" + "=" * 72)
    print(" Your free NVIDIA node is ready. In Maestro on your computer:")
    print("   Models → Free NVIDIA GPU → paste this code → Connect\n")
    print(code)
    print("\n Keep this notebook running. The address changes every session: paste the new code next time.")
    print("=" * 72 + "\n", flush=True)

    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    try:
        while not stop["now"]:
            if server.poll() is not None:
                log(f"Maestro stopped (exit {server.returncode}); see {log_path}.")
                break
            if tunnel.poll() is not None:
                log("The tunnel closed; run this cell again for a new code.")
                break
            usage = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                                    "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
            log(f"alive · GPU {usage}")
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.terminate()
        server.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
