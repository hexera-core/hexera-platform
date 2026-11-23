# Responsibility: The staged deterministic healer: ten stages in the cross-vendor order, each one
#                 a TRANSACTION (apply on a fork, judge, keep only if the score did not drop).
# Owns: `Stage`, `STAGES`, `StagedResult`, `run_staged`, `run_stage`, the per-stage defaults.
# Boundaries: no OCCT and no geometry here; every change is a registered op (`ops.REGISTRY`) and
#             every judgement is the checker's (`cheap_verdict`, `score`). Finding WHERE a defect
#             is (sites) is not this module's job: ops that need named targets run only when the
#             caller's `params` name them.
"""The deterministic half of CRS v4.

Order (docs/superpowers/specs/crs-v4/03-healing-research-summary.md): baseline, normalise, edges,
faces, shells, solids, bodies, simplify, condition, finalise. Simplify runs BEFORE condition so the
splits that mesh conditioning adds are not merged away again; `unify_same_domain` therefore keeps
no split edges.

THE TRANSACTION RULE. A stage runs on the workspace it was given (ops return NEW workspaces, so
the input is never touched). The result is judged with `cheap_verdict`; it is kept only if
`score(after) >= score(before)`, otherwise the caller's workspace is returned unchanged and the
stage is listed in `rolled_back`.

Three outcomes are NOT a score drop and are never reported as one:
  * the stage changed nothing (every op declined, or none applied): `skipped`;
  * the stage raised (an op declined mid-chain, a worker failed, an op raised RuntimeError):
    `skipped`, workspace unchanged, reason recorded;
  * the verdict was inconclusive (`InconclusiveVerdict` from `score`, or `CheckerUnavailable`):
    the judgement is retried once, then the stage is listed in `inconclusive`, the workspace is
    unchanged, and the stage is neither kept nor counted as rolled back. An unjudged change is
    never kept, and never blamed on the stage.

Stage defaults (override per stage through `params[<stage>]`):
  normalise  tighten_tolerances(target_rel=1e-5) over the whole part
  edges      fix_poles over every face; `targets` (edge ids) -> rebuild_edge_as_intersection
  faces      to_canonical(tol_mm=0.01) over every face; `rebuild` (face ids) -> rebuild_face
             (nearly vestigial as a self-intersection fixer: it usually declines)
  shells     no solid in the part -> sew(ladder) THEN make_volume as one unit (sew destroys a
             solid, so a sew without make_volume is never left standing); a solid -> orient_and_close.
             `fill_edges` (edge ids) -> fill_loop; `small` (+ `small_width_mm`) -> remove_small
  solids     no solid -> make_volume (the MakerVolume fallback); else orient_and_close
  bodies     two or more solids -> conformalise(mode="fragment")
  simplify   unify_same_domain(scope="all", keep=[])
  condition  split_angle(90 deg), split_closed, split_continuity(order 1) over every face;
             split_by_proximity(target_ratio=0.25) over `face_pairs` when the caller supplies them
  finalise   orient_and_close over every solid
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from meshpipeline.cad.repair_v4 import ops
from meshpipeline.cad.repair_v4.ops import OpDeclined, OpFailed
from meshpipeline.cad.repair_v4.types import StableId
from meshpipeline.cad.repair_v4.verdict.assemble import (
    Declared,
    InconclusiveVerdict,
    cheap_verdict,
    score,
)
from meshpipeline.cad.repair_v4.verdict.faithful import DeclaredIdError, FaithfulReport, check_faithful
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

DEFAULT_OP_TIMEOUT_S = 300.0
#: how many times a judgement that came back inconclusive is attempted before giving up on it
JUDGE_ATTEMPTS = 2

Score = tuple[int, int, float, float, int]

DEFAULTS: dict[str, dict[str, Any]] = {
    "baseline": {},
    "normalise": {"target_rel": 1e-5},
    "edges": {"targets": [], "poles": True},
    "faces": {"tol_mm": 0.01, "rebuild": [], "continuity": "G0"},
    "shells": {"tol_ladder": [0.001, 0.01, 0.05], "fill_edges": [], "small": [],
               "small_width_mm": 0.05, "continuity": "G0"},
    "solids": {},
    "bodies": {"fuzzy_mm": 0.0},
    "simplify": {"scope": "all"},
    "condition": {"angle_deg": 90.0, "closed": True, "continuity": 1, "proximity_ratio": 0.25,
                  "face_pairs": []},
    "finalise": {},
}


class _ChainBroken(Exception):
    """An op that a later op in the same stage depends on did not apply: discard the whole stage."""


class _Steps:
    """What one stage body did: the diffs of the ops that applied, and why the others did not."""

    def __init__(self, pool: WorkerPool, ws: Workspace, timeout_s: float) -> None:
        self.pool, self.ws, self.timeout_s = pool, ws, timeout_s
        self.diffs: list[Diff] = []
        self.notes: list[str] = []

    def ids(self, prefix: str) -> list[StableId]:
        return sorted((s for s in self.ws.ids if s[0] == prefix), key=lambda s: int(s[1:]))

    def apply(self, op: str, targets: list[StableId], params: dict, *, required: bool = False) -> bool:
        """Apply `op`; a decline or failure is noted and (unless `required`) is not an error.
        `required` is for the middle of a chain: the stage is abandoned instead of left half done."""
        try:
            errors = ops.validate_call(op, targets, params, self.ws)
            if errors:
                raise OpDeclined("invalid call: " + "; ".join(errors))
            new, diff = ops.REGISTRY[op].apply(self.pool, self.ws, targets, params,
                                               timeout_s=self.timeout_s)
        except OpDeclined as why:
            self.notes.append(f"{op}: declined: {why.reason}")
        except OpFailed as why:
            self.notes.append(f"{op}: failed: {why}")
        except Exception as why:  # noqa: BLE001 - several ops raise RuntimeError/ValueError; a stage failure, not a crash
            self.notes.append(f"{op}: error: {type(why).__name__}: {why}")
        else:
            declined = diff.params.get("declined")
            if declined is None:
                self.ws = new
                self.diffs.append(diff)
                return True
            # Some ops (conformalise) answer a decline with an unchanged workspace and a Diff
            # carrying `params["declined"]` instead of raising: that is not a change.
            self.notes.append(f"{op}: declined: {declined}")
        if required:
            raise _ChainBroken(self.notes[-1])
        return False


Body = Callable[[_Steps, dict], None]


def _normalise(s: _Steps, p: dict) -> None:
    s.apply("tighten_tolerances", [], {"target_rel": p["target_rel"]})


def _edges(s: _Steps, p: dict) -> None:
    if p["poles"] and s.ids("F"):
        s.apply("fix_poles", s.ids("F"), {})
    if p["targets"]:
        s.apply("rebuild_edge_as_intersection", list(p["targets"]), {})


def _faces(s: _Steps, p: dict) -> None:
    if s.ids("F"):
        s.apply("to_canonical", s.ids("F"), {"tol_mm": p["tol_mm"]})
    for face in p["rebuild"]:
        if face in s.ws.ids:
            s.apply("rebuild_face", [face], {"continuity": p["continuity"]})


def _volume_or_orient(s: _Steps) -> None:
    if s.ids("B"):
        s.apply("orient_and_close", s.ids("B"), {})
    else:
        s.apply("make_volume", [], {})


def _shells(s: _Steps, p: dict) -> None:
    if p["small"]:
        s.apply("remove_small", list(p["small"]), {"max_width_mm": p["small_width_mm"]})
    if p["fill_edges"]:
        s.apply("fill_loop", list(p["fill_edges"]), {"continuity": p["continuity"]})
    if s.ids("B"):
        s.apply("orient_and_close", s.ids("B"), {})
        return
    # No solid: sew the loose faces, then rebuild the solid sew is known to destroy. One unit.
    if s.apply("sew", [], {"tol_ladder": list(p["tol_ladder"])}):
        s.apply("make_volume", [], {}, required=True)


def _solids(s: _Steps, p: dict) -> None:
    _volume_or_orient(s)


def _bodies(s: _Steps, p: dict) -> None:
    solids = s.ids("B")
    if len(solids) >= 2:
        s.apply("conformalise", solids, {"mode": "fragment", "fuzzy_mm": p["fuzzy_mm"]})


def _simplify(s: _Steps, p: dict) -> None:
    # keep=[]: nothing has been split yet, and the condition stage that follows adds its own splits.
    s.apply("unify_same_domain", [], {"scope": p["scope"], "keep": []})


def _condition(s: _Steps, p: dict) -> None:
    faces = s.ids("F")
    if faces:
        s.apply("split_angle", faces, {"max_deg": p["angle_deg"]})
    if p["closed"] and s.ids("F"):
        s.apply("split_closed", s.ids("F"), {})
    if s.ids("F"):
        s.apply("split_continuity", s.ids("F"), {"order": p["continuity"]})
    pairs = [list(x) for x in p["face_pairs"] if all(f in s.ws.ids for f in x)]
    if pairs:
        s.apply("split_by_proximity", [], {"face_pairs": pairs, "target_ratio": p["proximity_ratio"]})
    elif not p["face_pairs"]:
        s.notes.append("split_by_proximity: skipped: no face_pairs (they come from site detection)")


def _finalise(s: _Steps, p: dict) -> None:
    if s.ids("B"):
        s.apply("orient_and_close", s.ids("B"), {})


def _baseline(s: _Steps, p: dict) -> None:
    """No op: the baseline is the verdict of the part as handed in (taken by `run_staged`)."""


@dataclass(frozen=True)
class Stage:
    name: str
    body: Body

    def run(self, pool: WorkerPool, ws: Workspace, params: dict) -> tuple[Workspace, list[Diff]]:
        """Apply the stage's ops to `ws` (untouched) and return the new workspace and the Diffs.
        NOT transactional by itself: `run_staged` / `run_stage` judge and roll back. A chain that
        breaks halfway raises `_ChainBroken`; any other op trouble is just skipped."""
        steps = self.execute(pool, ws, params)
        return steps.ws, steps.diffs

    def execute(self, pool: WorkerPool, ws: Workspace, params: dict) -> _Steps:
        steps = _Steps(pool, ws, float(params.get("timeout_s", DEFAULT_OP_TIMEOUT_S)))
        self.body(steps, {**DEFAULTS[self.name], **params})
        return steps


STAGES: list[Stage] = [
    Stage("baseline", _baseline), Stage("normalise", _normalise), Stage("edges", _edges),
    Stage("faces", _faces), Stage("shells", _shells), Stage("solids", _solids),
    Stage("bodies", _bodies), Stage("simplify", _simplify), Stage("condition", _condition),
    Stage("finalise", _finalise),
]
_BY_NAME = {s.name: s for s in STAGES}


@dataclass
class StagedResult:
    ws: Workspace
    #: stages whose result was judged no worse than before and so now stand
    kept: list[str] = field(default_factory=list)
    #: stages whose result scored LOWER than before; the workspace was left as it was
    rolled_back: list[str] = field(default_factory=list)
    #: stage -> the score tuple it was judged on (the "after" score, even when it was rolled back)
    verdicts: dict[str, tuple] = field(default_factory=dict)
    #: stages that could not be judged (infrastructure), so were neither kept nor blamed
    inconclusive: list[str] = field(default_factory=list)
    #: stage -> why it changed nothing: "no_change" or the first op trouble
    skipped: dict[str, str] = field(default_factory=dict)


# --- judging ------------------------------------------------------------------------------------

_STEPS_DIR: list[str] = []
_STEP_FOR: dict[tuple[str, float], str] = {}


def step_for(pool: WorkerPool, brep: str) -> str:
    """The workspace's BREP as a STEP file (mm), written once per BREP path, for the checker."""
    key = (brep, os.stat(brep).st_mtime)
    if key in _STEP_FOR and os.path.exists(_STEP_FOR[key]):
        return _STEP_FOR[key]
    if not _STEPS_DIR:
        _STEPS_DIR.append(tempfile.mkdtemp(prefix="stages_step_"))
        atexit.register(shutil.rmtree, _STEPS_DIR[0], True)  # the exports are scratch: gone when the process is
    out = os.path.join(_STEPS_DIR[0], f"state_{len(_STEP_FOR)}.step")
    result = pool.call("write_step_xcaf", {"brep": brep, "labels": {}, "units": "mm", "path": out},
                       timeout_s=120)
    if not result.ok:
        raise CheckerUnavailable(f"cannot write {brep!r} as STEP: {result.status}: {result.error}")
    _STEP_FOR[key] = out
    return out


