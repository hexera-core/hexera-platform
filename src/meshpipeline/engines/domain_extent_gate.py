# Responsibility: Check a requested external domain against the geometry it must enclose.
# Boundaries: a bounded geometric check; it does not author the domain and does not modify the request.
from __future__ import annotations

import logging
import re

from meshpipeline.engines.ground_plane import VERTICAL_AXIS, manifest_is_grounded

logger = logging.getLogger(__name__)

_KEYS = ("upstream", "downstream", "lateral")

# #
# SYMMETRY PLANES. A half model is cut on a plane and meshed on one side, with a symmetry plane on
# the cut; a 2.5D slab has one on each end of its sweep. Like the floor under a grounded body,
# such a face sits ON the body by design: it is not far field, and no margin is owed on it. The
# builder that placed the planes records them (the manifest's geometry.symmetry_faces, the snappy
# pre-flight's own list) as [{patch, axis, side}], and the gate reads them from there - it never
# guesses which face "looks like" a cut, so a plane on the wrong face is still caught.
# #

#: how far a symmetry face may stand off the body and still be its cut, as a fraction of the
#: body's diagonal - the tolerance the half-model detector calls a face the cut with
#: (snappy_runner.detect_symmetry_plane measures 0.005 of the longest extent, never more than this)
SEAT_TOL_FRACTION = 0.005
#: how far the body may reach THROUGH its symmetry plane: float noise only. The box cuts off
#: whatever lies beyond the plane, so a body crossing it by more is refused, not meshed short.
#: The snappy builder lays the plane on the cut face itself, so a real half model sits at zero.
CLIP_TOL_FRACTION = 1e-4

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
_SIDES = ("min", "max")


def _face(j: int, side: str) -> str:
    return f"{'xyz'[j]}-{side}"


def _symmetry_seats(faces) -> dict[tuple[int, str], str]:
    """(axis index, side) -> patch name for every recorded symmetry face. Rows that do not name
    an axis and a side are ignored rather than guessed."""
    out: dict[tuple[int, str], str] = {}
    for f in faces or ():
        if not isinstance(f, dict):
            continue
        ax, side = f.get("axis"), str(f.get("side") or "").strip().lower()
        if isinstance(ax, str):
            j = _AXIS_INDEX.get(ax.strip().lower()[-1:])
        elif isinstance(ax, int) and not isinstance(ax, bool) and 0 <= ax <= 2:
            j = ax
        else:
            j = None
        if j is None or side not in _SIDES:
            continue
        out[(j, side)] = str(f.get("patch") or f.get("name") or "symmetry")
    return out


def _gap(box: dict, body: dict, j: int, side: str) -> float:
    """The room between the body and one box face, in metres; zero or less touches or clips."""
    n = "xyz"[j]
    return (float(body[f"{n}min"]) - float(box[f"{n}min"]) if side == "min"
            else float(box[f"{n}max"]) - float(body[f"{n}max"]))


def _sits_on(box: dict, body: dict, j: int, side: str) -> bool:
    # the diagonal over the axes the record carries (a legacy 2-axis body box still has a size)
    diag = sum((float(body[f"{n}max"]) - float(body[f"{n}min"])) ** 2 for n in "xyz"
               if f"{n}min" in body and f"{n}max" in body) ** 0.5
    return (-CLIP_TOL_FRACTION * diag <= _gap(box, body, j, side)
            <= SEAT_TOL_FRACTION * diag)


def _flow_sides(flow_axis: str | None) -> tuple[int, str, str]:
    """(flow axis index, upstream side, downstream side). No declaration is the legacy +x."""
    ax = (flow_axis or "+x").strip().lower()
    sign, letter = (ax[0], ax[1]) if ax[0] in "+-" else ("+", ax[0])
    up, down = ("min", "max") if sign == "+" else ("max", "min")
    return _AXIS_INDEX[letter], up, down


def manifest_symmetry_faces(manifest: dict | None) -> list:
    """The symmetry planes a built mesh's box carries, as its builder recorded them. Read from the
    manifest, so a gate judges the mesh that was actually built; none for an internal flow."""
    m = manifest or {}
    if str(m.get("flow_topology") or "") == "internal":
        return []
    faces = (m.get("geometry") or {}).get("symmetry_faces")
    return list(faces) if isinstance(faces, list) else []


