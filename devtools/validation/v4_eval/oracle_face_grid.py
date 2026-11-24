"""Lane A's grid extensions for the D21 oracle sweep: FACE ops the det sweep cannot reach, or cannot reach at the
right rung.

Same contract as `oracle_grid.extra_rows`: `oracle.py` appends these candidates to the det sweep's at every state.
Every row is a `search.Candidate` tagged `oracle_face_grid: ...` so the table can attribute it, every size comes from
the ORIGINAL's diagonal rather than the current state's, and a row the det sweep already makes for the same site is
NOT repeated, because the oracle spends a full verdict on each candidate.

WHY FACE OPS, and why these extensions. The pilot found `split_angle` at 60 and at 45 reaching CLEAN IN ONE STEP on
3596803faa, a file every arm fails - an op the det sweep already has, at a rung it already uses, which means the
reason det misses it is not the parameter but WHERE it is allowed to propose it. So the extensions are of two sorts:

1. KIND GATING, the same defect lane C found on the body ops. `DetGenerator._sweep` allows
   `split_angle` only at `proximity` / `self_overlap` / `periodic`, `split_continuity` only at `invalid`,
   `reapproximate` only at four kinds, `unify_same_domain` only at `sliver` (plus A11's named pair at
   `self_overlap`), and `remove_small` only at `sliver` or a coarse site's own soft findings. A mesher failure and a
   layer-1 finding on the same faces merge into ONE site that takes the mesher's kind, so a file whose defect is a
   face can end up at a site kind that forbids the very op that would fix it. Here: every face op is offered at
   every kind, aimed at the faces a member LOCATED.
2. RUNGS AND RANGES PAST DET'S. `split_angle` 30 (the schema's floor) and the wide 120 / 150;
   `split_continuity` at both orders anywhere; `rebuild_face` at G1, which det never proposes and which the op
   reports back as `continuity_achieved`; `reapproximate` from a twentieth of the bound to twice it, at degrees
   3 to 9, where det sweeps two pairs only; `remove_small` at 3e-2 and 1e-1 of the diagonal, past the det ladder's
   1e-2 top rung, AIMED at a located face and never part-wide.

A11's caveat is carried: a `low_quality` location is the worst ELEMENT, so the named face is not necessarily the
defect. Rows are therefore aimed at the located faces AND, where the op takes one face at a time, at each of them
separately, so the table can attribute a win to a particular face rather than to a batch.

WHAT THIS DOES NOT DO: it proposes no body op (lane C's `oracle_grid`), no edge op, and nothing part-wide. A row
needing a size is SKIPPED, with its reason, when no diagonal is known - never sized from a guess.
"""
from __future__ import annotations

from typing import Any

#: `search._SPLIT_ANGLES`, mirrored so det's own rungs are not repeated at the kinds det covers
DET_SPLIT_ANGLES = (90.0, 60.0, 45.0)
DET_SPLIT_KINDS = ("proximity", "self_overlap", "periodic")
#: `search._COARSE_ANGLES`: what a coarse site already gets, part-wide
DET_COARSE_ANGLES = (60.0, 45.0)
#: rungs det has at no kind. 30 is `ops.split.SplitAngle`'s schema floor; the wide two divide only the bluntest
#: joins, which is what a mesher failing on a near-tangent wedge needs.
EXTRA_SPLIT_ANGLES = (30.0, 120.0, 150.0)
#: `search._REAPPROX_SWEEP` (share of the fidelity bound, max degree), mirrored
DET_REAPPROX = ((0.25, 9), (0.5, 6))
#: shares and degrees past it: a twentieth of the bound up to twice it, at the schema's degree floor and ceiling
EXTRA_REAPPROX = ((0.05, 3), (0.05, 9), (0.1, 6), (1.0, 9), (2.0, 9))
#: `search.det_sliver_widths` ratios, mirrored: the oracle must not repeat the det ladder
DET_SLIVER_RATIOS = (1e-4, 1e-3, 1e-2)
#: past it. 1e-1 of the diagonal is a large removal; the oracle's job is to find out whether it ever pays, and the
#: verdict refuses anything that loses layer 1 or a panel member.
EXTRA_SLIVER_RATIOS = (3e-2, 1e-1)
#: the kernel's fidelity bound, min(0.1 mm, 1e-3 x diagonal) (`ops.faces.fidelity_bound`), for the tolerance shares
BOUND_MAX_MM = 0.1
BOUND_DIAGONAL_RATIO = 1e-3
#: faces aimed at per op per site; the oracle pays a verdict for each, so this is a cost cap, not a judgement
MAX_AIMED_FACES = 3
#: `ops.faces._TOL`'s floor: a tolerance under this is not a legal call
MIN_TOL_MM = 1e-6