#: what layer 3 always reports when the ORIGINAL has no solid (6 loose faces -> 1 body, no volume)
_SOLIDLESS_OK_REASONS = frozenset({"volume_changed", "body_count_changed"})
_ORIGINAL_FACTS: dict[tuple[str, float], tuple[int, int]] = {}


def original_facts(pool: WorkerPool, brep: str) -> tuple[int, int]:
    """(faces, solids) of the lineage's original BREP, read once per (path, mtime): a reused path whose file was
    rewritten is read again, not served stale."""
    key = (brep, os.stat(brep).st_mtime)
    if key not in _ORIGINAL_FACTS:
        result = pool.call("ws_index", {"brep_path": brep}, timeout_s=120)
        if not result.ok or result.value is None:
            raise CheckerUnavailable(f"cannot index {brep!r}: {result.status}: {result.error}")
        entities = result.value["entities"]
        faces = sorted(int(e["index"]) for e in entities if e["type"] == "face")
        if faces != list(range(1, len(faces) + 1)):
            raise CheckerUnavailable(f"face indices of {brep!r} are not 1..n")
        _ORIGINAL_FACTS[key] = (len(faces), sum(1 for e in entities if e["type"] == "solid"))
    return _ORIGINAL_FACTS[key]


def _successors(ws: Workspace, sid: StableId, after: int) -> list[StableId]:
    """The live ids `sid` became (itself if live), following Modified links from diff `after` on."""
    if sid in ws.ids:
        return [sid]
    for k in range(after, len(ws.history)):
        if sid in ws.history[k].modified:
            return [t for kid in ws.history[k].modified[sid] for t in _successors(ws, kid, k + 1)]
    return []


