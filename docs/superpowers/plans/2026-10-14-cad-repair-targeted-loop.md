# CAD Repair Targeted Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single whole-shape CAD repair with a loop of small operations that each take target entities, report what the kernel did to those entities, and are kept only when the defect they aimed at is gone.

**Architecture:** A `RepairSession` holds one OpenCASCADE shape in memory. A planner proposes one `(operation, targets)` candidate at a time; the session applies it, checks caps, re-inspects with `localize_scan`, and keeps or rolls back. Entities are named by a bounding-box fingerprint that survives a repair, and each operation returns the kernel's own old→new mapping so "this defect is gone" is a fact rather than a count. Slice 6 fills the planner seat with fixed rules; a model takes that seat in slice 8 behind the same interface.

**Spec:** `docs/superpowers/specs/2026-10-14-cad-repair-targeted-loop-design.md`

> **This file was reconstructed mid-execution.** Tasks 1–9 are complete and committed; the controller destroyed the original plan file with a bad in-place edit while amending Task 10 (see the SDD ledger). Tasks 1–9's requirements remain recoverable from `task-N-brief.md` in this plan's SDD workspace and from the commits themselves, so only the remaining tasks are reproduced here. Nothing about tasks 1–9 changed.

## Global Constraints

- **Every length is millimetres.** OpenCASCADE normalises to mm on STEP transfer. Any payload carrying a position or size states `"unit": "mm"`.
- **The customer's upload is never modified.** Repair writes a new file; the input path is read-only.
- **`CAD_REPAIR_ENABLED` gates everything.** With it false, `repair_step_file` raises `RepairUnavailable` *before any kernel call*.
- **No face removal** unless `CAD_REPAIR_ALLOW_FACE_REMOVAL` is true.
- **`BRepBuilderAPI_Sewing.SetNonManifoldMode(False)`** on every sewing call, without exception.
- **`ShapeFix_*.FixVertexPositionMode = 0`** wherever that mode exists. Topology may be repaired; geometry may not be rewritten.
- **Cumulative caps are measured against the original upload, never against the previous iteration.**
- **OCP imports are function-local.** A module-level `import OCP` breaks the hermetic unit tier, which has no kernel.
- **Every new module starts with the repo's header comment block:** `# Responsibility:`, then `# Owns:` / `# Boundaries:` / `# Collaborates with:`.
- **`from __future__ import annotations`** in every new module.
- **ruff line-length is 120** (E501 is in ruff's ignore list, so long comments are fine). `make lint` and `make typecheck` (a zero-new-error ratchet) must pass.
- Kernel tests live in `tests/native/` behind `pytest.importorskip("OCP.ShapeFix", ...)` and are named `test_native_*` so their basenames do not collide with hermetic counterparts. Hermetic tests live in `tests/unit/cad/` and import no OCP.
- Use `.venv/bin/python`.
- **Do not run `make test-fast`.** On this host its deploy/dev/cfmesh/snapgrid families need docker or absent toolchains and produce ~33 unrelated failures. The proportionate check for this plan is `tests/unit/cad` + `tests/unit/pipeline`.

## Kernel facts established during execution

Each of these cost a blocked task to discover. Do not re-derive them, and do not write a test that contradicts one.

- **`BRepCheck_Analyzer.IsValid()` is TRUE for an open shell.** Validity is never a proxy for "broken" — that is why `localize.py` runs free-bound analysis alongside the status walk. Assert on what `localize_scan` *reports*.
- **A STEP round trip changes topology.** The loose-faces fixture arrives as **6 one-face shells, 0 solids, 6 faces**; the reader wraps each loose face in its own shell. Sewing collapses it to 1 shell keeping all 6 faces, which `_as_solid_if_closed` then promotes to 1 solid.
- **The open-shell fixture is a missing WALL, not a hole.** Its opening spans 0.8165 of the part diagonal, over the 0.75 cap, so `fill_planar_hole` correctly declines it as `too_large`. Sewing has nothing to stitch a missing face to, and `fix_shape_global` does not close an opening. **The loop cannot repair that fixture, and that is correct.**
- **The missing-bore fixture's rims span 0.1596** and are genuinely fillable; one `apply` patches exactly one rim (faces 6→7, `open_shell` count 2→1).
- **`_paired_rims` false-positives readily.** On the loose-faces fixture all 6 loops pair, because opposite faces of a box are planar, parallel and offset along their shared normal — the same signature as a bore. The planner's run-level fill refusal therefore fires on many shapes, not only on bores. Conservative by design.
- **Defect severities:** `open_shell` is `error`; `small_edge` and `small_face` are `warning`. The sliver fixture carries **only warnings**, which is why the loop's stopping rule cannot be a severity test alone (see Task 10).
- **`DefectCode.bad_orientation` is unreachable** — `BRepCheck_Analyzer` never attributes `BadOrientation` to a sub-shape. Its operation was cut; the catalogue is **five** operations.
- **No fixture produces `curve_inconsistency`, `wire_gap` or `duplicate_surface_data`**, so `fix_face_boundary` is unexercised by the synthetic suite. Accepted: those codes are reachable on real translated files, and OCC's own constructors build sound geometry.
- **Operation scopes** are 1 (`fix_small_edges`), 2 (`fix_face_boundary`), 4 (`sew_faces`), 5 (`fill_planar_hole`), 99 (`fix_shape_global`) — distinct, so the scope sort is a total order.

## Review Focus

1. **A defect with no measurable extent** gets an empty fingerprint, is untargetable, and the planner reports skipping it. *(Tasks 2, 5, 8, 9 — done.)*
2. **A shape with zero extent** must not divide by zero; a zero original is "no opinion". *(Tasks 2, 3 — done.)*
3. **A truncated defect scan** is never reported as `resolved`. *(Tasks 8, 10, 12.)*
4. **Two entities sharing one bounding box** are both `ambiguous`, and a verdict about one never claims `basis="kernel"`. *(Tasks 2, 5, 8 — done.)*
5. **A shape with no defects** returns immediately with zero verdicts and does not run `fix_shape_global` "just in case". *(Task 10.)*

---

### Task 10: `loop.py` — the driver and its budget

**Files:**
- Create: `src/meshpipeline/cad/repair/loop.py`
- Test: `tests/unit/cad/test_repair_loop.py`
- Test: `tests/native/test_native_repair_loop.py`
- Modify: `src/meshpipeline/cad/repair/session.py` (only if `current()` is absent)

**Interfaces:**
- Consumes: `session.RepairSession`, `session.OpVerdict`, `session.current()`; `plan.RulesPlanner`, `plan.RunHistory`, `plan.Candidate`; `rcfg.CAD_REPAIR_MAX_OPS`, `rcfg.CAD_REPAIR_STALL_LIMIT`, `rcfg.CAD_REPAIR_TIMEOUT_S`.
- Produces:
  - `LoopBudget(max_ops, stall_limit, timeout_s)` with `LoopBudget.from_settings()`
  - `RepairRun(shape, verdicts, refusals, defects, stopped_because, complete_scan, planner_label, original, final)` with `.resolved` and `.to_dict()`
  - Stop reasons: `RESOLVED`, `NO_CANDIDATE`, `OP_BUDGET`, `TIME_BUDGET`, `STALLED`, `SCAN_INCOMPLETE`
  - `run(shape, *, planner=None, budget=None, tolerance_mm, session=None, clock=time.monotonic) -> RepairRun`
- Tasks 11 and 12 call `run`.

**Why the planner decides whether there is work.** An earlier draft stopped as `resolved` the moment no error-severity defect remained — which made `fix_small_edges` unreachable on the very fixture that proves it works, because a sliver edge is a *warning*. Yet a sliver is exactly what breaks a mesher. So the loop runs while the planner still has something it is willing to try, and severity decides only what the stop is *called* when the planner runs dry.

- [ ] **Step 1: Write the failing hermetic tests**

Create `tests/unit/cad/test_repair_loop.py`:

```python
# Responsibility: Verify the loop stops for the right reason, spends no more than its budget, and tells a caller apart a finished run from one that ran out of room.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import (
    NO_CANDIDATE,
    OP_BUDGET,
    RESOLVED,
    SCAN_INCOMPLETE,
    STALLED,
    TIME_BUDGET,
    LoopBudget,
    run,
)
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.plan import Candidate
from meshpipeline.cad.repair.session import RepairSession

# WHY `stopped_because` IS NOT A DETAIL. "No error defect remains" and "I ran out of operations"
# produce the same shape and mean opposite things to a reviewer, to the corpus, and to a model
# being scored. Every one of these tests is about keeping them apart.

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)


def _defect(*, fingerprint="edge:a", code=DefectCode.small_edge,
            severity=DefectSeverity.error, index=1) -> EntityDefect:
    return EntityDefect(code=code, severity=severity, entity_type="edge", entity_index=index,
                        message="x", fingerprint=fingerprint)


class _Op:
    name = "double"
    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def __init__(self):
        self.calls = 0

    def targets_for(self, defect):
        return (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        self.calls += 1
        return OpOutcome(shape=f"fixed-{self.calls}")


class _Planner:
    """Proposes the op for every defect it is shown, until the ledger bans the pair."""

    label = "double-planner"

    def __init__(self, op):
        self._op = op
        self.calls = 0

    def propose(self, defects, history, *, shape):
        self.calls += 1
        for defect in defects:
            if history.was_ineffective(defect.fingerprint, self._op.name):
                continue
            return Candidate(op=self._op, targets=self._op.targets_for(defect),
                             defect_code=defect.code, defect_fingerprint=defect.fingerprint)
        return None


class _Tireless:
    """Proposes the same pair forever, ignoring the ledger.

    DELIBERATELY UNLIKE THE REAL PLANNER. The budget and stall tests are about the DRIVER's
    arithmetic, and a planner that honours the ineffective-pair ledger runs dry after one
    rejection - which would make those tests assert `no_candidate` and prove nothing about the
    budget. Isolating the two is the point of the double.
    """

    label = "tireless"

    def __init__(self, op):
        self._op = op
        self.calls = 0

    def propose(self, defects, history, *, shape):
        self.calls += 1
        if not defects:
            return None
        return Candidate(op=self._op, targets=self._op.targets_for(defects[0]),
                         defect_code=defects[0].code,
                         defect_fingerprint=defects[0].fingerprint)


def _session(scans):
    queue = list(scans)

    def scanner(_shape, valid=None):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return RepairSession("original", measurer=lambda _s: _SOUND, scanner=scanner)


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)


# THE STOP REASONS


def test_a_shape_with_no_defects_is_resolved_without_trying_anything():
    # IT DOES NOT RUN THE WHOLE-SHAPE FALLBACK "JUST IN CASE". A sound part must come out of the
    # loop untouched - a conservative pass over sound geometry has nothing to fix, and anything it
    # changed would be damage.
    op = _Op()
    result = run(None, planner=_Planner(op), session=_session([((), True)]), tolerance_mm=0.01)

    assert result.stopped_because == RESOLVED and result.resolved
    assert result.verdicts == () and op.calls == 0
    assert result.shape == "original"


def test_a_run_that_cleared_every_error_defect_is_resolved():
    op = _Op()
    result = run(None, planner=_Planner(op),
                 session=_session([((_defect(),), True), ((), True)]), tolerance_mm=0.01)

    assert result.stopped_because == RESOLVED and result.resolved
    assert len(result.verdicts) == 1 and result.verdicts[0].kept


def test_a_warning_present_all_along_does_not_stop_the_run_being_resolved():
    # `resolved` is about ERRORS. A sliver-face warning the repair never claimed is a remark for a
    # person, not a failure. It must be in BOTH scans - a warning that APPEARS after an operation
    # is a new defect and the session rightly rolls that operation back.
    warning = _defect(fingerprint="face:w", code=DefectCode.small_face,
                      severity=DefectSeverity.warning, index=2)
    op = _Op()
    result = run(None, planner=_Planner(op),
                 session=_session([((_defect(), warning), True), ((warning,), True)]),
                 tolerance_mm=0.01)

    assert result.stopped_because == RESOLVED and result.resolved


def test_a_truncated_scan_is_never_reported_as_resolved():
    # A walk that stopped at its budget has a short list because it stopped, not because the part
    # is sound. Calling that resolved is how a repairable defect gets delivered as a clean bill.
    result = run(None, planner=_Planner(_Op()), session=_session([((), False)]),
                 tolerance_mm=0.01)

    assert result.stopped_because == SCAN_INCOMPLETE
    assert not result.resolved
    assert result.verdicts == ()


def test_an_error_the_planner_cannot_address_stops_the_run_as_no_candidate():
    class _Silent:
        label = "silent"

        def propose(self, defects, history, *, shape):
            return None

    result = run(None, planner=_Silent(),
                 session=_session([((_defect(),), True)]), tolerance_mm=0.01)

    assert result.stopped_because == NO_CANDIDATE and not result.resolved
    assert result.verdicts == ()


def test_the_operation_budget_stops_a_run_that_keeps_finding_work():
    op = _Op()
    result = run(None, planner=_Tireless(op),
                 session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=3, stall_limit=99, timeout_s=120.0),
                 tolerance_mm=0.01)

    assert result.stopped_because == OP_BUDGET
    assert len(result.verdicts) == 3


def test_consecutive_rollbacks_stop_the_run_as_stalled():
    op = _Op()
    result = run(None, planner=_Tireless(op),
                 session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=99, stall_limit=2, timeout_s=120.0),
                 tolerance_mm=0.01)

    assert result.stopped_because == STALLED
    assert len(result.verdicts) == 2
    assert all(not v.kept for v in result.verdicts)


def test_the_time_budget_covers_the_whole_loop_not_one_operation():
    # CHANGED MEANING, DELIBERATELY. CAD_REPAIR_TIMEOUT_S used to bound one repair; a loop that
    # applied it per operation could spend six times a job's allowance.
    ticks = iter([0.0, 10.0, 200.0, 300.0, 400.0])
    op = _Op()
    result = run(None, planner=_Tireless(op),
                 session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=99, stall_limit=99, timeout_s=120.0),
                 tolerance_mm=0.01, clock=lambda: next(ticks))

    assert result.stopped_because == TIME_BUDGET


def test_the_budget_reads_the_settings_by_default(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 11)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 4)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 7.5)

    budget = LoopBudget.from_settings()

    assert (budget.max_ops, budget.stall_limit, budget.timeout_s) == (11, 4, 7.5)


# THE LEDGER AND THE RECORD


def test_a_rolled_back_pair_is_fed_back_to_the_planner():
    # THE FEEDBACK HALF OF THE LOOP. Without this the planner proposes the same failing pair until
    # the budget runs out, and nothing in the run has learned anything.
    op = _Op()
    planner = _Planner(op)
    result = run(None, planner=planner, session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=99, stall_limit=99, timeout_s=120.0),
                 tolerance_mm=0.01)

    # One attempt, then the pair is banned and the planner has nothing left.
    assert len(result.verdicts) == 1 and not result.verdicts[0].kept
    assert result.stopped_because == NO_CANDIDATE
    assert planner.calls >= 2


def test_the_run_records_every_attempt_kept_or_not():
    # THE SLICE 8 CORPUS. A rolled-back operation is as much a training example as a kept one -
    # more, arguably - so the log is of attempts, not of successes.
    op = _Op()
    result = run(None, planner=_Tireless(op), session=_session([((_defect(),), True)]),
                 budget=LoopBudget(max_ops=2, stall_limit=99, timeout_s=120.0),
                 tolerance_mm=0.01)

    payload = result.to_dict()
    assert len(payload["operations"]) == 2
    assert payload["stopped_because"] == OP_BUDGET
    assert payload["planner"] == "tireless"
    assert payload["resolved"] is False


def test_the_run_carries_the_planners_refusals_so_a_report_can_state_them():
    class _Refusing:
        label = "refusing"

        def propose(self, defects, history, *, shape):
            history.refuse("edge:a", "paired_rim", "would seal a through-hole")
            return None

    result = run(None, planner=_Refusing(), session=_session([((_defect(),), True)]),
                 tolerance_mm=0.01)

    assert result.refusals and result.refusals[0]["reason"] == "paired_rim"
    assert result.to_dict()["refusals"][0]["detail"] == "would seal a through-hole"


def test_the_final_defect_list_is_what_the_run_ended_with():
    # `edge:left` is in BOTH scans, so it is pre-existing rather than a new defect the session
    # would hold against the operation.
    left = _defect(fingerprint="edge:left", index=9)
    op = _Op()
    result = run(None, planner=_Planner(op),
                 session=_session([((_defect(), left), True), ((left,), True)]),
                 tolerance_mm=0.01)

    assert [d.fingerprint for d in result.defects] == ["edge:left"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_loop.py -q`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'meshpipeline.cad.repair.loop'`.

- [ ] **Step 3: Write the module**

Create `src/meshpipeline/cad/repair/loop.py`:

```python
# Responsibility: Drive the repair - ask the planner for one operation, have the session try it,
#                 and stop when the part is sound or the budget is spent.
# Owns: the budget, the stop reasons, and the record of the whole run.
# Boundaries: it chooses nothing and judges nothing. It reads no file and writes none.
# Collaborates with: cad/repair/plan.py (what to try), cad/repair/session.py (trying it),
#                    settings/cad_repair.py (the budget), cad/repair/conservative.py (its caller).
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import DefectSeverity
from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory
from meshpipeline.cad.repair.session import OpVerdict, RepairSession

logger = logging.getLogger(__name__)

#: Every error defect is gone and the scan that says so was exhaustive.
RESOLVED = "resolved"
#: The planner has nothing left to try - every pair tried, or nothing claims what is left.
NO_CANDIDATE = "no_candidate"
#: `CAD_REPAIR_MAX_OPS` attempts made.
OP_BUDGET = "op_budget"
#: `CAD_REPAIR_TIMEOUT_S` spent on the WHOLE loop.
TIME_BUDGET = "time_budget"
#: `CAD_REPAIR_STALL_LIMIT` consecutive attempts were not kept.
STALLED = "stalled"
#: The defect scan hit its own budget, so the list is partial and no absence can be claimed.
SCAN_INCOMPLETE = "scan_incomplete"

_ERRORS = (DefectSeverity.error, DefectSeverity.fatal)


@dataclass(frozen=True, slots=True)
class LoopBudget:
    """How much a run may spend. Read from settings unless a caller says otherwise."""

    max_ops: int
    stall_limit: int
    timeout_s: float

    @classmethod
    def from_settings(cls) -> LoopBudget:
        return cls(max_ops=int(rcfg.CAD_REPAIR_MAX_OPS),
                   stall_limit=int(rcfg.CAD_REPAIR_STALL_LIMIT),
                   timeout_s=float(rcfg.CAD_REPAIR_TIMEOUT_S))


@dataclass(frozen=True, slots=True)
class RepairRun:
    """What a whole run did, and why it stopped.

    `stopped_because` IS NOT A DETAIL. "No error defect remains" and "I ran out of operations"
    produce the same shape and mean opposite things to a reviewer, to the corpus, and to a model
    being scored. Code downstream must be able to tell them apart, so the reason travels with the
    result rather than being guessed from the defect list.
    """

    shape: object
    verdicts: tuple[OpVerdict, ...]
    refusals: tuple[dict, ...]
    defects: tuple
    stopped_because: str
    complete_scan: bool
    planner_label: str = ""
    original: dict = field(default_factory=dict)
    final: dict = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.stopped_because == RESOLVED

    def to_dict(self) -> dict:
        return {"stopped_because": self.stopped_because, "resolved": self.resolved,
                "complete_scan": bool(self.complete_scan), "planner": self.planner_label,
                "operations": [v.to_dict() for v in self.verdicts],
                "refusals": [dict(r) for r in self.refusals],
                "defects_remaining": [d.to_dict() for d in self.defects],
                "original": dict(self.original), "final": dict(self.final)}


def _errors_remain(session: RepairSession) -> bool:
    """Is any defect left that the kernel calls an error or worse?"""
    return any(d.severity in _ERRORS for d in session.defects())


def run(shape, *, planner=None, budget: LoopBudget | None = None, tolerance_mm: float,
        session: RepairSession | None = None, clock=time.monotonic) -> RepairRun:
    """Repair `shape` by trying one operation at a time, and report everything that happened.

    `session` and `clock` are injectable so the driver's budget arithmetic can be tested without
    a kernel and without waiting.

    WHY THE PLANNER DECIDES WHETHER THERE IS WORK, rather than a severity test on the defect list.
    An earlier version stopped as `resolved` the moment no ERROR-severity defect remained - which
    made `fix_small_edges` unreachable on the very fixture that proves it works, because a sliver
    edge is reported as a WARNING. Yet a sliver is exactly what breaks a mesher: it forces cells
    orders of magnitude smaller than the part. So the loop runs while the planner still has
    something it is willing to try, and severity decides only what the STOP IS CALLED when the
    planner runs dry: `resolved` if nothing at error severity is left, `no_candidate` if something
    is. A sound part still produces zero verdicts, because the planner proposes nothing for it.
    """
    chosen = planner if planner is not None else RulesPlanner()
    limits = budget if budget is not None else LoopBudget.from_settings()
    work = session if session is not None else RepairSession(shape)
    history = RunHistory()
    started = clock()

    original = work.original.to_dict()

    # THE FIRST SCAN HAPPENS HERE, and its completeness is read AFTER it. `complete_scan` starts
    # out True, so asking before any scan has run reports a truncated walk as exhaustive.
    work.defects()
    if not work.complete_scan:
        # A SHORT LIST FROM A STOPPED WALK IS NOT AN ABSENCE OF DEFECTS. Reporting it as resolved
        # is how a repairable defect gets delivered as a clean bill of health.
        return RepairRun(shape=work.shape, verdicts=(), refusals=tuple(history.refusals),
                         defects=work.defects(), stopped_because=SCAN_INCOMPLETE,
                         complete_scan=False, planner_label=getattr(chosen, "label", ""),
                         original=original, final=original)

    consecutive_misses = 0
    stopped = NO_CANDIDATE
    while True:
        if len(work.log) >= limits.max_ops:
            stopped = OP_BUDGET
            break
        if clock() - started >= limits.timeout_s:
            # THE WHOLE LOOP, not one operation. Applied per operation, a six-operation run could
            # spend six times a job's allowance before admission has judged the original at all.
            stopped = TIME_BUDGET
            break

        candidate = chosen.propose(work.defects(), history, shape=work.shape)
        if candidate is None:
            stopped = RESOLVED if not _errors_remain(work) else NO_CANDIDATE
            break

        verdict = work.try_op(candidate.op, candidate.targets, candidate.defect_code,
                              tolerance_mm=tolerance_mm)
        if verdict.kept:
            consecutive_misses = 0
            continue

        # THE FEEDBACK. Without this the planner proposes the same failing pair until the budget
        # runs out, and nothing in the run has learned anything. In slice 8 this ledger is exactly
        # what the model is shown on its next turn.
        history.mark_ineffective(candidate.defect_fingerprint, candidate.op.name)
        consecutive_misses += 1
        if consecutive_misses >= limits.stall_limit:
            stopped = STALLED
            break

    logger.info("repair loop: stopped=%s attempts=%d kept=%d planner=%s",
                stopped, len(work.log), sum(1 for v in work.log if v.kept),
                getattr(chosen, "label", ""))
    return RepairRun(shape=work.shape, verdicts=tuple(work.log),
                     refusals=tuple(history.refusals), defects=work.defects(),
                     stopped_because=stopped, complete_scan=work.complete_scan,
                     planner_label=getattr(chosen, "label", ""),
                     original=original, final=work.current().to_dict())
```

`RepairSession.current()` is used for the final measurement. If it is absent from `src/meshpipeline/cad/repair/session.py`, add it:

```python
    def current(self):
        """The current shape's measurement. Taken fresh: the caller wants the run's end state."""
        return self._measure(self.shape)
```

- [ ] **Step 4: Run the hermetic tests**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_loop.py -q`
Expected: PASS, 13 tests.

- [ ] **Step 5: Write and run the native tests**

Create `tests/native/test_native_repair_loop.py`:

```python
# Responsibility: Verify the loop repairs real broken geometry end to end, leaves alone what it cannot safely repair, and never seals a bore.
from __future__ import annotations

import pytest
from tests.cad_fixtures import (
    write_loose_faces_step,
    write_missing_bore_wall_step,
    write_open_shell_step,
    write_small_edge_step,
    write_step,
)

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.loop import RESOLVED, run

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def _attempts(result):
    return [(v.op, v.status, v.reason) for v in result.verdicts]


def test_a_sound_part_comes_out_of_the_loop_untouched(tmp_path):
    from meshpipeline.cad.repair.caps import measure

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    before = measure(shape)

    result = run(shape, tolerance_mm=0.01)

    assert result.stopped_because == RESOLVED
    assert result.verdicts == (), f"a sound part had operations run on it: {_attempts(result)}"
    assert measure(result.shape).to_dict() == before.to_dict()


def test_the_loop_repairs_the_sliver_edge_fixture(tmp_path):
    # THE CLEANEST END-TO-END SUCCESS: a located sliver, a scoped operation aimed at it, and a
    # re-inspection confirming THAT defect is gone.
    #
    # Note the fixture carries only WARNINGS (small_edge, small_face). This is the test that
    # catches a stopping rule which ignores warnings - an earlier draft returned `resolved` here
    # with zero operations, leaving the sliver that breaks the mesher in place.
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.localize import localize_scan

    shape = _read(write_small_edge_step(tmp_path / "sliver.step"))
    before = [d for d in localize_scan(shape)[0] if d.code is DefectCode.small_edge]
    assert before, "the fixture carries no sliver"

    result = run(shape, tolerance_mm=0.02)

    assert any(v.op == "fix_small_edges" and v.kept for v in result.verdicts), \
        f"the sliver was not repaired; stopped {result.stopped_because}, {_attempts(result)}"
    left = [d for d in result.defects if d.code is DefectCode.small_edge]
    assert len(left) < len(before), f"{len(before)} slivers before, {len(left)} after"


def test_the_loop_sews_the_loose_faces_fixture(tmp_path):
    from meshpipeline.cad.repair.caps import measure

    shape = _read(write_loose_faces_step(tmp_path / "loose.step"))
    before = measure(shape)
    # The reader hands back six one-face shells and no solid; sewing collapses them to one shell,
    # which `_as_solid_if_closed` then promotes.
    assert before.shells == 6 and before.solids == 0

    result = run(shape, tolerance_mm=0.02)

    after = measure(result.shape)
    assert after.shells == 1 and after.solids == 1, \
        f"nothing was joined; stopped {result.stopped_because}, {_attempts(result)}"
    assert any(v.op == "sew_faces" and v.kept for v in result.verdicts)


def test_the_loop_leaves_a_missing_wall_alone_and_says_why(tmp_path):
    # AN HONEST NON-REPAIR, and the loop must be able to produce one. The open-shell fixture is a
    # box with a whole face gone: filling declines it as a missing wall (0.8165 of the diagonal,
    # over the 0.75 cap), sewing has nothing to stitch a missing face to, and the whole-shape
    # fallback does not close an opening either. What the loop owes the operator is a clear
    # reason rather than a silent no-op or a flat lid.
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.localize import localize_scan

    shape = _read(write_open_shell_step(tmp_path / "holed.step"))
    before = measure(shape)
    assert [d for d in localize_scan(shape)[0] if d.code is DefectCode.open_shell]

    result = run(shape, tolerance_mm=0.02)

    assert [d for d in result.defects if d.code is DefectCode.open_shell]
    assert not result.resolved
    assert result.stopped_because in ("no_candidate", "stalled", "op_budget")
    # NOTHING WAS LIDDED. A patch here would be a different part handed back to the customer.
    assert measure(result.shape).faces == before.faces, "a missing wall was lidded over"


def test_the_loop_never_seals_a_missing_bore_wall(tmp_path):
    # THE SAFETY CLAIM OF THE WHOLE SLICE, end to end. The plate must still have its bolt hole.
    from meshpipeline.cad.repair.caps import measure

    shape = _read(write_missing_bore_wall_step(tmp_path / "bore.step"))
    before = measure(shape)

    result = run(shape, tolerance_mm=0.02)

    assert not any(v.op == "fill_planar_hole" and v.kept for v in result.verdicts), \
        f"a rim of the bore was patched: {_attempts(result)}"
    assert any(r["reason"] == "paired_rim" for r in result.refusals), \
        "the refusal reached nothing a report could state"
    after = measure(result.shape)
    # Volume rising by the bore's own volume would mean the hole was sealed. The bore is 8 mm
    # radius through 10 mm of plate: about 2010 mm3, far above any accounted residual.
    if before.volume_mm3 > 0:
        assert after.volume_mm3 == pytest.approx(before.volume_mm3, rel=0.05)


def test_the_loop_is_not_worse_than_the_single_whole_shape_pass_it_replaces(tmp_path):
    # THE FLOOR. If the whole-shape fallback alone improves a file, the loop must not do worse.
    # Measured on the SLIVER fixture: neither approach repairs a missing wall, so comparing them
    # there proves nothing.
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.operations import FixShapeGlobal

    shape = _read(write_small_edge_step(tmp_path / "sliver.step"))
    single_shape = FixShapeGlobal().apply(shape, (), tolerance_mm=0.02).shape
    single = measure(single_shape)
    single_slivers = len([d for d in localize_scan(single_shape)[0]
                          if d.code is DefectCode.small_edge])

    result = run(shape, tolerance_mm=0.02)
    looped = measure(result.shape)
    looped_slivers = len([d for d in result.defects if d.code is DefectCode.small_edge])

    assert looped.faces >= single.faces
    assert looped.solids >= single.solids
    assert looped_slivers <= single_slivers, (
        f"the loop left {looped_slivers} slivers where the single pass left {single_slivers}")
```

Run: `.venv/bin/python -m pytest tests/native/test_native_repair_loop.py -q`
Expected: PASS, 6 tests, none skipped.

- [ ] **Step 6: Lint, typecheck, and commit**

Run: `make lint && make typecheck`
Expected: PASS both.

Run: `.venv/bin/python -m pytest tests/unit/cad tests/unit/pipeline -q`
Expected: only the pre-existing `test_bore_staging` failure.

```bash
git add src/meshpipeline/cad/repair/loop.py src/meshpipeline/cad/repair/session.py \
        tests/unit/cad/test_repair_loop.py tests/native/test_native_repair_loop.py
git commit -m "The repair runs as a loop with a budget, and says why it stopped"
```

---