def _bound(diag_mm: float) -> float:
    return min(BOUND_MAX_MM, BOUND_DIAGONAL_RATIO * diag_mm)


def _faces(site: Any) -> list[str]:
    return [e for e in getattr(site, "entities", None) or () if isinstance(e, str) and e[:1] == "F"]


def _located(site: Any) -> list[str]:
    """The faces a panel member's own located failure named, in the site (`graph["mesher_faces"]`, D16)."""
    graph = getattr(site, "graph", None) or {}
    mine = set(_faces(site))
    return [f for f in graph.get("mesher_faces") or [] if f in mine]


def _aim(site: Any) -> list[str]:
    """What a face op is aimed at: the located faces first, then the site's own, capped."""
    named = _located(site)
    rest = [f for f in _faces(site) if f not in set(named)]
    return (named + rest)[:MAX_AIMED_FACES]


def _freeform(site: Any, faces: list[str]) -> list[str]:
    """The site's free-form faces, by the kernel's own surface names (`search._FREEFORM`)."""
    from meshpipeline.cad.repair_v4.search import _FREEFORM

    graph = getattr(site, "graph", None) or {}
    surface = {n.get("id"): n.get("surface") for n in graph.get("nodes") or []}
    return [f for f in faces if surface.get(f) in _FREEFORM]


def _cand(op: str, targets: list[str], params: dict, why: str) -> Any:
    from meshpipeline.cad.repair_v4.search import Candidate

    return Candidate(op, list(targets), dict(params), "det", f"oracle_face_grid: {why}")


#: why a sized row was not built, appended to rather than printed, so the caller can report it and a skip is never
#: silent (the owner's rule after a glob quietly dropped a file: say what you did not do).
SKIPPED: list[str] = []


def _diagonal_of(site: Any, diagonal_mm: float | None) -> float | None:
    """The ORIGINAL's diagonal in mm, or None when nothing measured one.

    Preference order: what the caller passed (the oracle has a pool and can measure `ws.original`), then the site's
    own `graph["diagonal_mm"]`.
    """
    if diagonal_mm and diagonal_mm > 0:
        return float(diagonal_mm)
    graph = getattr(site, "graph", None) or {}
    d = graph.get("diagonal_mm")
    return float(d) if d else None