def declared_edits(ws: Workspace, original_faces: int) -> Declared:
    """The edits the workspace's ops DECLARED, as the checker wants them: `F<k>` is a face index of
    the BREP it reads. Added faces are indexed in the repaired shape (`ws.indices`, following
    Modified links to the id that now lives); removed faces in the ORIGINAL, where an id's number
    is its index (ids are minted in index order and never reused, so a number above the original's
    face count was minted later and has no original face to exempt)."""
    added: list[str] = []
    removed: list[str] = []
    for k, diff in enumerate(ws.history):
        for sid in diff.declared_added:
            for live in _successors(ws, sid, k + 1):
                if live[0] == "F" and f"F{ws.indices[live]}" not in added:
                    added.append(f"F{ws.indices[live]}")
        for sid in diff.declared_removed:
            if sid[0] == "F" and sid not in ws.ids and int(sid[1:]) <= original_faces \
                    and f"F{int(sid[1:])}" not in removed:
                removed.append(f"F{int(sid[1:])}")
    return Declared(added=added, removed=removed)


def _judge(pool: WorkerPool, ws: Workspace) -> Score:
    """`score(cheap_verdict(...))` of `ws` against the lineage's original. Raises
    `InconclusiveVerdict` / `CheckerUnavailable` when the checker could not say.

    Two things `cheap_verdict` cannot know are added here:
      * the edits the ops DECLARED (fill_loop, remove_small, refit_cluster add and remove surface on
        purpose); without them layer 3 calls every such edit unfaithful and the op is rolled back
        every time. Layer 3 is then re-run on the BREPs, where the declared indices are exact.
      * an original with NO SOLID: layer 3 then always reports volume_changed/body_count_changed,
        and `valid.ok` lives only in score key 0, so key 0 is replaced by a local gate: valid AND
        every layer-3 reason is one of those two (area and deviation reasons are not)."""
    faces, solids = original_facts(pool, ws.original)
    v = cheap_verdict(pool, step_for(pool, ws.brep_path), step_for(pool, ws.original))
    declared = declared_edits(ws, faces)
    if v.valid.ok and (declared.added or declared.removed):
        report: FaithfulReport | None
        try:
            report = check_faithful(pool, ws.original, ws.brep_path, declared_added=declared.added,
                                    declared_removed=declared.removed)
        except DeclaredIdError:
            report = v.faithful  # a mistranslated id must not crash a run; judge it undeclared
        v = replace(v, faithful=report)
    result = score(v)
    if solids == 0:
        gate = bool(v.valid.ok and v.faithful is not None
                    and set(v.faithful.reasons) <= _SOLIDLESS_OK_REASONS)
        result = (int(gate), *result[1:])
    return result


