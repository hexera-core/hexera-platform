# Responsibility: Say what a part would cost a mesher - cheap, standard, heavy, or unknown - from
#                 the kind of mesh failure tier 2 reported, the stage it was refused in, and the
#                 part's face count.
# Owns: the tier names, the seven ordered rules and their names, and the corpus figures the rules
#       were calibrated on (CITED).
# Boundaries: pure. It runs no mesher, reads no shape, touches no settings and decides nothing: the
#             estimate is recorded beside a verdict, and no branch reads it until Task 8's
#             investigator budget does. It never raises.
# Collaborates with: cad/repair/mesh_probe.py (the failure kinds, stages and mesh defect codes it
#                    reads), cad/repair/goal.py (which records `record(...)` on its verdict).
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass

from meshpipeline.cad.repair.contracts import DefectSeverity
from meshpipeline.cad.repair.mesh_probe import (
    EDGE_THROUGH_FACE,
    MESH_CODES,
    NO_VOLUME,
    OPEN_LOOP,
    OTHER,
    OVERLAPPING_FACETS,
    PERIODIC_SURFACE,
    PROBE_ERROR,
    STAGE_2D,
    STAGE_3D,
    TIMEOUT,
)

#: Tier 2's own seconds: gmsh refused in the surface stage, or meshed, on a modest part.
CHEAP = "cheap"
#: An ordinary mesh job: gmsh built the whole volume before it refused.
STANDARD = "standard"
#: A part a mesher would grind on or escalate: it never answered, or it is very large.
HEAVY = "heavy"
#: Not enough is known to say (no face count).
UNKNOWN = "unknown"

#: A crashed child is the harvest's name for a probe that died without answering.
CRASH = "crash"

#: Faces above which a part is heavy whatever its failure: ~7x the overlap family's median (139)
#: and ~2.3x the periodic family's (439), the largest family median in the corpus.
HEAVY_FACES = 1000

RULE_NO_ANSWER = "no_answer"
RULE_FACES_UNKNOWN = "faces_unknown"
RULE_MANY_FACES = "many_faces"
RULE_VOLUME_REFUSAL = "volume_refusal"
RULE_SURFACE_REFUSAL = "surface_refusal"
RULE_MESHED = "meshed"
RULE_DEFAULT = "default"
RULES = frozenset({RULE_NO_ANSWER, RULE_FACES_UNKNOWN, RULE_MANY_FACES, RULE_VOLUME_REFUSAL,
                   RULE_SURFACE_REFUSAL, RULE_MESHED, RULE_DEFAULT})

_NO_ANSWER_KINDS = frozenset({TIMEOUT, CRASH, PROBE_ERROR})
_VOLUME_KINDS = frozenset({OVERLAPPING_FACETS, EDGE_THROUGH_FACE, OTHER})
_SURFACE_KINDS = frozenset({OPEN_LOOP, PERIODIC_SURFACE})

#: The figures the rules rest on, as literals. Cited from
#: devtools/validation/evidence/2026-11-19-corpus-harvest/aggregate.json (`all.families`): 9,956
#: real mesh failures, diag/40, 120 s per file. tests/unit/cad/test_repair_grind.py holds them
#: equal to that file.
CITED: dict[str, dict] = {
    "crash": {"total": 45, "stage": {"2d": 33, "3d": 12, "unknown": 0},
              "solids": {"single": 0, "multi": 0, "unknown": 45}, "median_faces": None},
    "edge_through_face": {"total": 2376, "stage": {"2d": 0, "3d": 2349, "unknown": 27},
                          "solids": {"single": 982, "multi": 1394, "unknown": 0},
                          "median_faces": 173.0},
    "no_volume": {"total": 168, "stage": {"2d": 0, "3d": 167, "unknown": 1},
                  "solids": {"single": 162, "multi": 6, "unknown": 0}, "median_faces": 21.5},
    "open_loop": {"total": 227, "stage": {"2d": 223, "3d": 0, "unknown": 4},
                  "solids": {"single": 94, "multi": 133, "unknown": 0}, "median_faces": 138},
    "other": {"total": 288, "stage": {"2d": 9, "3d": 127, "unknown": 152},
              "solids": {"single": 51, "multi": 90, "unknown": 147}, "median_faces": 150},
    "overlapping_facets": {"total": 5871, "stage": {"2d": 0, "3d": 5840, "unknown": 31},
                           "solids": {"single": 2230, "multi": 3641, "unknown": 0},
                           "median_faces": 139},
    "periodic_surface": {"total": 394, "stage": {"2d": 390, "3d": 1, "unknown": 3},
                         "solids": {"single": 133, "multi": 261, "unknown": 0},
                         "median_faces": 439.0},
    "timeout": {"total": 587, "stage": {"2d": 386, "3d": 86, "unknown": 115},
                "solids": {"single": 0, "multi": 0, "unknown": 587}, "median_faces": None},
}


