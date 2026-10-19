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

### Task 11: `repair_step_file` becomes the loop

**Files:**
- Modify: `src/meshpipeline/cad/repair/conservative.py` (`repair_step_file`'s body, and `_refusal` as a shim over `caps`)
- Modify: `tests/unit/cad/test_repair_conservative.py` (append)
- Modify: `tests/native/test_native_cad_repair.py` (append)

**Interfaces:**
- Consumes: `loop.run`, `loop.RESOLVED`, `loop.SCAN_INCOMPLETE`; `caps.measure`, `caps.Measure`, `caps.sanity_refusal`, `caps.cumulative_refusal`.
- Produces: `repair_step_file(source, destination, *, profile=RepairProfile.conservative) -> RepairReport` — **same signature**. `RepairReport.operations` entries gain `targets`, `status`, `reason`, `basis`, `delta`, `evidence`; `RepairReport.entities` becomes the final defect list; `RepairReport.measurements` gains `stopped_because`, `resolved`, `complete_scan`, `planner`, `refusals`, `operations_attempted`, `operations_kept`. The old `holes_filled` / `holes_left` keys are **removed**.
- `RepairRefused.measurements` keeps its shape, so the node's existing handler is untouched.

**This is the task that makes the slice live.** Until now nothing calls the loop; `repair_step_file` still runs the old whole-shape `_fix`. The signature is unchanged on purpose, so `devtools/validation/repair_corpus.py`, the native suite and the pipeline node all keep working, and the risk is confined to one function body.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/cad/test_repair_conservative.py`:

```python
# THE REFUSAL IS NOW THE CAPS MODULE'S, AND MUST NOT HAVE DRIFTED


def test_the_old_refusal_entry_point_still_refuses_everything_it_used_to():
    # `_refusal` is kept as a shim because the native suite and the corpus drive it directly with
    # recorded measurement payloads. If splitting it into sanity and cumulative changed any
    # verdict, that is a silent behaviour change to a safety check.
    assert "no faces" in _refusal(_BEFORE, _after(faces=0))
    assert "solid" in _refusal(_BEFORE, _after(solids=0))
    assert "face" in _refusal(_BEFORE, _after(faces=19))
    assert "tolerance" in _refusal(_BEFORE, _after(max_tolerance_mm=0.5))
    assert "diagonal" in _refusal(_BEFORE, _after(diagonal_mm=201.0))
    assert "invalid" in _refusal(_BEFORE, _after(valid=False))
    assert _refusal(_BEFORE, _BEFORE) == ""


def test_the_shim_tolerates_the_measurement_keys_the_old_report_carried():
    # Recorded pipeline payloads outlive a deploy, and `Measure.from_dict` has to read one written
    # before area and volume were recorded at all.
    sparse = {"valid": True, "diagonal_mm": 200.0, "max_tolerance_mm": 0.01,
              "solids": 1, "shells": 1, "faces": 20, "edges": 60, "vertices": 40}
    assert _refusal(sparse, dict(sparse)) == ""
```

Append to `tests/native/test_native_cad_repair.py`:

```python
def test_the_report_states_what_the_loop_did_and_why_it_stopped(tmp_path, monkeypatch):
    from tests.cad_fixtures import write_loose_faces_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)

    # THE LOOSE-FACES FIXTURE, because it is the one fixture this loop actually repairs.
    source = write_loose_faces_step(tmp_path / "loose.step")
    report = repair_step_file(source, tmp_path / "out.step")

    measured = {m.name: m.value for m in report.measurements}
    # WHY THE LOOP STOPPED IS PART OF THE REPORT. An operator approving a repair needs to know
    # whether it finished or ran out of room, and those look identical without this.
    assert measured["stopped_because"]
    assert isinstance(measured["resolved"], bool)
    assert isinstance(measured["complete_scan"], bool)
    assert measured["planner"] == "rules-v1"
    # EVERY ATTEMPT, KEPT OR NOT.
    assert report.operations, "the report lists no operations"
    for op in report.operations:
        assert op["name"] and op["status"] in ("kept", "rolled_back", "failed")
        assert "targets" in op and "delta" in op and "basis" in op
    assert measured["operations_attempted"] >= measured["operations_kept"] >= 1
    # THE OLD KEYS ARE GONE.
    assert "holes_filled" not in measured and "holes_left" not in measured


def test_the_reports_entities_are_what_is_left_rather_than_what_arrived(tmp_path, monkeypatch):
    from tests.cad_fixtures import write_loose_faces_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import localize_scan

    source = write_loose_faces_step(tmp_path / "loose.step")
    arrived, _ = localize_scan(_read_shape(source))
    assert arrived, "the fixture is not broken"

    report = repair_step_file(source, tmp_path / "out.step")

    # An operator reading "repaired" over the ORIGINAL defect list is how a ruined delivery
    # happens. The delivered report states what remains.
    assert len(report.entities) < len(arrived)


