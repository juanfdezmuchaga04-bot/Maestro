"""Engine provider registry — the inference engine is never hard-wired.

The film studio does not talk to a model, a pipeline or a vendor.  It talks to
*capabilities*, and a capability is resolved to whichever provider can satisfy it
right now.  That is what makes the frontend independent of the engine: swapping
the hardware, installing a model, or plugging a ComfyUI instance in changes the
registry, not the application.

Design rules taken from the studio spec, and enforced here:

* **Capabilities are registered one by one.**  A provider that can render video
  but not speech simply does not declare ``voice_generation``.  Nothing is
  inferred from a provider's name.
* **Commercial APIs are optional.**  They can be registered, but they are never
  required: :func:`resolve` only returns one when the caller explicitly allows
  it, and :func:`availability` reports the local-only picture by default.  A
  local-only installation must be able to satisfy every capability it declares.
* **No silent fallback.**  If a capability has no provider, or a requested
  provider cannot do it, we raise with a message naming what *can* do it.  We
  never pretend an unavailable engine ran.
* **Built from measured state.**  :func:`build_registry` composes providers from
  what actually exists (the local engine's own pipelines, live ComfyUI probes,
  online compute nodes).  It fabricates nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_SPECS",
    "PROVIDER_KINDS",
    "EngineProvider",
    "EngineProviderError",
    "EngineRegistry",
    "build_registry",
    "capability_spec",
    "describe_capabilities",
]


class EngineProviderError(ValueError):
    """Raised when a capability cannot be satisfied by any available provider."""


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
# Every capability is declared separately (spec §8: "Cada capacidad debe
# registrarse por separado").  ``task`` is the kind of work the studio schedules,
# ``media`` what the provider produces, and ``needs_gpu`` is a *hint* used to
# explain why a capability is unavailable on CPU-only hardware — it is not a
# gate, because a provider on another node can satisfy it.
CAPABILITY_SPECS: dict[str, dict[str, Any]] = {
    "image_generation": {
        "label": "Image generation",
        "task": "image",
        "media": "image",
        "needs_gpu": True,
    },
    "text_to_video": {
        "label": "Text to video",
        "task": "video",
        "media": "video",
        "needs_gpu": True,
    },
    "image_to_video": {
        "label": "Image to video",
        "task": "video",
        "media": "video",
        "needs_gpu": True,
    },
    "video_to_video": {
        "label": "Video to video",
        "task": "video",
        "media": "video",
        "needs_gpu": True,
    },
    "video_extension": {
        "label": "Video extension",
        "task": "video",
        "media": "video",
        "needs_gpu": True,
    },
    "frame_interpolation": {
        "label": "Frame interpolation",
        "task": "video",
        "media": "video",
        "needs_gpu": True,
    },
    "upscaling": {
        "label": "Resolution upscaling",
        "task": "video",
        "media": "video",
        "needs_gpu": True,
    },
    "inpainting": {
        "label": "Inpainting / region edit",
        "task": "image",
        "media": "image",
        "needs_gpu": True,
    },
    "voice_generation": {
        "label": "Speech synthesis",
        "task": "audio",
        "media": "audio",
        "needs_gpu": True,
    },
    "audio_generation": {
        "label": "Music / sound generation",
        "task": "audio",
        "media": "audio",
        "needs_gpu": True,
    },
    "transcription": {
        "label": "Speech transcription",
        "task": "text",
        "media": "text",
        "needs_gpu": False,
    },
    "auxiliary_processing": {
        "label": "Auxiliary processing",
        "task": "processing",
        "media": "any",
        "needs_gpu": False,
    },
}

CAPABILITIES: tuple[str, ...] = tuple(CAPABILITY_SPECS)

# Ordered by preference.  A local engine beats a remote node beats a generic
# external server beats a paid API, and that order is stable so the same request
# always resolves the same way.
PROVIDER_KINDS: tuple[str, ...] = (
    "local_engine",
    "compute_node",
    "comfyui",
    "commercial_api",
)

_KIND_RANK = {kind: index for index, kind in enumerate(PROVIDER_KINDS)}

_LOCAL_KINDS = frozenset({"local_engine", "compute_node", "comfyui"})


def capability_spec(capability: str) -> dict[str, Any]:
    """Return the spec for ``capability`` or raise a clear error."""
    try:
        return dict(CAPABILITY_SPECS[capability])
    except KeyError:
        raise EngineProviderError(
            f"Unknown capability {capability!r}. Known capabilities: "
            + ", ".join(CAPABILITIES)
        ) from None


def describe_capabilities() -> list[dict[str, Any]]:
    """The capability catalogue, for the UI's engine panel."""
    return [
        {"id": name, **dict(spec)}
        for name, spec in CAPABILITY_SPECS.items()
    ]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineProvider:
    """One way of satisfying some capabilities.

    ``provider_id`` is stable and namespaced by the caller (``local:maestro``,
    ``node:<node_id>``, ``comfyui:<base_url>``) so two providers can never
    collide.
    """

    provider_id: str
    label: str
    kind: str
    capabilities: tuple[str, ...]
    local: bool = True
    requires_api_key: bool = False
    node_id: str = "local"
    models: tuple[str, ...] = ()
    notes: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _KIND_RANK:
            raise EngineProviderError(
                f"Unknown provider kind {self.kind!r}. Known kinds: "
                + ", ".join(PROVIDER_KINDS)
            )
        if self.requires_api_key and self.kind != "commercial_api":
            raise EngineProviderError(
                f"{self.provider_id}: only commercial_api providers may require an API key"
            )
        unknown = [c for c in self.capabilities if c not in CAPABILITY_SPECS]
        if unknown:
            raise EngineProviderError(
                f"{self.provider_id}: unknown capabilities {', '.join(sorted(unknown))}"
            )
        if len(set(self.capabilities)) != len(self.capabilities):
            raise EngineProviderError(f"{self.provider_id}: duplicate capability declared")

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def describe(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "local": bool(self.local),
            "requires_api_key": bool(self.requires_api_key),
            "node_id": self.node_id,
            "models": list(self.models),
            "notes": self.notes,
            "detail": dict(self.detail),
        }