@dataclass(frozen=True, slots=True)
class CostEstimate:
    tier: str
    rule: str


def _count(value: object) -> int | None:
    """A face count, or None for anything that is not one (bools included)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _predict(kind: str, stage: str, faces: int | None) -> CostEstimate:
    if kind in _NO_ANSWER_KINDS:
        # 632 of 9,956 (587 timeouts, 45 crashes) never answered within 120 s
        return CostEstimate(HEAVY, RULE_NO_ANSWER)
    if faces is None:
        return CostEstimate(UNKNOWN, RULE_FACES_UNKNOWN)
    if faces > HEAVY_FACES:
        return CostEstimate(HEAVY, RULE_MANY_FACES)
    if kind in _VOLUME_KINDS and stage == STAGE_3D:
        # 5,840/5,871 overlaps and 2,349/2,376 edge-through-face were refused in the volume stage
        return CostEstimate(STANDARD, RULE_VOLUME_REFUSAL)
    if (kind in _SURFACE_KINDS and stage == STAGE_2D) or kind == NO_VOLUME:
        # 223/227 open loops and 390/394 periodic were refused in 2D; no_volume: median 21.5 faces
        return CostEstimate(CHEAP, RULE_SURFACE_REFUSAL)
    if kind == "":
        return CostEstimate(CHEAP, RULE_MESHED)
    return CostEstimate(STANDARD, RULE_DEFAULT)


def predict(kind: str, stage: str, *, solids: int | None, faces: int | None) -> CostEstimate:
    """The expected mesh cost of a part tier 2 judged. Pure; never raises.

    `kind` is the mesh failure kind (`""` when tier 2 meshed), `stage` the stage it was refused in.
    The rules apply first to last and the estimate names the one that fired. `solids` is accepted
    for the verdict's shape and later rules; no rule reads it yet.
    """
    del solids
    try:
        return _predict(kind if isinstance(kind, str) else "",
                        stage if isinstance(stage, str) else "", _count(faces))
    except Exception:  # noqa: BLE001 - a cost estimate must never break the verdict it sits on
        return CostEstimate(UNKNOWN, RULE_FACES_UNKNOWN)


def record(kind: str, stage: str, *, solids: int | None, faces: int | None) -> dict[str, str]:
    """The verdict's `"grind"` entry: `{"tier": ..., "rule": ...}`."""
    return asdict(predict(kind, stage, solids=solids, faces=faces))


_SEVERITY_RANK = {DefectSeverity.info: 0, DefectSeverity.warning: 1, DefectSeverity.error: 2,
                  DefectSeverity.fatal: 3}


def tier_of(defects: Iterable[object], *, solids: int | None, faces: int | None) -> str:
    """The tier of a part from its defects: the most severe mesh-coded one's kind and stage, or a
    meshed part (`""`) when there is none. Ties go to the first listed. Never raises."""
    kind, stage = "", ""
    try:
        best = -2
        for defect in defects or ():
            if getattr(defect, "code", None) not in MESH_CODES:
                continue
            rank = _SEVERITY_RANK.get(getattr(defect, "severity", None), -1)  # type: ignore[arg-type]
            if rank > best:
                best = rank
                measurements = getattr(defect, "measurements", None) or {}
                kind = str(measurements.get("mesh_kind") or "")
                stage = str(measurements.get("stage") or "")
    except Exception:  # noqa: BLE001 - fall through with what was read
        pass
    return predict(kind, stage, solids=solids, faces=faces).tier