def test_a_summary_never_claims_a_repair_the_run_did_not_achieve(tmp_path, monkeypatch):
    # The sliver fixture ends `stalled` with the sliver still present. The summary must not read
    # as success - `resolved` is False, and the wording has to follow it.
    from tests.cad_fixtures import write_small_edge_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)

    report = repair_step_file(write_small_edge_step(tmp_path / "sliver.step"),
                              tmp_path / "out.step")

    measured = {m.name: m.value for m in report.measurements}
    assert measured["resolved"] is False
    assert "Repaired within the conservative caps." not in report.summary
    assert report.entities, "the remaining defect is not reported"


def test_a_sound_part_reports_resolved_with_no_operations(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)

    report = repair_step_file(write_step(tmp_path / "box.step", "MM"), tmp_path / "out.step")

    measured = {m.name: m.value for m in report.measurements}
    assert measured["resolved"] is True
    assert report.operations == (), "a sound part had operations run on it"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/native/test_native_cad_repair.py -q`
Expected: FAIL — `KeyError: 'stopped_because'`, and `op["status"]` missing.

- [ ] **Step 3: Rewrite `repair_step_file`**

Replace everything in `repair_step_file` from `_occ()` onward with:

```python
    _occ()

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.loop import run as run_loop

    src, dest = Path(source), Path(destination)
    before_shape = _read_shape(src)
    original = measure(before_shape)

    tolerance = min(rcfg.CAD_REPAIR_MAX_TOLERANCE_MM,
                    max(original.diagonal_mm, 1.0) * rcfg.CAD_REPAIR_MAX_DEVIATION_RATIO)

    result = run_loop(before_shape, tolerance_mm=tolerance)
    final = measure(result.shape)

    measurements = {
        "before": original.to_dict(), "after": final.to_dict(),
        "tolerance_requested_mm": tolerance,
        # WHY IT STOPPED, which is not inferable from the shape. "Every error cleared" and "I ran
        # out of operations" look identical in the geometry and mean opposite things to a reviewer.
        "stopped_because": result.stopped_because,
        "resolved": result.resolved,
        "complete_scan": result.complete_scan,
        "planner": result.planner_label,
        # WHAT WAS DELIBERATELY NOT ATTEMPTED AND WHY - the paired rims of a missing bore wall
        # above all. This used to be a silent skip inside the fill.
        "refusals": [dict(r) for r in result.refusals],
        "operations_attempted": len(result.verdicts),
        "operations_kept": sum(1 for v in result.verdicts if v.kept),
    }

    # THE AFTER-CHECKS STILL HAVE THE LAST WORD. The session already refused any individual
    # operation that left the caps, so reaching here with a refusal would mean a bug - but the
    # customer's part is on the other side of this function, so it is checked again on the result
    # rather than trusted from the process that produced it.
    refusal = _refusal(original.to_dict(), final.to_dict())
    if refusal:
        raise RepairRefused(refusal, measurements=measurements)

    _write_step(result.shape, dest)

    defects: tuple[RepairDefect, ...] = ()
    if not final.valid:
        # Not a refusal: the shape improved and is honest about not being fully valid. An operator
        # decides whether it is good enough to mesh, with this on the screen.
        defects = (
            RepairDefect(
                code=DefectCode.invalid_brep,
                severity=DefectSeverity.warning,
                message="The repaired shape is still not fully valid; a person should look at it.",
            ),
        )

    return RepairReport(
        defects=defects,
        measurements=tuple(RepairMeasurement(name=k, value=v) for k, v in measurements.items()),
        # EVERY ATTEMPT, KEPT OR NOT, with what it aimed at and what it measured. A rolled-back
        # operation is evidence an operator wants and the training example a later model planner
        # needs, so the log is of attempts rather than of successes.
        operations=tuple({"name": v.op, "mutated": v.kept, **v.to_dict()}
                         for v in result.verdicts),
        summary=_summary(result, final),
        diagnostics={"profile": profile.value, "source_suffix": src.suffix.lower()},
        # WHAT IS LEFT, not what arrived. An operator reading "repaired" over the original defect
        # list is how a ruined delivery happens.
        entities=tuple(d.to_dict() for d in result.defects),
    )


