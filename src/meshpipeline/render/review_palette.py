# Responsibility: Assign distinguishable colours to named patches, and prove the contrast.
# Boundaries: colour is measured against a contrast ratio, because a vision model reads these images.
from __future__ import annotations

# THE REVIEW RENDERER OWNS EVERY PRESENTATION CHOICE ITS EVIDENCE IS MADE UNDER.
# The reviewer judges a mesh. If anything downstream of the component that built that mesh
# could choose the colours, the framing or the visibility the judgement happens under, a
# defect could be made hard to see: a patch the colour of the background, two patches the
# same colour, a camera pointed away from the flaw. Review evidence has to be reproducible
# and uninfluenced by what it is reviewing, so presentation is decided here - from geometry
# and semantic identity alone.
# The manifest carries engineering facts: which patches exist, what they are called, what
# role each plays, bounds, statistics. It carries no presentation whatsoever.
import hashlib
import logging
from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

# The review background. The categorical set below is chosen to clear a contrast ratio
# against THIS value - change one and the other must be re-measured.
REVIEW_BACKGROUND: tuple[int, int, int] = (255, 255, 255)

# Minimum contrast a categorical colour must reach against the background. 3:1 is the WCAG
# floor for a graphical object, and a patch boundary is something a reviewer has to find.
MIN_CONTRAST_RATIO = 3.0

# RESERVED - one meaning each, wherever they appear. Never handed out as a category.
REVIEW_DEFECT_COLOR: tuple[int, int, int] = (255, 0, 0)        # a marked defect / failed check
REVIEW_SELECTION_COLOR: tuple[int, int, int] = (255, 0, 230)   # the patch under inspection
RESERVED_COLORS: tuple[tuple[int, int, int], ...] = (
    REVIEW_DEFECT_COLOR, REVIEW_SELECTION_COLOR)

# CONTEXT - a far-field or enclosing domain frames the body; it must not compete with it.
# Subdued, but still clearly above the background.
REVIEW_CONTEXT_COLOR: tuple[int, int, int] = (110, 120, 132)

# Roles whose geometry is context rather than the subject of the review. The role vocabulary
# is the ENGINE's (manifest patch_types); this only decides prominence.
CONTEXT_ROLES: frozenset[str] = frozenset({"farfield", "far_field", "external", "domain"})

# THE CATEGORICAL SET - explicit RGB, ordered to step between hue families so two patches
# landing on adjacent slots are not two neighbouring reds. Every entry is measured against
# REVIEW_BACKGROUND by test; bright is deliberate (these have to read at a glance in a
# screenshot), but they are a renderer implementation detail, not a product feature and not
# anything a model or a manifest can ask for.
REVIEW_CATEGORICAL_COLORS: tuple[tuple[int, int, int], ...] = (
    (0, 102, 255),      # blue
    (204, 0, 255),      # purple
    (0, 26, 255),       # deep blue
    (255, 77, 0),       # red-orange
    (255, 0, 153),      # pink
    (255, 0, 77),       # rose
    (51, 0, 255),       # indigo
    (128, 0, 255),      # violet
)

# Renderer-owned drawing constants. Nothing outside this module chooses them.
REVIEW_EDGE_COLOR = "black"
REVIEW_EDGE_VISIBLE = True
REVIEW_LINE_WIDTH = 0.5
REVIEW_OPACITY = 1.0
REVIEW_LIGHTING = False
# The camera preset every derived patch view uses. Deterministic, and not selectable by
# anything upstream.
REVIEW_DEFAULT_PRESET = "iso"


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def _c(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * _c(r) + 0.7152 * _c(g) + 0.0722 * _c(b)


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _slot(identifier: str, modulus: int) -> int:
    # A STABLE hash, not Python's: PYTHONHASHSEED would otherwise make the same mesh render
    # differently between two runs, and reproducible evidence is the whole point.
    digest = hashlib.sha256(identifier.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % modulus


def assign(patch_names: Sequence[str] | None,
           roles: Mapping[str, str] | None = None) -> dict[str, tuple[int, int, int]]:
    names = sorted({str(n) for n in (patch_names or [])})
    role_map = {str(k): str(v).strip().lower() for k, v in (roles or {}).items()}

    out: dict[str, tuple[int, int, int]] = {}
    subjects: list[str] = []
    for n in names:
        if role_map.get(n, "") in CONTEXT_ROLES:
            out[n] = REVIEW_CONTEXT_COLOR      # context never consumes a categorical slot
        else:
            subjects.append(n)

    taken: set[int] = set()
    for n in subjects:
        start = _slot(n, len(REVIEW_CATEGORICAL_COLORS))
        idx = start
        for step in range(len(REVIEW_CATEGORICAL_COLORS)):
            cand = (start + step) % len(REVIEW_CATEGORICAL_COLORS)
            if cand not in taken:
                idx = cand
                break
        else:
            idx = start                        # more patches than colours: reuse, deterministically
        taken.add(idx)
        out[n] = REVIEW_CATEGORICAL_COLORS[idx]
    return out


def assign_hex(patch_names: Sequence[str] | None,
               roles: Mapping[str, str] | None = None) -> dict[str, str]:
    return {n: to_hex(rgb) for n, rgb in assign(patch_names, roles).items()}


def derive_patch_views(patch_bounds: Mapping[str, Sequence[float]],
                       roles: Mapping[str, str] | None = None) -> dict[str, dict]:
    role_map = {str(k): str(v).strip().lower() for k, v in (roles or {}).items()}
    views: dict[str, dict] = {}
    for name in sorted(patch_bounds or {}):
        if role_map.get(name, "") in CONTEXT_ROLES:
            continue
        b = list(patch_bounds[name] or [])
        if len(b) < 6:
            continue
        xmin, xmax, ymin, ymax, zmin, zmax = (float(v) for v in b[:6])
        span = max(xmax - xmin, ymax - ymin, zmax - zmin) or 1e-6
        views[name] = {
            "x": round((xmin + xmax) / 2, 6),
            "y": round((ymin + ymax) / 2, 6),
            "z": round((zmin + zmax) / 2, 6),
            "span": round(span, 6),
            "preset": REVIEW_DEFAULT_PRESET,
            "isolate": True,      # hide the enclosing domain while inspecting this patch
        }
    return views


__all__ = [
    "REVIEW_BACKGROUND", "MIN_CONTRAST_RATIO",
    "REVIEW_CATEGORICAL_COLORS", "REVIEW_CONTEXT_COLOR",
    "REVIEW_DEFECT_COLOR", "REVIEW_SELECTION_COLOR", "RESERVED_COLORS",
    "CONTEXT_ROLES", "REVIEW_EDGE_COLOR", "REVIEW_EDGE_VISIBLE", "REVIEW_LINE_WIDTH",
    "REVIEW_OPACITY", "REVIEW_LIGHTING", "REVIEW_DEFAULT_PRESET",
    "contrast_ratio", "to_hex", "assign", "assign_hex", "derive_patch_views",
]
