# Responsibility: Carry the typed inputs a render session was built from.
# Boundaries: values only.
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.contracts.mesh_units import completed_mesh_unit
from meshpipeline.sandbox.review_session import ResolvedArtifact

logger = logging.getLogger(__name__)

# The screenshot geometry the reviewer's evidence has always been produced at. Declared rather
# than embedded so a caller can bound it, and so a future engine adapter cannot quietly raise it.
DEFAULT_SCREENSHOT_W = 1280
DEFAULT_SCREENSHOT_H = 960


@dataclass(frozen=True)
class RenderLimits:

    screenshot_w: int = DEFAULT_SCREENSHOT_W
    screenshot_h: int = DEFAULT_SCREENSHOT_H


# Presentation keys that were once carried in the manifest and are no longer part of any
# supported contract. Review presentation is owned by render.review_palette; a manifest that
# still prescribes it is not a current manifest, and is refused rather than half-honoured.
RETIRED_PRESENTATION_KEYS: frozenset[str] = frozenset({"patch_colors", "patch_views"})


@dataclass(frozen=True)
class RenderMetadata:

    # NO DEFAULT. This used to be "mm", so a manifest missing the field made the reviewer describe
    # a metre mesh in millimetres - while the viewer, defaulting to "m", described the same
    # artifact correctly. The unit is now always the artifact's own, or there is no metadata.
    mesh_units: str
    # Patch ROLES are engineering facts the engine declares (wall / inlet / farfield / ...).
    # The renderer uses them to decide subject from context. It takes no presentation from
    # anyone: colours, framing, opacity, edges and background are all its own.
    patch_roles: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any] | None) -> RenderMetadata:
        m = manifest or {}
        retired = sorted(k for k in RETIRED_PRESENTATION_KEYS if k in m)
        if retired:
            raise ValueError(
                "mesh manifest carries retired review-presentation field(s): "
                + ", ".join(retired)
                + " - review presentation is renderer-owned and manifests must contain "
                  "engineering facts only")
        # THE canonical reader. Raises MeshUnitsError on a missing, empty or non-metre value
        # rather than substituting one - the renderer must not narrate dimensions in a unit the
        # artifact never claimed.
        units = completed_mesh_unit(m)
        roles_raw = m.get("patch_types") or {}
        roles = {str(k): str(v) for k, v in roles_raw.items()
                 if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(roles_raw, Mapping) else {}
        return cls(mesh_units=units.value, patch_roles=roles)


@dataclass(frozen=True)
class ResolvedRenderInputs:

    surface: ResolvedArtifact
    volume: ResolvedArtifact | None = None

    @property
    def has_volume(self) -> bool:
        return self.volume is not None


@dataclass(frozen=True)
class ResolvedRegion:

    region_id: str
    kind: str
    normal: tuple[float, float, float]
    origin: tuple[float, float, float] | None = None
    clip_box: tuple[float, ...] | None = None
    requires_volume: bool = True


def _finite_triple(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    out: list[float] = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        f = float(v)
        if not math.isfinite(f):
            return None
        out.append(f)
    return (out[0], out[1], out[2])


def resolve_region(raw: Mapping[str, Any]) -> ResolvedRegion | None:
    if not isinstance(raw, Mapping):
        return None
    region_id = raw.get("name")
    if not isinstance(region_id, str) or not region_id.strip():
        return None

    normal = _finite_triple(raw.get("normal", [0, 1, 0]))
    if normal is None or not any(normal):
        return None                       # a zero normal defines no plane

    # An origin that is ABSENT is fine - VTK slices through the centre, which is the established
    # default. An origin that is DECLARED and invalid is not the same thing and must not collapse
    # into it: the region would then slice at the centre while claiming to be the named location,
    # producing a plausible image of the wrong place that the coverage floor would count.
    origin: tuple[float, float, float] | None = None
    if raw.get("origin") is not None:
        origin = _finite_triple(raw.get("origin"))
        if origin is None:
            return None

    clip_box: tuple[float, ...] | None = None
    raw_box = raw.get("clip_box")
    if raw_box is not None:
        if (not isinstance(raw_box, (list, tuple)) or len(raw_box) != 6
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(float(v)) for v in raw_box)):
            return None                   # declared but unusable - do not half-honour it
        clip_box = tuple(float(v) for v in raw_box)

    kind = raw.get("kind")
    return ResolvedRegion(
        region_id=region_id,
        kind=kind if isinstance(kind, str) and kind.strip() else "slice",
        normal=normal, origin=origin, clip_box=clip_box,
        requires_volume=True,             # every region today is a cut through the volume
    )


def resolve_regions(manifest: Mapping[str, Any] | None) -> dict[str, ResolvedRegion]:
    raw = (manifest or {}).get("inspection_regions") or []
    if not isinstance(raw, (list, tuple)):
        return {}
    out: dict[str, ResolvedRegion] = {}
    for entry in raw:
        region = resolve_region(entry) if isinstance(entry, Mapping) else None
        if region is not None:
            out[region.region_id] = region
    return out


def legend_pairs(assigned_colors: Mapping[str, str],
                 loaded_names: Any) -> list[tuple[str, str]]:
    # `assigned_colors` is the RENDERER's own assignment (hex), never manifest input.
    return [(n, c) for n, c in assigned_colors.items() if n in loaded_names]