def _judged(pool: WorkerPool, ws: Workspace) -> Score | None:
    """`_judge`, retried on an inconclusive verdict; None when it stays inconclusive."""
    for _ in range(JUDGE_ATTEMPTS):
        try:
            return _judge(pool, ws)
        except (InconclusiveVerdict, CheckerUnavailable):
            continue
    return None


def _transact(pool: WorkerPool, ws: Workspace, stage: Stage, params: dict, before: Score | None,
              out: StagedResult) -> tuple[Workspace, Score | None]:
    """One stage as a transaction; returns the workspace that stands and its score."""
    name = stage.name
    if name == "baseline":
        score_now = before if before is not None else _judged(pool, ws)
        if score_now is None:
            out.inconclusive.append(name)
        else:
            out.kept.append(name)
            out.verdicts[name] = score_now
        return ws, score_now
    try:
        steps = stage.execute(pool, ws, params)
    except Exception as why:  # noqa: BLE001 - an unexpected exception is a failed stage, never a crash
        out.skipped[name] = f"{type(why).__name__}: {why}"
        return ws, before
    if not steps.diffs:
        out.skipped[name] = steps.notes[0] if steps.notes else "no_change"
        return ws, before
    if before is None:
        before = _judged(pool, ws)  # the earlier judgement of the standing state was inconclusive
    after = _judged(pool, steps.ws)
    if before is None or after is None:
        out.inconclusive.append(name)
        return ws, before
    out.verdicts[name] = after
    if after >= before:
        out.kept.append(name)
        return steps.ws, after
    out.rolled_back.append(name)
    return ws, before


