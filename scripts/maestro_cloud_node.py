#!/usr/bin/env python3
"""Turn a free NVIDIA notebook session (Kaggle, Lightning AI) into your own Maestro render node.

Run it from a clone of this repository inside the notebook:

    python scripts/maestro_cloud_node.py                  # install, start, tunnel, print the code
    python scripts/maestro_cloud_node.py --check          # only report GPU / driver / disk, change nothing
    python scripts/maestro_cloud_node.py --nodes 1        # one node even on a 2-GPU session

It prints a ``maestro-node:…`` connection code.  Paste it into Maestro on your own
computer (Auto → Free NVIDIA GPU → Connect).  From then on Studio and Films send
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

With two GPUs (Kaggle "GPU T4 x2") it starts **two** independent Maestro nodes, one
per GPU (``CUDA_VISIBLE_DEVICES``), each with its own port, token and tunnel, and
prints one code that connects both.  The second node runs from a ``git worktree``
of this checkout so the two never share settings or queue files; the model folder
is shared, so weights are downloaded once.  Both share the session's system RAM
(about 29 GB on Kaggle): use models that fit in half of it, or ``--nodes 1``.

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
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

from services import cloud_node  # noqa: E402

VENV = os.path.join(ROOT, ".node-venv")
PORT = 7860
SECOND_TREE = os.path.join(os.path.dirname(ROOT), "maestro-node-2")
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


def place_storage(storage: str, app: str = APP) -> None:
    target = os.path.join(storage, "maestro-ckpts")
    os.makedirs(target, exist_ok=True)
    link = os.path.join(app, "ckpts")
    if os.path.islink(link) or not os.path.exists(link):
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(target, link)
        log(f"Model folder: {link} → {target}")
    else:
        log(f"Model folder {link} already exists; keeping it.")
    os.environ.setdefault("HF_HOME", os.path.join(storage, "hf-home"))


def last_line(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4096))
            lines = [line for line in handle.read().decode("utf-8", "replace").splitlines() if line.strip()]
        return lines[-1][-160:] if lines else "(no output yet)"
    except OSError:
        return "(no log yet)"


def wait_for(url: str, token: str, timeout: float, process: subprocess.Popen | None = None,
             log_path: str = "", label: str = "Maestro") -> bool:
    """Wait until ``url`` answers 200, reporting progress every minute.

    Stops early when the server process has exited, and says so when the server
    answers but refuses the token (a sign-in problem, not a slow start).
    """
    started = time.time()
    deadline = started + timeout
    next_report = started + 60
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            log(f"{label} exited (code {process.returncode}) while starting. Last log line: {last_line(log_path)}")
            return False
        try:
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status == 200:
                    log(f"{label} is up after {int(time.time() - started)} s.")
                    return True
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                log(f"{label} answers but refuses the token (HTTP {error.code}); check MAESTRO_ACCESS_TOKEN.")
        except Exception:  # noqa: BLE001 - still starting
            pass
        if time.time() >= next_report:
            log(f"{label} still starting ({int(time.time() - started)} s) · {last_line(log_path)}")
            next_report += 60
        time.sleep(5)
    return False


def start_tunnel(storage: str, port: int = PORT) -> tuple[subprocess.Popen, str]:
    binary = shutil.which("cloudflared") or os.path.join(storage, "cloudflared")
    if not os.path.isfile(binary):
        log("Downloading cloudflared (Cloudflare's tunnel client)…")
        urllib.request.urlretrieve(CLOUDFLARED_URL, binary)
        os.chmod(binary, 0o755)
    process = subprocess.Popen([binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
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


def system_ram_gb() -> float:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 0.0


def second_tree() -> str:
    """A git worktree of this checkout for the second node (separate settings and queue)."""
    if not os.path.isdir(SECOND_TREE):
        run(["git", "-C", ROOT, "worktree", "add", "--detach", SECOND_TREE, "HEAD"])
    else:
        # A re-run after `git pull` must not leave the second node on older code.
        run(["git", "-C", SECOND_TREE, "checkout", "--detach", "--force",
             subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()])
    return os.path.join(SECOND_TREE, "app")


def start_node(index: int, app: str, gpu_index: int, port: int, token: str, python: str, storage: str,
               timeout: float) -> tuple[subprocess.Popen, subprocess.Popen, str, str]:
    """Start one Maestro on one GPU, wait for it, tunnel it and start its queue worker."""
    env = {**os.environ, "MAESTRO_ACCESS_TOKEN": token, "SERVER_NAME": "127.0.0.1", "SERVER_PORT": str(port),
           "PYTHONUNBUFFERED": "1", "CUDA_VISIBLE_DEVICES": str(gpu_index),
           # Jupyter (Kaggle) exports its inline plotting backend, which only exists
           # in the notebook's own Python; Maestro's environment needs a headless one.
           "MPLBACKEND": "Agg"}
    log_path = os.path.join(storage, f"maestro-node-{index + 1}.log")
    log(f"Starting node {index + 1} on GPU {gpu_index}, port {port} (log: {log_path}). "
        "The first start imports the engine and can take several minutes.")
    server = subprocess.Popen([python, "launch.py"], cwd=app, env=env,
                              stdout=open(log_path, "a", encoding="utf-8"), stderr=subprocess.STDOUT)
    if not wait_for(f"http://127.0.0.1:{port}/api/v1/engines", token, timeout, server, log_path,
                    label=f"Node {index + 1}"):
        server.terminate()
        raise cloud_node.CloudNodeError(f"Node {index + 1} did not answer within {int(timeout)} s; "
                                        f"see the end of {log_path}.")
    tunnel, url = start_tunnel(storage, port)
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/production/queue/worker/start", data=b"{}",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                     method="POST")
    urllib.request.urlopen(request, timeout=30).close()
    return server, tunnel, url, log_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="only measure and report")
    parser.add_argument("--token", default=os.environ.get("MAESTRO_ACCESS_TOKEN", ""),
                        help="reuse a fixed token (default: a new random one per session)")
    parser.add_argument("--startup-timeout", type=float, default=1800)
    parser.add_argument("--nodes", default="auto", choices=["auto", "1", "2"],
                        help="how many Maestro nodes to run (auto: one per GPU, at most 2)")
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

    count = min(len(info["gpus"]), cloud_node.MAX_NODES) if args.nodes == "auto" else int(args.nodes)
    if count > len(info["gpus"]):
        log(f"Asked for {count} nodes but this session has {len(info['gpus'])} GPU; running 1.")
        count = 1
    ram = system_ram_gb()
    if count > 1:
        log(f"Two nodes share {ram:.0f} GB of system RAM. Models that offload to RAM (14B, BlockSwap) can run "
            "out of memory with two at once; if a node dies, re-run with --nodes 1.")

    python = ensure_environment(torch)
    place_storage(info["storage"])
    apps = [APP]
    if count > 1:
        try:
            apps.append(second_tree())
            place_storage(info["storage"], apps[1])
        except (subprocess.SubprocessError, OSError) as error:
            log(f"Could not prepare the second node ({error}); running one node.")
            count = 1
    platform_label = {"kaggle": "Kaggle", "lightning": "Lightning AI"}.get(info["platform"], "Cloud")
    processes: list[subprocess.Popen] = []
    nodes = []
    try:
        for index in range(count):
            gpu = info["gpus"][index]
            token = (args.token if index == 0 else "") or secrets.token_urlsafe(32)
            try:
                server, tunnel, url, _log_path = start_node(index, apps[index], index, PORT + index, token, python,
                                                            info["storage"], args.startup_timeout)
            except (cloud_node.CloudNodeError, OSError) as error:
                log(str(error))
                if index == 0:
                    return 1
                log("Continuing with one node.")
                break
            processes += [server, tunnel]
            suffix = f" #{index + 1}" if count > 1 else ""
            nodes.append({"url": url, "token": token, "label": f"{platform_label} {gpu['name']}{suffix}",
                          "gpu": f"{gpu['name']} {gpu['memory_mb']} MiB"})
        code = cloud_node.make_multi_code(nodes, expires_hint="until this notebook session ends")
        with open(os.path.join(info["storage"], "maestro-connection-code.txt"), "w", encoding="utf-8") as handle:
            handle.write(code + "\n")
        print("\n" + "=" * 72)
        print(f" Your free NVIDIA node{'s are' if len(nodes) > 1 else ' is'} ready ({len(nodes)} GPU). "
              "In Maestro on your computer:")
        print("   Auto → Free NVIDIA GPU → paste this code → Connect\n")
        print(code)
        print("\n Keep this notebook running. The address changes every session: paste the new code next time.")
        print("=" * 72 + "\n", flush=True)

        stop = {"now": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
        while not stop["now"]:
            if any(process.poll() is not None for process in processes):
                log("A node or its tunnel stopped; see the node logs in " + info["storage"]
                    + ". Run this cell again for a new code.")
                break
            usage = subprocess.run(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used",
                                    "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
            log("alive · GPU " + usage.replace("\n", " | "))
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
