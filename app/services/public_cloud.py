"""Public compute over the internet — a free engine that is nobody's computer.

Maestro is local-first, and that ordering does not change here: a machine that can
generate generates, and nothing is sent away while it could have done the work.
The tiers below it are the ones this module adds to:

    this computer (CUDA here)
        → a peer Maestro you own (services.remote_engine)
        → **public compute** (this module): a free, public GPU on the internet

The last tier exists for the computer that has neither.  Nobody has to own a
second machine, install anything, or create an account: Maestro asks the public
Hugging Face Hub which of its Gradio Spaces are *running right now* on free
ZeroGPU hardware, connects to one that declares the task, and runs the request
there.  The choosing is done from the Hub's own report (``runtime.stage`` and the
Space's own ``/gradio_api/info``), never from a name that merely sounds right.

Five rules, because this is the tier where it is easiest to lie:

* **Measured, never hard-coded.**  Nothing here assumes a particular Space exists.
  Discovery reads the Hub API, the probe reads the Space's own OpenAPI, and a
  Space that is paused, erroring or gone is simply not a source.  A Space that
  disappears next month is re-discovered, not worked around.
* **An account is optional, never required.**  Anonymous ZeroGPU quota is a real
  but small allowance (minutes per day, shared queue).  When the launcher has
  already signed Hugging Face in, that same free account is used for its larger
  free allowance — no new signup, no key of ours, and the token never leaves this
  computer.
* **No invented output.**  What comes back is validated against the capability
  that asked for it (an image is an image, a video is a video).  A JSON error, an
  HTML page or an empty body is a failure with its own reason, never a "result".
* **A public engine is not this engine.**  The Space runs its own model with its
  own defaults, so the render is an approximation of the Maestro recipe, not a
  faithful replay of it.  That is stated on the route (``fidelity``) and in the
  job's own message, instead of being passed off as the same thing.
* **Unavailable is not failed.**  A Space that is down, queued out or rate
  limited *defers* the job (it keeps waiting, consuming no attempt) — the same
  rule the peer tier already follows.

The module is stdlib-only on purpose: it has to import on a machine with no GPU,
no torch and no ComfyUI, which is exactly the machine that needs it.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
import time
import uuid
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlsplit

from .queue_worker import JobCancelled

__all__ = [
    "DEFAULT_RUN_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "HUB_API",
    "PUBLIC_ENGINE_PREFIX",
    "PUBLIC_ONLINE_TTL",
    "PUBLIC_REFRESH_AFTER",
    "PublicCloudError",
    "PublicUnavailable",
    "SEARCH_TERMS",
    "SERVED_CAPABILITIES",
    "STATE_FILENAME",
    "add_space",
    "connect",
    "connected_sources",
    "engine_for",
    "forget_provider",
    "list_providers",
    "load_state",
    "provider_entries",
    "probe_provider",
    "register_public_handlers",
    "run_job",
    "unregister_public_handlers",
]

# A public job names its engine as ``public:<capability>``: the capability is what
# the queue has to route (the exact Space that serves it is chosen at run time,
# because a public Space can go away between two renders).
PUBLIC_ENGINE_PREFIX = "public"
STATE_FILENAME = "public_cloud.json"

# The Hub's own API is how discovery stays honest: it reports, per Space, the SDK
# and the *current* runtime stage, so "this Space is running on a free GPU now" is
# a measurement instead of a hope.
HUB_API = "https://huggingface.co/api"
SPACE_HOST = "https://{subdomain}.hf.space"
GRADIO = "/gradio_api"

DEFAULT_TIMEOUT = 15.0
# A public queue can be long (a free GPU is shared), but it is not unbounded: past
# this the render is given up and the job goes back to waiting.
DEFAULT_RUN_TIMEOUT = 900.0
DEFAULT_DISCOVERY_LIMIT = 20

# How long a successful probe keeps a public provider usable.  Shorter than the
# peer window on purpose: a public service is not ours and can vanish quietly.
PUBLIC_ONLINE_TTL = 1800.0
# Past this age a connected provider is asked again before work is sent to it, so
# a Space that went down overnight is not discovered at render time.
PUBLIC_REFRESH_AFTER = 300.0

# After the best candidate is found, a couple more Spaces are still probed and kept
# as alternates: a public engine that breaks between the probe and the render has to
# have somewhere for the job to go, or one broken service would lose a render.
PUBLIC_FALLBACK_PROBES = 2

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_MEDIA_BYTES = 512 * 1024 * 1024

# The capabilities a public engine may serve.  Each one maps to the Hub *tags* its
# Spaces carry and to the media its result has to be; a capability that is not here
# is never routed to the public tier, however tempting it looks.
#
# Tags, not free text: the Hub indexes a Space by what it declares itself to be, so
# ``text-to-video`` is a Space saying it makes videos from text, while searching for
# the words in a name only finds the ones that happened to put them there.
SEARCH_TERMS: dict[str, tuple[str, ...]] = {
    "image_generation": ("text-to-image",),
    "inpainting": ("image-inpainting", "inpainting"),
    "text_to_video": ("text-to-video",),
    "image_to_video": ("image-to-video",),
    "voice_generation": ("text-to-speech",),
    "audio_generation": ("text-to-audio", "text-to-music"),
}

SERVED_CAPABILITIES: tuple[str, ...] = tuple(SEARCH_TERMS)

# capability -> the kind of file its result must be.  Enforced on the bytes that
# come back (see :func:`_classify`), so a public error page can never be recorded
# as a finished render.
EXPECTED_MEDIA: dict[str, str] = {
    "image_generation": "image",
    "inpainting": "image",
    "text_to_video": "video",
    "image_to_video": "video",
    "voice_generation": "audio",
    "audio_generation": "audio",
}

# A request's own media, in the order it is offered to a Space's file inputs.
# ``image`` and ``video`` name what the parameter has to be about.
_MEDIA_KEYS: dict[str, tuple[str, ...]] = {
    "image": ("image_start", "image_end", "image_refs", "image_guide", "image_mask", "viggle_character"),
    "video": ("video_guide", "video_guide2"),
}

# Chinese walls: the prompt is the only text a public Space is given.
_PROMPT_NAMES = ("prompt", "text", "input", "caption", "positive_prompt", "instruction")
_NEGATIVE_NAMES = ("negative_prompt", "negative", "neg_prompt")
_SEED_NAMES = ("seed", "seed_ui", "random_seed")
_STEPS_NAMES = ("num_inference_steps", "steps", "num_steps", "sampling_steps")
_WIDTH_NAMES = ("width", "width_ui", "w")
_HEIGHT_NAMES = ("height", "height_ui", "h")
_DURATION_NAMES = ("duration", "duration_ui", "seconds", "video_seconds")
_RANDOMIZE_NAMES = ("randomize_seed", "randomize")
# Maestro counts video in *frames* and a public Space counts it in *seconds*, so a
# duration is only ever sent when it can be derived from both — a frame count sent
# as seconds would ask a public engine for a two-minute video.
_FRAME_KEYS = ("video_length", "num_frames", "frames")
_FPS_KEYS = ("fps", "frame_rate", "framerate")

# Sliders and numbers declare their own bounds ("numeric value between 10 and
# 50").  A public engine cannot honour a value outside them, and sending one is
# rejected with the service's own message, so the request is brought inside the
# range the engine really supports — and says so, instead of quietly changing it.
_NUMERIC_RANGE_RE = re.compile(r"between\s+(-?[\d.]+)\s+and\s+(-?[\d.]+)")

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")


class PublicCloudError(RuntimeError):
    """A public provider answered something that cannot be used as a render."""


class PublicUnavailable(PublicCloudError):
    """No public provider is connected for the capability, or it is away now.

    Raised where a job should be *deferred* rather than failed: the work is still
    owed, the public service is simply not there at this moment.
    """


# What a public service says when the problem is *its* capacity rather than the
# request: the daily free GPU allowance being spent, a shared queue that is full, a
# model still loading.  These are exactly the cases where waiting is right and
# failing would be wrong, so they are recognised from the service's own words.
_TRANSIENT_MARKERS = (
    "quota",
    "rate limit",
    "rate-limit",
    "too many requests",
    "try again",
    "gpu task aborted",
    "out of memory",
    "queue is full",
    "overloaded",
    "temporarily",
    "loading",
    "no gpu",
    "503",
    "429",
)


def _is_transient(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


# ---------------------------------------------------------------------------
# HTTP, over stdlib only
# ---------------------------------------------------------------------------


class HttpResponse:
    """The small part of an HTTP response this module needs."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in dict(headers).items()}
        self.body = bytes(body)

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except ValueError:
            return {"detail": self.body.decode("utf-8", errors="replace")[:400]}