def _summary(result, final) -> str:
    """One sentence an operator reads first. It must not claim more than the run achieved.

    KEYED ON `resolved`, NOT ON THE STOP REASON. `resolved` already means the loop succeeded -
    complete scan, no error defects, and nothing a failed attempt abandoned - so anything else
    has to read as partial, whatever the loop happened to stop on.
    """
    from meshpipeline.cad.repair.loop import SCAN_INCOMPLETE

    if result.resolved:
        return ("Repaired within the conservative caps."
                if final.valid else
                "Repaired, but the result is not fully valid - needs review.")
    if result.stopped_because == SCAN_INCOMPLETE:
        return ("Repaired as far as the inspection could see; the defect scan was partial, so "
                "this is not a clean bill of health.")
    if not any(v.kept for v in result.verdicts):
        return "Nothing could be repaired safely; a person should look at this."
    return (f"Partially repaired - the run stopped at {result.stopped_because} with "
            f"{len(result.defects)} defect(s) left.")
```

Keep `_CONSERVATIVE_OPERATIONS` — the existing unit test imports it, and it is still the honest statement of what a conservative profile may draw from. Add to its comment:

```python
#: SUPERSEDED AS A RECIPE, KEPT AS A POLICY. The loop (cad/repair/loop.py) chooses which of
#: cad/repair/operations.py's operations to run and in what order, so this tuple no longer
#: describes a sequence. What it still states is the SET a conservative profile may draw from.
```

- [ ] **Step 4: Rewrite `_refusal` as a shim over `caps`**

```python
def _refusal(before: dict, after: dict) -> str:
    """The one place a repair RESULT is judged. Returns the reason, or "" to accept.

    NOW A SHIM over cad/repair/caps.py, which owns the arithmetic and splits it into the
    per-operation and cumulative halves the loop needs. Kept because the native suite and
    `devtools/validation/repair_corpus.py` drive it directly with recorded measurement payloads -
    including ones written before area and volume were recorded at all, which is why
    `Measure.from_dict` tolerates a sparse dict.

    Declares no area or volume: a caller holding only two payloads has no operation evidence to
    account for, so an unexplained change is exactly what it should refuse on.
    """
    from meshpipeline.cad.repair.caps import Measure, cumulative_refusal, sanity_refusal

    was, now = Measure.from_dict(before), Measure.from_dict(after)
    return (sanity_refusal(was, now)
            or cumulative_refusal(was, now, declared_area_mm2=0.0, declared_volume_mm3=0.0))
```

Remove the now-unused `_fix`, `_fill_planar_holes`, `_sew_loose_bodies`, `_sew` and `_paired_rims` **only if nothing else imports them**. `plan.py` imports `_paired_rims` and `operations.py` imports `_as_solid_if_closed` and `_span`, so check each with grep before deleting, and leave anything still referenced.

- [ ] **Step 5: Run every repair test**

Run: `.venv/bin/python -m pytest tests/unit/cad tests/unit/pipeline -q`
Expected: the pre-existing `test_bore_staging` failure only. Any test asserting `measured["operations"]` as a list of names, or `holes_filled`/`holes_left`, needs updating — those keys are gone, replaced by `operations_attempted`/`operations_kept` and the per-operation detail on `report.operations`. Fix them in place and say which in the report.

Run: `.venv/bin/python -m pytest tests/native -k "repair or fixture" -q`
Expected: PASS.

- [ ] **Step 6: Lint, typecheck, and commit**

Run: `make lint && make typecheck`

```bash
git add src/meshpipeline/cad/repair/conservative.py \
        tests/unit/cad/test_repair_conservative.py tests/native/test_native_cad_repair.py
git commit -m "A conservative repair is the loop, and the report says what it tried and what is left"
```

---

### Task 12: The pipeline node records the op sequence

**Files:**
- Modify: `src/meshpipeline/pipeline/repair_attempt.py` (the success return and the training-log call)
- Modify: `src/meshpipeline/contracts/pipeline_state.py` (the `repair_attempt` record's documented shape)
- Modify: `tests/unit/pipeline/test_repair_attempt.py`

**Interfaces:**
- Consumes: `repair_step_file`'s report as reshaped by Task 11 — `measurements` now carries `stopped_because`, `resolved`, `complete_scan`, `planner`, `refusals`, `operations_attempted`, `operations_kept`, `declared_area_mm2`, `declared_volume_mm3`, and **no longer carries `holes_filled` or `holes_left`**.
- Produces: the `repair_attempt` state record gains `operations` (the ordered attempt list), `stopped_because`, `refusals` and `defects_remaining`; `resolved` is taken from the report instead of inferred. `repair_status` stays untouched.

**This task fixes a live bug.** After Task 11 the node still reads the removed `holes_left` key, so `repair_attempt["resolved"]` is true whenever no warning defect exists — *even when the loop reported `resolved=False`* — and its `operations` list silently includes rolled-back attempts. The node's unit tests mock the report, which is why they do not catch it. Nothing may ship in that state.

- [ ] **Step 1: Update the existing harness, then write the failing tests**

`tests/unit/pipeline/test_repair_attempt.py` already has `_state(tmp_path, **over)`, `_run(state)`, `_repaired_report(**measured)` and a `repairs` fixture. **Two of those must change first**, because they still speak the old report's language.

Replace `_repaired_report`:

```python
def _repaired_report(*, operations=(), defects=(), entities=(), **measured) -> RepairReport:
    """The report a looped repair returns. `operations` is the ordered attempt list."""
    return RepairReport(
        defects=tuple(defects),
        measurements=tuple(RepairMeasurement(name=k, value=v) for k, v in measured.items()),
        operations=tuple(operations),
        summary="Repaired within the conservative caps.",
        entities=tuple(entities))
