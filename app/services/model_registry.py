"""Normalized model registry — a catalogue only if it can prove what it says.

Maestro already knows which models *it* wants (``app/models/*`` family handlers
declare them, and ``wgp.py`` loads that catalogue), but it has no way to describe
a model that comes from anywhere else: what it is, what it can do, what it costs
in disk and memory, what licence and authentication it needs, whether it can be
downloaded at all, and — the part that is easiest to fake — *how we know*.

This module is that description.  It is deliberately boring and deliberately
suspicious:

* **Unknown is a first-class value.**  Every field is optional and defaults to
  "not known" (``None`` or empty).  Nothing is filled in from a model's name, a
  repo's popularity or a hope.  A record with an empty ``license`` says "licence
  not verified", which is different from "permissive".
* **Positive claims need evidence.**  Saying a model is downloadable, that it
  needs no authentication, that a licence applies, that it runs on an accelerator
  or that some remote service can execute it requires an :class:`Evidence` entry
  naming the URL it was read from and the date.  An unsupported claim raises.
* **A repository is not a service.**  ``download_available`` and
  ``remote_inference_available`` are separate facts: the Hugging Face adapter
  below can report the first and *never* the second, because having the weights
  says nothing about anybody running them.
* **Resources carry their basis.**  A number is only ever ``measured``,
  ``documented`` or ``estimated`` (:class:`Resource`), so a guess can never be
  displayed as a requirement.
* **Vocabulary is shared, not re-invented.**  Tasks are the capabilities
  :mod:`services.engine_providers` already resolves (``text_to_video``,
  ``image_generation``, …), so a registered model and a registered engine speak
  the same language.

The module is stdlib-only and imports nothing heavy, because the machine that
needs it is exactly the machine without a GPU.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .engine_providers import CAPABILITIES

__all__ = [
    "ACCELERATORS",
    "BASES",
    "EVIDENCE_REQUIRED_FIELDS",
    "ENGINES",
    "ModelRecord",
    "ModelRegistry",
    "ModelRegistryError",
    "PRECISIONS",
    "RESOURCE_FIELDS",
    "Resource",
    "SOURCES",
    "TASKS",
    "WEIGHT_FORMATS",
    "from_json",
    "to_json",
]


class ModelRegistryError(ValueError):
    """A record, a claim or a registry operation that cannot be trusted."""


# Tasks are the capability ids the engine registry already resolves; a model that
# declares a task nobody can route is a model nobody can use.
TASKS: tuple[str, ...] = tuple(CAPABILITIES)

# Where a record came from.  Only sources with a documented, public interface are
# listed; anything else would have to be added with its own verified adapter.
SOURCES: tuple[str, ...] = ("huggingface", "civitai", "local")

# Container formats of the weight files themselves.
WEIGHT_FORMATS: tuple[str, ...] = (
    "safetensors", "sft", "gguf", "pt", "pth", "ckpt", "onnx", "bin", "zip",
)

# Numerical precision of the weights, as declared by the publisher.
PRECISIONS: tuple[str, ...] = (
    "fp32", "fp16", "bf16", "fp8", "int8", "w4a8", "int4",
    "q4_k_m", "q5_k_m", "q8_0",
)

# Hardware a record claims to run on.  These are the backends the studio spec
# names; ``cpu`` and ``cuda`` are the two that exist in Maestro today.
ACCELERATORS: tuple[str, ...] = (
    "cuda", "cpu", "mps", "rocm", "directml", "openvino", "onnxruntime",
)

# Inference engine a record claims to need.
ENGINES: tuple[str, ...] = (
    "wangp", "llama.cpp", "diffusers", "onnxruntime", "openvino", "directml",
    "comfyui", "remote_api",
)

# How a resource figure was obtained.  ``estimated`` is allowed to be wrong;
# ``measured`` is not allowed to be a guess.
BASES: tuple[str, ...] = ("measured", "documented", "estimated", "unknown")

# The resource figures a record may carry, each with its own basis.
RESOURCE_FIELDS: tuple[str, ...] = (
    "download_bytes", "disk_bytes", "ram_bytes", "vram_bytes",
)

# Fields that are *claims about the world* rather than identity.  Each one
# requires at least one :class:`Evidence` entry naming where it was read.
EVIDENCE_REQUIRED_FIELDS: tuple[str, ...] = (
    "accelerators",
    "download_available",
    "engine",
    "formats",
    "precisions",
    "license",
    "requires_auth",
    "remote_inference_available",
    "download_bytes",
    "ram_bytes",
    "vram_bytes",
)

_ISO_DATE_LENGTH = 10


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _iso_date(value: Any) -> str:
    """The date part of an ISO-8601 stamp, or ``""`` when it is not one."""
    text = _clean(value)
    if len(text) < _ISO_DATE_LENGTH:
        return ""
    head = text[:_ISO_DATE_LENGTH]
    parts = head.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return ""
    return head


@dataclass(frozen=True)
class Evidence:
    """Where one fact was read, and when.  A fact without one is not a fact."""

    field: str
    source: str
    checked_at: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not _clean(self.field):
            raise ModelRegistryError("Evidence.field is required")
        if not _clean(self.source):
            raise ModelRegistryError(
                f"Evidence for {self.field!r} needs the source it was read from"
            )
        if not _iso_date(self.checked_at):
            raise ModelRegistryError(
                f"Evidence for {self.field!r} needs an ISO-8601 date, "
                f"got {self.checked_at!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "source": self.source,
            "checked_at": self.checked_at,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        return cls(
            field=str(data.get("field") or ""),
            source=str(data.get("source") or ""),
            checked_at=str(data.get("checked_at") or ""),
            detail=str(data.get("detail") or ""),
        )


@dataclass(frozen=True)
class Resource:
    """One resource figure, with the basis it was obtained from.

    A ``None`` value means "not known"; ``BASES[3]`` (``unknown``) is what the
    record carries when the publisher says nothing and nobody measured it.
    """

    value: Optional[int] = None
    basis: str = "unknown"
    source: str = ""

    def __post_init__(self) -> None:
        if self.basis not in BASES:
            raise ModelRegistryError(
                f"Unknown resource basis {self.basis!r}. Known: " + ", ".join(BASES)
            )
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise ModelRegistryError("A resource value must be an integer number of bytes")
            if self.value < 0:
                raise ModelRegistryError("A resource value cannot be negative")
            if self.basis == "unknown":
                raise ModelRegistryError(
                    "A resource value with basis 'unknown' contradicts itself: "
                    "give it 'measured', 'documented' or 'estimated'"
                )
        elif self.basis != "unknown":
            raise ModelRegistryError(
                "A resource without a value must have basis 'unknown'"
            )

    @classmethod
    def unknown(cls) -> "Resource":
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "basis": self.basis, "source": self.source}

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "Resource":
        if data is None:
            return cls()
        return cls(
            value=data.get("value"),
            basis=str(data.get("basis") or "unknown"),
            source=str(data.get("source") or ""),
        )


@dataclass(frozen=True)
class ModelRecord:
    """One model, described only by what is actually known about it."""

    model_id: str
    name: str
    source: str
    repo_id: str
    url: str
    tasks: tuple[str, ...] = ()
    author: str = ""
    family: str = ""
    architecture: str = ""
    formats: tuple[str, ...] = ()
    precisions: tuple[str, ...] = ()
    weight_files: tuple[str, ...] = ()
    accelerators: tuple[str, ...] = ()
    engine: str = ""
    operating_systems: tuple[str, ...] = ()
    license: str = ""
    requires_auth: Optional[bool] = None
    download_available: Optional[bool] = None
    remote_inference_available: Optional[bool] = None
    remote_providers: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    maintenance: Mapping[str, Any] = field(default_factory=dict)
    download_bytes: Resource = field(default_factory=Resource.unknown)
    disk_bytes: Resource = field(default_factory=Resource.unknown)
    ram_bytes: Resource = field(default_factory=Resource.unknown)
    vram_bytes: Resource = field(default_factory=Resource.unknown)
    extra: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()

    # ------------------------------------------------------------------ rules
    def __post_init__(self) -> None:
        for attribute in ("model_id", "name", "source", "repo_id", "url"):
            if not _clean(getattr(self, attribute)):
                raise ModelRegistryError(f"ModelRecord.{attribute} is required")
        if self.source not in SOURCES:
            raise ModelRegistryError(
                f"Unknown model source {self.source!r}. Known: " + ", ".join(SOURCES)
            )
        for task in self.tasks:
            if task not in TASKS:
                raise ModelRegistryError(
                    f"Unknown task {task!r} on {self.model_id!r}. Known tasks: "
                    + ", ".join(TASKS)
                )
        self._check_vocabulary("formats", self.formats, WEIGHT_FORMATS)
        self._check_vocabulary("precisions", self.precisions, PRECISIONS)
        self._check_vocabulary("accelerators", self.accelerators, ACCELERATORS)
        if self.engine and self.engine not in ENGINES:
            raise ModelRegistryError(
                f"Unknown inference engine {self.engine!r}. Known: " + ", ".join(ENGINES)
            )
        for resource_name in RESOURCE_FIELDS:
            if not isinstance(getattr(self, resource_name), Resource):
                raise ModelRegistryError(
                    f"ModelRecord.{resource_name} must be a Resource, "
                    "so its basis is never lost"
                )
        if self.requires_auth is None and self.license and not self.has_evidence("license"):
            raise ModelRegistryError(
                f"{self.model_id!r} states a licence without evidence for it"
            )
        self._check_claims()

    def _check_vocabulary(self, attribute: str, values: Sequence[str], allowed: Sequence[str]) -> None:
        for value in values:
            if value not in allowed:
                raise ModelRegistryError(
                    f"Unknown {attribute[:-1] if attribute.endswith('s') else attribute} "
                    f"{value!r} on {self.model_id!r}. Known: " + ", ".join(allowed)
                )

    def _check_claims(self) -> None:
        """Every positive claim about the world needs somewhere it was read."""
        for field_name in EVIDENCE_REQUIRED_FIELDS:
            value = getattr(self, field_name)
            claimed = (
                bool(value)
                if not isinstance(value, Resource)
                else value.value is not None
            )
            if not claimed:
                continue
            if getattr(value, "basis", "") == "estimated":
                continue  # an estimate is the record's own arithmetic, not a claim
            if not self.has_evidence(field_name):
                raise ModelRegistryError(
                    f"{self.model_id!r} claims {field_name}={value!r} without evidence "
                    "for it; an unverified claim is not stored"
                )

    # ------------------------------------------------------- queries & export
    def has_evidence(self, field_name: str) -> bool:
        return any(item.field == field_name for item in self.evidence)

    def evidence_for(self, field_name: str) -> tuple[Evidence, ...]:
        return tuple(item for item in self.evidence if item.field == field_name)

    @property
    def last_checked_at(self) -> str:
        """The newest verification date in the record, or ``""`` when unverified."""
        stamps = [_iso_date(item.checked_at) for item in self.evidence]
        return max(stamps) if stamps else ""

    def resource(self, field_name: str) -> Resource:
        if field_name not in RESOURCE_FIELDS:
            raise ModelRegistryError(
                f"Unknown resource {field_name!r}. Known: " + ", ".join(RESOURCE_FIELDS)
            )
        return getattr(self, field_name)

    def with_evidence(self, *items: Evidence) -> "ModelRecord":
        """Return a copy carrying more evidence (records are immutable)."""
        return replace(self, evidence=self.evidence + tuple(items))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "model_id": self.model_id,
            "name": self.name,
            "source": self.source,
            "repo_id": self.repo_id,
            "url": self.url,
            "tasks": list(self.tasks),
            "author": self.author,
            "family": self.family,
            "architecture": self.architecture,
            "formats": list(self.formats),
            "precisions": list(self.precisions),
            "weight_files": list(self.weight_files),
            "accelerators": list(self.accelerators),
            "engine": self.engine,
            "operating_systems": list(self.operating_systems),
            "license": self.license,
            "requires_auth": self.requires_auth,
            "download_available": self.download_available,
            "remote_inference_available": self.remote_inference_available,
            "remote_providers": list(self.remote_providers),
            "dependencies": list(self.dependencies),
            "maintenance": dict(self.maintenance),
            "extra": dict(self.extra),
            "evidence": [item.to_dict() for item in self.evidence],
        }
        for resource_name in RESOURCE_FIELDS:
            data[resource_name] = getattr(self, resource_name).to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelRecord":
        return cls(
            model_id=str(data.get("model_id") or ""),
            name=str(data.get("name") or ""),
            source=str(data.get("source") or ""),
            repo_id=str(data.get("repo_id") or ""),
            url=str(data.get("url") or ""),
            tasks=tuple(str(item) for item in (data.get("tasks") or ())),
            author=str(data.get("author") or ""),
            family=str(data.get("family") or ""),
            architecture=str(data.get("architecture") or ""),
            formats=tuple(str(item) for item in (data.get("formats") or ())),
            precisions=tuple(str(item) for item in (data.get("precisions") or ())),
            weight_files=tuple(str(item) for item in (data.get("weight_files") or ())),
            accelerators=tuple(str(item) for item in (data.get("accelerators") or ())),
            engine=str(data.get("engine") or ""),
            operating_systems=tuple(str(item) for item in (data.get("operating_systems") or ())),
            license=str(data.get("license") or ""),
            requires_auth=data.get("requires_auth"),
            download_available=data.get("download_available"),
            remote_inference_available=data.get("remote_inference_available"),
            remote_providers=tuple(str(item) for item in (data.get("remote_providers") or ())),
            dependencies=tuple(str(item) for item in (data.get("dependencies") or ())),
            maintenance=dict(data.get("maintenance") or {}),
            extra=dict(data.get("extra") or {}),
            evidence=tuple(
                Evidence.from_dict(item)
                for item in (data.get("evidence") or ())
                if isinstance(item, Mapping)
            ),
            **{name: Resource.from_dict(data.get(name)) for name in RESOURCE_FIELDS},
        )


@dataclass
class ModelRegistry:
    """The catalogue itself: add, replace, look up, and export.

    Deliberately in-memory and explicit.  Discovering a model does **not** put it
    in here and certainly does not download it: a record enters the registry when
    something (the user, or an adapter that checked a source) decided to store
    it.
    """

    _records: dict[str, ModelRecord] = field(default_factory=dict)

    def add(self, record: ModelRecord) -> ModelRecord:
        if not isinstance(record, ModelRecord):
            raise ModelRegistryError("Only a ModelRecord can be registered")
        if record.model_id in self._records:
            raise ModelRegistryError(
                f"{record.model_id!r} is already registered; use replace()"
            )
        self._records[record.model_id] = record
        return record

    def replace(self, record: ModelRecord) -> ModelRecord:
        if record.model_id not in self._records:
            raise ModelRegistryError(
                f"{record.model_id!r} is not registered; use add()"
            )
        self._records[record.model_id] = record
        return record

    def remove(self, model_id: str) -> ModelRecord:
        try:
            return self._records.pop(model_id)
        except KeyError:
            raise ModelRegistryError(f"{model_id!r} is not registered") from None

    def get(self, model_id: str) -> ModelRecord:
        try:
            return self._records[model_id]
        except KeyError:
            raise ModelRegistryError(f"{model_id!r} is not registered") from None

    def __contains__(self, model_id: object) -> bool:
        return isinstance(model_id, str) and model_id in self._records

    def __len__(self) -> int:
        return len(self._records)

    def list(self) -> list[ModelRecord]:
        """Every record, in a stable order so two runs agree."""
        return [self._records[key] for key in sorted(self._records)]

    def find(
        self,
        *,
        task: str = "",
        accelerator: str = "",
        engine: str = "",
        source: str = "",
        download_available_only: bool = False,
        max_download_bytes: Optional[int] = None,
    ) -> list[ModelRecord]:
        """Records that match every stated requirement — and nothing else.

        Filters are applied only to facts the record actually contains.  A record
        that has not verified its accelerator list does **not** match
        ``accelerator="cpu"``: unknown is not a match, it is a gap to fill.
        """
        if task and task not in TASKS:
            raise ModelRegistryError(
                f"Unknown task {task!r}. Known tasks: " + ", ".join(TASKS)
            )
        if accelerator and accelerator not in ACCELERATORS:
            raise ModelRegistryError(
                f"Unknown accelerator {accelerator!r}. Known: " + ", ".join(ACCELERATORS)
            )
        if engine and engine not in ENGINES:
            raise ModelRegistryError(
                f"Unknown inference engine {engine!r}. Known: " + ", ".join(ENGINES)
            )
        if source and source not in SOURCES:
            raise ModelRegistryError(
                f"Unknown source {source!r}. Known: " + ", ".join(SOURCES)
            )
        if max_download_bytes is not None and max_download_bytes < 0:
            raise ModelRegistryError("max_download_bytes cannot be negative")

        matched: list[ModelRecord] = []
        for record in self.list():
            if task and task not in record.tasks:
                continue
            if accelerator and accelerator not in record.accelerators:
                continue
            if engine and record.engine != engine:
                continue
            if source and record.source != source:
                continue
            if download_available_only and record.download_available is not True:
                continue
            if max_download_bytes is not None:
                size = record.download_bytes.value
                if size is None or size > max_download_bytes:
                    continue
            matched.append(record)
        return matched

    def tasks(self) -> dict[str, list[str]]:
        """Which registered model ids cover each task, for the interface."""
        coverage: dict[str, list[str]] = {}
        for record in self.list():
            for task in record.tasks:
                coverage.setdefault(task, []).append(record.model_id)
        return coverage

    def dump(self) -> dict[str, Any]:
        return {
            "version": 1,
            "records": [record.to_dict() for record in self.list()],
        }

    @classmethod
    def load(cls, payload: Mapping[str, Any]) -> "ModelRegistry":
        registry = cls()
        records = payload.get("records") if isinstance(payload, Mapping) else None
        for item in records or ():
            if isinstance(item, Mapping):
                registry.add(ModelRecord.from_dict(item))
        return registry

    def save(self, path: str) -> str:
        """Write the catalogue atomically; a half-written file is never left."""
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".model_registry.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.dump(), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return path

    @classmethod
    def load_file(cls, path: str) -> "ModelRegistry":
        """A corrupt catalogue reads as an empty one, never as a crash."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return cls()
        return cls.load(payload if isinstance(payload, Mapping) else {})