_EXPLICIT_RE = {
    "upstream":   re.compile(r"D_UPSTREAM\s*[=:]\s*([\d.]+)", re.I),
    "downstream": re.compile(r"D_DOWNSTREAM\s*[=:]\s*([\d.]+)", re.I),
    "lateral":    re.compile(r"D_LATERAL\s*[=:]\s*([\d.]+)", re.I),
}
_PHRASE_RE = {
    "upstream":   re.compile(r"([\d.]+)\s*(?:c|chord|chords)?\s*(?:upstream|inlet|in front)", re.I),
    "downstream": re.compile(r"([\d.]+)\s*(?:c|chord|chords)?\s*(?:downstream|outlet|wake|behind)", re.I),
    "lateral":    re.compile(r"([\d.]+)\s*(?:c|chord|chords)?\s*(?:lateral|above/below|above|below|side)", re.I),
}


def parse_requested_extents(request_txt: str) -> dict:
    out: dict = {}
    if not request_txt:
        return out
    for key in _KEYS:
        m = _EXPLICIT_RE[key].search(request_txt) or _PHRASE_RE[key].search(request_txt)
        if m:
            try:
                out[key] = float(m.group(1))
            except ValueError:
                pass
    return out


def _applied_multipliers(geom: dict) -> dict | None:
    box  = geom.get("domain_box") or geom.get("box")
    body = geom.get("body_bbox") or geom.get("body_box")
    # The DIVISOR is the user's own reference length when they stated one, because their "Nc"
    # request is in that unit. Dividing by the body length judged a 20-MAC request as 3.2 and
    # rejected a correct mesh; the fallback (no stated reference) is the body length, which is
    # both the old behaviour and the unit the planner's margins are natively in.
    chord = geom.get("reference_length") or geom.get("chord") or geom.get("CHORD")
    if not (isinstance(box, dict) and isinstance(body, dict) and chord):
        return None
    try:
        c = float(chord)
        if c <= 0:
            return None
        up   = (float(body["xmin"]) - float(box["xmin"])) / c
        down = (float(box["xmax"])  - float(body["xmax"])) / c
        # lateral is the y room, read on y-min as it always was - unless the body sits on a
        # symmetry plane there (a half model cut on y = 0), whose room is on the other side.
        sits = {s for (j, s) in _symmetry_seats(geom.get("symmetry_faces"))
                if j == 1 and _sits_on(box, body, 1, s)}
        lat: float | None = (float(body["ymin"]) - float(box["ymin"])) / c
        if "min" in sits:
            lat = None if "max" in sits else (float(box["ymax"]) - float(body["ymax"])) / c
        return {"upstream": up, "downstream": down, "lateral": lat}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def check_domain_extents(requested: dict | None, manifest: dict,
                         tol: float = 0.15) -> tuple[bool, str]:
    if not requested:
        return True, ""  # nothing to enforce
    geom = (manifest or {}).get("geometry", {}) or {}
    applied = _applied_multipliers(geom)
    if applied is None:
        logger.info("domain_extent_gate: manifest lacks box/body/chord - gate inconclusive, passing")
        return True, ""

    mismatches = []
    for k in _KEYS:
        rv = requested.get(k)
        av = applied.get(k)
        if rv is None or av is None:
            continue
        try:
            rv = float(rv)
        except (TypeError, ValueError):
            continue
        if rv == 0:
            continue
        if abs(av - rv) / abs(rv) > tol:
            mismatches.append(f"{k}: requested {rv:.3g}c, mesh has {av:.3g}c")

    if mismatches:
        ruler = float(geom.get("reference_length") or geom.get("chord") or 0.0)
        src = geom.get("reference_length_source") or "body_streamwise_extent"
        diag = (
            "[DOMAIN_EXTENT_MISMATCH] Domain extents do not match the user's request "
            f"(tolerance {int(tol*100)}%, measured in units of the {src} reference length "
            f"{ruler:.4g} m): " + "; ".join(mismatches) + ". "
            "Recompute the far-field box corners (domain_min/domain_max passed to "
            "prepare_surface) so the box spans EXACTLY the requested chord multiples "
            "around the body - do NOT fall back to a default external-aero prior. "
            "Then re-run prepare_surface and run_mesh."
        )
        return False, diag
    return True, ""


def extent_gate_for_request(request_txt: str, manifest: dict) -> tuple[bool, str]:
    return check_domain_extents(parse_requested_extents(request_txt), manifest)


# #
# TYPED evaluation (approved intent v5): the request arrives as numbers the user approved, the
# ruler is the approved reference length - never the manifest's, which a re-plan can lose or
# change (the heat-sink retries measured a correct box with a wrong-axis ruler and rejected two
# production-grade meshes). Tri-state: a request that CANNOT be measured is "unmeasured", never
# a silent pass. One-sided: over-delivering a margin is never a defect. Floored: under half of
# what was asked - or a box touching the body - is a category error and blocks outright.
# #

#: below this fraction of the requested margin, a miss is no longer a near-miss
FLOOR_FRACTION = 0.5