```

and in the `repairs` fixture's `_repair`, return:

```python
        return _repaired_report(
            operations=({"name": "sew_faces", "mutated": True, "status": "kept",
                         "basis": "kernel", "targets": ["edge:aaaa"],
                         "defect_code": "open_shell", "reason": "",
                         "evidence": {}, "delta": {}},),
            stopped_because="resolved", resolved=True, complete_scan=True, refusals=[],
            planner="rules-v1", operations_attempted=1, operations_kept=1,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)
```

Then append the new tests:

```python
# WHAT THE RUN RECORDS ABOUT A LOOPED REPAIR


def _looped(monkeypatch, *, operations=(), entities=(), **measured):
    """Stub `repair_step_file` with a looped report, and stub staging so promotion succeeds."""
    def _repair(source, destination, **kw):
        destination.write_bytes(source.read_bytes() + b"\n/* repaired */\n")
        return _repaired_report(operations=operations, entities=entities, **measured)

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="": (True, ""))


def test_the_run_records_every_operation_the_loop_tried(monkeypatch, tmp_path):
    # THE SLICE 8 CORPUS STARTS HERE. A rolled-back operation is as much a training example as a
    # kept one, so what is recorded is attempts, not successes.
    _looped(monkeypatch,
            operations=({"name": "fix_small_edges", "mutated": True, "status": "kept",
                         "basis": "kernel", "targets": ["edge:aaaa"],
                         "defect_code": "small_edge", "reason": "",
                         "evidence": {}, "delta": {}},
                        {"name": "fill_planar_hole", "mutated": False, "status": "rolled_back",
                         "basis": "kernel", "targets": ["edge:bbbb"],
                         "defect_code": "open_shell", "reason": "target_remains",
                         "evidence": {}, "delta": {}}),
            stopped_because="stalled", resolved=False, complete_scan=True, refusals=[],
            planner="rules-v1", operations_attempted=2, operations_kept=1,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)

    attempt = _run(_state(tmp_path))["repair_attempt"]

    assert [op["name"] for op in attempt["operations"]] == \
           ["fix_small_edges", "fill_planar_hole"]
    assert attempt["operations"][1]["status"] == "rolled_back"
    assert attempt["operations"][1]["reason"] == "target_remains"
    assert attempt["stopped_because"] == "stalled"


def test_resolved_is_taken_from_the_loop_not_inferred(monkeypatch, tmp_path):
    # THE LIVE BUG THIS TASK FIXES. It used to be inferred from `holes_left`, a key the report no
    # longer carries - so `resolved` read True for any run with no warning defect, including a
    # run the loop explicitly reported as a failure.
    _looped(monkeypatch, stopped_because="stalled", resolved=False, complete_scan=True,
            refusals=[], planner="rules-v1", operations_attempted=2, operations_kept=0,
            declared_area_mm2=0.0, declared_volume_mm3=0.0,
            entities=({"code": "small_edge", "severity": "warning", "entity": "edge:9"},))

    attempt = _run(_state(tmp_path))["repair_attempt"]

    assert attempt["resolved"] is False, "the node contradicted the loop"
    assert attempt["defects_remaining"], "what is left was not recorded"


def test_a_run_that_stopped_on_a_partial_scan_is_not_recorded_as_resolved(
        monkeypatch, tmp_path):
    _looped(monkeypatch, stopped_because="scan_incomplete", resolved=False, complete_scan=False,
            refusals=[], planner="rules-v1", operations_attempted=0, operations_kept=0,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)

    attempt = _run(_state(tmp_path))["repair_attempt"]

    assert attempt["resolved"] is False
    assert attempt["stopped_because"] == "scan_incomplete"