def to_json(records: Iterable[ModelRecord]) -> str:
    """A JSON list of records, for the API and the interface."""
    return json.dumps([record.to_dict() for record in records], indent=2, sort_keys=True)


def from_json(payload: str) -> list[ModelRecord]:
    """The inverse of :func:`to_json`; a bad payload raises a named error."""
    try:
        data = json.loads(payload)
    except ValueError as error:
        raise ModelRegistryError(f"The registry payload is not JSON: {error}") from None
    if not isinstance(data, list):
        raise ModelRegistryError("The registry payload must be a JSON list of records")
    return [
        ModelRecord.from_dict(item)
        for item in data
        if isinstance(item, Mapping)
    ]


# ---------------------------------------------------------------------------
# Sources: Hugging Face Hub, read through the same transport seam the public
# compute tier uses, so it is testable without a network and never surprises a
# machine with nothing installed.
# ---------------------------------------------------------------------------

HUB_API = "https://huggingface.co/api"

# What the Hub's *declared* pipeline tag means in this registry's vocabulary.
# A tag is the repository saying what it is; a name that merely sounds right is
# not a task (and is not consulted here at all).
_PIPELINE_TAGS: dict[str, tuple[str, ...]] = {
    "text-to-video": ("text_to_video",),
    "image-to-video": ("image_to_video",),
    "video-to-video": ("video_to_video",),
    "text-to-image": ("image_generation",),
    "image-to-image": ("image_generation", "inpainting"),
    "text-to-speech": ("voice_generation",),
    "text-to-audio": ("audio_generation",),
    "text-to-music": ("audio_generation",),
    "automatic-speech-recognition": ("transcription",),
}

