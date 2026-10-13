"""Local video models for the self-hosted ComfyUI engine (spec §8, §18, §22.19).

This is a *catalogue of facts about files*: which model, under which licence, in
which repository and path, how big it is, what it needs, and where that claim
comes from.  It decides nothing about the user's machine by itself — that is
:func:`check_fit`, which compares a model's stated requirements with the
hardware :mod:`services.local_hardware` actually measured.

Three rules, all required by the spec:

1. **Free per generation, local only.** The default path is a self-hosted
   ComfyUI on the user's own computer. No commercial generation API, no credit
   system, no cloud dependency (`POLICY`). A model that only exists behind a
   paid API is not in the catalogue at all.
2. **Licences are not interchangeable** (§22.19). Each entry carries its own
   licence, and where a licence restricts commercial use that is stated in
   ``commercial_note`` with a source. Nothing is claimed about commercial
   usability beyond what the licence text itself says.
3. **Requirements are quoted, not guessed.** ``min_vram_mb`` comes from a
   documented figure for *that* implementation (Wan's own reference code and
   ComfyUI's own workflow guide are different implementations and say different
   things, so both are recorded).

The primary video path is **Wan 2.2 TI2V-5B** (Apache-2.0): it is the variant
Wan and ComfyUI both present as the consumer-GPU one.  The 14B variants are kept
in the catalogue because they are the same licence and the natural upgrade on a
workstation, and **LTX-2 is recorded as evaluated and not selected**, with the
exact reasons.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "MODELS",
    "POLICY",
    "ComfyModelError",
    "ModelFile",
    "check_fit",
    "evaluated_but_not_selected",
    "get_model",
    "model_ids",
    "VRAM_TOLERANCE",
    "required_disk_gb",
    "select_video_model",
    "usable_vram_floor",
]

# The product rule this module exists to enforce, in data form.
POLICY: dict[str, Any] = {
    "commercial_providers": False,
    "cloud_generation": False,
    "credits": False,
    "generation_cost_per_clip": "your own electricity and hardware time",
    "note": (
        "Generation runs on your own computer through ComfyUI's local API. No account, "
        "no API key and no credit balance is involved at any point."
    ),
}

COMFY_REPO = "Comfy-Org/Wan_2.2_ComfyUI_Repackaged"
_HF = f"https://huggingface.co/{COMFY_REPO}/blob/main"


class ComfyModelError(ValueError):
    """Unknown model, or a catalogue entry used in a way it does not support."""


class ModelFile:
    """One weight file: where it comes from and where ComfyUI wants it."""

    def __init__(
        self,
        *,
        filename: str,
        path_in_repo: str,
        directory: str,
        size_gb: float,
        repo: str = COMFY_REPO,
        url: str = "",
    ) -> None:
        self.filename = filename
        self.path_in_repo = path_in_repo
        self.directory = directory
        self.size_gb = float(size_gb)
        self.repo = repo
        self.url = url or f"{_HF}/{path_in_repo}"

    def describe(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "path_in_repo": self.path_in_repo,
            "directory": self.directory,
            "size_gb": self.size_gb,
            "repo": self.repo,
            "url": self.url,
        }


class LocalVideoModel:
    """A model that can be run locally, with the evidence for every claim."""

    def __init__(
        self,
        *,
        model_id: str,
        label: str,
        licence: str,
        licence_url: str,
        commercial_note: str,
        capability: str,
        templates: Sequence[str],
        files: Sequence[ModelFile],
        template_params: Mapping[str, str],
        min_vram_mb: int,
        vram_source: str,
        min_ram_gb: Optional[int],
        ram_is_estimate: bool,
        resolution: str,
        fps: int,
        frames: int,
        notes: str,
        sources: Sequence[str],
        selectable: bool = True,
        untested_below_vram: bool = True,
    ) -> None:
        self.model_id = model_id
        self.label = label
        self.licence = licence
        self.licence_url = licence_url
        self.commercial_note = commercial_note
        self.capability = capability
        self.templates = tuple(templates)
        self.files = tuple(files)
        self.template_params = dict(template_params)
        self.min_vram_mb = int(min_vram_mb)
        self.vram_source = vram_source
        self.min_ram_gb = min_ram_gb
        self.ram_is_estimate = bool(ram_is_estimate)
        self.resolution = resolution
        self.fps = int(fps)
        self.frames = int(frames)
        self.notes = notes
        self.sources = tuple(sources)
        self.selectable = bool(selectable)
        self.untested_below_vram = bool(untested_below_vram)

    @property
    def model_disk_gb(self) -> float:
        return round(sum(f.size_gb for f in self.files), 2)

    def describe(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "label": self.label,
            "licence": self.licence,
            "licence_url": self.licence_url,
            "commercial_note": self.commercial_note,
            "capability": self.capability,
            "templates": list(self.templates),
            "files": [f.describe() for f in self.files],
            "template_params": dict(self.template_params),
            "min_vram_mb": self.min_vram_mb,
            "vram_source": self.vram_source,
            "min_ram_gb": self.min_ram_gb,
            "ram_is_estimate": self.ram_is_estimate,
            "model_disk_gb": self.model_disk_gb,
            "recommended_free_disk_gb": self.recommended_free_disk_gb(),
            "resolution": self.resolution,
            "fps": self.fps,
            "frames": self.frames,
            "notes": self.notes,
            "sources": list(self.sources),
            "selectable": self.selectable,
        }

    def recommended_free_disk_gb(self) -> float:
        """Model files plus headroom for ComfyUI, dependencies, cache and outputs."""
        return round(self.model_disk_gb * 1.4 + 8, 1)


# A driver reports slightly less than the marketing size of a card (an "8 GB"
# card typically answers ~8188 MiB), so a stated requirement is compared with a
# small documented tolerance instead of demanding the last 4 MB.
VRAM_TOLERANCE = 0.03


def usable_vram_floor(stated_min_vram_mb: int) -> int:
    """The measured VRAM below which a stated requirement is considered unmet."""
    return int(int(stated_min_vram_mb) * (1.0 - VRAM_TOLERANCE))


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

_WAN_LICENCE = "Apache-2.0"
_WAN_LICENCE_URL = "https://github.com/Wan-Video/Wan2.2/blob/main/LICENSE.txt"
_WAN_COMMERCIAL = (
    "Apache-2.0: no commercial-use restriction was found in the licence file or the model "
    "cards for any Wan 2.2 variant. The model authors state that they do not claim rights "
    "over generated content, and that users are responsible for complying with applicable "
    "law and the licence."
)

MODELS: dict[str, LocalVideoModel] = {
    "wan22_ti2v_5b": LocalVideoModel(
        model_id="wan22_ti2v_5b",
        label="Wan 2.2 TI2V-5B (text and image to video)",
        licence=_WAN_LICENCE,
        licence_url=_WAN_LICENCE_URL,
        commercial_note=_WAN_COMMERCIAL,
        capability="text_to_video",
        templates=("wan22_ti2v_5b_txt2video",),
        files=(
            ModelFile(
                filename="wan2.2_ti2v_5B_fp16.safetensors",
                path_in_repo="split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors",
                directory="diffusion_models",
                size_gb=10.0,
            ),
            ModelFile(
                filename="umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                path_in_repo="split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                directory="text_encoders",
                size_gb=6.74,
            ),
            ModelFile(
                filename="wan2.2_vae.safetensors",
                path_in_repo="split_files/vae/wan2.2_vae.safetensors",
                directory="vae",
                size_gb=1.41,
            ),
        ),
        template_params={
            "unet_name": "wan2.2_ti2v_5B_fp16.safetensors",
            "clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "vae_name": "wan2.2_vae.safetensors",
        },
        min_vram_mb=8192,
        vram_source=(
            "ComfyUI's own Wan 2.2 guide states the TI2V-5B workflow fits in 8 GB VRAM using "
            "ComfyUI's native offloading. Wan's reference implementation instead documents "
            "'at least 24 GB' for its own 5B command — a different implementation, quoted "
            "separately in the notes."
        ),
        min_ram_gb=32,
        ram_is_estimate=True,
        resolution="1280x704",
        fps=24,
        frames=121,
        notes=(
            "The consumer-GPU option, and the default here: Wan's high-compression hybrid "
            "model for text-to-video and image-to-video. Official config: 1280x704 (or "
            "704x1280), 24 fps, 121 frames by default (~5 s at 24 fps). Wan's reference "
            "command needs ~24 GB VRAM; ComfyUI's workflow needs ~8 GB with native "
            "offloading. Keep Wan's optional prompt-extension API (DashScope) disabled for a "
            "fully local run."
        ),
        sources=(
            "https://comfyanonymous.github.io/ComfyUI_examples/wan22/",
            "https://docs.comfy.org/tutorials/video/wan/wan2_2",
            "https://github.com/Wan-Video/Wan2.2",
            "https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B",
        ),
    ),
    "wan22_t2v_a14b": LocalVideoModel(
        model_id="wan22_t2v_a14b",
        label="Wan 2.2 T2V-A14B (text to video, two experts)",
        licence=_WAN_LICENCE,
        licence_url=_WAN_LICENCE_URL,
        commercial_note=_WAN_COMMERCIAL,
        capability="text_to_video",
        templates=(),  # graph not shipped: no verified 14B graph in this repository yet
        files=(
            ModelFile(
                filename="wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
                path_in_repo="split_files/diffusion_models/wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
                directory="diffusion_models",
                size_gb=14.3,
            ),
            ModelFile(
                filename="wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
                path_in_repo="split_files/diffusion_models/wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
                directory="diffusion_models",
                size_gb=14.3,
            ),
            ModelFile(
                filename="umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                path_in_repo="split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                directory="text_encoders",
                size_gb=6.74,
            ),
            ModelFile(
                filename="wan_2.1_vae.safetensors",
                path_in_repo="split_files/vae/wan_2.1_vae.safetensors",
                directory="vae",
                size_gb=0.25,
            ),
        ),
        template_params={
            "unet_name": "wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
            "clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "vae_name": "wan_2.1_vae.safetensors",
        },
        min_vram_mb=81920,
        vram_source=(
            "Wan's own single-GPU example for the A14B models runs at 1280x720 and documents "
            "'at least 80 GB VRAM'. ComfyUI ships FP8-scaled expert files and links community "
            "quantisations, but publishes no VRAM minimum for them."
        ),
        min_ram_gb=64,
        ram_is_estimate=True,
        resolution="1280x720",
        fps=24,
        frames=81,
        notes=(
            "Same licence as the 5B, higher fidelity, workstation-class requirement by Wan's "
            "own numbers (>= 80 GB VRAM at 720p). No workflow graph is shipped for it here: "
            "export one from your own ComfyUI (Save (API format)) once you have the hardware, "
            "because shipping a 14B graph nobody has run would be claiming an untested "
            "integration."
        ),
        sources=(
            "https://comfyanonymous.github.io/ComfyUI_examples/wan22/",
            "https://github.com/Wan-Video/Wan2.2",
            "https://huggingface.co/Wan-AI/Wan2.2-T2V-A14B",
        ),
    ),
}


def model_ids(*, selectable_only: bool = False) -> list[str]:
    return sorted(
        model_id for model_id, model in MODELS.items() if model.selectable or not selectable_only
    )


def get_model(model_id: str) -> LocalVideoModel:
    identifier = str(model_id or "").strip()
    if identifier not in MODELS:
        raise ComfyModelError(
            f"Unknown local video model {model_id!r}. Known: " + ", ".join(model_ids())
        )
    return MODELS[identifier]


def required_disk_gb(model: LocalVideoModel) -> float:
    return model.recommended_free_disk_gb()


def check_fit(model: LocalVideoModel, hardware: Mapping[str, Any]) -> dict[str, Any]:
    """Compare a model's stated requirements with the measured machine.

    Returns ``fits`` only when every *matching* requirement is met.  A requirement
    the probe could not measure (VRAM with no ``nvidia-smi``, RAM on a platform
    without ``os.sysconf``) never counts as satisfied: it is reported as unknown,
    with ``would_fit_if`` saying what would have to be true.
    """
    vram_mb = hardware.get("vram_total_mb")
    memory_gb = (hardware.get("memory") or {}).get("total_gb")
    free_disk_gb = (hardware.get("disk") or {}).get("free_gb")

    blockers: list[str] = []
    unknowns: list[str] = []

    if vram_mb is None:
        unknowns.append(
            f"No GPU VRAM could be measured, and {model.label} declares a {model.min_vram_mb} MB "
            "requirement."
        )
    elif int(vram_mb) < usable_vram_floor(model.min_vram_mb):
        blockers.append(
            f"{model.label} declares {model.min_vram_mb} MB VRAM; this machine reported "
            f"{int(vram_mb)} MB. ({model.vram_source})"
        )

    if model.min_ram_gb is not None:
        if memory_gb is None:
            unknowns.append(
                f"System memory could not be measured; {model.min_ram_gb} GB is a planning "
                "estimate for this model."
            )
        elif float(memory_gb) < float(model.min_ram_gb):
            suffix = " (planning estimate, not an official minimum)" if model.ram_is_estimate else ""
            blockers.append(
                f"{model.label} is planned for {model.min_ram_gb} GB of system RAM; this machine "
                f"reported {memory_gb} GB{suffix}."
            )

    needed_disk = required_disk_gb(model)
    if free_disk_gb is None:
        unknowns.append(f"Free disk space could not be measured; about {needed_disk} GB is needed.")
    elif float(free_disk_gb) < needed_disk:
        blockers.append(
            f"{needed_disk} GB of free disk is needed (model files plus headroom); "
            f"{free_disk_gb} GB is free."
        )

    return {
        "model_id": model.model_id,
        "fits": not blockers and not unknowns,
        "blockers": blockers,
        "unknowns": unknowns,
        "measured": {
            "vram_total_mb": vram_mb,
            "memory_total_gb": memory_gb,
            "free_disk_gb": free_disk_gb,
        },
        "required": {
            "min_vram_mb": model.min_vram_mb,
            "vram_floor_mb": usable_vram_floor(model.min_vram_mb),
            "vram_tolerance_note": (
                "A driver reports slightly less than the card's nominal size, so a 3% tolerance "
                "is applied to the stated requirement."
            ),
            "min_ram_gb": model.min_ram_gb,
            "model_disk_gb": model.model_disk_gb,
            "recommended_free_disk_gb": needed_disk,
        },
        "untested_below_vram": model.untested_below_vram,
    }


def select_video_model(
    hardware: Mapping[str, Any],
    *,
    preferred: Sequence[str] = (),
) -> dict[str, Any]:
    """Pick the local video model to install, and say why.

    Preference order: the caller's ``preferred`` ids first, then the catalogue's
    own order (5B before 14B).  A model that does not fit is never chosen — the
    answer is then ``model: None`` plus exactly what is missing, which is what the
    setup screen shows the user.
    """
    order = [model_id for model_id in preferred if model_id in MODELS]
    order += [model_id for model_id in model_ids(selectable_only=True) if model_id not in order]

    considered: list[dict[str, Any]] = []
    chosen: Optional[LocalVideoModel] = None
    chosen_fit: dict[str, Any] = {}
    for model_id in order:
        model = MODELS[model_id]
        fit = check_fit(model, hardware)
        considered.append({**fit, "label": model.label, "selectable": model.selectable})
        if chosen is None and fit["fits"] and model.selectable:
            chosen = model
            chosen_fit = fit

    result: dict[str, Any] = {
        "model": chosen.describe() if chosen else None,
        "fit": chosen_fit,
        "considered": considered,
        "policy": dict(POLICY),
    }
    if chosen is None:
        result["blockers"] = sorted({b for entry in considered for b in entry["blockers"]})
        result["unknowns"] = sorted({u for entry in considered for u in entry["unknowns"]})
        result["detail"] = (
            "No local video model in the catalogue fits the hardware that was measured here. "
            "Nothing will be installed; the reasons are listed above."
        )
    else:
        result["detail"] = (
            f"{chosen.label} fits this machine. It is free to run locally, under {chosen.licence}."
        )
    return result


def evaluated_but_not_selected() -> list[dict[str, Any]]:
    """Models considered for this project and the measured reasons they are not the default.

    Data, not prose: LTX-2 is a real local option, but its licence is not an open
    licence and its official hardware requirement is above consumer GPUs, so it is
    recorded here rather than silently ignored.
    """
    return [
        {
            "model_id": "ltx2",
            "label": "LTX-2 / LTX-2.5 (Lightricks)",
            "local_and_free": True,
            "selected": False,
            "licence": "LTX-2.x Community License Agreement (custom, not an open-source licence)",
            "licence_url": "https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2_x",
            "reasons": [
                "Licence: open weights, but under Lightricks' own Community License. Entities "
                "with at least US$10M annual revenue need a paid Commercial Use Agreement for "
                "commercial use (non-commercial testing excepted). Apache-2.0 it is not.",
                "Hardware: Lightricks' published system requirement is an NVIDIA GPU with 32 GB+ "
                "VRAM (their Desktop path documents >= 15 GiB), so it does not fit the "
                "consumer-GPU class this engine targets.",
            ],
            "credits": (
                "No credits are needed to run it locally; only their hosted API is paid. One "
                "official text-encoding node (GemmaAPITextEncode) does call an API, so a fully "
                "local run must use local text encoding instead."
            ),
            "sources": (
                "https://github.com/Lightricks/LTX-2",
                "https://docs.ltx.io/open-source-model/getting-started/system-requirements",
                "https://github.com/Lightricks/LTX-Desktop",
                "https://github.com/Lightricks/ComfyUI-LTXVideo",
            ),
            "when_it_would_be_selected": (
                "On a 32 GB+ GPU, and after the user accepts a non-open licence and checks the "
                "commercial terms for their organisation."
            ),
        },
        {
            "model_id": "ltx_video_0_9_x",
            "label": "LTX-Video 0.9.x (Lightricks)",
            "local_and_free": True,
            "selected": False,
            "licence": "LTXV Open Weights License 0.X (custom; commercial threshold for large entities)",
            "licence_url": "https://huggingface.co/Lightricks/LTX-Video/blob/main/LTX-Video-Open-Weights-License-0.X.txt",
            "reasons": [
                "Same licence family as LTX-2: royalty-free use under its conditions, but "
                "entities at or above the revenue threshold need a paid commercial licence.",
                "No official minimum VRAM is published for the 13B variants; the documented "
                "8 GB result is one optimised quantised configuration, not a guarantee.",
            ],
            "credits": "No credits for local generation.",
            "sources": (
                "https://github.com/Lightricks/LTX-Video",
                "https://github.com/Lightricks/ComfyUI-LTXVideo",
            ),
            "when_it_would_be_selected": (
                "If a user prefers a Lightricks model and accepts the licence, a quantised "
                "configuration can be added the same way any other template is: export the API "
                "graph from their ComfyUI and let the adapter verify it."
            ),
        },
    ]