def test_a_planner_refusal_reaches_the_run_so_an_operator_can_read_it(monkeypatch, tmp_path):
    # The paired-rim case. A silent skip is how "we chose not to seal your bolt hole" stops being
    # something anybody can see.
    _looped(monkeypatch, stopped_because="no_candidate", resolved=False, complete_scan=True,
            refusals=[{"defect": "edge:rim", "reason": "paired_rim",
                       "detail": "would seal a through-hole"}],
            planner="rules-v1", operations_attempted=0, operations_kept=0,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)

    refusals = _run(_state(tmp_path))["repair_attempt"]["refusals"]

    assert refusals[0]["reason"] == "paired_rim"
    assert refusals[0]["detail"] == "would seal a through-hole"


def test_the_inspections_verdict_on_the_customers_file_is_still_not_overwritten(
        monkeypatch, tmp_path):
    # `repair_status` is the inspection's verdict on the file the customer SENT, and
    # `repair_report` beside it is that inspection. Overwriting it with the repair's own verdict
    # produced a pairing that read "repaired" next to the original defect list.
    _looped(monkeypatch, stopped_because="resolved", resolved=True, complete_scan=True,
            refusals=[], planner="rules-v1", operations_attempted=1, operations_kept=1,
            declared_area_mm2=0.0, declared_volume_mm3=0.0)

    assert "repair_status" not in _run(_state(tmp_path))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/pipeline/test_repair_attempt.py -q`
Expected: FAIL — `KeyError` on the new record keys, and `test_resolved_is_taken_from_the_loop_not_inferred` failing because the node still infers from `holes_left`.

- [ ] **Step 3: Rewrite the node's success return**

In `src/meshpipeline/pipeline/repair_attempt.py`, replace everything from `operations = [op["name"] for op in repair_report.operations]` to the end of the function with:

```python
    measured = {m.name: m.value for m in repair_report.measurements}
    attempted = [dict(op) for op in repair_report.operations]
    logger.info("node_repair_attempt: repaired and promoted for engine=%s - stopped=%s "
                "- kept=%s/%s - job_id=%s", engine, measured.get("stopped_because"),
                measured.get("operations_kept"), measured.get("operations_attempted"), job_id)
    try:
        from meshpipeline.capture.logger import TrainingLogger
        # THE SLICE 8 CORPUS. Every attempt, kept or rolled back, with the defect it aimed at and
        # the basis on which that was judged. A rolled-back operation is as much a training
        # example as a kept one - more, arguably - which is why the whole sequence is logged
        # rather than only what worked. This starts filling the day the deterministic loop is
        # enabled, which is the reason it had to go in first.
        TrainingLogger(job_id).log("repair_attempt", op_id="repair:attempt", payload={
            "route": advice.route, "targets": [dict(t) for t in advice.targets],
            "operations": attempted,
            "stopped_because": measured.get("stopped_because", ""),
            "refusals": measured.get("refusals", []),
            "planner": measured.get("planner", ""),
            "report": repair_report.to_dict(),
            "lineage": update.get("repair_lineage", {})})
    except Exception:  # noqa: BLE001 - the corpus is never load-bearing for a run
        pass

    # `repair_status` IS NOT TOUCHED. It is the inspection's verdict on the file the customer
    # SENT, and `repair_report` beside it is that inspection - so overwriting it with the repair's
    # own verdict produced a pairing that read "repaired" next to the original defect list.
    return {
        **update,
        "repair_attempt": {
            "attempted": True,
            "repair_status": status_for(repair_report).value,
            # TAKEN FROM THE LOOP, NOT INFERRED. It used to be derived from `holes_left`, a key
            # the report no longer carries - which made it read True for any run with no warning
            # defect, including a run the loop had explicitly reported as a failure.
            "resolved": bool(measured.get("resolved", False)),
            # WHY THE LOOP STOPPED. "Every error cleared" and "I ran out of operations" leave the
            # same geometry and mean opposite things to the operator reading this.
            "stopped_because": measured.get("stopped_because", ""),
            "route": advice.route,
            # EVERY ATTEMPT, with its targets, verdict, reason and measured delta.
            "operations": attempted,
            # WHAT THE REPAIR AIMED AT, kept with what it did.
            "targets": [dict(t) for t in advice.targets],
            # WHAT WAS DELIBERATELY NOT ATTEMPTED AND WHY - the rims of a missing bore wall above
            # all, which used to be a silent skip inside the fill executor.
            "refusals": measured.get("refusals", []),
            "defects_remaining": [dict(e) for e in repair_report.entities],
            "summary": repair_report.summary,
        },
    }