def extra_rows(state: Any, diagonal_mm: float | None = None) -> list:
    """Face-op candidates for `state`, det's own rows excluded. `diagonal_mm` is the ORIGINAL's diagonal; without
    one the sized rows (reapproximate, remove_small) are skipped rather than guessed."""
    rows: list = []
    seen: set[tuple] = set()

    def add(op: str, targets: list[str], params: dict, why: str) -> None:
        key = (op, tuple(targets), tuple(sorted((k, repr(v)) for k, v in params.items())))
        if key not in seen:
            seen.add(key)
            rows.append(_cand(op, targets, params, why))

    for site in getattr(state, "sites", None) or []:
        faces = _faces(site)
        if not faces:
            continue
        kind = getattr(site, "kind", "")
        coarse = bool(((getattr(site, "graph", None) or {}).get("coarse") or {}))
        aim = _aim(site)
        named = set(_located(site))
        diag = _diagonal_of(site, diagonal_mm)

        def where(f: str) -> str:
            return "the mesher's own face" if f in named else "a face at the site"

        # --- split_angle: det's rungs at the kinds det forbids, and new rungs everywhere ----------------------
        # det's own rungs are skipped wherever det may propose them at all (its kinds, or any coarse site, where it
        # sweeps 60 and 45 part-wide). Its target list differs from this aim, so these are near-duplicates rather
        # than exact ones - and a near-duplicate still costs a full verdict, so the skip is the cheap side.
        det_may = kind in DET_SPLIT_KINDS or coarse
        for deg in DET_SPLIT_ANGLES:
            if det_may:
                continue
            add("split_angle", aim, {"max_deg": deg},
                f"split at {deg:g} deg on a {kind} site, which det's kind gate forbids "
                f"(the pilot's 3596803faa went clean in one step at 60 and 45)")
        for deg in EXTRA_SPLIT_ANGLES:
            add("split_angle", aim, {"max_deg": deg}, f"split at {deg:g} deg, a rung det has at no kind")

        # --- split_continuity: det proposes it at `invalid` only -----------------------------------------------
        for order in (1, 2):
            if kind == "invalid" or (coarse and order == 2):
                continue
            add("split_continuity", aim, {"order": order},
                f"split at C{order} breaks on a {kind} site (det: `invalid` only)")

        # --- rebuild_face: det is G0 only, located-first, two faces ------------------------------------------
        for f in aim:
            add("rebuild_face", [f], {"continuity": "G1"},
                f"re-fill {f} ({where(f)}) at G1, which det never proposes; the op reports what it achieved")

        # --- unify_same_domain: det scopes it `all` at `sliver`, and A11 aims a named PAIR at self_overlap ----
        if len(aim) >= 2:
            add("unify_same_domain", aim, {"scope": "targets", "keep": []},
                f"merge the {len(aim)} aimed faces among themselves on a {kind} site")
        for f in aim:
            add("unify_same_domain", [f], {"scope": "targets", "keep": []},
                f"merge {f} ({where(f)}) into its own domain, one face at a time")
        if kind != "sliver":
            add("unify_same_domain", [], {"scope": "all", "keep": []},
                f"merge same-domain faces part-wide on a {kind} site (det: `sliver` only)")

        # --- the sized rows ---------------------------------------------------------------------------------
        if diag is None:
            SKIPPED.append(f"site {getattr(site, 'id', '?')} ({kind}): reapproximate and remove_small not built: "
                           "no diagonal was measured for the original, and a size is not guessed")
            continue
        bound = _bound(diag)
        free = _freeform(site, aim)
        for share, degree in EXTRA_REAPPROX:
            tol = share * bound
            if tol < MIN_TOL_MM or not free:
                continue
            add("reapproximate", free, {"tol_mm": tol, "max_degree": degree},
                f"re-approximate {len(free)} free-form face(s) within {share:g} of the bound at degree <= {degree} "
                f"(det sweeps {DET_REAPPROX} only)")
        for share, degree in DET_REAPPROX:
            if free and kind not in ("self_overlap", "pole", "periodic", "proximity"):
                add("reapproximate", free, {"tol_mm": share * bound, "max_degree": degree},
                    f"det's own rung on a {kind} site, which its kind gate forbids")
        for ratio in EXTRA_SLIVER_RATIOS:
            width = ratio * diag
            for f in aim:
                add("remove_small", [f], {"max_width_mm": width},
                    f"remove {f} ({where(f)}) if under {width:.4g} mm, {ratio:g} of the ORIGINAL's diagonal, past "
                    "the det ladder's 1e-2 top rung; aimed at one face, never part-wide")
    return rows