# ``(method, url, data, headers, timeout) -> HttpResponse``.  The transport is the
# only seam a test needs, and the default one is stdlib ``urllib`` so the module
# keeps working on a machine with nothing installed.
Transport = Callable[..., HttpResponse]


def _default_transport(
    method: str,
    url: str,
    *,
    data: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> HttpResponse:
    request = urlrequest.Request(
        url,
        data=data,
        headers={"User-Agent": "maestro-public-cloud", **dict(headers or {})},
        method=method.upper(),
    )
    try:
        with urlrequest.urlopen(request, timeout=max(1.0, float(timeout))) as response:
            return HttpResponse(response.status, dict(response.headers), response.read())
    except urlerror.HTTPError as error:
        # A 4xx/5xx is an answer, not a transport failure: the body usually says
        # why (quota, queue full, model loading) and that reason is what the job
        # reports.  Raising here is what makes a public refusal look like a bug.
        return HttpResponse(error.code, dict(error.headers or {}), error.read())


def _hf_token() -> str:
    """The free Hugging Face account this install already has, if any.

    Anonymous public Spaces work; a signed-in account simply gets a larger free
    ZeroGPU allowance and a better place in the queue.  Maestro's launcher already
    points ``HF_TOKEN_PATH`` at Pinokio's shared token store, so this reads the
    same credential the downloader uses — and never stores, logs or returns it.
    """
    token = str(os.environ.get("HF_TOKEN") or "").strip()
    if token:
        return token
    path = str(os.environ.get("HF_TOKEN_PATH") or "").strip()
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _auth_headers() -> dict[str, str]:
    token = _hf_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------------------
# Where the connection is remembered  (app/settings/public_cloud/public_cloud.json)
# ---------------------------------------------------------------------------


def _state_path(root: str) -> str:
    return os.path.join(root, STATE_FILENAME)


def _empty_state() -> dict[str, Any]:
    return {"version": 1, "providers": {}, "searches": {}}


def load_state(root: str) -> dict[str, Any]:
    """The remembered connection.  A corrupt file is an empty one, never a crash."""
    path = _state_path(root)
    if not os.path.isfile(path):
        return _empty_state()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return _empty_state()
    state = _empty_state()
    providers = raw.get("providers") if isinstance(raw, Mapping) else None
    if isinstance(providers, Mapping):
        state["providers"] = {
            str(key): dict(value)
            for key, value in providers.items()
            if isinstance(value, Mapping)
        }
    searches = raw.get("searches") if isinstance(raw, Mapping) else None
    if isinstance(searches, Mapping):
        state["searches"] = {
            str(key): dict(value) for key, value in searches.items() if isinstance(value, Mapping)
        }
    return state


def _write_state(root: str, state: Mapping[str, Any]) -> None:
    """Write atomically.  Nothing secret lives here, so the file stays ordinary."""
    directory = os.path.dirname(_state_path(root)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".public_cloud.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _state_path(root))
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def engine_for(capability: str) -> str:
    """The queue engine string a public job carries: ``public:<capability>``."""
    return f"{PUBLIC_ENGINE_PREFIX}:{str(capability or '').strip()}"


def capability_from_engine(engine: str) -> str:
    prefix = f"{PUBLIC_ENGINE_PREFIX}:"
    text = str(engine or "")
    return text[len(prefix):] if text.startswith(prefix) else ""


# ---------------------------------------------------------------------------
# Discovery: ask the Hub which public Spaces are running now
# ---------------------------------------------------------------------------


def _space_base_url(space: Mapping[str, Any]) -> str:
    subdomain = str(space.get("subdomain") or "").strip().lower()
    if not subdomain:
        identifier = str(space.get("id") or "").strip()
        subdomain = re.sub(r"[^a-z0-9]+", "-", identifier.lower()).strip("-")
    if not _SUBDOMAIN_RE.match(subdomain):
        return ""
    return SPACE_HOST.format(subdomain=subdomain)


def _hardware(space: Mapping[str, Any]) -> str:
    runtime = space.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    hardware = runtime.get("hardware")
    hardware = hardware if isinstance(hardware, Mapping) else {}
    return str(hardware.get("current") or "").strip()


def _is_free_gpu(hardware: str) -> bool:
    """ZeroGPU is the free tier; anything else is either weaker or paid for.

    ``zero-*`` is Hugging Face's shared free GPU pool.  A Space on ``cpu-basic``
    cannot render a video in a reasonable time and a Space on a paid GPU may stop
    running without notice, so neither is connected to by default.
    """
    return hardware.lower().startswith("zero")


def discover(task_terms: Sequence[str], *, transport: Transport = _default_transport,
             timeout: float = DEFAULT_TIMEOUT, limit: int = DEFAULT_DISCOVERY_LIMIT) -> list[dict[str, Any]]:
    """Running, free-GPU, Gradio Spaces for one task — as the Hub reports them.

    Nothing is cached or assumed: the Hub is asked, and only Spaces whose
    ``runtime.stage`` is ``RUNNING`` on ZeroGPU hardware come back, most liked
    first, so the busiest public Space is tried before an obscure one.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for term in task_terms:
        for selector in ("filter", "search"):
            url = (
                f"{HUB_API}/spaces?{selector}={quote(str(term))}"
                "&sort=likes&direction=-1"
                f"&limit={max(1, int(limit))}"
                # The SDK and the runtime are asked for explicitly: a plain listing
                # leaves both out, and without them "is this a Gradio Space that is
                # running on a free GPU right now" could only be guessed.
                "&expand%5B%5D=runtime&expand%5B%5D=sdk&expand%5B%5D=likes"
            )
            try:
                response = transport("GET", url, timeout=timeout, headers=_auth_headers())
            except Exception:  # noqa: BLE001 - an unreachable Hub is "nothing found"
                continue
            if response.status == 200:
                payload = response.json()
            else:
                payload = None
            _collect(payload, found, seen, term)
    found.sort(key=lambda entry: (-int(entry["likes"]), str(entry["space_id"])))
    return found


def _collect(
    payload: Any,
    found: list[dict[str, Any]],
    seen: set[str],
    term: str,
) -> None:
    """Keep the Spaces the Hub reported as running on free hardware, once each."""
    if isinstance(payload, list):
        for space in payload:
            if not isinstance(space, Mapping):
                continue
            space_id = str(space.get("id") or "").strip()
            if not space_id or space_id in seen:
                continue
            sdk = str(space.get("sdk") or "").strip().lower()
            if sdk and sdk != "gradio":
                continue
            runtime = space.get("runtime") if isinstance(space.get("runtime"), Mapping) else {}
            if str(runtime.get("stage") or "").strip().upper() != "RUNNING":
                continue
            hardware = _hardware(space)
            if not _is_free_gpu(hardware):
                continue
            base_url = _space_base_url(space)
            if not base_url:
                continue
            seen.add(space_id)
            found.append({
                "space_id": space_id,
                "base_url": base_url,
                "hardware": hardware,
                "likes": int(space.get("likes") or 0),
                "subdomain": str(space.get("subdomain") or ""),
                "term": str(term),
            })
    found.sort(key=lambda entry: (-int(entry["likes"]), str(entry["space_id"])))
    return found


def _provider_id(space_id: str) -> str:
    return f"hf-space:{space_id}"


def _space_id_from_provider(provider_id: str) -> str:
    text = str(provider_id or "")
    return text[len("hf-space:"):] if text.startswith("hf-space:") else text


# ---------------------------------------------------------------------------
# Probing: read the Space's own API description
# ---------------------------------------------------------------------------


def _fetch_info(base_url: str, *, transport: Transport, timeout: float) -> dict[str, Any]:
    response = transport(
        "GET", f"{base_url}{GRADIO}/info", timeout=timeout, headers=_auth_headers()
    )
    if response.status != 200:
        raise PublicCloudError(
            f"the public Space answered {response.status} when asked for its API"
        )
    payload = response.json()
    if not isinstance(payload, Mapping) or not isinstance(payload.get("named_endpoints"), Mapping):
        raise PublicCloudError("the public Space did not describe its API")
    return payload


def _parameter_kind(parameter: Mapping[str, Any]) -> str:
    python_type = parameter.get("python_type")
    python_type = python_type if isinstance(python_type, Mapping) else {}
    return str(python_type.get("type") or "").strip()


def _parameter_values(parameter: Mapping[str, Any]) -> list[str]:
    """The allowed values a parameter declares, in any of the shapes Gradio uses.

    A closed choice reaches this side as ``Literal['text-to-video', ...]`` inside
    the type string, so the values are read from wherever Gradio put them — a list
    when it is one, and the literal syntax otherwise.  This is what lets a public
    endpoint that needs to be *told* which mode it is in be told correctly.
    """
    texts: list[str] = []
    for source in (parameter.get("python_type"), parameter.get("type")):
        if not isinstance(source, Mapping):
            continue
        for key in ("values", "allowed_values", "choices"):
            values = source.get(key)
            if isinstance(values, (list, tuple)) and values:
                return [str(value) for value in values]
        for key in ("type", "description"):
            text = source.get(key)
            if isinstance(text, str) and ("Literal" in text or "['" in text):
                texts.append(text)
    for text in texts:
        values = re.findall(r"['\"]([^'\"]+)['\"]", text)
        if values:
            return values
    return []


def _is_file_parameter(parameter: Mapping[str, Any]) -> bool:
    kind = _parameter_kind(parameter).lower()
    if "file" in kind or "filepath" in kind:
        return True
    if "dict(" in kind and "path" in kind and "url" in kind:
        return True
    declared = parameter.get("type")
    declared = declared if isinstance(declared, Mapping) else {}
    if "properties" in declared:
        keys = set((declared.get("properties") or {}).keys())
        return bool(keys & {"path", "url"})
    return False


def _normalize_api_name(api_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(api_name or "").strip().lower()).strip("_")


# What a public Space calls the endpoint that actually renders.  A name is only
# ever used to *order* candidates that all passed the same measured checks — a
# Space whose endpoint is called something else entirely still works, one rank
# lower.
_GENERATION_VERBS = (
    "generate",
    "generation",
    "infer",
    "inference",
    "predict",
    "predictions",
    "run",
    "generate_video",
    "generate_image",
    "txt2img",
    "img2img",
    "t2v",
    "i2v",
    "text_to_video",
    "text_to_image",
    "image_to_video",
)

# Gradio publishes its own event callbacks alongside the real endpoint: they are
# named ``_something`` or ``lambda``, and a couple of them take a string, so the
# prompt check alone would happily hand a render to ``_on_user_loras_change``.
_INTERNAL_API_RE = re.compile(r"^(?:_|lambda(_\d+)?$|param_\d+$)")
_SETTINGS_PARAM_RE = re.compile(r"^param_\d+$")


def _is_internal_endpoint(api_name: str, names: Sequence[str]) -> bool:
    text = str(api_name or "").strip().strip("/").lower()
    if not text or _INTERNAL_API_RE.match(text):
        return True
    if names and all(_SETTINGS_PARAM_RE.match(name) for name in names):
        # A wall of positional parameters is a settings export, not a render.
        return True
    return False


# The strings that mean "this endpoint hands back a file".  A Space tagged
# ``text-to-video`` also exposes helpers (prompt builders, prompt enhancers,
# upscalers) that take a prompt and return *text*; sending a render to one of them
# produces a job that looks successful and yields no video, so the declared return
# type is checked instead of trusted to the Space's name.
_MEDIA_RETURN_MARKERS = (
    "filepath",
    "file data",
    "gradio.filedata",
    # A returned file is described either as ``dict(path: …, url: …)`` or as an
    # object with those two properties; both say "a file" and a plain ``str`` or
    # ``float`` says nothing of the sort.
    "path:",
    "url:",
    '\"path\"',
    '\"url\"',
)

# The capabilities whose request brings a file the public endpoint has to receive.
_MEDIA_CAPABILITIES: dict[str, str] = {
    "image_to_video": "image",
    "inpainting": "image",
}


def _returns_media(spec: Mapping[str, Any]) -> bool:
    returns = spec.get("returns")
    if not isinstance(returns, (list, tuple)) or not returns:
        return False
    blob = json.dumps(list(returns), default=str).lower()
    return any(marker in blob for marker in _MEDIA_RETURN_MARKERS)


def choose_endpoint(info: Mapping[str, Any], capability: str) -> dict[str, Any]:
    """Which of the Space's own endpoints runs ``capability``.

    Preference is measured, in this order, and each step is a fact from the
    Space's report rather than a guess about it:

    1. an endpoint whose name *is* the capability (``/text_to_video``);
    2. an endpoint whose name is that capability written differently;
    3. an endpoint whose name is one of the words a renderer is normally called
      (``generate``, ``infer``);
    4. otherwise, the endpoint that takes an actual ``prompt``.

    Four things are not usable and are excluded before that ordering applies:
    Gradio's own event callbacks (they take a string too, and sending a render to
    ``_on_user_loras_change`` would look successful and produce nothing usable),
    endpoints that declare no media in their result, endpoints that *require* an
    input file a text request does not have (a start frame, say), and — for a
    capability whose request does bring media — endpoints with nowhere to put it.
    A Space with no usable endpoint is reported as such rather than being handed a
    call that cannot work.
    """
    endpoints = info.get("named_endpoints")
    if not isinstance(endpoints, Mapping):
        raise PublicCloudError("the public Space did not describe its API")
    wanted = _normalize_api_name(capability)
    media_kind = _MEDIA_CAPABILITIES.get(str(capability))
    best: list[tuple[int, int, str, Mapping[str, Any], list[dict[str, Any]]]] = []
    for api_name, spec in endpoints.items():
        if not isinstance(spec, Mapping) or spec.get("show_api") is False:
            continue
        parameters = [dict(p) for p in (spec.get("parameters") or []) if isinstance(p, Mapping)]
        names = [str(p.get("parameter_name") or "").strip().lower() for p in parameters]
        if _is_internal_endpoint(api_name, names):
            continue
        if not _returns_media(spec):
            continue
        named_prompt = any(name in _PROMPT_NAMES for name in names)
        loose_prompt = any(
            "prompt" in name or name in {"text", "input", "caption"} for name in names
        )
        if not (named_prompt or loose_prompt):
            continue
        takes = {
            kind: any(_param_kind_matches(p, kind) for p in parameters)
            for kind in ("image", "video")
        }
        if media_kind:
            if not takes[media_kind]:
                # Nothing here can receive the file this request brings, so the
                # render would silently lose it.
                continue
        elif any(
            not p.get("parameter_has_default") and _is_file_parameter(p)
            for p in parameters
        ):
            # This endpoint demands a file the request does not have.
            continue
        normalized = _normalize_api_name(api_name)
        if normalized == wanted:
            rank = 0
        elif wanted and wanted in normalized:
            rank = 1
        elif normalized in _GENERATION_VERBS or any(
            verb in normalized for verb in _GENERATION_VERBS
        ):
            rank = 2
        else:
            rank = 3
        # A required parameter nothing can fill makes the endpoint unusable, so the
        # plainest endpoint that answers the question is preferred.
        unfillable = sum(1 for p in parameters if not p.get("parameter_has_default"))
        best.append((rank, unfillable, str(api_name), spec, parameters))
    if not best:
        raise PublicCloudError(
            "the public Space offers no endpoint that takes a prompt and returns "
            "media, so it cannot run this request"
        )
    best.sort(key=lambda item: (item[0], item[1], item[2]))
    rank, _, api_name, spec, parameters = best[0]
    takes = {
        kind: any(_param_kind_matches(p, kind) for p in parameters)
        for kind in ("image", "video")
    }
    return {
        "api_name": api_name,
        "rank": rank,
        "exact": rank == 0,
        "parameters": parameters,
        "accepts_media": any(takes.values()),
        "media_kinds": sorted(kind for kind, present in takes.items() if present),
        "returns": [dict(r) for r in (spec.get("returns") or []) if isinstance(r, Mapping)],
    }


def probe_provider_url(
    base_url: str,
    space_id: str,
    *,
    capability: str,
    transport: Transport = _default_transport,
    timeout: float = DEFAULT_TIMEOUT,
    metadata: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Ask one public Space what it can do for ``capability``.  Returns a record.

    A Space that cannot answer, or that answers without an endpoint for the
    capability, comes back with ``reachable`` false and the reason — never with an
    optimistic default.  This is the function that decides whether the internet is
    a usable engine right now.
    """
    start = time.time()
    record: dict[str, Any] = {
        "provider_id": _provider_id(space_id),
        "space_id": space_id,
        "base_url": base_url.rstrip("/"),
        "label": f"{space_id} (public Hugging Face Space)",
        "hardware": str((metadata or {}).get("hardware") or ""),
        "likes": int((metadata or {}).get("likes") or 0),
        "capabilities": [],
        "endpoints": {},
        "accepts_media": False,
        "last_probe": {},
    }
    try:
        info = _fetch_info(record["base_url"], transport=transport, timeout=timeout)
        endpoint = choose_endpoint(info, capability)
    except Exception as error:  # noqa: BLE001 - not answering is a normal outcome
        record["last_probe"] = {
            "at": time.time(),
            "reachable": False,
            "latency_ms": int((time.time() - start) * 1000),
            "detail": str(error)[:400],
        }
        return record
    record["capabilities"] = [str(capability)]
    record["endpoints"] = {
        str(capability): {
            "api_name": endpoint["api_name"],
            "parameters": endpoint["parameters"],
            "returns": endpoint["returns"],
            "exact": bool(endpoint["exact"]),
            "media_kinds": list(endpoint.get("media_kinds") or ()),
        }
    }
    record["accepts_media"] = bool(endpoint["accepts_media"])
    record["last_probe"] = {
        "at": time.time(),
        "reachable": True,
        "latency_ms": int((time.time() - start) * 1000),
        "detail": f"endpoint {endpoint['api_name']}",
        "api_name": endpoint["api_name"],
    }
    return record


def _probe_age(record: Mapping[str, Any], stamp: float) -> float:
    probe = record.get("last_probe")
    probe = probe if isinstance(probe, Mapping) else {}
    try:
        seen = float(probe.get("at") or 0.0)
    except (TypeError, ValueError):
        return float("inf")
    return stamp - seen if seen > 0 else float("inf")


def _is_online(record: Mapping[str, Any], stamp: float) -> bool:
    probe = record.get("last_probe")
    probe = probe if isinstance(probe, Mapping) else {}
    try:
        seen = float(probe.get("at") or 0.0)
    except (TypeError, ValueError):
        return False
    if not probe.get("reachable") or seen <= 0:
        return False
    return (stamp - seen) <= PUBLIC_ONLINE_TTL


# ---------------------------------------------------------------------------
# Connecting: discovery + probing, remembered
# ---------------------------------------------------------------------------


def connect(
    root: str,
    *,
    capabilities: Optional[Sequence[str]] = None,
    now: Optional[float] = None,
    transport: Transport = _default_transport,
    timeout: float = DEFAULT_TIMEOUT,
    candidates_per_capability: int = 10,
    refresh_after: float = PUBLIC_REFRESH_AFTER,
) -> dict[str, Any]:
    """Connect to a public engine for each capability, without being asked to.

    This is the whole "it just connects" behaviour, and it is intentionally dull:
    search the Hub, try the most liked running free-GPU Space first, keep the first
    one that really answers with an endpoint for the capability, and remember it.
    Everything that fails is reported with its reason and changes nothing else —
    a public service being down must never look like a local error.

    Reconnecting is cheap and idempotent: a provider whose last probe is still
    fresh is not re-probed, so this may be called at startup, on a timer, and again
    just before a request is refused.
    """
    wanted = [
        str(name)
        for name in (capabilities if capabilities is not None else SERVED_CAPABILITIES)
        if str(name) in SEARCH_TERMS
    ]
    stamp = float(now) if now is not None else time.time()
    state = load_state(root)
    providers: dict[str, Any] = dict(state.get("providers") or {})
    searches: dict[str, Any] = dict(state.get("searches") or {})

    connected: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for capability in wanted:
        if any(
            capability in (record.get("capabilities") or [])
            and _is_online(record, stamp)
            and _probe_age(record, stamp) <= float(refresh_after)
            for record in providers.values()
            if isinstance(record, Mapping)
        ):
            attempts.append({"capability": capability, "outcome": "already_connected"})
            continue
        terms = SEARCH_TERMS[str(capability)]
        try:
            found = discover(terms, transport=transport, timeout=timeout)
        except Exception as error:  # noqa: BLE001 - the Hub being away is not fatal
            attempts.append({
                "capability": capability,
                "outcome": "discovery_failed",
                "reason": f"{type(error).__name__}: {error}"[:300],
            })
            continue
        searches[str(capability)] = {
            "at": stamp,
            "terms": list(terms),
            "found": [entry["space_id"] for entry in found],
        }
        if not found:
            attempts.append({
                "capability": capability,
                "outcome": "nothing_running",
                "reason": (
                    "No public Space is running on free hardware for this capability "
                    "right now."
                ),
            })
            continue
        outcome = "nothing_answered"
        reachable: list[dict[str, Any]] = []
        best_index = -1
        for index, candidate in enumerate(found[: max(1, int(candidates_per_capability))]):
            # An endpoint *named* after the capability is the best a public Space can
            # offer, but the search does not stop dead there: a couple of Spaces after
            # it are probed too, because the engine that really renders has to have
            # somewhere to fall back to when a public service breaks between two
            # renders.  (It does; that is the point of a fallback.)
            if best_index >= 0 and index > best_index + PUBLIC_FALLBACK_PROBES:
                break
            space_id = str(candidate["space_id"])
            provider_id = _provider_id(space_id)
            record = probe_provider_url(
                candidate["base_url"],
                space_id,
                capability=capability,
                transport=transport,
                timeout=timeout,
                metadata=candidate,
            )
            existing = providers.get(provider_id)
            if isinstance(existing, Mapping):
                # A Space can serve more than one capability; keep what it already
                # declared instead of overwriting it with the last probe.
                merged = dict(existing)
                merged.update(record)
                merged["capabilities"] = sorted(
                    {
                        str(name)
                        for name in list(existing.get("capabilities") or [])
                        + list(record.get("capabilities") or [])
                    }
                )
                endpoints = dict(existing.get("endpoints") or {})
                endpoints.update(record.get("endpoints") or {})
                merged["endpoints"] = endpoints
                record = merged
            providers[provider_id] = record
            if not record.get("last_probe", {}).get("reachable"):
                attempts.append({
                    "capability": capability,
                    "space_id": space_id,
                    "outcome": "not_usable",
                    "reason": str(record.get("last_probe", {}).get("detail") or "")[:300],
                })
                continue
            reachable.append(record)
            if not isinstance(existing, Mapping):
                print(
                    f"[Public compute] {space_id} answered for {capability} "
                    f"({record.get('hardware') or 'free GPU'}); "
                    f"endpoint {record['last_probe'].get('api_name')}"
                )
            if _rank(record, capability)[0] == 0 and best_index < 0:
                best_index = index
        if reachable:
            best = sorted(reachable, key=lambda item: _rank(item, capability))[0]
            connected.append(describe_provider(best, now=stamp))
            outcome = "connected"
            chosen = str(best.get("space_id"))
            print(
                f"[Public compute] Connected to {chosen} for {capability} "
                f"({best.get('hardware') or 'free GPU'}); "
                f"endpoint {(best.get('endpoints') or {}).get(capability, {}).get('api_name')}"
            )
        else:
            attempts.append({
                "capability": capability,
                "outcome": outcome,
                "reason": "The public Spaces that answered could not run this capability.",
            })

    state["providers"] = providers
    state["searches"] = searches
    try:
        _write_state(root, state)
    except OSError as error:  # pragma: no cover - a full disk is not a render failure
        print(f"[Public compute] Could not save the connection: {error}")
    return {
        "connected": connected,
        "attempts": attempts,
        "connected_count": len(connected),
        "providers": [describe_provider(record, now=stamp) for record in providers.values()],
    }


def describe_provider(record: Mapping[str, Any], *, now: Optional[float] = None) -> dict[str, Any]:
    """One provider as the API and the interface may see it."""
    stamp = float(now) if now is not None else time.time()
    probe = record.get("last_probe")
    probe = dict(probe) if isinstance(probe, Mapping) else {}
    capabilities = [str(name) for name in (record.get("capabilities") or []) if str(name)]
    return {
        "provider_id": str(record.get("provider_id") or ""),
        "space_id": str(record.get("space_id") or _space_id_from_provider(str(record.get("provider_id") or ""))),
        "label": str(record.get("label") or record.get("space_id") or ""),
        "base_url": str(record.get("base_url") or ""),
        "kind": "hf_space",
        "hardware": str(record.get("hardware") or ""),
        "free": True,
        "requires_key": False,
        "capabilities": capabilities,
        "endpoints": {
            str(name): dict(spec)
            for name, spec in (record.get("endpoints") or {}).items()
            if isinstance(spec, Mapping)
        },
        "accepts_media": bool(record.get("accepts_media")),
        "online": _is_online(record, stamp),
        "online_ttl_seconds": PUBLIC_ONLINE_TTL,
        "last_probe": probe or None,
        "engine_ids": [engine_for(name) for name in capabilities],
        "likes": int(record.get("likes") or 0),
        "notes": (
            "A public Hugging Face Space on free shared GPU hardware, reached over the "
            "internet. It runs its own model, so a render here approximates the Maestro "
            "recipe instead of replaying it."
        ),
    }


def list_providers(root: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    state = load_state(root)
    described = [
        describe_provider(record, now=now)
        for record in state["providers"].values()
        if isinstance(record, Mapping)
    ]
    described.sort(key=lambda entry: (not entry["online"], entry["provider_id"]))
    return described


def searches(root: str) -> dict[str, Any]:
    return dict(load_state(root).get("searches") or {})


def probe_provider(
    root: str,
    provider_id: str,
    *,
    now: Optional[float] = None,
    transport: Transport = _default_transport,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Ask one remembered provider again and store the answer."""
    stamp = float(now) if now is not None else time.time()
    state = load_state(root)
    record = state["providers"].get(provider_id)
    if not isinstance(record, Mapping):
        raise FileNotFoundError(f"Public provider {provider_id!r} is not connected")
    capability = str((record.get("capabilities") or [""])[0] or "")
    refreshed = probe_provider_url(
        str(record.get("base_url") or ""),
        str(record.get("space_id") or _space_id_from_provider(provider_id)),
        capability=capability,
        transport=transport,
        timeout=timeout,
        metadata={"hardware": record.get("hardware"), "likes": record.get("likes")},
    )
    merged = dict(record)
    merged.update(refreshed)
    if refreshed.get("capabilities"):
        merged["capabilities"] = sorted(
            {
                str(name)
                for name in list(record.get("capabilities") or [])
                + list(refreshed.get("capabilities") or [])
            }
        )
    state["providers"][provider_id] = merged
    _write_state(root, state)
    return {"provider": describe_provider(merged, now=stamp), "probe": merged["last_probe"]}


def add_space(
    root: str,
    space: str,
    *,
    capability: str,
    now: Optional[float] = None,
    transport: Transport = _default_transport,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Add a public Space by hand — the same probe, a different way to name it.

    Automatic discovery covers the common case; this is how someone points Maestro
    at one particular public Space they trust.  It is still probed before it is
    used, so adding a Space that cannot run the capability reports that instead of
    queueing work for it.
    """
    text = str(space or "").strip()
    if not text:
        raise PublicCloudError("A Space id or URL is required")
    if text.startswith("http://") or text.startswith("https://"):
        base_url = text.rstrip("/")
        host = urlsplit(base_url).hostname or ""
        space_id = host.split(".")[0].replace("-", "/", 0) or host
        subdomain = host.split(".")[0]
        space_id = space_id if "/" in space_id else subdomain
        space_id = _space_id_from_provider(space_id)
    else:
        space_id = text
        subdomain = re.sub(r"[^a-z0-9]+", "-", space_id.lower()).strip("-")
        base_url = SPACE_HOST.format(subdomain=subdomain)
    if str(capability) not in SEARCH_TERMS:
        raise PublicCloudError(
            f"Unknown public capability {capability!r}. Known: "
            + ", ".join(SERVED_CAPABILITIES)
        )
    record = probe_provider_url(
        base_url,
        space_id,
        capability=str(capability),
        transport=transport,
        timeout=timeout,
        metadata={"hardware": "added by hand", "likes": 0},
    )
    state = load_state(root)
    state["providers"][str(record["provider_id"])] = record
    _write_state(root, state)
    stamp = float(now) if now is not None else time.time()
    return {"provider": describe_provider(record, now=stamp), "probe": record["last_probe"]}


def forget_provider(root: str, provider_id: str) -> dict[str, Any]:
    state = load_state(root)
    if state["providers"].pop(str(provider_id), None) is None:
        raise FileNotFoundError(f"Public provider {provider_id!r} is not connected")
    _write_state(root, state)
    return {
        "removed": str(provider_id),
        "note": (
            "Forgetting a public Space changes nothing outside this computer: the next "
            "automatic connection may pick it again, because discovery follows what is "
            "running."
        ),
    }


def connected_sources(root: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Connected public providers, shaped like the other generation sources.

    Only providers whose probe is fresh appear, so a Space that went down is not
    advertised as a place where a render could run.
    """
    sources: list[dict[str, Any]] = []
    for provider in list_providers(root, now=now):
        if not provider["online"] or not provider["capabilities"]:
            continue
        sources.append({
            "kind": "public_cloud",
            "provider_id": provider["provider_id"],
            "label": provider["label"],
            "base_url": provider["base_url"],
            "hardware": provider["hardware"],
            "capabilities": list(provider["capabilities"]),
            "accepts_media": bool(provider["accepts_media"]),
            "online": True,
            "free": True,
            "requires_key": False,
            "fidelity": "public_approximation",
            "engine_ids": list(provider["engine_ids"]),
            "last_probe_at": (provider.get("last_probe") or {}).get("at"),
            "endpoint_names": {
                name: str((spec or {}).get("api_name") or "")
                for name, spec in provider["endpoints"].items()
            },
            "exact_endpoints": [
                name for name, spec in provider["endpoints"].items() if (spec or {}).get("exact")
            ],
            "likes": int(provider.get("likes") or 0),
            "notes": provider["notes"],
        })
    # Best first, by the same ranking the run-time choice uses, so the engine the
    # route names in its reason is the one that really renders the job.
    sources.sort(key=lambda source: (
        0 if source["exact_endpoints"] else 1,
        -int(source["likes"]),
        str(source["provider_id"]),
    ))
    return sources


def provider_entries(root: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Providers shaped for :func:`services.engine_providers.build_registry`."""
    entries: list[dict[str, Any]] = []
    for provider in list_providers(root, now=now):
        entries.append({
            "provider_id": provider["provider_id"],
            "label": f"{provider['label']} — free public GPU",
            "capabilities": list(provider["capabilities"]),
            "models": [],
            "online": bool(provider["online"]),
            "hardware": provider["hardware"],
            "address": provider["base_url"],
            "free": True,
            "requires_key": False,
            "notes": provider["notes"],
        })
    return entries


def _rank(record: Mapping[str, Any], capability: str) -> tuple[int, int, str]:
    """How good a public provider is for one capability — lower is better.

    First an endpoint *named* after the capability (a Space that offers
    ``/text_to_video`` has been built for exactly this, while one that offers
    ``/generate`` may be doing something else with a prompt), then how well liked
    the Space is, then its id so the choice never wobbles between two runs.
    """
    endpoint = (record.get("endpoints") or {}).get(capability)
    endpoint = endpoint if isinstance(endpoint, Mapping) else {}
    return (
        0 if endpoint.get("exact") else 1,
        -int(record.get("likes") or 0),
        str(record.get("provider_id") or ""),
    )


def _usable(record: Mapping[str, Any], capability: str, stamp: float) -> bool:
    return (
        _is_online(record, stamp)
        and capability in (record.get("capabilities") or [])
        and isinstance(record.get("endpoints"), Mapping)
        and capability in (record.get("endpoints") or {})
    )


def pick_provider(
    root: str,
    capability: str,
    *,
    now: Optional[float] = None,
    require_media: bool = False,
) -> dict[str, Any]:
    """The connected provider that will run ``capability``, or nothing.

    Deterministic: most liked first, then provider id, so the same request always
    resolves to the same public engine while it stays up.
    """
    stamp = float(now) if now is not None else time.time()
    state = load_state(root)
    candidates = [
        record
        for record in state["providers"].values()
        if isinstance(record, Mapping) and _usable(record, capability, stamp)
    ]
    if require_media:
        candidates = [
            record
            for record in candidates
            if require_media in ((record.get("endpoints") or {}).get(capability, {}).get("media_kinds") or [])
        ]
    candidates.sort(key=lambda record: _rank(record, capability))
    return dict(candidates[0]) if candidates else {}


# ---------------------------------------------------------------------------
# Running one request on a public engine
# ---------------------------------------------------------------------------


def _first_value(params: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in params and params[name] not in (None, "", [], {}):
            return params[name]
    return None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _numeric_range(parameter: Mapping[str, Any]) -> Optional[tuple[float, float]]:
    for source in (parameter.get("python_type"), parameter.get("type")):
        if not isinstance(source, Mapping):
            continue
        text = f"{source.get('description') or ''} {source.get('type') or ''}"
        match = _NUMERIC_RANGE_RE.search(text)
        if match:
            low, high = float(match.group(1)), float(match.group(2))
            if low <= high:
                return low, high
    return None


def _resolution_parts(params: Mapping[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """Width and height the request asked for, from any of the shapes Maestro uses."""
    text = _as_text(params.get("resolution"))
    if text and "x" in text:
        left, _, right = text.partition("x")
        width = _as_int(left.strip())
        height = _as_int(right.strip())
        if width and height:
            return width, height
    return _as_int(params.get("width")), _as_int(params.get("height"))


def _duration_seconds(params: Mapping[str, Any]) -> Optional[int]:
    """How many seconds this request asked for, if it really said so."""
    explicit = _as_int(params.get("duration_seconds") or params.get("seconds"))
    if explicit is not None:
        return explicit
    frames = _as_int(_first_value(params, _FRAME_KEYS))
    raw_fps = _first_value(params, _FPS_KEYS)
    try:
        fps = float(raw_fps) if raw_fps is not None else None
    except (TypeError, ValueError):
        fps = None
    if frames and fps and fps > 0:
        return max(1, int(round(frames / fps)))
    return None


def _media_value(params: Mapping[str, Any], kind: str) -> str:
    for key in _MEDIA_KEYS[kind]:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)) and value:
            first = value[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
    return ""


def _param_kind_matches(parameter: Mapping[str, Any], kind: str) -> bool:
    if not _is_file_parameter(parameter):
        return False
    name = str(parameter.get("parameter_name") or "").lower()
    label = str(parameter.get("label") or "").lower()
    haystack = f"{name} {label}"
    if kind == "image":
        return "image" in haystack or "img" in haystack or "frame" in haystack
    return "video" in haystack


def build_data(
    endpoint: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    media: Optional[Mapping[str, Any]] = None,
    notes: Optional[list[str]] = None,
) -> list[Any]:
    """The positional ``data`` array a Gradio endpoint expects, from a Studio request.

    Built from the Space's own declaration: every parameter is filled from the
    request when the names agree (a prompt is a prompt, a seed is a seed), from a
    declared literal when the parameter is a choice that includes this capability,
    and from the declared default otherwise.  A required parameter that nothing can
    fill stops the render with its name, because sending a guess would produce a
    video that has nothing to do with what was asked for.
    """
    prompt = _as_text(params.get("prompt"))
    negative = _as_text(params.get("negative_prompt"))
    seed = _as_int(params.get("seed"))
    steps = _as_int(params.get("num_inference_steps"))
    width, height = _resolution_parts(params)
    duration = _duration_seconds(params)
    capability = str(params.get("capability") or "")
    supplied = dict(media or {})
    prefix = capability.replace("_", "-")

    data: list[Any] = []
    for parameter in endpoint.get("parameters") or []:
        if not isinstance(parameter, Mapping):
            continue
        name = str(parameter.get("parameter_name") or "").strip()
        lowered = name.lower()
        has_default = bool(parameter.get("parameter_has_default"))
        default = parameter.get("parameter_default")
        value: Any = None
        filled = False

        values = _parameter_values(parameter)
        if values and prefix and any(str(item).strip() == prefix for item in values):
            value = prefix
            filled = True
        elif _param_kind_matches(parameter, "image") and supplied.get("image"):
            value = supplied["image"]
            filled = True
        elif _param_kind_matches(parameter, "video") and supplied.get("video"):
            value = supplied["video"]
            filled = True
        elif lowered in _NEGATIVE_NAMES or "negative" in lowered:
            # Before the prompt branch: ``negative_prompt`` contains "prompt", and a
            # negative prompt sent as the prompt would render its opposite.
            if negative:
                value = negative
                filled = True
        elif lowered in _PROMPT_NAMES or "prompt" in lowered:
            if prompt:
                value = prompt
                filled = True
        elif lowered in _SEED_NAMES:
            if seed is not None and seed >= 0:
                value = seed
                filled = True
        elif lowered in _STEPS_NAMES:
            if steps is not None:
                value = steps
                filled = True
        elif lowered in _WIDTH_NAMES:
            if width is not None:
                value = width
                filled = True
        elif lowered in _HEIGHT_NAMES:
            if height is not None:
                value = height
                filled = True
        elif lowered in _DURATION_NAMES:
            if duration is not None:
                value = duration
                filled = True
        elif lowered in _RANDOMIZE_NAMES:
            if seed is not None and seed >= 0:
                # A seed was asked for, so it must not be randomized away.
                value = False
                filled = True

        if filled and isinstance(value, (int, float)) and not isinstance(value, bool):
            bounds = _numeric_range(parameter)
            if bounds is not None and not (bounds[0] <= float(value) <= bounds[1]):
                clamped = int(bounds[0]) if float(value) < bounds[0] else int(bounds[1])
                if notes is not None:
                    notes.append(
                        f"{name}: {value} is outside what the public engine supports "
                        f"({bounds[0]:g}-{bounds[1]:g}), so {clamped} was used"
                    )
                value = clamped

        if not filled:
            if has_default:
                value = default
                filled = True
            else:
                raise PublicCloudError(
                    f"The public Space asks for {name!r}, which this request has nothing "
                    "to fill, so it was not sent — guessing would render something else."
                )
        data.append(value)
    return data


def _upload_media(
    base_url: str,
    path: str,
    *,
    transport: Transport,
    timeout: float,
) -> dict[str, Any]:
    """Send one local file to the public Space's upload endpoint.

    Gradio hands back a server-side path; the FileData object below is what its own
    client sends for a file input.  This is how a start frame from this computer
    reaches a public GPU at all — without it, a request that carries media would be
    sent without its media, which is worse than refusing it.
    """
    if not os.path.isfile(path):
        raise PublicCloudError(f"The file this request needs is not on disk: {path}")
    size = os.path.getsize(path)
    if size > MAX_MEDIA_BYTES:
        raise PublicCloudError(
            f"{os.path.basename(path)} is {size} bytes, past the public upload ceiling"
        )
    with open(path, "rb") as handle:
        payload = handle.read()
    boundary = f"----maestro{uuid.uuid4().hex}"
    name = os.path.basename(path)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    response = transport(
        "POST",
        f"{base_url}{GRADIO}/upload",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **_auth_headers(),
        },
        timeout=max(timeout, 60.0),
    )
    if response.status != 200:
        raise PublicCloudError(
            f"the public Space refused the upload ({response.status}): "
            f"{str(response.json().get('detail') or '')[:200]}"
        )
    payload_json = response.json()
    if isinstance(payload_json, list) and payload_json:
        server_path = str(payload_json[0])
    elif isinstance(payload_json, Mapping) and payload_json.get("path"):
        server_path = str(payload_json["path"])
    else:
        raise PublicCloudError("the public Space accepted the upload without naming a file")
    return {"path": server_path, "orig_name": name, "meta": {"_type": "gradio.FileData"}}


def _parse_event(block: str) -> tuple[str, str]:
    name = "message"
    data: list[str] = []
    for line in block.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    return name, "\n".join(data)


def _note_from_event(data: str, on_note: Callable[[str], None]) -> None:
    """Forward the queue position a public service reports, and nothing else.

    This is real information the Space measured (how many renders are ahead), so it
    becomes a note; a tick that learned nothing is not an update.
    """
    try:
        payload = json.loads(data) if data else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, Mapping):
        return
    rank = payload.get("rank")
    if isinstance(rank, int) and rank > 0:
        on_note(f"Waiting in the public queue (position {rank})")
    elif payload.get("msg"):
        on_note("The public engine is rendering")


def _open_event_stream(
    url: str,
    *,
    timeout: float,
    cancelled: Optional[Callable[[], bool]] = None,
    on_note: Optional[Callable[[str], None]] = None,
) -> str:
    """Read a Space's event stream incrementally, watching for cancellation.

    Reading in chunks is what makes a cancellation and a queue update visible while
    the render is still going on, rather than only after it finished.
    """
    request = urlrequest.Request(
        url, headers={"User-Agent": "maestro-public-cloud", **_auth_headers()}
    )
    buffer = ""
    collected = ""
    with urlrequest.urlopen(request, timeout=max(30.0, timeout)) as response:
        while True:
            if cancelled is not None and cancelled():
                raise JobCancelled("Cancelled while a public engine was rendering")
            chunk = response.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                block, _, buffer = buffer.partition("\n\n")
                collected += block + "\n\n"
                if on_note is not None:
                    name, data = _parse_event(block)
                    if name == "generating":
                        _note_from_event(data, on_note)
    if buffer.strip():
        collected += buffer
    return collected


def _events_of(raw: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        name, data = _parse_event(block)
        if name == "heartbeat":
            continue
        events.append((name, data))
    return events


def _stream_result(
    base_url: str,
    api_name: str,
    event_id: str,
    *,
    transport_timeout: float,
    transport: Transport = _default_transport,
    cancelled: Optional[Callable[[], bool]] = None,
    on_note: Optional[Callable[[str], None]] = None,
) -> list[Any]:
    """Follow the Space's own event stream until it completes or fails.

    Only the ``complete`` event carries a result; an ``error`` event carries the
    public service's own message, which is what the job reports.  The default
    transport streams the response so a cancellation is seen mid-render; an injected
    transport (tests, and any transport that cannot stream) is asked for the whole
    body instead.
    """
    url = f"{base_url}{GRADIO}/call/{quote(str(api_name).lstrip('/'), safe='')}/{event_id}"
    if transport is _default_transport:
        try:
            raw = _open_event_stream(
                url, timeout=transport_timeout, cancelled=cancelled, on_note=on_note
            )
        except JobCancelled:
            raise
        except urlerror.HTTPError as error:
            raise PublicUnavailable(
                f"the public Space refused to report on the render ({error.code})"
            ) from None
        except urlerror.URLError as error:
            raise PublicUnavailable(
                f"the public Space stopped answering while rendering: {error.reason}"
            ) from None
    else:
        if cancelled is not None and cancelled():
            raise JobCancelled("Cancelled while a public engine was rendering")
        response = transport("GET", url, timeout=max(30.0, transport_timeout))
        if response.status != 200:
            raise PublicUnavailable(
                f"the public Space refused to report on the render ({response.status})"
            )
        raw = response.body.decode("utf-8", errors="replace")
        if on_note is not None:
            for name, data in _events_of(raw):
                if name == "generating":
                    _note_from_event(data, on_note)

    events = _events_of(raw)

    for name, data in events:
        if name == "error":
            detail = data
            try:
                parsed = json.loads(data)
            except ValueError:
                parsed = None
            if isinstance(parsed, Mapping):
                detail = str(parsed.get("error") or parsed.get("detail") or data)
            elif isinstance(parsed, str):
                detail = parsed
            # A public service's own message, forwarded as it was written: it is
            # the only thing that can say whether the queue was full, the quota was
            # spent or the request was wrong.  A capacity problem keeps the job
            # waiting; anything else is a failure of the render.
            message = str(detail or "").strip()[:300]
            if not message or message.lower() in {"null", "none"}:
                # Gradio sends an empty error when the hosted function dies before it
                # can explain itself — a shared free GPU being reclaimed looks exactly
                # like this.  No message is no evidence that the request was wrong.
                raise PublicUnavailable(
                    "the public engine failed without saying why; the free GPU it runs "
                    "on was most likely reclaimed"
                )
            if _is_transient(message):
                raise PublicUnavailable(f"the public engine is out of capacity: {message}")
            raise PublicCloudError(f"the public engine reported an error: {message}")
    for name, data in reversed(events):
        if name == "complete":
            try:
                payload = json.loads(data)
            except ValueError:
                raise PublicCloudError("the public engine completed with an unreadable result") from None
            return payload if isinstance(payload, list) else [payload]
    raise PublicUnavailable("the public engine ended without sending a result")


def _file_url(base_url: str, value: Any) -> str:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, Mapping):
        url = value.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
        path = value.get("path")
        if isinstance(path, str) and path:
            return f"{base_url}{GRADIO}/file={path}"
    if isinstance(value, str) and value:
        return f"{base_url}{GRADIO}/file={value}"
    return ""


def _referenced_files(payload: Sequence[Any]) -> list[str]:
    found: list[str] = []
    for item in payload:
        if isinstance(item, Mapping):
            for key in ("video", "image", "audio", "path", "url"):
                if item.get(key):
                    found.append(str(item[key]))
        elif isinstance(item, str) and item:
            found.append(item)
    return found


def _download(url: str, *, transport: Transport, timeout: float) -> tuple[bytes, str]:
    response = transport("GET", url, timeout=max(timeout, 120.0), headers=_auth_headers())
    if response.status != 200:
        raise PublicCloudError(f"the public result could not be downloaded ({response.status})")
    body = response.body
    if not body:
        raise PublicCloudError("the public engine produced an empty file")
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise PublicCloudError(
            f"the public result is {len(body)} bytes, past the download ceiling"
        )
    return body, response.headers.get("content-type", "")


_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image": (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM"),
    "video": (b"\x1aE\xdf\xa3",),
    "audio": (b"ID3", b"OggS", b"fLaC", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
}


def _classify(body: bytes, content_type: str, kind: str) -> str:
    """What the bytes really are: an extension, or an error.

    A public endpoint that answered with JSON, an HTML page or a redirect body is
    the common failure mode of this tier, and recording one as a finished render is
    exactly the lie this function exists to prevent.
    """
    head = body[:16]
    declared = str(content_type or "").split(";")[0].strip().lower()
    stripped = body.lstrip()[:1]
    if stripped in (b"{", b"<", b"["):
        raise PublicCloudError(
            "the public engine answered with text instead of media "
            f"({body[:120].decode('utf-8', errors='replace')!r})"
        )
    if declared.startswith(f"{kind}/"):
        return declared
    if kind == "image":
        if declared in {"application/octet-stream", ""} or declared.startswith("image"):
            if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or head.startswith(b"GIF8") or head.startswith(b"BM"):
                return "image/png" if head.startswith(b"\x89PNG") else "image/jpeg"
            if head.startswith(b"RIFF") and body[8:12] in (b"WEBP", b"webp"):
                return "image/webp"
        raise PublicCloudError("the public engine's result is not an image")
    if kind == "video":
        if head[4:8] == b"ftyp":
            return "video/mp4"
        if head.startswith(b"\x1aE\xdf\xa3"):
            return "video/webm"
        raise PublicCloudError("the public engine's result is not a video")
    if any(head.startswith(signature) for signature in _SIGNATURES["audio"]) or (
        head.startswith(b"RIFF") and body[8:12] == b"WAVE"
    ):
        return declared if declared.startswith("audio/") else "audio/mpeg"
    if declared.startswith("audio"):
        return declared
    raise PublicCloudError("the public engine's result is not audio")


def _written_name(capability: str, media_type: str, source_url: str) -> str:
    extension = mimetypes.guess_extension(media_type.split(";")[0].strip()) or ""
    if not extension:
        suffix = os.path.splitext(urlsplit(source_url).path)[1]
        extension = suffix if suffix and len(suffix) <= 5 else ""
    extension = extension.lstrip(".")
    if not extension:
        extension = {"image": "png", "video": "mp4", "audio": "mp3"}[EXPECTED_MEDIA[capability]]
    if extension == "jpe":
        extension = "jpg"
    return f"public_{capability}_{uuid.uuid4().hex[:10]}.{extension}"


def run_job(
    root: str,
    job: Mapping[str, Any],
    context: Any,
    *,
    out_dir: str,
    now: Optional[float] = None,
    transport: Transport = _default_transport,
    timeout: float = DEFAULT_RUN_TIMEOUT,
    provider_id: str = "",
) -> list[str]:
    """Render one queued job on a public engine and return the file it produced.

    The job is the same durable queue job the local and peer tiers use: this
    function is registered as its handler, so retries, deferrals and cancellation
    keep behaving exactly as they do everywhere else.  What is different is the
    engine — someone else's GPU, a queue this computer does not own, and a model
    that is not Maestro's — so the result is validated before it is announced and
    a public service that is simply not answering *defers* the job.

    ``out_dir`` is the workspace the job will be published into; the written file is
    named after the capability that asked for it, never after anything the public
    service said.
    """
    params = job.get("params") if isinstance(job.get("params"), Mapping) else {}
    params = dict(params)
    capability = str(params.get("capability") or capability_from_engine(str(job.get("engine") or "")))
    if capability not in SEARCH_TERMS:
        raise PublicCloudError(
            f"Public compute cannot serve {capability!r}. It serves: "
            + ", ".join(SERVED_CAPABILITIES)
        )
    stamp = float(now) if now is not None else time.time()
    wanted = _media_value(params, "image") or _media_value(params, "video")
    media_kind = EXPECTED_MEDIA.get(capability) or ""
    require_media = "image" if (wanted and media_kind == "image") else ""
    candidates = _candidates_for(root, capability, provider_id=provider_id, now=stamp, require_media=require_media)
    if not candidates:
        context.defer(
            f"No public engine is connected for {capability} right now. Maestro keeps "
            "looking for a running free public Space; this render waits instead of failing."
        )
        return []

    failures: list[str] = []
    unavailable = 0
    for index, record in enumerate(candidates, start=1):
        space_id = str(record.get("space_id") or "")
        if index > 1:
            context.report(note=f"Trying {space_id} instead")
        try:
            return _render_on(record, params, capability, context, out_dir=out_dir,
                              transport=transport, timeout=timeout)
        except JobCancelled:
            raise
        except PublicUnavailable as error:
            unavailable += 1
            failures.append(f"{space_id}: {error}")
        except PublicCloudError as error:
            failures.append(f"{space_id}: {error}")
        except OSError as error:
            failures.append(f"{space_id}: {type(error).__name__}: {error}")

    detail = " | ".join(failures)[:600]
    if unavailable == len(candidates):
        # Every engine was either absent or out of capacity, which says nothing about
        # the request: the job keeps waiting for a public engine with room.
        context.defer(
            "No public engine could take this render right now: "
            f"{detail}. It stays queued and is retried on its own."
        )
        return []
    raise PublicCloudError(
        f"No public engine could render this request. Tried {len(candidates)}: {detail}"
    )


def _candidates_for(
    root: str,
    capability: str,
    *,
    provider_id: str = "",
    now: Optional[float] = None,
    require_media: str = "",
) -> list[dict[str, Any]]:
    """The public engines to try, best first: the named one, then its alternates.

    The one the route named is tried first, but it is not the only chance: a public
    Space can break between the probe and the render (that happens, and it is not
    this computer's fault), so the other connected engines for the same capability
    are kept as fallbacks rather than the job failing on the first refusal.
    """
    stamp = float(now) if now is not None else time.time()
    state = load_state(root)
    usable = [
        dict(record)
        for record in state["providers"].values()
        if isinstance(record, Mapping) and _usable(record, capability, stamp)
    ]
    if require_media:
        usable = [
            record
            for record in usable
            if require_media
            in ((record.get("endpoints") or {}).get(capability, {}).get("media_kinds") or [])
        ]
    usable.sort(key=lambda record: _rank(record, capability))
    named = state["providers"].get(str(provider_id))
    if isinstance(named, Mapping) and any(
        str(record.get("provider_id")) == str(provider_id) for record in usable
    ):
        usable.sort(key=lambda record: str(record.get("provider_id")) != str(provider_id))
    return usable


def _render_on(
    record: Mapping[str, Any],
    params: Mapping[str, Any],
    capability: str,
    context: Any,
    *,
    out_dir: str,
    transport: Transport,
    timeout: float,
) -> list[str]:
    """One attempt on one public Space.  Returns the file, or says why not."""
    endpoint = dict((record.get("endpoints") or {}).get(capability) or {})
    space_id = str(record.get("space_id") or "")
    if not endpoint:
        raise PublicUnavailable(f"{space_id} no longer offers {capability}")
    base_url = str(record.get("base_url") or "")
    context.set_state("preparing")
    context.link(f"public:{space_id}")
    context.report(note=f"Preparing on {space_id} (free public GPU)")

    media: dict[str, Any] = {}
    for kind in ("image", "video"):
        path = _media_value(params, kind)
        if not path:
            continue
        if not any(_param_kind_matches(p, kind) for p in (endpoint.get("parameters") or [])):
            raise PublicCloudError(
                f"{os.path.basename(path)} has to travel with this request, but "
                f"{space_id} has no {kind} input, so the render would have lost it. "
                "Nothing was sent."
            )
        media[kind] = _upload_media(base_url, path, transport=transport, timeout=timeout)

    adjustments: list[str] = []
    data = build_data(endpoint, params, media=media, notes=adjustments)
    for adjustment in adjustments:
        context.report(note=adjustment)

    context.set_state("running")
    api_name = str(endpoint.get("api_name") or "")
    response = transport(
        "POST",
        f"{base_url}{GRADIO}/call/{quote(api_name.lstrip('/'), safe='')}",
        data=json.dumps({"data": data}).encode("utf-8"),
        headers={"Content-Type": "application/json", **_auth_headers()},
        timeout=max(60.0, min(float(timeout), 300.0)),
    )
    if response.status != 200:
        detail = str(response.json().get("detail") or response.body[:200].decode("utf-8", "replace"))
        refusal = f"refused the request ({response.status}): {detail[:200]}"
        if response.status in {408, 429, 500, 502, 503, 504} or _is_transient(detail):
            raise PublicUnavailable(refusal)
        raise PublicCloudError(refusal)
    event_id = str(response.json().get("event_id") or "")
    if not event_id:
        raise PublicCloudError("accepted the request but returned no event id")

    last_note: list[str] = [""]

    def note(text: str) -> None:
        if text and text != last_note[0]:
            last_note[0] = text
            context.report(note=text)

    payload = _stream_result(
        base_url,
        api_name,
        event_id,
        transport_timeout=float(timeout),
        transport=transport,
        cancelled=lambda: bool(context.cancelled()),
        on_note=note,
    )
    context.set_state("finalizing")
    urls = [url for url in (_file_url(base_url, value) for value in _referenced_files(payload)) if url]
    if not urls:
        raise PublicCloudError("finished without naming a file to download")
    body, content_type = _download(urls[0], transport=transport, timeout=timeout)
    media_type = _classify(body, content_type, EXPECTED_MEDIA[capability])

    os.makedirs(out_dir, exist_ok=True)
    destination = os.path.join(out_dir, _written_name(capability, media_type, urls[0]))
    with open(destination, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    print(
        f"[Public compute] {space_id} rendered {capability} "
        f"({len(body)} bytes) → {os.path.basename(destination)}"
    )
    return [os.path.abspath(destination)]


# ---------------------------------------------------------------------------
# The queue side: one handler per capability, registered whether or not it is up
# ---------------------------------------------------------------------------
# A handler is registered for every capability this tier *can* serve, connected or
# not.  The handler itself checks, from measured state, whether a public engine is
# reachable and defers the job when none is — so a job that names a public engine
# never dies because the internet was busy at the moment it was queued.


def make_handler(root: str, *, out_dir: Callable[[Mapping[str, Any]], str],
                 transport: Transport = _default_transport,
                 timeout: float = DEFAULT_RUN_TIMEOUT) -> Callable[..., list[str]]:
    def handler(job: Mapping[str, Any], context: Any) -> list[str]:
        params = job.get("params") if isinstance(job.get("params"), Mapping) else {}
        directory = out_dir(job)
        return run_job(
            root,
            job,
            context,
            out_dir=directory,
            transport=transport,
            timeout=timeout,
            provider_id=str((params or {}).get("public_provider") or ""),
        )

    return handler


def register_public_handlers(
    root: str,
    *,
    out_dir: Callable[[Mapping[str, Any]], str],
    transport: Transport = _default_transport,
    timeout: float = DEFAULT_RUN_TIMEOUT,
    capabilities: Optional[Sequence[str]] = None,
) -> list[str]:
    """Register the public runner for every capability this tier serves."""
    from . import queue_worker

    names: list[str] = []
    for capability in (capabilities if capabilities is not None else SERVED_CAPABILITIES):
        engine = engine_for(capability)
        queue_worker.register_handler(
            engine,
            make_handler(root, out_dir=out_dir, transport=transport, timeout=timeout),
            replace=True,
        )
        names.append(engine)
    return names


def unregister_public_handlers(capabilities: Optional[Sequence[str]] = None) -> list[str]:
    from . import queue_worker

    removed: list[str] = []
    for capability in (capabilities if capabilities is not None else SERVED_CAPABILITIES):
        if queue_worker.unregister_handler(engine_for(capability)):
            removed.append(engine_for(capability))
    return removed