```

- [ ] **Step 4: Update the state contract's documentation**

In `src/meshpipeline/contracts/pipeline_state.py`, replace the part of the `repair_attempt` comment describing `holes_filled`/`holes_left` with:

```python
#:   operations        every operation the loop TRIED, in order, each with its targets, verdict
#:                     (kept / rolled_back / failed), reason, the basis on which its target's fate
#:                     was judged (kernel history or a geometric inference), its own evidence and
#:                     the measured before/after. Attempts, not successes: a rolled-back operation
#:                     is the evidence an operator wants and the training example a later model
#:                     planner needs.
#:   stopped_because   why the loop ended - resolved, no_candidate, op_budget, time_budget,
#:                     stalled, scan_incomplete. NOT inferable from the geometry: a run that
#:                     cleared every error and one that ran out of operations leave the same shape.
#:   refusals          what was deliberately not attempted, and why. The paired rims of a missing
#:                     bore wall are the case this exists for.
#:   defects_remaining the located defects LEFT after the repair, so nothing reads "repaired" next
#:                     to the list the file arrived with.
```

- [ ] **Step 5: Run the tests and the proportionate check**

Run: `.venv/bin/python -m pytest tests/unit/pipeline -q`
Expected: PASS.

Run: `.venv/bin/python -m pytest tests/unit/cad tests/unit/pipeline -q`
Expected: the pre-existing `test_bore_staging` failure only.

Run: `make check-fast`
Expected: PASS — it includes the graph-wiring check, so a node-shape change that broke the graph surfaces here.

- [ ] **Step 6: Lint, typecheck, and commit**

Run: `make lint && make typecheck`

```bash
git add src/meshpipeline/pipeline/repair_attempt.py src/meshpipeline/contracts/pipeline_state.py \
        tests/unit/pipeline/test_repair_attempt.py
git commit -m "The run records every repair the loop tried, why it stopped, and what it refused"
```

---

### Task 13: The corpus tool reports the op sequence

**Files:**
- Modify: `devtools/validation/repair_corpus.py` (the row builder's repair branch, and `main`'s output)

**Interfaces:**
- Consumes: `repair_step_file`'s report as reshaped by Task 11.
- Produces: each row gains `operations` as the full attempt list plus `stopped_because`, `operations_attempted`, `operations_kept`, `refusals` and `planner`; the dead `holes_filled`/`holes_left` columns are dropped. New summary sections: why runs stopped, which operations were kept versus rolled back, and what the planner refused. A `--loop` flag prints each file's sequence.

**Why this task matters more than it looks.** This is the only thing that answers "what does the repair actually do to a real part". Three of the five operations are unexercised or unprovable on synthetic fixtures — `fix_face_boundary` has no fixture producing its defects at all — so a corpus run on real files is the only validation they will get. It also carries the evidence for the two decisions still open: whether planar filling should default on, and whether small faces stay routed to a person.

- [ ] **Step 1: Replace the row's repair-result block**

In `devtools/validation/repair_corpus.py`, the repair branch currently reads:

```python
    measured = {m.name: m.value for m in repaired.measurements}
    row.update(operations=[op["name"] for op in repaired.operations],
               holes_filled=measured.get("holes_filled", []),
               holes_left=measured.get("holes_left", []))
```

Those last two keys no longer exist, so they silently record empty lists. Replace the whole block with:

```python
    measured = {m.name: m.value for m in repaired.measurements}
    row.update(
        # EVERY ATTEMPT, KEPT OR NOT. The rolled-back ones are the interesting half: they are
        # where the rules planner ran out of ideas, which is exactly what a model later has to
        # beat, and a corpus run that only listed successes could not show it.
        operations=[{"op": op.get("name", ""), "status": op.get("status", ""),
                     "reason": op.get("reason", ""), "basis": op.get("basis", ""),
                     "defect_code": op.get("defect_code", ""),
                     "targets": op.get("targets", [])}
                    for op in repaired.operations],
        operations_attempted=int(measured.get("operations_attempted", 0) or 0),
        operations_kept=int(measured.get("operations_kept", 0) or 0),
        # FINISHED OR OUT OF ROOM. The single most useful number from a corpus run: a corpus full
        # of `op_budget` says raise the budget, one full of `stalled` says the catalogue is missing
        # an operation, and one full of `no_candidate` says the planner cannot aim at what real
        # files actually carry.
        stopped_because=str(measured.get("stopped_because", "")),
        resolved=bool(measured.get("resolved", False)),
        complete_scan=bool(measured.get("complete_scan", True)),
        planner=str(measured.get("planner", "")),
        refusals=list(measured.get("refusals", []) or []),
        declared_area_mm2=float(measured.get("declared_area_mm2", 0.0) or 0.0),
    )