_EXTENSION_FORMATS: dict[str, str] = {
    ".safetensors": "safetensors",
    ".sft": "sft",
    ".gguf": "gguf",
    ".pt": "pt",
    ".pth": "pth",
    ".ckpt": "ckpt",
    ".onnx": "onnx",
    ".bin": "bin",
    ".zip": "zip",
}

_PRECISION_HINTS: tuple[tuple[str, str], ...] = (
    ("fp32", "fp32"),
    ("float32", "fp32"),
    ("bf16", "bf16"),
    ("bfloat16", "bf16"),
    ("fp16", "fp16"),
    ("float16", "fp16"),
    ("fp8", "fp8"),
    ("w4a8", "w4a8"),
    ("int8", "int8"),
    ("int4", "int4"),
    ("q4_k_m", "q4_k_m"),
    ("q5_k_m", "q5_k_m"),
    ("q8_0", "q8_0"),
)


# ``(method, url, headers, timeout) -> object with .status/.body``.  The default
# transport is stdlib ``urllib``; tests inject their own.
HubTransport = Callable[..., Any]


class _HubResponse:
    __slots__ = ("status", "body")

    def __init__(self, status: int, body: bytes) -> None:
        self.status = int(status)
        self.body = bytes(body)

    def json(self) -> Any:
        if not self.body:
            return {}
        return json.loads(self.body.decode("utf-8"))