def run_staged(pool: WorkerPool, ws: Workspace, *, params: dict[str, dict] | None = None) -> StagedResult:
    """Every stage in `STAGES` order, each a transaction; `ws` itself is never modified."""
    params = params or {}
    unknown = sorted(set(params) - set(_BY_NAME))
    if unknown:
        raise ValueError(f"unknown stage(s) {unknown}; known: {[s.name for s in STAGES]}")
    result = StagedResult(ws=ws)
    current, standing = ws, None
    for stage in STAGES:
        current, standing = _transact(pool, current, stage, params.get(stage.name, {}), standing, result)
        result.ws = current
        if stage.name == "baseline" and "baseline" in result.inconclusive:
            # A dead checker must not look like a healer that found nothing to do.
            raise CheckerUnavailable("the baseline state could not be judged; no stage can be "
                                     "kept or rolled back without a verdict")
    return result


def run_stage(pool: WorkerPool, ws: Workspace, name: str, params: dict | None = None) -> StagedResult:
    """One stage as a transaction: the search's macro action. `params` are THAT stage's."""
    if name not in _BY_NAME:
        raise ValueError(f"unknown stage {name!r}; known: {[s.name for s in STAGES]}")
    result = StagedResult(ws=ws)
    result.ws, _ = _transact(pool, ws, _BY_NAME[name], params or {}, None, result)
    return result


__all__ = ["DEFAULTS", "STAGES", "Stage", "StagedResult", "run_stage", "run_staged"]