_ALL_DIRECTIONS = ("upstream", "downstream", "lateral", "vertical")


class ExtentVerdict:
    __slots__ = ("status", "caveats", "detail", "misses")

    def __init__(self, status: str, caveats: list, detail: str, misses: list | None = None):
        self.status = status      # "na" | "unmeasured" | "pass" | "miss" | "block"
        self.caveats = caveats    # [{direction, requested, measured, units, ruler_m, ruler_source}]
        self.detail = detail
        # every direction short of its request, blocked or near-missed alike:
        # [{direction, requested, measured}] - what the user is told, in numbers. A box that
        # touches the body also names the face ("y-min"), and, when a symmetry plane is misplaced,
        # the plane (symmetry_patch, symmetry_face, crosses).
        self.misses = list(misses or [])


class _Room:
    """One direction's margin in ruler units, the box face it was read on, and - when that face
    touches the body - why, in words and as facts for the user's sentence."""
    __slots__ = ("margin", "face", "why", "facts")

    def __init__(self, margin: float, face: str, why: str = "", facts: dict | None = None):
        self.margin, self.face, self.why, self.facts = margin, face, why, dict(facts or {})


def _axis_margins(box: dict, body: dict, r: float, flow_axis: str | None,
                  grounded: bool = False, symmetry_faces=None) -> dict:
    """Margins in ruler units, oriented by the DECLARED flow axis. No declaration keeps the
    legacy assume-X convention byte-for-byte (pre-v5 behaviour, documented). Sign matters:
    flow -y puts downstream at ymin. Lateral/vertical are the remaining axes in (x, y, z)
    order, each taken as the SMALLER of its open sides.

    A face that sits on the body by design is not an open side: the floor under a body on the
    ground (its vertical is the room ABOVE, which is what the user was asked for: '5 above'),
    and a symmetry plane the body lies on - a half model's lateral is the room on the side away
    from its cut. A direction with no open side at all (a slab, a symmetry plane on each end of
    its sweep) has no margin to judge and reads None. A symmetry plane never excuses the flow
    axis: a half model is cut along the flow, and a plane facing the flow or the wake is wrong
    (evaluate_domain_extents refuses one whatever was requested)."""
    i, up_side, down_side = _flow_sides(flow_axis)
    seats = _symmetry_seats(symmetry_faces)
    seated = {k for k in seats if k[0] != i and _sits_on(box, body, *k)}
    if grounded and i != VERTICAL_AXIS:
        seated.add((VERTICAL_AXIS, "min"))       # the floor: on the body whatever the gap

    def _why(j: int, side: str) -> tuple[str, dict]:
        here = seats.get((j, side))
        if here is not None and j != i:
            return (f" - the body crosses the symmetry plane '{here}' there; a half model lies "
                    "wholly on one side of its cut",
                    {"symmetry_patch": here, "symmetry_face": _face(j, side), "crosses": True})
        unused = [(k, n) for k, n in seats.items() if k not in seated and k[0] != i]
        if unused:
            (uj, us), name = unused[0]
            return (f" - that face is far field, while the symmetry plane '{name}' was put on "
                    f"the {_face(uj, us)} face, which the body does not lie on; a half model's "
                    "symmetry plane has to be the face it was cut on",
                    {"symmetry_patch": name, "symmetry_face": _face(uj, us)})
        return "", {}

    def _room(j: int, sides: tuple[str, ...]) -> _Room | None:
        open_sides = [(_gap(box, body, j, s) / r, s) for s in sides if (j, s) not in seated]
        if not open_sides:
            return None
        g, s = min(open_sides)
        why, facts = _why(j, s) if g <= 0 else ("", {})
        return _Room(g, _face(j, s), why, facts)

    rest = [j for j in range(3) if j != i]
    return {"upstream": _room(i, (up_side,)), "downstream": _room(i, (down_side,)),
            "lateral": _room(rest[0], _SIDES), "vertical": _room(rest[1], _SIDES)}


