#!/usr/bin/env python3
"""Regenerate ``app/services/model_manifest.json`` — every model Maestro ships and every file it needs.

Maestro's model definitions (``app/defaults/*.json``) name the main weights, but the
text encoders, VAEs, tokenizers and other companions are declared in code, by each
family handler's ``query_model_files``.  Those handlers import PyTorch and other
heavy packages at module level, which a computer without an NVIDIA GPU (or without
any of those packages) may not have.  This script reads the handlers *without*
running them on real hardware: every third-party import is replaced by an inert
stub, so only the plain-Python file declarations execute.  The result is
deterministic on every machine, and the committed manifest lets the downloader run
on any CPU with nothing but the standard library.

    python scripts/build_model_manifest.py          # rewrite the manifest
    python scripts/build_model_manifest.py --check  # exit 1 if it is stale

``tests/test_model_downloads.py`` runs ``--check`` so a new or edited model
definition cannot ship without its manifest entry.
"""

from __future__ import annotations

import argparse
import glob
import importlib
import importlib.abc
import importlib.machinery
import json
import os
import sys
import types
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
OUTPUT = os.path.join(APP, "services", "model_manifest.json")
INTERNAL = {"models", "shared", "services", "preprocessing", "postprocessing", "plugins"}

# Mirrors wgp.family_handlers.  The test fails if wgp gains a handler this misses.
FAMILY_HANDLERS = [
    "models.wan.wan_handler", "models.wan.ovi_handler", "models.wan.df_handler", "models.hyvideo.hunyuan_handler",
    "models.ltx_video.ltxv_handler", "models.ltx2.ltx2_handler", "models.ltx25.ltx25_handler",
    "models.ltx2.scenema_audio_handler", "models.ltx2.ltx_audio_tts_handler", "models.minimax_h3.minimax_h3_handler",
    "models.longcat.longcat_handler", "models.flux.flux_handler", "models.qwen.qwen_handler",
    "models.qwen21.qwen21_handler", "models.kandinsky5.kandinsky_handler", "models.z_image.z_image_handler",
    "models.krea2.krea2_handler", "models.hidream.hidream_handler", "models.TTS.ace_step_handler",
    "models.TTS.chatterbox_handler", "models.TTS.qwen3_handler", "models.TTS.yue_handler",
    "models.TTS.yue2.yue2_handler", "models.TTS.heartmula_handler", "models.TTS.kugelaudio_handler",
    "models.TTS.minimax_music3_handler", "models.TTS.index_tts2_handler",
]
AUDIO_HANDLERS = {h for h in FAMILY_HANDLERS if ".TTS." in h or "audio" in h}
URL_KEYS = ("URLs", "URLs2", "preload_URLs", "VAE_URLs", "text_encoder_URLs", "loras")


# ---------------------------------------------------------------------------
# Inert stand-ins for every third-party module
# ---------------------------------------------------------------------------


class _Stub:
    def __init__(self, *a: Any, **k: Any) -> None: pass
    def __call__(self, *a: Any, **k: Any) -> "_Stub": return _Stub()
    def __getattr__(self, name: str) -> "_Stub": return _Stub()
    def __mro_entries__(self, bases: Any) -> tuple: return (type("StubBase", (), {"__init__": lambda s, *a, **k: None}),)
    def __iter__(self): return iter((_Stub(), _Stub()))
    def __getitem__(self, key: Any) -> "_Stub": return _Stub()
    def __setitem__(self, key: Any, value: Any) -> None: pass
    def __contains__(self, key: Any) -> bool: return False
    def __lt__(self, other: Any) -> bool: return False
    __le__ = __gt__ = __ge__ = __lt__
    def __bool__(self) -> bool: return False
    def __int__(self) -> int: return 0
    def __index__(self) -> int: return 0
    def __float__(self) -> float: return 0.0
    def __hash__(self) -> int: return id(self)
    def __eq__(self, other: Any) -> bool: return self is other
    def __add__(self, other: Any) -> "_Stub": return self
    __radd__ = __mul__ = __rmul__ = __sub__ = __rsub__ = __truediv__ = __rtruediv__ = __and__ = __rand__ = __add__
    __or__ = __ror__ = __add__