class EngineRegistry:
    """An ordered, de-duplicated set of providers."""

    def __init__(self, providers: Iterable[EngineProvider] = ()) -> None:
        self._providers: dict[str, EngineProvider] = {}
        for provider in providers:
            self.register(provider)

    # -- mutation ---------------------------------------------------------

    def register(self, provider: EngineProvider) -> EngineProvider:
        if provider.provider_id in self._providers:
            raise EngineProviderError(f"Provider {provider.provider_id!r} is already registered")
        self._providers[provider.provider_id] = provider
        return provider

    def replace(self, provider: EngineProvider) -> EngineProvider:
        """Register or refresh a provider (used when a node reconnects)."""
        self._providers[provider.provider_id] = provider
        return provider

    def unregister(self, provider_id: str) -> bool:
        return self._providers.pop(provider_id, None) is not None

    def unregister_kind(self, kind: str) -> list[str]:
        removed = [pid for pid, prov in self._providers.items() if prov.kind == kind]
        for pid in removed:
            del self._providers[pid]
        return removed

    # -- lookup -----------------------------------------------------------

    def get(self, provider_id: str) -> EngineProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise EngineProviderError(f"Unknown provider {provider_id!r}")
        return provider

    def providers(self) -> list[EngineProvider]:
        return [
            self._providers[pid]
            for pid in sorted(
                self._providers,
                key=lambda pid: (_KIND_RANK[self._providers[pid].kind], pid),
            )
        ]

    def list(
        self,
        *,
        capability: Optional[str] = None,
        kind: Optional[str] = None,
        node_id: Optional[str] = None,
        include_commercial: bool = True,
    ) -> list[dict[str, Any]]:
        if capability is not None:
            capability_spec(capability)
        result = []
        for provider in self.providers():
            if capability is not None and not provider.supports(capability):
                continue
            if kind is not None and provider.kind != kind:
                continue
            if node_id is not None and provider.node_id != node_id:
                continue
            if not include_commercial and provider.kind == "commercial_api":
                continue
            result.append(provider.describe())
        return result

    def capabilities(self, *, include_commercial: bool = True) -> dict[str, list[str]]:
        """capability -> provider ids that can satisfy it."""
        mapping: dict[str, list[str]] = {name: [] for name in CAPABILITIES}
        for provider in self.providers():
            if not include_commercial and provider.kind == "commercial_api":
                continue
            for capability in provider.capabilities:
                mapping[capability].append(provider.provider_id)
        return mapping

    def availability(
        self,
        *,
        node_ids: Optional[Sequence[str]] = None,
        include_commercial: bool = False,
    ) -> list[dict[str, Any]]:
        """Per-capability report: satisfiable now, by whom, and why not."""
        reachable = set(node_ids) if node_ids is not None else None
        report = []
        for name, spec in CAPABILITY_SPECS.items():
            candidates = self._candidates(
                name,
                allow_commercial=include_commercial,
                node_ids=reachable,
            )
            report.append(
                {
                    "capability": name,
                    "label": spec["label"],
                    "task": spec["task"],
                    "needs_gpu": spec["needs_gpu"],
                    "available": bool(candidates),
                    "providers": [p.provider_id for p in candidates],
                }
            )
        return report

    # -- resolution -------------------------------------------------------

    def _candidates(
        self,
        capability: str,
        *,
        allow_commercial: bool,
        node_ids: Optional[set[str]],
    ) -> list[EngineProvider]:
        candidates = []
        for provider in self.providers():
            if not provider.supports(capability):
                continue
            if not allow_commercial and provider.kind == "commercial_api":
                continue
            if node_ids is not None and provider.kind != "local_engine":
                if provider.node_id not in node_ids:
                    continue
            candidates.append(provider)
        return candidates

    def resolve(
        self,
        capability: str,
        *,
        preferred: Optional[str] = None,
        allow_commercial: bool = False,
        node_ids: Optional[Sequence[str]] = None,
    ) -> EngineProvider:
        """Pick the provider that will actually run ``capability``.

        ``node_ids`` restricts the choice to compute nodes that are reachable
        now, which is how a disconnected desktop stops receiving new work
        without breaking the local engine.
        """
        capability_spec(capability)
        reachable = set(node_ids) if node_ids is not None else None
        candidates = self._candidates(
            capability, allow_commercial=allow_commercial, node_ids=reachable
        )

        if preferred is not None:
            match = next((p for p in candidates if p.provider_id == preferred), None)
            if match is not None:
                return match
            known = self._providers.get(preferred)
            if known is None:
                raise EngineProviderError(f"Unknown provider {preferred!r} for {capability}")
            if not known.supports(capability):
                raise EngineProviderError(
                    f"Provider {preferred!r} does not implement {capability}. "
                    f"It implements: {', '.join(known.capabilities) or 'nothing'}"
                )
            if known.kind == "commercial_api" and not allow_commercial:
                raise EngineProviderError(
                    f"Provider {preferred!r} is a commercial API and the studio is in local-only mode"
                )
            raise EngineProviderError(
                f"Provider {preferred!r} cannot run {capability} right now "
                "(its compute node is not reachable)"
            )

        if not candidates:
            local = self._candidates(
                capability, allow_commercial=False, node_ids=reachable
            )
            hint = ""
            if not local:
                hint = (
                    " — no local provider implements it; a commercial adapter could, "
                    "but none is required or configured"
                )
            raise EngineProviderError(
                f"No available engine can {capability_spec(capability)['label'].lower()}{hint}"
            )

        return candidates[0]

    # -- status -----------------------------------------------------------

    def status(self, *, node_ids: Optional[Sequence[str]] = None) -> dict[str, Any]:
        """Everything the UI engine panel needs, from measured state only."""
        return {
            "providers": self.list(),
            "capabilities": describe_capabilities(),
            "availability": self.availability(node_ids=node_ids),
            "local_only": not any(
                p.kind == "commercial_api" for p in self._providers.values()
            ),
        }