```

- [ ] **Step 2: Add the new summary sections**

In `main`, after the existing `== what triage advised ==` section:

```python
    print("\n== why the loop stopped ==")
    stops: dict[str, int] = {}
    for row in rows:
        if row.get("stopped_because"):
            stops[row["stopped_because"]] = stops.get(row["stopped_because"], 0) + 1
    for reason, count in sorted(stops.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {reason}")
    if not stops:
        print("  no file reached the loop")

    print("\n== operations, kept vs rolled back ==")
    # THE PLANNER'S HIT RATE, PER OPERATION. An operation proposed often and kept rarely is
    # either aimed at the wrong defect or missing from the catalogue in its useful form - and it
    # is the baseline a model planner gets scored against.
    tally: dict[str, dict[str, int]] = {}
    for row in rows:
        for op in row.get("operations", []) or []:
            entry = tally.setdefault(op.get("op", "?"),
                                     {"kept": 0, "rolled_back": 0, "failed": 0})
            key = op.get("status", "failed")
            entry[key] = entry.get(key, 0) + 1
    for name, counts in sorted(tally.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {name:<22} kept={counts['kept']:<4} "
              f"rolled_back={counts['rolled_back']:<4} failed={counts['failed']}")
    if not tally:
        print("  nothing was attempted")

    refused = [r for row in rows for r in (row.get("refusals") or [])]
    if refused:
        print("\n== what the planner refused to attempt ==")
        by_reason: dict[str, int] = {}
        for record in refused:
            key = str(record.get("reason", "?"))
            by_reason[key] = by_reason.get(key, 0) + 1
        for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>4}  {reason}")

    print("\n== operations never exercised on this corpus ==")
    # THE POINT OF RUNNING THIS AT ALL. Three of the five operations are unprovable on synthetic
    # fixtures, so a name absent from the tally above is an operation no real file has ever given
    # this loop a reason to try.
    from meshpipeline.cad.repair.operations import OPERATIONS

    never = [op.name for op in OPERATIONS if op.name not in tally]
    for name in never:
        print(f"        {name}")
    if not never:
        print("  every operation was attempted at least once")
```

- [ ] **Step 3: Add a `--loop` flag that prints each file's sequence**

Beside the existing arguments:

```python
    parser.add_argument("--loop", action="store_true",
                        help="print each file's operation sequence, not just the summary")
```

and in the per-row print loop, after the existing `print(f"{row['outcome']:<28} ...")` line:

```python
        if args.loop:
            for op in row.get("operations", []) or []:
                mark = "+" if op.get("status") == "kept" else "-"
                detail = f" ({op['reason']})" if op.get("reason") else ""
                print(f"      {mark} {op.get('op', '?'):<20} {op.get('defect_code', ''):<22}"
                      f"{op.get('status', '')}{detail}")
            for record in row.get("refusals") or []:
                print(f"      ! refused {record.get('reason', '?')}: "
                      f"{record.get('detail', '')[:60]}")
            if row.get("stopped_because"):
                print(f"      = stopped: {row['stopped_because']} "
                      f"(resolved={row.get('resolved')})")
```

- [ ] **Step 4: Verify the tool on the fixtures it can generate itself**

There is no licensed corpus on this host, so prove the tool on generated fixtures:

```bash
cd /Users/kitts/archductor/workspaces/Hexera/apache
rm -rf /tmp/repair-corpus-check && mkdir -p /tmp/repair-corpus-check
.venv/bin/python - <<'PY'
import sys
sys.path.insert(0, ".")
from pathlib import Path
from tests.cad_fixtures import (write_open_shell_step, write_missing_bore_wall_step,
                                write_loose_faces_step, write_small_edge_step,
                                write_mixed_assembly_step, write_step)
out = Path("/tmp/repair-corpus-check")
write_open_shell_step(out / "open_shell.step")
write_missing_bore_wall_step(out / "missing_bore.step")
write_loose_faces_step(out / "loose_faces.step")
write_small_edge_step(out / "small_edge.step")
write_mixed_assembly_step(out / "mixed_assembly.step")
write_step(out / "sound_box.step", "MM")
print("wrote", sorted(p.name for p in out.glob("*.step")))
PY
CAD_REPAIR_ENABLED=true CAD_REPAIR_FILL_PLANAR_HOLES=true \
  .venv/bin/python devtools/validation/repair_corpus.py /tmp/repair-corpus-check --loop
