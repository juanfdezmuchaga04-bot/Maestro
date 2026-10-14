#!/usr/bin/env python3
"""Browse and download Maestro's models from any computer — only Python's standard library needed.

    python scripts/maestro_models.py list [--modality video|image|audio|text] [--free-api] [--family wan]
    python scripts/maestro_models.py show t2v                 # licence, every access route, how to run here
    python scripts/maestro_models.py plan t2v [--compact]     # files, sizes and availability (asks the Hub)
    python scripts/maestro_models.py download t2v [--compact] [--accept-license] [--for cuda|rocm|mps|cpu]
    python scripts/maestro_models.py routes                   # every free API / trial / Space / local engine

Files land where Maestro looks for them (``app/ckpts``, ``app/loras``), so a
model downloaded here — on a laptop with no GPU, for example — is used as-is by
Maestro on this machine or after copying the folder to a computer with a GPU.
``--for`` plans for another machine's hardware (e.g. download on a CPU-only
laptop for an NVIDIA desktop).  Set HF_TOKEN for gated repositories.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from services import model_access, model_downloads  # noqa: E402


def _gb(value: float) -> str:
    return f"{value / 1024 ** 3:.2f} GB"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--modality")
    p_list.add_argument("--family")
    p_list.add_argument("--free-api", action="store_true")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--markdown", action="store_true", help="a Markdown table (used for the docs appendix)")
    p_list.add_argument("--for", dest="target", default="", help="hardware to plan for")
    for name in ("show", "plan", "download"):
        p = sub.add_parser(name)
        p.add_argument("model")
        p.add_argument("--compact", action="store_true", help="prefer smaller INT8 / GGUF files")
        p.add_argument("--variant", default="", help="exact file name of the transformer variant")
        p.add_argument("--for", dest="target", default="", help="hardware to plan for: cuda, rocm, mps, vulkan, cpu")
        p.add_argument("--accept-license", action="store_true")
        p.add_argument("--no-loras", action="store_true")
        p.add_argument("--checkpoints", default="ckpts", help="Maestro's primary model folder (relative to app/)")
    sub.add_parser("routes")
    args = parser.parse_args(argv)

    if args.command == "routes":
        for key, route in model_access.ROUTES.items():
            print(f"{route['label']} [{route['kind']}, {route['confidence']}]\n  {route['limits']}\n  "
                  f"account: {route['account']}\n  Maestro: {route['maestro']}\n  {route['source']}\n")
        for key, backend in model_access.LOCAL_BACKENDS.items():
            print(f"{backend['label']} (local): {backend['hardware']} — {backend['maestro']}")
        return 0

    hardware = model_downloads.hardware_profile(getattr(args, "target", ""))
    accelerator = hardware["accelerator"]
    catalog = model_downloads.catalog(accelerator=accelerator)

    if args.command == "list":
        rows = [i for i in catalog
                if (not args.modality or i["modality"] == args.modality)
                and (not args.family or i["family"] == args.family)
                and (not args.free_api or i["access"].get("free_api"))]
        if args.json:
            print(json.dumps(rows, indent=1, ensure_ascii=False))
            return 0
        if args.markdown:
            kinds = {"free_recurring": "gratis", "public_space": "Space", "trial_credits": "prueba",
                     "free_web": "web gratis", "paid": "pago",
                     "free_gpu_notebook": "cuaderno GPU gratis"}
            print("| Id | Modelo | Tipo | Licencia | Acceso remoto | Pesos portables (sin NVIDIA) |")
            print("|---|---|---|---|---|---|")
            for item in rows:
                routes = "; ".join(
                    f"{model_access.ROUTES[r['id']]['label']} ({kinds[r['kind']]}"
                    + (", no verificado" if r["confidence"] == "unverified" else "")
                    + (", modelo base" if not r["same_weights"] else "") + ")"
                    for r in item["access"].get("routes", [])) or "solo local"
                licence = item["access"].get("license", "")
                if item["access"].get("territory"):
                    licence += " ⚠ territorio restringido"
                cells = [f"`{item['id']}`", item["name"].strip(), item["modality"], licence, routes,
                         "sí" if item["portable_here"] else "no (solo NVIDIA)"]
                print("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
            return 0
        print(f"Hardware: {accelerator} ({hardware['basis']}) · {len(rows)} models\n")
        for item in rows:
            free = [r["label"] for r in item["access"].get("routes", []) if r["kind"] == "free_recurring"]
            flag = "" if item["portable_here"] else "  [NVIDIA-only weights]"
            print(f"{item['id']:<46} {item['modality']:<6} {item['name']}{flag}")
            if free:
                print(f"{'':<46} free API: {', '.join(free)}")
        return 0

    item = next((i for i in catalog if i["id"] == args.model), None)
    if item is None:
        print(f"Unknown model '{args.model}'. Use 'list' to see ids.", file=sys.stderr)
        return 2
    profile = "compact" if args.compact else "quality"

    if args.command == "show":
        access = item["access"]
        print(f"{item['name']} ({item['id']}) — {item['modality']}\n{item['description']}\n")
        print(f"Licence: {access.get('license')}  {access.get('license_url')}")
        if access.get("territory"):
            print(f"TERRITORY: {access['territory']}")
        print(f"Max output: {access.get('max_output')}\n\nAccess routes (researched {access.get('researched_on')}):")
        for route in access.get("routes", []):
            print(f"  - {route['label']} [{route['kind']}, {route['confidence']}] {route['note']}\n"
                  f"    {route['limits']}\n    {route['source']}")
        print("\nLocal engines:")
        for backend in access.get("local_backends", []):
            print(f"  - {backend['label']}: {backend['hardware']}")
        f = item["feasibility"]
        print(f"\nOn this computer ({accelerator}): {item['run_here']}\nCPU estimate: {f['cpu']} — {f['why']} "
              f"(≈{f['estimated_ram_gb']} GB RAM for BF16, estimate)")
        return 0

    manager = model_downloads.DownloadManager(os.path.join(ROOT, "app"), checkpoints_root=args.checkpoints,
                                              start_threads=False)
    if args.command == "plan":
        try:
            report = manager.inspect(model_downloads.plan(args.model, profile=profile, accelerator=accelerator,
                                                          variant=args.variant, include_loras=not args.no_loras))
        except model_downloads.DownloadError as error:
            print(f"{error.code}: {error.message}", file=sys.stderr)
            return 1
        for f in report["files"]:
            state = "present" if f["present"] else f["availability"]
            size = _gb(f["size"]) if f.get("size") else "?"
            print(f"  {state:<16} {size:>10}  {f['target']}")
        for note in report["notes"]:
            print(f"  note: {note}")
        print(f"\nTo download: {report['files_to_download']} file(s), {_gb(report['bytes_to_download'])}. "
              f"Free to download: {'yes' if report['free_to_download'] else 'no — ' + ', '.join(report['blocked'][:3])}")
        if item["license_ack_required"]:
            print("Licence acknowledgement required: " + " · ".join(item["license_ack_required"]))
        return 0

    try:
        job = manager.start(args.model, profile=profile, accelerator=accelerator, variant=args.variant,
                            accept_license=args.accept_license, include_loras=not args.no_loras)
    except model_downloads.DownloadError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 1
    import threading
    worker = threading.Thread(target=manager.run, args=(job["id"],), daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            state = manager.get(job["id"])
            total = state.get("bytes_total") or 0
            pct = f"{100 * state['bytes_done'] / total:5.1f}%" if total else "  ... "
            print(f"\r{pct} {_gb(state['bytes_done'])} / {_gb(total)}  {state['current'][:70]:<70}", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        manager.cancel(job["id"])
        worker.join(30)
    final = manager.get(job["id"])
    print(f"\n{final['state']}: {final['current']}")
    return 0 if final["state"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
