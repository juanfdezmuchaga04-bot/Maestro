"""Where a generation request should run: on this computer, or on a peer's GPU.

Maestro is local-first, and that ordering is not negotiable: a machine that can
generate generates, and nothing is ever sent away while the local engine could
have done it.  What this module adds is the other half of the same rule — on a
computer that *cannot* generate (no CUDA GPU: a modest laptop, a Mac, a
CPU-only machine) a request is not refused, it is addressed to a peer that
declared it can render, using the peer registry and the durable queue that
already exist in :mod:`services.remote_engine`.

When there is no peer either, it is still not refused: it goes to **public
compute** (:mod:`services.public_cloud`), a free public GPU on the internet that
Maestro connects to on its own.  That is the tier for the computer that has no
second machine to borrow, and it is deliberately last: a public Space runs its
own model with its own defaults, so a render there *approximates* the Maestro
recipe while a peer replays it exactly.

Four things this module refuses to do, because they are how an integration
becomes a lie:

* **It never invents a target.**  A peer counts only while its last probe is
  fresh, and only for a capability it reported or the user declared.  No online
  peer → ``target = "none"`` with a reason that names what was measured.
* **It never sends a request that cannot arrive.**  A request that carries media
  input (a start frame, references, a control video, a soundtrack) names a file
  on *this* computer, which a peer cannot read.  Those files are **transferred**
  with the request and the parameters are rewritten to the copies the peer owns
  (``remote_engine.transfer_media``); a file that cannot travel stops the
  hand-over with its name, instead of arriving as a render that silently lost a
  reference.  This module names what has to travel; it does not move bytes.  A
  public engine is held to the same rule: it is only used for a request that
  brings media when it *reported* a file input that fits, because a public Space
  given a job without its start frame renders a different video entirely.
* **It does not decide *how* the peer renders.**  The peer runs the job with its
  own Maestro engine (``remote_engine`` below), which is the same pipeline this
  machine would have run.

The decision is a pure function over measured state, so it is testable without
a GPU, a peer, or the application.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "LOCAL_ENGINE",
    "LOCAL_MEDIA_PARAMS",
    "NO_TARGET",
    "PEER_ENGINE_PREFIX",
    "PUBLIC_ENGINE_PREFIX",
    "PUBLIC_FIDELITY",
    "PUBLIC_MEDIA_PARAMS",
    "PUBLIC_TARGET",
    "REMOTE_ENGINE",
    "REMOTE_TARGET",
    "LOCAL_TARGET",
    "capability_for_request",
    "choose_target",
    "local_media_inputs",
    "rendering_capabilities",
]

# The provider id of this computer's own engine, as engine_providers registers it.
LOCAL_ENGINE = "local:maestro"

# A delegated job is executed by the peer's own Maestro engine: that is the only
# engine that can run a Studio/Films parameter set (the same code, on a machine
# with the GPU).  The directing machine asks for it per job, through
# ``params.remote_engine``, so a peer that is used for ComfyUI work by hand keeps
# its own default.
REMOTE_ENGINE = "maestro"
# The engine a peer registers for *finishing* a finished video (SeedVR2 upscale to
# 1440p/4K).  It is asked for by name; Studio requests never route to it.
FINISH_ENGINE = "maestro-finish"

# ``node:<peer_id>`` — the queue engine string a delegated job carries.
PEER_ENGINE_PREFIX = "node"

# ``public:<capability>`` — what a job routed to public compute carries
# (services.public_cloud picks, at run time, the public Space that serves it).
PUBLIC_ENGINE_PREFIX = "public"

LOCAL_TARGET = "local"
REMOTE_TARGET = "remote"
PUBLIC_TARGET = "public"
NO_TARGET = "none"

# How faithful a public render is, said in one word so no screen has to imply it:
# the public engine runs its own model, so this is an approximation of the
# Maestro recipe and never a replay of it (which is what a peer gives you).
PUBLIC_FIDELITY = "public_approximation"

# The request keys whose files a public engine can be *given*: they are uploaded to
# its own file inputs.  Anything else this computer holds (a soundtrack, a mask, a
# character identity) has no input over there, so such a request is not sent.
PUBLIC_MEDIA_PARAMS = (
    "image_start",
    "image_end",
    "image_refs",
    "image_guide",
    "image_mask",
    "viggle_character",
    "video_guide",
    "video_guide2",
)

# Capabilities that actually render something.  Everything else the registry
# knows (assembly, retiming, transcription) is work this computer already does
# locally with FFmpeg, so it is never a reason to move a job.
_GPU_CAPABILITIES = (
    "image_generation",
    "text_to_video",
    "image_to_video",
    "video_to_video",
    "video_extension",
    "frame_interpolation",
    "upscaling",
    "inpainting",
    "voice_generation",
    "audio_generation",
)

# Request keys that name a file on this computer.  A peer that has not received
# the file cannot open it, so a request carrying any of these is not delegable
# yet — refusing is honest; sending it would fail over there for a reason the
# user did not cause and cannot see.
LOCAL_MEDIA_PARAMS = (
    "image_start",
    "image_end",
    "image_refs",
    "image_guide",
    "image_mask",
    "video_guide",
    "video_guide2",
    "audio_guide",
    "audio_guide2",
    "viggle_character",
    "multi_clip_concat_audio",
    "voice_reference",
    "per_clip_keyframes",
)


def rendering_capabilities(peer: Mapping[str, Any]) -> list[str]:
    """The capabilities of ``peer`` that render media on a GPU."""
    declared = peer.get("capabilities")
    if not isinstance(declared, (list, tuple, set, frozenset)):
        return []
    return [str(name) for name in declared if str(name) in _GPU_CAPABILITIES]


def _first_text(params: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def local_media_inputs(params: Mapping[str, Any]) -> list[str]:
    """The request keys that carry media from this computer, in a stable order."""
    found: list[str] = []
    for key in LOCAL_MEDIA_PARAMS:
        value = params.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, set, frozenset)) and not list(value):
            continue
        found.append(key)
    return found


def capability_for_request(params: Mapping[str, Any]) -> str:
    """The capability a Studio/Films request needs, from what it declares.

    Only facts the request states are used; nothing is inferred from a model's
    name.  An empty string means "the request did not say", and the router then
    accepts any peer that can render, instead of guessing a narrower capability
    and refusing a peer that would have worked.
    """
    mode = _first_text(params, ("generation_mode",)).lower()
    try:
        image_mode = int(params.get("image_mode") or 0)
    except (TypeError, ValueError):
        image_mode = 0
    if mode == "audio" or params.get("audio_only"):
        return "voice_generation" if _first_text(params, ("voice_reference",)) else "audio_generation"
    if mode == "image" or image_mode in {1, 2}:
        return "inpainting" if image_mode == 2 else "image_generation"
    # Video: a start frame is an image-to-video request; otherwise text.
    if _first_text(params, ("image_start", "image_refs")):
        return "image_to_video"
    if str(params.get("model_type") or "").strip() or mode:
        return "text_to_video"
    return ""


def _public_candidates(
    sources: Sequence[Mapping[str, Any]],
    capability: str,
    media: Sequence[str],
) -> list[Mapping[str, Any]]:
    """The public engines that can run this request, from what they measured.

    A public source counts only while its probe is fresh, only for a capability it
    really declared, and — for a request bringing media from this computer — only
    when it reported a file input those media can be uploaded to.  A public Space
    that cannot take the start frame is not used at all: the render would quietly be
    a different video.
    """
    usable = [
        source
        for source in sources
        if source.get("online") and (source.get("capabilities") or [])
    ]
    if capability:
        usable = [
            source for source in usable if capability in (source.get("capabilities") or [])
        ]
    if media:
        if [key for key in media if key not in PUBLIC_MEDIA_PARAMS]:
            return []
        usable = [source for source in usable if source.get("accepts_media")]
    return list(usable)


def _no_public_reason(
    sources: Sequence[Mapping[str, Any]],
    capability: str,
    media: Sequence[str],
) -> str:
    """Why public compute could not take this request either, in one sentence.

    A refusal that only says "no GPU" sends people looking for hardware they may not
    need; naming the public tier and what it did report is what makes the message
    actionable.
    """
    if not sources:
        return (
            "Public compute is not connected either: no free public engine answered a "
            "recent probe. Maestro keeps looking for one by itself, so a later attempt "
            "may find it."
        )
    sources = [source for source in sources if source.get("online")]
    if not sources:
        return (
            "Public compute answered before but did not answer a recent probe, so it is "
            "not a place to send this job. Maestro asks the public engines again by "
            "itself — a later attempt may find one."
        )
    unmappable = [key for key in media if key not in PUBLIC_MEDIA_PARAMS]
    if unmappable:
        return (
            "A public engine cannot take this request either: its media ("
            + ", ".join(unmappable)
            + ") has no input to be uploaded to over there."
        )
    if media and not [source for source in sources if source.get("accepts_media")]:
        return (
            "A public engine cannot take this request either: the public engines that "
            "answered report no file input for this request's media ("
            + ", ".join(media)
            + ")."
        )
    if capability:
        reported = sorted({
            str(name)
            for source in sources
            for name in (source.get("capabilities") or [])
        })
        return (
            f"No connected public engine reports {capability!r} either; public engines "
            "currently answer for: " + (", ".join(reported) or "nothing") + "."
        )
    return "No connected public engine can take this request either."


def choose_target(
    *,
    capability: str,
    local_generation_possible: bool,
    peers: Sequence[Mapping[str, Any]],
    request_params: Optional[Mapping[str, Any]] = None,
    public_sources: Sequence[Mapping[str, Any]] = (),
    peer_load: Optional[Mapping[str, int]] = None,
) -> dict[str, Any]:
    """Pick the engine that will run this request.

    ``peers`` is :func:`services.remote_engine.list_peers` output — the same
    records the API and the UI see, whose ``online`` flag is only true while a
    probe answered recently.  ``public_sources`` is
    :func:`services.public_cloud.connected_sources` output: free public engines on
    the internet, connected automatically, and again only while a probe is fresh.
    The returned dictionary is what the caller stores and what the UI can show;
    ``reason`` is written for the person reading it.
    """
    params = request_params if isinstance(request_params, Mapping) else {}
    online = [peer for peer in peers if peer.get("online")]
    renderers = [peer for peer in online if rendering_capabilities(peer)]
    candidates = [str(peer.get("peer_id")) for peer in renderers]
    # The files this request brings from this computer.  They travel with the job
    # (the peer cannot read a path that only exists here); the route carries the
    # list so the caller — and the interface — can say what will be sent.
    media = local_media_inputs(params)
    public = _public_candidates(public_sources, capability, media)

    def result(
        target: str,
        *,
        engine: str = "",
        peer: Mapping[str, Any] | None = None,
        source: Mapping[str, Any] | None = None,
        reason: str,
    ) -> dict[str, Any]:
        chosen = peer or {}
        cloud = source or {}
        return {
            "target": target,
            "engine": engine,
            "peer_id": str(chosen.get("peer_id") or ""),
            "peer_label": str(chosen.get("label") or ""),
            "peer_base_url": str(chosen.get("base_url") or ""),
            "public_provider_id": str(cloud.get("provider_id") or ""),
            "public_provider_label": str(cloud.get("label") or ""),
            "public_provider_base_url": str(cloud.get("base_url") or ""),
            "public_capability": capability if cloud else "",
            "public_generation_available": bool(public),
            "public_candidates": [str(item.get("provider_id") or "") for item in public],
            "public_fidelity": PUBLIC_FIDELITY if cloud else "",
            "capability": capability,
            "local_generation_possible": bool(local_generation_possible),
            "remote_generation_available": bool(renderers),
            "remote_engine": REMOTE_ENGINE,
            "candidates": candidates,
            "media_to_transfer": media,
            "reason": reason,
        }

    if local_generation_possible:
        return result(
            LOCAL_TARGET,
            engine=LOCAL_ENGINE,
            reason=(
                "This computer has a CUDA GPU, so the job runs here. A local engine "
                "always wins over a remote one."
            ),
        )

    def to_public(*, because: str) -> dict[str, Any]:
        source = public[0]
        return result(
            PUBLIC_TARGET,
            engine=f"{PUBLIC_ENGINE_PREFIX}:{capability}",
            source=source,
            reason=(
                because + " " if because else ""
            ) + (
                f"This computer cannot generate, so the job runs on {source.get('label')} "
                f"({source.get('hardware') or 'free public GPU'}), a free public engine on "
                "the internet that Maestro connected to by itself. That engine runs its own "
                "model with its own defaults, so the render approximates the Maestro recipe "
                "rather than replaying it "
                f"({PUBLIC_FIDELITY}). The result is downloaded into this workspace."
            ),
        )

    if not online:
        if peers:
            reason = (
                f"{len(peers)} remote computer(s) are configured but none answered a "
                "recent probe, so none was treated as able to render. Check that the "
                "other computer is running and use Probe."
            )
        else:
            reason = (
                "This computer has no CUDA GPU and no remote computer is configured to "
                "render for it. Add one (Studio → Compute & transfer) or install the "
                "engine on a machine with an NVIDIA GPU."
            )
        if public:
            return to_public(because=reason)
        return result(NO_TARGET, reason=reason + " " + _no_public_reason(public_sources, capability, media))

    if not renderers or (capability and not [
        peer for peer in renderers if capability in (peer.get("capabilities") or [])
    ]):
        if not renderers:
            reason = (
                "A remote computer answered, but it reported no capability that renders "
                "media. Only a peer that declares a GPU capability can take this job."
            )
        else:
            reason = (
                f"No remote computer reports {capability!r}, which this request needs. "
                "Reported by the peers that answered: "
                + "; ".join(
                    f"{peer.get('peer_id')}: "
                    + (", ".join(str(name) for name in (peer.get("capabilities") or [])) or "nothing")
                    for peer in renderers
                )
                + "."
            )
        if public:
            return to_public(because=reason)
        return result(NO_TARGET, reason=reason + " " + _no_public_reason(public_sources, capability, media))

    exact = (
        [peer for peer in renderers if capability in (peer.get("capabilities") or [])]
        if capability
        else renderers
    )
    # Several computers can render (for example the two GPUs of one free notebook):
    # give the job to the one with the fewest jobs of ours in flight, keeping the
    # configured order as the tie-break, so independent jobs run side by side.
    load = peer_load if isinstance(peer_load, Mapping) else {}
    chosen = min(exact, key=lambda peer: (int(load.get(str(peer.get("peer_id")), 0) or 0), exact.index(peer)))
    return result(
        REMOTE_TARGET,
        engine=f"{PEER_ENGINE_PREFIX}:{chosen.get('peer_id')}",
        peer=chosen,
        reason=(
            f"This computer cannot generate locally, so the job goes to {chosen.get('label')} "
            f"({chosen.get('base_url')}), which reported: "
            + (", ".join(str(name) for name in (chosen.get("capabilities") or [])) or "nothing")
            + ". "
            + (
                "This request's own media ("
                + ", ".join(media)
                + ") is sent to that computer first, and the result is transferred back "
                "into this workspace."
                if media
                else "The result is transferred back into this workspace."
            )
        ),
    )