```

Expected: a row per file, each repaired file's operation sequence beneath it, and the four new summary sections. Check specifically that `missing_bore.step` shows a `paired_rim` entry under "what the planner refused to attempt" — that is the corpus-level proof of the safety claim.

- [ ] **Step 5: Record what the run reported**

Paste the run's output into the report file. A corpus run nobody wrote down is a claim rather than a result, and the next person comparing a model planner against this baseline needs the numbers. Note in particular which operations appear under "never exercised".

- [ ] **Step 6: Lint and commit**

Run: `make lint`

```bash
git add devtools/validation/repair_corpus.py
git commit -m "The corpus tool prints each file's operation sequence and why the loop stopped"
```

---

### Task 14: Prove it in a personal environment

**Files:** no source changes. This task's deliverable is evidence.

**Interfaces:** consumes everything above. Produces a named request, a log line and a state record that together make the node change claimable. Per AGENTS.md the host tiers cover the library code; the node change wants a dev deploy before anyone says it works.

**The user has explicitly authorized `CAD_REPAIR_ENABLED` in the `pranav` environment for this proof.** Deploying a personal slug is already authorized by AGENTS.md; that approval adds the config switch.

- [ ] **Step 1: Run the full host gate**

```bash
cd /Users/kitts/archductor/workspaces/Hexera/apache
make lint && make typecheck && make check-fast
.venv/bin/python -m pytest tests/unit/cad tests/unit/pipeline -q
```
Expected: lint, typecheck and check-fast pass; the unit run shows only the pre-existing `test_bore_staging` failure. Deploying to discover that ruff is unhappy wastes twenty minutes for an answer that took two seconds.

- [ ] **Step 2: Run the whole native repair tier**

```bash
.venv/bin/python -m pytest tests/native -k "repair or fixture" -q
```
Expected: PASS, none skipped. If these skip for want of a kernel they prove nothing, and they must then run inside the mesh image via `make test-native-all` before this task is complete.

- [ ] **Step 3: Deploy to the owned slug**

The slug this task owns is `pranav`. Do not guess another.

```bash
gh workflow run deploy.yml --ref "$(git branch --show-current)" \
  -f slug=pranav -f app=true -f console=true
gh run watch
```
Expected: a green run.

- [ ] **Step 4: Confirm repair is switched on in that environment**

Repair is dormant unless `CAD_REPAIR_ENABLED` is set. Confirm it before concluding anything from a run that reported `attempted: false`:

```bash
gcloud run services describe dev-pranav-api --region us-central1 \
  --format='value(spec.template.spec.containers[0].env)' | tr ',' '\n' | grep -i cad_repair
```

If it is absent, set it per the personal-environments doc and redeploy. `CAD_REPAIR_FILL_PLANAR_HOLES` stays at its default unless the user decides otherwise — that decision is still open.

- [ ] **Step 5: Run a broken file through the real pipeline, and read the record**

Upload the loose-faces fixture through the normal simulation flow against that API and start a mesh job. It is the fixture the loop demonstrably repairs, so it is the one that proves the path end to end.

```bash
API=$(gcloud run services describe dev-pranav-api --region us-central1 --format='value(status.url)')
echo "$API"
gcloud logging read \
  'resource.labels.service_name="dev-pranav-api" AND textPayload:"node_repair_attempt"' \
  --limit 20 --freshness 30m
```
Expected: a `node_repair_attempt: repaired and promoted` line naming `stopped=` and `kept=N/M`.

Then fetch the job's state through the admin repair API (`src/meshpipeline/api/v1/admin_repair.py`) and confirm `repair_attempt` carries `operations` with per-operation `status` and `targets`, plus `stopped_because` and `resolved`. A green deploy with an unexercised code path is an untested change; this is the request and the record that make the claim.

Write down the job id, the `stopped_because`, and how many operations were kept out of how many tried.

- [ ] **Step 6: Leave the environment standing**

Do **not** run `make destroy-env`. The branch is not merged, the proof may need repeating, and the user owns that environment. Report its URL and what is running there.

---

## Notes for the executor

**What this plan deliberately does not do.** Slice 7's entity context (surface type, adjacency, the paired-bore finding as its own defect), slice 8's model planner, and slice 9's operator-queue writer are sketched in the spec and are **not** in this plan. If a task seems to want one of them, it does not.

**The two open questions stay open.** Whether `CAD_REPAIR_FILL_PLANAR_HOLES` should default on is to be decided after Task 13's corpus run, not during implementation — and the evidence now cuts both ways, because the rim discriminator false-positives readily. Whether small faces become advisory is a separate question; no task here adds an operation for them.

**`wire_gap`, `tolerance_outlier` and `bad_orientation`** are all claimed somewhere and produced by nothing. That is slice 7's to resolve — emit them or delete them. Do not delete them from `DefectCode` here; `triage.py` reads them and the change belongs with the decision.