class _StubModule(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _Stub()


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):  # noqa: ANN001
        root = name.split(".")[0]
        if root in INTERNAL or root in sys.stdlib_module_names:
            return None
        return importlib.machinery.ModuleSpec(name, self, is_package=True)

    def create_module(self, spec):  # noqa: ANN001
        module = _StubModule(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module) -> None:  # noqa: ANN001
        pass


class _Args:
    """wgp's argparse namespace: every per-family lora override is unset."""

    def __getattr__(self, name: str) -> str:
        return ""


def _plain(value: Any) -> Any:
    """Keep only JSON data; anything a stub produced is dropped."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items() if not isinstance(v, _Stub)}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value if not isinstance(v, _Stub)]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _literal_assignment(path: str, name: str) -> Any:
    """A module-level literal read from source, so the module never has to import."""
    import ast
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    raise LookupError(f"{name} not found in {path}")


def _compute_list(filename: Any) -> list[str]:
    if not filename:
        return []
    return [str(filename)[str(filename).rfind("/") + 1:]]


def _resolve(defs: dict[str, dict], model_type: str, key: str, stack: tuple = ()) -> Any:
    value = defs.get(model_type, {}).get(key)
    if isinstance(value, str) and not value.startswith("http"):
        if len(stack) > 10 or value in stack:
            return []
        return _resolve(defs, value, key, stack + (value,))
    return value


def _urls(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str) and value.startswith("http"):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            out += _urls(item)
    elif isinstance(value, list):
        for item in value:
            out += _urls(item)
    return out


def _module_groups(defs: dict[str, dict], value: Any, stack: tuple = ()) -> list[list[str]]:
    """``modules`` holds groups of alternative files, or names of models whose modules to reuse."""
    groups: list[list[str]] = []
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    for item in items:
        if isinstance(item, str) and not item.startswith("http"):
            if item not in stack and len(stack) < 10:
                groups += _module_groups(defs, defs.get(item, {}).get("modules"), stack + (item,))
        else:
            urls = _urls(item)
            if urls:
                groups.append(urls)
    return groups


def build() -> dict[str, Any]:
    sys.path.insert(0, APP)
    finder = _StubFinder()
    sys.meta_path.insert(0, finder)
    cwd = os.getcwd()
    os.chdir(APP)
    handlers: dict[str, Any] = {}
    handler_paths: dict[str, str] = {}
    errors: list[str] = []
    try:
        for path in FAMILY_HANDLERS:
            try:
                handler = importlib.import_module(path).family_handler
                for model_type in handler.query_supported_types():
                    handlers[model_type] = handler
                    handler_paths[model_type] = path
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
        raw: dict[str, dict] = {}
        for file in sorted(glob.glob(os.path.join(APP, "defaults", "*.json"))):
            with open(file, encoding="utf-8") as handle:
                data = json.load(handle)
            raw[os.path.splitext(os.path.basename(file))[0]] = data.get("model", {})
        defs: dict[str, dict] = {}
        for model_type, model in raw.items():
            merged = dict(model)
            handler = handlers.get(model.get("architecture"))
            if handler is not None:
                try:
                    extra = handler.query_model_def(model.get("architecture"), dict(model))
                    if isinstance(extra, dict):
                        merged = {**_plain(extra), **model}
                except Exception:  # noqa: BLE001 - declarations only
                    pass
            defs[model_type] = merged
        models: dict[str, Any] = {}
        for model_type, model in sorted(defs.items()):
            arch = model.get("architecture", "")
            handler = handlers.get(arch)
            entry: dict[str, Any] = {
                "name": model.get("name", model_type),
                "architecture": arch,
                "description": model.get("description", ""),
                "visible": model.get("visible", True) is not False,
                "modality": ("image" if model.get("image_outputs") else
                             "audio" if handler_paths.get(arch) in AUDIO_HANDLERS else "video"),
                "family": "",
                "license_name": model.get("license_name", ""),
                "license_url": model.get("license_url", ""),
                "resolutions": _plain(model.get("resolutions")) if isinstance(model.get("resolutions"), list) else None,
            }
            for key in URL_KEYS:
                entry[key] = _urls(_resolve(defs, model_type, key))
            entry["modules"] = _module_groups(defs, model.get("modules"))
            folder = model.get("text_encoder_folder")
            entry["text_encoder_folder"] = folder if isinstance(folder, str) else ""
            entry["lora_dir"] = ""
            if handler is not None and getattr(handler, "get_lora_dir", None) is not None:
                try:
                    lora_dir = handler.get_lora_dir(arch, _Args(), "loras")
                    entry["lora_dir"] = lora_dir.replace(os.sep, "/") if isinstance(lora_dir, str) else ""
                except Exception:  # noqa: BLE001
                    pass
            companions: list[dict] = []
            if handler is not None:
                try:
                    entry["family"] = str(_plain(handler.query_model_family()) or "")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    files = handler.query_model_files(_compute_list, arch, dict(model))
                    for item in (files if isinstance(files, list) else [files]):
                        item = _plain(item) or {}
                        if not item.get("repoId"):
                            continue
                        folders = item.get("sourceFolderList") or [""]
                        lists = item.get("fileList") or [[]]
                        targets = item.get("targetFolderList") or [None] * len(folders)
                        for folder, names, target in zip(folders, lists, targets):
                            companions.append({"repo": item["repoId"], "folder": folder or "",
                                               "target": target or "", "files": [n for n in names if n],
                                               "revision": item.get("revision") or ""})
                except Exception as exc:  # noqa: BLE001
                    entry["companion_error"] = f"{type(exc).__name__}: {exc}"
            else:
                entry["companion_error"] = f"no handler for architecture '{arch}'"
            entry["companions"] = companions
            models[model_type] = entry
        llm: dict[str, Any] = {}
        try:
            registry = _literal_assignment(os.path.join(APP, "services", "llm_service.py"), "MODEL_REGISTRY")
            for repo, info in sorted(registry.items()):
                info = _plain(info) or {}
                stem = repo.split("/")[-1].replace("-GGUF", "")
                llm[repo] = {
                    "name": info.get("label", repo), "modality": "text",
                    "size_hint": info.get("size_hint", ""), "gguf_file": info.get("gguf_file", ""),
                    "mmproj_file": info.get("mmproj_file", ""), "mmproj_repo": info.get("mmproj_repo", repo),
                    "folder": "llm/" + (info.get("cache_dir_override") or stem),
                }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"services.llm_service: {type(exc).__name__}: {exc}")
    finally:
        os.chdir(cwd)
        sys.meta_path.remove(finder)
    return {
        "generated_by": "scripts/build_model_manifest.py",
        "handler_errors": errors,
        "shared": [
            {"repo": "DeepBeepMeep/Wan2.1", "folder": f, "target": "", "files": files, "revision": "",
             "purpose": purpose}
            for f, files, purpose in (
                ("pose", ["dw-ll_ucoco_384.onnx", "yolox_l.onnx"], "pose control"),
                ("depth", ["depth_anything_v2_vitl.pth"], "depth control"),
                ("flow", ["raft-things.pth"], "motion flow"),
            )
        ],
        "models": models,
        "llm": llm,
        "extras": {
            "mmaudio": {
                "name": "MMAudio (sound effects for video)", "modality": "audio",
                "companions": [
                    {"repo": "DeepBeepMeep/Wan2.1", "folder": "mmaudio", "target": "", "revision": "",
                     "files": ["synchformer_state_dict.pth", "v1-44.pth",
                               "mmaudio_large_44k_gold_8.5k_final_fp16.safetensors"]},
                    {"repo": "DeepBeepMeep/Wan2.1", "folder": "DFN5B-CLIP-ViT-H-14-378", "target": "", "revision": "",
                     "files": ["open_clip_config.json", "open_clip_pytorch_model.bin"]},
                    {"repo": "DeepBeepMeep/Wan2.1", "folder": "bigvgan_v2_44khz_128band_512x", "target": "",
                     "revision": "", "files": ["config.json", "bigvgan_generator.pt"]},
                ],
            },
            "whisper_small": {
                "name": "faster-whisper small (lyrics / speech transcription)", "modality": "text",
                "hf_cache": {"repo": "Systran/faster-whisper-small", "folder": "whisper",
                             "files": ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]},
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    manifest = build()
    text = json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if args.check:
        try:
            with open(OUTPUT, encoding="utf-8") as handle:
                current = handle.read()
        except OSError:
            current = ""
        if current != text:
            print("model_manifest.json is stale: run python scripts/build_model_manifest.py")
            return 1
        print("model_manifest.json is up to date")
        return 0
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {OUTPUT}: {len(manifest['models'])} models, handler errors: {len(manifest['handler_errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