def _default_transport(method: str, url: str, *, headers: Optional[Mapping[str, str]] = None,
                       timeout: float = 20.0) -> _HubResponse:  # pragma: no cover - network
    from urllib import error as urlerror
    from urllib import request as urlrequest

    request = urlrequest.Request(
        url,
        headers={"User-Agent": "maestro-model-registry", **dict(headers or {})},
        method=method.upper(),
    )
    try:
        with urlrequest.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            return _HubResponse(response.status, response.read())
    except urlerror.HTTPError as error:
        return _HubResponse(error.code, error.read())


class HuggingFaceSource:
    """Reads a public Hub repository into a :class:`ModelRecord`.

    It reads the Hub's *own* report (``/api/models/<repo>``) and records where
    each fact came from.  Two things it deliberately never does:

    * **It never claims remote inference.**  A repository that publishes weights
      says nothing about a service that runs them, so
      ``remote_inference_available`` stays ``None`` and only a separate, verified
      provider adapter may set it.
    * **It never guesses a task from the name.**  Only the declared
      ``pipeline_tag``/tags are translated, through the fixed table above.
    """

    def __init__(self, *, transport: Optional[HubTransport] = None, timeout: float = 20.0) -> None:
        self._transport = transport or _default_transport
        self._timeout = float(timeout)

    def fetch(self, repo_id: str, *, checked_at: str) -> ModelRecord:
        repo = _clean(repo_id)
        if not repo:
            raise ModelRegistryError("A Hugging Face repository id is required")
        url = f"{HUB_API}/models/{repo}"
        response = self._transport("GET", url, headers={}, timeout=self._timeout)
        if int(getattr(response, "status", 0)) != 200:
            raise ModelRegistryError(
                f"Hugging Face answered {getattr(response, 'status', 'nothing')} "
                f"for {repo!r}"
            )
        payload = response.json() if hasattr(response, "json") else {}
        if not isinstance(payload, Mapping):
            raise ModelRegistryError(f"Hugging Face returned no repository data for {repo!r}")
        return self.record_from_hub(payload, url=url, checked_at=checked_at)

    def record_from_hub(
        self,
        payload: Mapping[str, Any],
        *,
        url: str,
        checked_at: str,
    ) -> ModelRecord:
        """Turn one Hub API answer into a record, without inventing a single field."""
        repo_id = _clean(payload.get("id") or payload.get("modelId"))
        if not repo_id:
            raise ModelRegistryError("The Hub answer has no repository id")
        page = _clean(url) or f"https://huggingface.co/{repo_id}"
        tags = [str(item) for item in (payload.get("tags") or ()) if str(item).strip()]
        pipeline = _clean(payload.get("pipeline_tag"))
        tasks = self._tasks(pipeline, tags)

        files = self._filenames(payload)
        formats = tuple(sorted({_EXTENSION_FORMATS[os.path.splitext(name)[1].lower()]
                                for name in files
                                if os.path.splitext(name)[1].lower() in _EXTENSION_FORMATS}))
        precisions = tuple(sorted({precision for hint, precision in _PRECISION_HINTS
                                   if any(hint in name.casefold() for name in files)}))

        gated = payload.get("gated")
        card = payload.get("cardData") if isinstance(payload.get("cardData"), Mapping) else {}
        licence = _clean((card or {}).get("license"))
        author = _clean(payload.get("author"))
        last_modified = _clean(payload.get("lastModified"))
        downloads = payload.get("downloads")
        likes = payload.get("likes")

        maintenance = {
            "downloads": downloads if isinstance(downloads, int) else None,
            "likes": likes if isinstance(likes, int) else None,
            "last_modified": last_modified,
            "archived": bool(payload.get("archived")) if payload.get("archived") is not None else None,
        }

        fields = {
            "tasks": tasks,
            "formats": formats,
            "precisions": precisions,
            "license": licence,
            "accelerators": (),
            "download_available": True,
        }
        evidence = [
            Evidence(field=name, source=page, checked_at=checked_at,
                     detail="read from the Hugging Face Hub API")
            for name, value in fields.items()
            if value
        ]
        if gated is not None:
            evidence.append(Evidence(
                field="requires_auth", source=page, checked_at=checked_at,
                detail=f"the Hub reports gated={gated!r}",
            ))

        return ModelRecord(
            model_id=f"huggingface:{repo_id}",
            name=repo_id,
            source="huggingface",
            repo_id=repo_id,
            url=page,
            tasks=tasks,
            author=author,
            formats=formats,
            precisions=precisions,
            weight_files=files,
            accelerators=(),
            license=licence,
            requires_auth=None if gated is None else bool(gated),
            # Weights are published; nothing here says a service runs them.
            download_available=True,
            remote_inference_available=None,
            maintenance=maintenance,
            evidence=tuple(evidence),
        )

    @staticmethod
    def _tasks(pipeline: str, tags: Sequence[str]) -> tuple[str, ...]:
        found: set[str] = set()
        for token in (pipeline, *tags):
            for task in _PIPELINE_TAGS.get(str(token).strip().lower(), ()):
                if task in TASKS:
                    found.add(task)
        return tuple(sorted(found))

    @staticmethod
    def _filenames(payload: Mapping[str, Any]) -> tuple[str, ...]:
        """The repository's *weight* files, in the order the Hub listed them.

        A sibling list also carries READMEs, thumbnails and configs; only files
        whose extension is a container this registry knows are counted, so
        ``formats`` and ``precisions`` are read from weights and not from
        whatever else the repository happens to contain.
        """
        names: list[str] = []
        for item in payload.get("siblings") or ():
            if isinstance(item, Mapping):
                name = _clean(item.get("rfilename") or item.get("path"))
            else:
                name = _clean(item)
            if not name or name in names:
                continue
            if os.path.splitext(name)[1].lower() not in _EXTENSION_FORMATS:
                continue
            names.append(name)
        return tuple(names)