# ---------------------------------------------------------------------------
# Composition from measured state
# ---------------------------------------------------------------------------


def build_registry(
    *,
    local_capabilities: Sequence[str] = (),
    local_models: Sequence[str] = (),
    local_detail: Optional[Mapping[str, Any]] = None,
    comfy_servers: Sequence[Mapping[str, Any]] = (),
    nodes: Sequence[Mapping[str, Any]] = (),
    commercial: Sequence[Mapping[str, Any]] = (),
) -> EngineRegistry:
    """Assemble the registry from what the caller actually measured.

    Every argument is data the caller obtained by probing: ``comfy_servers``
    entries come from :mod:`services.comfyui_adapter`, ``nodes`` from
    :mod:`services.compute_nodes`, and ``local_capabilities`` from what the
    installed models really support.  Nothing here guesses.
    """
    registry = EngineRegistry()

    # Always register the local engine, even with zero capabilities: the UI must
    # be able to say "this machine is here but can generate nothing yet".
    registry.register(
        EngineProvider(
            provider_id="local:maestro",
            label="This machine (local Maestro engine)",
            kind="local_engine",
            capabilities=tuple(local_capabilities),
            local=True,
            node_id="local",
            models=tuple(local_models),
            notes="Runs the models installed on this computer.",
            detail=dict(local_detail or {}),
        )
    )

    for server in comfy_servers:
        base_url = str(server.get("base_url") or "").strip()
        if not base_url:
            continue
        capabilities = tuple(server.get("capabilities") or ())
        if not capabilities:
            # A reachable server with no mapped capability can still do the
            # generic graph work; we say so instead of claiming image/video.
            capabilities = ("auxiliary_processing",)
        registry.register(
            EngineProvider(
                provider_id=f"comfyui:{base_url}",
                label=str(server.get("label") or f"ComfyUI at {base_url}"),
                kind="comfyui",
                capabilities=capabilities,
                local=True,
                node_id=str(server.get("node_id") or "local"),
                models=tuple(server.get("models") or ()),
                notes=str(server.get("notes") or "ComfyUI server via its local HTTP API."),
                detail={
                    "base_url": base_url,
                    "object_info_nodes": int(server.get("object_info_nodes") or 0),
                    "templates": list(server.get("templates") or ()),
                },
            )
        )

    for node in nodes:
        node_id = str(node.get("node_id") or "").strip()
        if not node_id:
            continue
        capabilities = tuple(node.get("capabilities") or ())
        registry.register(
            EngineProvider(
                provider_id=f"node:{node_id}",
                label=str(node.get("label") or node_id),
                kind="compute_node",
                capabilities=capabilities,
                local=bool(node.get("local", False)),
                node_id=node_id,
                models=tuple(node.get("models") or ()),
                notes=str(node.get("notes") or "Compute node run by another device."),
                detail={
                    "online": bool(node.get("online", False)),
                    "last_seen": node.get("last_seen"),
                },
            )
        )

    for entry in commercial:
        provider_id = str(entry.get("provider_id") or "").strip()
        if not provider_id:
            continue
        registry.register(
            EngineProvider(
                provider_id=provider_id,
                label=str(entry.get("label") or provider_id),
                kind="commercial_api",
                capabilities=tuple(entry.get("capabilities") or ()),
                local=False,
                requires_api_key=bool(entry.get("requires_api_key", True)),
                node_id=str(entry.get("node_id") or "remote"),
                models=tuple(entry.get("models") or ()),
                notes=str(
                    entry.get("notes")
                    or "Optional commercial adapter. Never required by the studio."
                ),
            )
        )

    return registry