def evaluate_domain_extents(requested: dict | None, reference_length_m: float | None,
                            manifest: dict, tol: float = 0.15,
                            flow_axis: str | None = None,
                            grounded: bool | None = None,
                            symmetry_faces: list | None = None) -> ExtentVerdict:
    # A body on the ground is read off the mesh that was built (its manifest carries the ground
    # wall), unless the caller says. The floor touches the body on purpose; judging the gap
    # under it would block every grounded mesh as "the domain box touches the body".
    if grounded is None:
        grounded = manifest_is_grounded(manifest)
    # The same for a symmetry plane: a half model lies on its cut, and judging the gap there
    # blocked every half model (job 011e1fe0, the CRM high-lift airliner: "the box touches the
    # body on the lateral side", on both attempts, before a single mesh).
    if symmetry_faces is None:
        symmetry_faces = manifest_symmetry_faces(manifest)
    if (not isinstance(requested, dict)
            or not any(v is not None for v in requested.values())
            or not reference_length_m):
        return ExtentVerdict("na", [], "")
    geom = (manifest or {}).get("geometry") or {}
    box = geom.get("domain_box") or geom.get("box")
    body = geom.get("body_bbox") or geom.get("body_box")
    if not isinstance(box, dict) or not isinstance(body, dict):
        return ExtentVerdict(
            "unmeasured", [],
            "[DOMAIN_EXTENT_UNMEASURED] the request declares far-field extents but the mesh "
            "manifest records no measurable domain/body box - an unmeasured requirement can "
            "neither pass nor be delivered with a caveat")
    try:
        r = float(reference_length_m)
        if r <= 0:
            raise ValueError("nonpositive ruler")
        margins = _axis_margins(box, body, r, flow_axis, grounded=bool(grounded),
                                symmetry_faces=symmetry_faces)
    except Exception:  # noqa: BLE001 - no measurable box/body: honesty demands "unmeasured"
        return ExtentVerdict(
            "unmeasured", [],
            "[DOMAIN_EXTENT_UNMEASURED] the request declares far-field extents but the mesh "
            "manifest records no measurable domain/body box - an unmeasured requirement can "
            "neither pass nor be delivered with a caveat")

    caveats: list = []
    blocks: list = []
    misses: list = []
    # A symmetry plane on the inflow or the outflow face is refused whatever was requested: a
    # half model is cut along the flow, and a plane across it leaves no far field on that side.
    i, up_side, down_side = _flow_sides(flow_axis)
    across: set[str] = set()
    for k, side in (("upstream", up_side), ("downstream", down_side)):
        name = _symmetry_seats(symmetry_faces).get((i, side))
        if name is None:
            continue
        across.add(k)
        room, f = margins[k], _face(i, side)
        blocks.append(f"{k}: the symmetry plane '{name}' is the {f} face, across the flow - a "
                      "half model is cut along the flow, never across it")
        misses.append({"direction": k, "requested": requested.get(k),
                       "measured": round(room.margin, 4) if room is not None else None,
                       "face": f, "symmetry_patch": name, "symmetry_face": f})
    for k in _ALL_DIRECTIONS:
        rv = requested.get(k)
        if rv is None or k in across:
            continue
        rv = float(rv)
        room = margins[k]
        if room is None:
            continue    # a symmetry plane on each side, both on the body: a slab has no margin here
        mv = room.margin
        if mv <= 0:
            blocks.append(f"{k}: the domain box touches or clips the body on its {room.face} "
                          f"face{room.why}")
            misses.append({"direction": k, "requested": rv, "measured": round(mv, 4),
                           "face": room.face, **room.facts})
            continue
        if mv >= rv * (1.0 - tol):
            continue                        # within tolerance, or over-delivered: never a defect
        misses.append({"direction": k, "requested": rv, "measured": round(mv, 4)})
        if mv < rv * FLOOR_FRACTION:
            blocks.append(f"{k}: requested {rv:g}L, mesh has {mv:.3g}L - under half of what "
                          "was asked, which is a different domain, not a near-miss")
        else:
            caveats.append({"direction": k, "requested": rv, "measured": round(mv, 4),
                            "units": "reference_lengths", "ruler_m": r,
                            "ruler_source": "user_stated"})
    if blocks:
        placed = any(m.get("symmetry_patch") for m in misses)
        return ExtentVerdict(
            "block", [],
            "[DOMAIN_EXTENT_BLOCK] " + "; ".join(blocks)
            + f" (measured in units of the approved reference length {r:.4g} m). "
            + ("The symmetry plane is not where the body was cut, and no far-field margin can "
               "change that - the plane has to be placed on the cut face." if placed else
               "Recompute the far-field box corners so every requested margin is met - do NOT "
               "shrink the request to fit the box."), misses)
    if caveats:
        stated = "; ".join(f"{c['direction']}: requested {c['requested']:g}L, mesh has "
                           f"{c['measured']:g}L" for c in caveats)
        return ExtentVerdict(
            "miss", caveats,
            f"[DOMAIN_EXTENT_MISMATCH] {stated} (tolerance {int(tol * 100)}%, measured in "
            f"units of the approved reference length {r:.4g} m). Rebuild with the box spanning "
            "the requested multiples; if attempts run out, the best quality-passing mesh is "
            "delivered with this miss stated.", misses)
    return ExtentVerdict("pass", [], "")
