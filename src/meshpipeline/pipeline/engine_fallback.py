# Responsibility: Decide when a run moves to another engine, and what to offer when no engine may be switched to on its own.
# Owns: the fallback order per flow topology, the same-contract test between two engines, the failure classes, and the ladder record.
# Boundaries: pure decisions over state and the engine registry, plus the one graph node that applies them; it meshes nothing.
"""THE FALLBACK LADDER.

A run used to live and die on the one engine the intake settled. When that engine met a shape it
structurally cannot mesh - it crashed, it never finished, its cells would not come out good enough
- every retry re-bought the same failure, although another engine would have meshed the shape.
Commercial meshers almost never fail on clean geometry because they fall back to a more robust
method. This module is that fallback, with one rule above every other:

    APPROVED EQUALS DELIVERED. The run moves to another engine ON ITS OWN only when that engine
    delivers what the user approved: the same boundaries (every name and type admitted by the
    same admission rules), the same units (every engine meshes the same metre-normalised surface),
    the same kind of file, the same kind of cells, a wall at least as true to the body, and the
    prism layers the brief asked for. Anything else - a tetrahedral mesh for a hex-dominant one,
    no layers where layers were asked for, a staircased wall for a body-fitted one - is OFFERED,
    in plain words, as the run's last sentence, and never delivered silently.

Two more rules:
* An engine the USER named (engine_source user_direct) is theirs. It is never switched without
  asking - the ladder only offers. A dispute rebuild keeps the engine of the mesh it disputes.
* The budgets stay as they are. A switch spends one of the run's existing attempts and needs the
  time for the new engine's own run cap; the retries are shared across rungs rather than spent
  on the first one alone.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meshpipeline.contracts.failure_cause import FailureCause, as_cause, retry_can_help

if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState

logger = logging.getLogger(__name__)

#: THE LADDER ORDER, per flow topology: the approved engine first, then the rest in this order.
#: Most robust input handling first (cfMesh wraps dirty surfaces), then the body-fitted hex mesher,
#: then the tetrahedral engines. Every implemented engine that produces a topology is listed under
#: it (test-enforced), so a new engine cannot silently miss the ladder. Only the rungs that pass
#: the approved request's own admission rules are ever considered.
FALLBACK_ORDER: dict[str, tuple[str, ...]] = {
    "external": ("cfmesh", "snappy", "gmsh"),
    "internal": ("cfmesh", "snappy", "vmtk", "gmsh"),
}

#: Engine provenance values, as the application seeds them (see pipeline_run). `user_direct` and
#: `suggested_confirmed` are the intake's own words; `dispute` and `system` are the run's.
SOURCE_USER = "user_direct"
SOURCE_SUGGESTED = "suggested_confirmed"
SOURCE_DISPUTE = "dispute"
SOURCE_SYSTEM = "system"
#: Who chose the engine decides whether it may be changed without asking. An unknown source is
#: treated as the user's: never switched on a guess.
_SWITCHABLE_SOURCES = frozenset({SOURCE_SUGGESTED, SOURCE_SYSTEM})

# THE FAILURE CLASSES the ladder reads. Named by what another engine could do about them.
#: This engine could not build a mesh at all (it crashed, timed out, stopped before writing one,
#: or refused the input). Another engine may well succeed: switch now.
ENGINE = "engine"
#: The mesh came out but failed a bar a retry on the same engine might clear (cell quality,
#: resolution, size, a trial solve, a lost boundary, review). Retry here while the shared
#: allowance lasts; switch when it does not, or when the same failure repeats.
FIXABLE = "fixable"
#: Another engine cannot change it: the request itself (the far-field size asked for), the
#: approval (a boundary our own authoring wrote wrong), or a multi-region split. Never switch.
NEVER = "never"

#: WHAT A DIFFERENT ENGINE CAN DO about each failure cause. The causes are the executor's
#: (contracts/failure_cause.py), named by the gate that failed - this module keeps no second
#: vocabulary and never reads a gate key for meaning. The failure-cause module already answers
#: "would ANOTHER ATTEMPT of this run change it" (retry_can_help); a cause it calls hopeless is
#: NEVER here too, unless it is listed in _ONLY_ANOTHER_ENGINE_CHANGES below. The rest is the
#: engine question alone: a boundary type our emission wrote, the far-field size the user asked
#: for and a multi-region split are the same whichever engine runs (NEVER); a mesher that stopped
#: is the engine's own failing (ENGINE); a mesh that missed a bar may be fixed here (FIXABLE).
_LADDER_CLASS: dict[FailureCause, str] = {
    FailureCause.ENGINE_CRASHED: ENGINE,
    FailureCause.GEOMETRY_REJECTED: ENGINE,
    FailureCause.MESH_QUALITY: FIXABLE,
    FailureCause.UNDER_RESOLVED: FIXABLE,
    FailureCause.CELL_BUDGET: FIXABLE,
    FailureCause.NOT_SOLVABLE: FIXABLE,
    FailureCause.PATCH_NOT_CAPTURED: FIXABLE,
    FailureCause.CONTRACT_MISMATCH: NEVER,
    FailureCause.BOUNDARY_TYPE: NEVER,
    FailureCause.DOMAIN_EXTENT: NEVER,
    FailureCause.REGION_SPLIT: NEVER,
    # the review of a validated mesh did not finish: our reviewer's failing, which the same mesh
    # from another engine would meet again - never a reason to leave the engine
    FailureCause.REVIEW_INCOMPLETE: NEVER,
}
#: The causes the retry policy calls hopeless that another ENGINE still changes. A refused
#: geometry is the same file on the next attempt - but a different engine has a different input
#: contract (VMTK refuses a self-intersecting surface that cfMesh and snappyHexMesh wrap), so it
#: stays an ENGINE failure: never a mid-run switch (the run ends at the refusal), always an offer.
_ONLY_ANOTHER_ENGINE_CHANGES: frozenset[FailureCause] = frozenset({FailureCause.GEOMETRY_REJECTED})

#: What each failure means, said to the user. Short and plain: the full account of the failure is
#: the terminal message's (failure_cause.describe); this is the half-sentence that says why the
#: engine was left.
_REASON_BY_CAUSE: dict[FailureCause, str] = {
    FailureCause.ENGINE_CRASHED: "it stopped before it finished the mesh",
    FailureCause.GEOMETRY_REJECTED: "it cannot take this geometry as it is",
    FailureCause.MESH_QUALITY: "its cells came out too badly shaped to use",
    FailureCause.UNDER_RESOLVED: "it could not fit enough cells across the narrowest passage",
    FailureCause.CELL_BUDGET: "its mesh came out bigger than one job allows",
    FailureCause.NOT_SOLVABLE: "a trial solve on its mesh did not converge",
    FailureCause.PATCH_NOT_CAPTURED: "it lost one of your boundaries",
    FailureCause.CONTRACT_MISMATCH: "a boundary did not come out under the name you approved",
    FailureCause.BOUNDARY_TYPE: "a boundary came out with the wrong type",
    FailureCause.DOMAIN_EXTENT: "the far-field domain came out short of the size you asked for",
    FailureCause.REGION_SPLIT: "the parts did not come out as separate meshes",
    FailureCause.REVIEW_INCOMPLETE: "the review of its mesh did not finish",
}
_REASON_REVIEW = "the mesh did not pass review"
_REASON_GENERIC = "it did not produce a mesh that passed its checks"


# #
# what the brief asked for
# #

#: A brief that says it wants NO layers. Checked first: "no prism layers" mentions layers too.
_NO_LAYERS = re.compile(
    r"\b(?:no|without|zero|0)\s+(?:prism\s+|boundary[\s-]|inflation\s+|near[\s-]wall\s+)?layers?\b",
    re.I)
_LAYER_COUNT = re.compile(
    r"\b(\d{1,2})\s*(?:x\s*)?(?:prism|boundary|inflation|near[\s-]wall|wall|surface)[\s-]+layers?\b",
    re.I)
#: Any other way a brief asks for near-wall resolution. Deliberately wide: a false "yes" only turns
#: an automatic switch into a question, while a false "no" could drop layers silently.
_LAYER_WORDS = re.compile(
    r"\b(?:prism[\s-]+layers?|boundary[\s-]+layers?|inflation[\s-]+layers?|near[\s-]wall\s+layers?|"
    r"y\s*\+|yplus|y\s+plus|wall[\s-]resolved|first[\s-]+(?:cell|layer)|nSurfaceLayers)", re.I)


@dataclass(frozen=True)
class LayerRequest:
    requested: bool
    count: int | None = None


def layer_request(request_txt: str, review_brief_txt: str = "") -> LayerRequest:
    # (findall, not search: the publication scanner reads any `.search(` as a search event)
    text = f"{request_txt or ''}\n{review_brief_txt or ''}"
    counts = [int(n) for n in _LAYER_COUNT.findall(text) if int(n) > 0]
    if counts:
        return LayerRequest(True, max(counts))
    if _NO_LAYERS.findall(text) and not _LAYER_WORDS.findall(_NO_LAYERS.sub(" ", text)):
        return LayerRequest(False)
    return LayerRequest(bool(_LAYER_WORDS.findall(text)))


# #
# the rungs
# #

@dataclass(frozen=True)
class Rung:
    engine: str
    #: delivers what was approved - the only rungs the run may move to on its own
    same_contract: bool
    #: what would differ from the approved mesh, in plain words (empty iff same_contract)
    changes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"engine": self.engine, "same_contract": self.same_contract,
                "changes": list(self.changes)}


def engine_label(name: str) -> str:
    try:
        from meshpipeline.engines.registry import engine_label as _label
        return _label(name) or name
    except Exception:  # noqa: BLE001 - a label must never break a decision
        return name


def record_of(state: Mapping) -> dict:
    rec = state.get("engine_ladder")
    return dict(rec) if isinstance(rec, Mapping) else {}


def approved_engine(state: Mapping) -> str:
    """The engine the run was approved with - the first rung, whatever runs now."""
    return str(record_of(state).get("approved") or state.get("engine") or "")


def engine_source(state: Mapping) -> str:
    if state.get("user_dispute"):
        return SOURCE_DISPUTE
    return str(state.get("engine_source") or "")


def may_switch_on_its_own(state: Mapping) -> bool:
    return engine_source(state) in _SWITCHABLE_SOURCES


def _declared_evidence(state: Mapping, engine: str):
    from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
    from meshpipeline.engines.registry import resolve_engine_params
    patches = tuple(
        PatchSummary(name=str(p.get("name", "")).strip(), type=str(p.get("type", "")).strip())
        for p in (state.get("intake_patches") or []) if isinstance(p, Mapping))
    return AdmissionEvidence(
        engine=engine,
        purpose=str(state.get("purpose", "") or ""),
        input_kind=str(state.get("input_kind", "") or "") or None,
        dimensionality=str(state.get("dimensionality", "") or "") or None,
        patches=patches,
        # the candidate's OWN parameters are judged separately (_params_carry_over): here only
        # "can it build what was declared at all"
        engine_params=resolve_engine_params(engine, {}))


#: The file a user receives, by the engine's native export format - words for the offer only.
_FORMAT_WORDS: dict[str, str] = {
    "openfoam_polymesh": "an OpenFOAM mesh",
    "openfoam_polymesh_multiregion": "a multi-region OpenFOAM case",
    "vtu": "a VTK (.vtu) mesh",
    "abaqus_inp": "an Abaqus (.inp) deck",
}


def _format_words(spec) -> str:
    fmt = spec.export_formats[0] if spec.export_formats else ""
    return _FORMAT_WORDS.get(fmt, f"a {fmt} file" if fmt else "a different file")


def _changes(approved_spec, spec, layers: LayerRequest) -> tuple[str, ...]:
    from meshpipeline.engines.base import WALL_FITS
    a, c = approved_spec.delivered_mesh, spec.delivered_mesh
    if a is None or c is None:
        return ("a kind of mesh this system cannot compare with the one you approved",)
    out: list[str] = []
    if spec.export_formats[:1] != approved_spec.export_formats[:1]:
        out.append(f"you would get {_format_words(spec)} instead of "
                   f"{_format_words(approved_spec)}")
    if c.cells != a.cells:
        out.append(f"{c.cells} cells instead of {a.cells} ones")
    if WALL_FITS.index(c.walls) < WALL_FITS.index(a.walls):
        out.append(f"a {c.walls} wall instead of a {a.walls} one")
    if layers.requested and not c.prism_layers:
        out.append("no reliable near-wall prism layers, which your request asks for")
    return tuple(out)


def _params_carry_over(state: Mapping, spec) -> bool:
    """The candidate's own questions are answered by what was approved: every parameter it
    requires is there, with a value it accepts. A default filled in for the user is a setting they
    never approved, so it makes the rung an offer. The approved engine's own parameters that the
    candidate does not have (gmsh's element order on a hex mesher) do not travel: the cell change
    they belong to is already stated."""
    given = dict(state.get("engine_params") or {})
    for p in spec.intake_params:
        v = str(given.get(p.key, "") or "").strip().lower()
        if (not v and p.required) or (v and v not in p.values):
            return False
    return True


def ladder(state: Mapping) -> list[Rung]:
    """The approved engine, then every other engine that can build the approved request, in the
    fallback order - each marked with whether it delivers what was approved."""
    from meshpipeline.engines.purposes import topology_of
    from meshpipeline.engines.registry import engine_names, get_spec

    approved = approved_engine(state)
    if not approved:
        return []
    rungs = [Rung(approved, True)]
    topo = topology_of(str(state.get("purpose", "") or ""))
    if not topo or engine_source(state) == SOURCE_DISPUTE:
        return rungs
    try:
        approved_spec = get_spec(approved)
    except Exception:  # noqa: BLE001 - an unknown engine has no ladder
        return rungs
    layers = layer_request(str(state.get("request_txt", "") or ""),
                           str(state.get("review_brief_txt", "") or ""))
    implemented = set(engine_names())
    for name in FALLBACK_ORDER.get(topo, ()):
        if name == approved or name not in implemented:
            continue
        spec = get_spec(name)
        if spec.admit(_declared_evidence(state, name)):
            continue            # it cannot build what was declared: not a rung at all
        changes = list(_changes(approved_spec, spec, layers))
        if not _params_carry_over(state, spec):
            changes.append(f"{engine_label(name)}'s own settings would take their defaults")
        rungs.append(Rung(name, not changes, tuple(changes)))
    return rungs


# #
# the failure that just happened
# #

@dataclass(frozen=True)
class Failure:
    kind: str       # ENGINE | FIXABLE | NEVER
    cause: str      # the recorded cause, the gate key, or "review"
    reason: str     # the plain half-sentence

    def as_dict(self) -> dict:
        return {"kind": self.kind, "cause": self.cause, "reason": self.reason}


def classify(state: Mapping) -> Failure | None:
    """What stopped the attempt whose result the state now holds, or None when it did not fail."""
    if state.get("executor_success"):
        if state.get("requirement_caveats"):
            return Failure(NEVER, "requirements_near_miss",
                           "the far-field domain came out short of the size you asked for")
        if str(state.get("reviewer_verdict", "") or "").upper() == "FAIL":
            return Failure(FIXABLE, "review", _REASON_REVIEW)
        return None
    gate = str(state.get("executor_failed_gate", "") or "")
    facts = state.get("executor_failure_facts")
    facts = facts if isinstance(facts, Mapping) else {}
    cause = _cause_of(state, gate)
    if cause is None:
        if gate:
            # a gate that names no cause, on no engine this system knows: never a reason to switch
            # at once, never a reason to refuse one
            return Failure(FIXABLE, gate, _REASON_GENERIC)
        # no gate at all: the executor had nothing to validate - the builder produced no mesh
        cause = FailureCause.ENGINE_CRASHED
    kind = _LADDER_CLASS[cause]
    if not retry_can_help(cause, facts) and cause not in _ONLY_ANOTHER_ENGINE_CHANGES:
        # the retry policy's own verdict: nothing that runs again changes this, so no engine does
        kind = NEVER
    return Failure(kind, cause.value, _REASON_BY_CAUSE[cause])


def _cause_of(state: Mapping, gate: str) -> FailureCause | None:
    """The executor's cause for the failed attempt: the one it recorded, else the one it WOULD have
    recorded - the failed gate's declared cause for this engine, else the seam's - resolved by the
    executor's own function, so the ladder and the retry policy can never read one failure two
    ways. (A state without a recorded cause predates the cause fields.)"""
    recorded = as_cause(state.get("executor_failure_cause"))
    if recorded is not None or not gate:
        return recorded
    from meshpipeline.pipeline.executor import _gate_cause
    return as_cause(_gate_cause(str(state.get("engine") or ""), gate, None))


def tried_engines(state: Mapping) -> list[str]:
    rec = record_of(state)
    seen = [str(a.get("engine")) for a in (rec.get("attempts") or []) if isinstance(a, Mapping)]
    current = str(state.get("engine") or "")
    out: list[str] = []
    for e in [approved_engine(state), *seen, current]:
        if e and e not in out:
            out.append(e)
    return out


def with_attempt(state: Mapping, failure: Failure | None) -> dict:
    """The ladder record with the attempt that just ended added (idempotent per attempt)."""
    rec = record_of(state)
    rec.setdefault("approved", approved_engine(state))
    rec.setdefault("source", engine_source(state))
    attempts = [dict(a) for a in (rec.get("attempts") or []) if isinstance(a, Mapping)]
    # A GEOMETRY-ADMISSION REFUSAL builds nothing: it jumps retry_count to the exhausted value, and
    # that number is not an attempt anyone ran. It is recorded as attempt 0 - the engine refused
    # the input before the first build - so the history never shows a build that did not happen.
    n = 0 if state.get("geometry_unsuitable_reason") else int(state.get("retry_count", 0) or 0)
    if (n > 0 or state.get("geometry_unsuitable_reason")) and not any(
            int(a.get("attempt", -1)) == n for a in attempts):
        entry: dict[str, Any] = {"attempt": n, "engine": str(state.get("engine") or "")}
        entry.update(failure.as_dict() if failure else {"kind": "passed"})
        if n == 0:
            entry["refused_before_building"] = True
        attempts.append(entry)
    rec["attempts"] = attempts
    rec.setdefault("switches", [])
    return rec


# #
# the mid-run decision
# #

@dataclass(frozen=True)
class Decision:
    switch_to: str = ""
    why: str = ""               # the policy reason, for the log and the record
    failure: Failure | None = None

    @property
    def switch(self) -> bool:
        return bool(self.switch_to)


def remaining_seconds(state: Mapping, *, now: float) -> float:
    ends = [float(v) for v in (state.get("builder_deadline_epoch"), state.get("pipeline_deadline_epoch"))
            if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) > 0]
    return (min(ends) - now) if ends else float("inf")


def rung_seconds(engine: str) -> float:
    """What one attempt on this engine needs: its own native run cap. A switch with less left
    would buy a run that cannot finish."""
    from meshpipeline.engines.registry import get_spec
    rp = get_spec(engine).run_policy
    return float(rp.run_timeout()) if rp is not None else 0.0


def _measures_its_input(engine: str) -> bool:
    """An engine whose input contract MEASURES the geometry (a self-intersection or thinness
    floor) is admitted by node_geometry_admission, which runs once, before the first build. A
    mid-run switch would skip that measurement, so such an engine is offered, never switched to."""
    from meshpipeline.engines.registry import get_spec
    ic = get_spec(engine).input_contract
    return ic is not None and (ic.require_no_self_intersection or ic.min_thickness_ratio > 0)


def _repeats(record: Mapping, engine: str, failure: Failure) -> bool:
    mine = [a for a in (record.get("attempts") or [])
            if isinstance(a, Mapping) and a.get("engine") == engine]
    return (len(mine) >= 2 and mine[-2].get("kind") == failure.kind
            and mine[-2].get("cause") == failure.cause)


def decide(state: Mapping, *, record: Mapping, now: float | None = None) -> Decision:
    """Stay on this engine for the next attempt, or move to the next same-contract rung.

    `record` is the ladder record with the attempt that just failed already in it."""
    import meshpipeline.agents.builder.settings as bcfg
    import meshpipeline.settings.policy as polcfg

    failure = classify(state)
    if failure is None:
        return Decision(why="the last attempt did not fail")
    if not polcfg.ENGINE_FALLBACK_ENABLED:
        return Decision(why="the fallback ladder is switched off", failure=failure)
    if failure.kind == NEVER:
        return Decision(why="another engine cannot change this failure", failure=failure)
    if not may_switch_on_its_own(state):
        return Decision(why=f"the engine is not ours to change (source {engine_source(state) or 'unknown'})",
                        failure=failure)
    engine = str(state.get("engine") or "")
    tried = set(tried_engines({**state, "engine_ladder": record}))
    open_rungs = [r for r in ladder({**state, "engine_ladder": record})
                  if r.same_contract and r.engine not in tried
                  and not _measures_its_input(r.engine)]
    if not open_rungs:
        return Decision(why="no untried engine delivers the approved mesh", failure=failure)
    # attempts left, the next one included (the reviewer-feedback bonus is not the ladder's)
    left = bcfg.MAX_BUILDER_RETRIES + 1 - int(state.get("retry_count", 0) or 0)
    if left < 1:
        return Decision(why="no regular attempt is left", failure=failure)
    if failure.kind == FIXABLE and not _repeats(record, engine, failure) and left > len(open_rungs):
        return Decision(why="a retry here fits and still leaves every untried rung an attempt",
                        failure=failure)
    target = open_rungs[0].engine
    clock = time.time() if now is None else now
    if remaining_seconds(state, now=clock) < rung_seconds(target):
        return Decision(why=f"too little time left for a {engine_label(target)} run",
                        failure=failure)
    return Decision(switch_to=target, why=(
        "this engine could not build a mesh" if failure.kind == ENGINE else
        "the same failure repeated here" if _repeats(record, engine, failure) else
        "the shared attempts go to the untried engine"), failure=failure)


def with_switch(record: Mapping, *, attempt: int, frm: str, to: str, failure: Failure,
                why: str) -> dict:
    rec = dict(record)
    switches = [dict(s) for s in (rec.get("switches") or []) if isinstance(s, Mapping)]
    if not any(int(s.get("attempt", -1)) == attempt for s in switches):
        switches.append({"attempt": attempt, "from": frm, "to": to, "because": failure.cause,
                         "kind": failure.kind, "reason": failure.reason, "why": why})
    rec["switches"] = switches
    return rec


def switch_note(frm: str, to: str, failure: Failure) -> str:
    return (f"{engine_label(frm)} could not mesh this shape: {failure.reason}. Trying "
            f"{engine_label(to)} next - same boundaries, same units, same settings you approved.")


def fresh_start_brief(frm: str, to: str, failure: Failure) -> str:
    return (f"This is the FIRST attempt on {engine_label(to)}. The previous engine, "
            f"{engine_label(frm)}, could not mesh this shape: {failure.reason}. Plan from scratch "
            f"for {engine_label(to)}; the approved boundaries, units and settings are unchanged.")


# #
# the end of the run
# #

#: The failures prism layers commonly cause: folded or squeezed layer cells fail the cell-quality
#: bars, the trial solve, and the review of layer coverage. Only these CAN earn a fewer-layers
#: offer, and only with measured evidence that the layers were to blame (_layers_are_to_blame).
_LAYER_SHAPED = frozenset({FailureCause.MESH_QUALITY.value, FailureCause.NOT_SOLVABLE.value,
                           "review"})


def _layers_are_to_blame(state: Mapping) -> bool:
    """EVIDENCE that the prism layers caused this failure, from what the run measured - never a
    guess from the failure's class alone: the review failed a layer axis, the mesh carries the
    layer-inversion signature (negative-volume or mis-oriented cells, which folding prism layers
    produce - the snappy layer policy reads the same signature), or a failed check the executor
    recorded names the layers. Without it no fewer-layers offer is made: telling a user that
    fewer layers fixes a failure they did not cause sends them to approve the wrong change."""
    if any(isinstance(f, Mapping) and f.get("passed") is False
           and "layer" in str(f.get("axis_key") or "").lower()
           for f in (state.get("reviewer_axis_findings") or [])):
        return True
    def _mapping(value: Any) -> Mapping:
        return value if isinstance(value, Mapping) else {}

    quality = _mapping(_mapping(state.get("mesh_manifest")).get("quality"))
    facts = _mapping(state.get("executor_failure_facts"))
    fatal = " ".join(str(x) for x in [*(quality.get("fatal") or []),
                                      *(facts.get("fatal") or [])]).lower()
    if "negative" in fatal or "orient" in fatal:
        return True
    return any(isinstance(c, Mapping) and "layer" in str(c.get("key") or "").lower()
               for c in (facts.get("checks") or []))


def _review_failed_only_layers(state: Mapping) -> bool:
    """The review's material findings are ALL about the layers. Fewer layers can only answer a
    review whose every complaint that counts is about the layers: job e0fa8ad0 failed prism
    coverage AND wake resolution, and was offered fewer layers - which does nothing for the wake.
    A judged review (agents/reviewer/review_policy) counts what failed the run - its wrong-problem
    findings; an unjudged record counts every axis that did not pass."""
    rows = [f for f in (state.get("reviewer_axis_findings") or [])
            if isinstance(f, Mapping) and f.get("passed") is False]
    if any("blocking" in f for f in rows):
        rows = [f for f in rows if f.get("blocking")]
    failed = [str(f.get("axis_key") or "").lower() for f in rows]
    return bool(failed) and all("layer" in k for k in failed)


def improvement_offer(state: Mapping) -> dict | None:
    """AN OPTIONAL IMPROVEMENT beside a DELIVERED mesh: when every point the review left open is
    about the near-wall layers, the same mesh with fewer layers - offered, never run. A mesh with
    nothing left open, or open points elsewhere, carries no offer."""
    from meshpipeline.engines.registry import get_spec
    engine = str(state.get("engine") or "")
    open_points = [str(f.get("axis_key") or "").lower()
                   for f in (state.get("reviewer_axis_findings") or [])
                   if isinstance(f, Mapping) and f.get("passed") is False]
    if not open_points or not all("layer" in k for k in open_points):
        return None
    lr = layer_request(str(state.get("request_txt", "") or ""),
                       str(state.get("review_brief_txt", "") or ""))
    try:
        dm = get_spec(engine).delivered_mesh
    except Exception:  # noqa: BLE001 - an unknown engine offers nothing
        return None
    if not lr.count or lr.count < 2 or dm is None or not dm.prism_layers:
        return None
    fewer = max(1, lr.count // 2)
    return {"kind": "fewer_layers", "engine": engine, "same_contract": False, "optional": True,
            "layers_from": lr.count, "layers_to": fewer,
            "changes": [f"{fewer} near-wall layers instead of {lr.count}"],
            "reply": f"use {fewer} layers",
            "text": (f"Optional: the review's open points are all about the {lr.count} near-wall "
                     f"layers. Fewer layers usually cover more of the wall - I can build the same "
                     f"mesh with {fewer} layers instead (same boundaries, same units). Reply "
                     f"\"use {fewer} layers\" if you want that run.")}


def _fewer_layers(state: Mapping, engine: str, failure: Failure) -> dict | None:
    from meshpipeline.engines.registry import get_spec
    lr = layer_request(str(state.get("request_txt", "") or ""),
                       str(state.get("review_brief_txt", "") or ""))
    dm = get_spec(engine).delivered_mesh
    if (failure.cause not in _LAYER_SHAPED or not lr.count or lr.count < 2
            or dm is None or not dm.prism_layers or not _layers_are_to_blame(state)):
        return None
    if failure.cause == "review" and not _review_failed_only_layers(state):
        return None
    fewer = max(1, lr.count // 2)
    # NAME THE CAUSE THAT HAPPENED. A review failure is a mesh that was built and passed every
    # automatic check; telling that user the mesher "could not finish" it with their layers sent
    # them to approve a change for a failure that never occurred.
    why = (f"{engine_label(engine)} built the mesh and it passed every automatic check, but the "
           f"review found its {lr.count} near-wall layers missing from the wall. Fewer layers "
           "usually grow where a full stack cannot."
           if failure.cause == "review" else
           f"{engine_label(engine)} could not finish this mesh with the {lr.count} near-wall "
           f"layers you asked for: {failure.reason}. Fewer layers usually fixes this.")
    return {"kind": "fewer_layers", "engine": engine, "same_contract": False,
            "layers_from": lr.count, "layers_to": fewer,
            "changes": [f"{fewer} near-wall layers instead of {lr.count}"],
            "reply": f"use {fewer} layers",
            "text": (f"{why} I can build the same mesh with {fewer} layers instead - same "
                     f"boundaries, same units. Reply \"use {fewer} layers\" and I will set that "
                     "run up.")}


def offer(state: Mapping, record: Mapping) -> dict | None:
    """THE ONE THING TO OFFER when a run ends without a mesh, or None. In order: another engine
    that delivers the approved mesh (it could not be switched to - it was the user's engine, or
    the attempts or the time ran out), the same engine with fewer layers, then another engine
    with the differences stated. Never a switch the user did not ask for."""
    failure = classify(state)
    if failure is None or failure.kind == NEVER or engine_source(state) == SOURCE_DISPUTE:
        return None
    engine = str(state.get("engine") or "")
    st = {**state, "engine_ladder": record}
    order = tried_engines(st)
    tried = set(order)
    rungs = [r for r in ladder(st) if r.engine not in tried]
    same = [r for r in rungs if r.same_contract]
    ran = [e for e in order if any(isinstance(a, Mapping) and a.get("engine") == e
                                   for a in (record.get("attempts") or []))] or [engine]
    names = [engine_label(e) for e in ran]
    who = (f"{names[0]} could not" if len(names) == 1 else
           f"Neither {names[0]} nor {names[1]} could" if len(names) == 2 else
           f"None of {', '.join(names[:-1])} and {names[-1]} could")
    head = f"{who} mesh this shape: {failure.reason}."
    if failure.cause == "review" and len(names) == 1:
        # the mesh exists and passed every gate - "could not mesh this shape" says it does not. A
        # review fails a run only when the mesh is the wrong problem (review_policy.WRONG_PROBLEM).
        head = (f"{names[0]} built a mesh that passed every automatic check, but the review found "
                "it represents a different problem than you asked for.")
    if same:
        to = same[0].engine
        why = (f"You chose {engine_label(engine)}, so I did not switch engines without asking."
               if not may_switch_on_its_own(state) else
               "There was no attempt or time left to try it in this run.")
        return {"kind": "engine", "engine": to, "same_contract": True, "changes": [],
                "reply": f"use {engine_label(to)}",
                "text": (f"{head} {engine_label(to)} can build the same mesh - same boundaries, "
                         f"same units, same settings. {why} Reply \"use {engine_label(to)}\" "
                         "and I will set that run up.")}
    fewer = _fewer_layers(state, engine, failure)
    if fewer is not None:
        return fewer
    if rungs:
        to = rungs[0].engine
        diff = "; ".join(rungs[0].changes)
        return {"kind": "engine", "engine": to, "same_contract": False,
                "changes": list(rungs[0].changes), "reply": f"use {engine_label(to)}",
                "text": (f"{head} {engine_label(to)} can mesh it, but not exactly as you "
                         f"approved: {diff}. I did not switch without asking. Reply "
                         f"\"use {engine_label(to)}\" if that works for you.")}
    return None


def final_record(state: Mapping, *, succeeded: bool, system_failure: bool) -> dict:
    """The ladder record the run ends with: every attempt, every switch, the engine that built
    the delivered mesh, and - on a mesh failure - the one offer."""
    failure = None if succeeded else classify(state)
    rec = with_attempt(state, failure)
    rec["delivered_by"] = str(state.get("engine") or "") if succeeded else ""
    # A delivered mesh carries at most an OPTIONAL improvement; a failed run the one offer.
    rec["offer"] = (improvement_offer(state) if succeeded else
                    None if system_failure else offer(state, rec))
    return rec


def delivered_note(record: Mapping) -> str:
    """The success message's line about a switch, or '' when the approved engine built it."""
    switches = [s for s in (record.get("switches") or []) if isinstance(s, Mapping)]
    if not switches or not record.get("delivered_by"):
        return ""
    first, last = switches[0], switches[-1]
    return (f"The first engine, {engine_label(str(first.get('from')))}, could not mesh this shape "
            f"({first.get('reason')}), so this mesh was built with "
            f"{engine_label(str(last.get('to')))} - same boundaries, same units, same settings "
            "you approved.")


# #
# the graph node
# #

async def node_engine_fallback(state: PipelineState) -> dict:
    """THE LADDER'S ONE MOVE, between the classifier and the next build attempt.

    Records the attempt that just failed, then either keeps the engine (the next attempt is an
    ordinary retry) or moves the run to the next engine that delivers the SAME approved mesh -
    starting it fresh, on one of the run's existing attempts. It never ends a run and never
    changes a user-named engine: when no move is allowed, the run's end offers one instead
    (engine_fallback.final_record)."""
    # the selection node's own event and stream helpers: a switch is a selection, announced and
    # recorded the way the first one was
    from meshpipeline.engines.registry import resolve_engine_params
    from meshpipeline.pipeline.engine_select import _ladder_facts, _log_selection, _publish

    job_id = state.get("job_id", "unknown")
    failure = classify(state)
    record = with_attempt(state, failure)
    decision = decide(state, record=record)
    attempt = int(state.get("retry_count", 0) or 0) + 1       # the attempt about to start
    if not decision.switch or decision.failure is None:
        logger.info("node_engine_fallback: staying on %s for attempt %d (%s) - job_id=%s",
                    state.get("engine"), attempt, decision.why, job_id)
        return {"engine_ladder": record}

    frm, to = str(state.get("engine") or ""), decision.switch_to
    record = with_switch(record, attempt=attempt, frm=frm, to=to,
                         failure=decision.failure, why=decision.why)
    logger.warning("node_engine_fallback: %s could not mesh this shape (%s: %s) - attempt %d "
                   "moves to %s, which delivers the same approved mesh - job_id=%s",
                   frm, decision.failure.kind, decision.failure.cause, attempt, to, job_id)
    await _publish(job_id, switch_note(frm, to, decision.failure), f"fallback:{attempt}")
    _log_selection(job_id, {
        "chosen": to, "source": "fallback", "from": frm,
        "because": decision.failure.cause, "failure_kind": decision.failure.kind,
        "why": decision.why, "attempt": attempt, "model": None, "usage": None,
        **_ladder_facts(state, to)}, op_id=f"engine-select:fallback:{attempt}")
    return {
        "engine": to,
        # the new engine's own declared parameters; the ladder only moves to an engine whose
        # questions the approval already answers, so nothing here is a new default
        "engine_params": resolve_engine_params(to, dict(state.get("engine_params") or {})),
        # a FRESH build on the new engine: the old engine's spec and the classifier's advice to it
        # mean nothing to this one, and the no-progress stop compares like with like
        "builder_mode": "initial",
        "builder_noop_count": 0,
        # the brief the new engine's planner reads as "what went wrong before": the old gate's
        # coaching speaks the old engine's vocabulary ("adjust the meshDict"), so it is replaced
        # by what actually happened, in words any engine's planner can use
        "classifier_result": {**dict(state.get("classifier_result") or {}),
                              "summary": fresh_start_brief(frm, to, decision.failure),
                              "error_source": "engine_fallback"},
        "engine_ladder": record,
    }


__all__ = ["ENGINE", "FALLBACK_ORDER", "FIXABLE", "NEVER", "SOURCE_DISPUTE", "SOURCE_SUGGESTED",
           "SOURCE_SYSTEM", "SOURCE_USER", "Decision", "Failure", "LayerRequest", "Rung",
           "approved_engine", "classify", "decide", "delivered_note", "engine_source",
           "fresh_start_brief",
           "final_record", "ladder", "layer_request", "may_switch_on_its_own",
           "node_engine_fallback", "offer", "remaining_seconds", "rung_seconds", "switch_note",
           "tried_engines", "with_attempt", "with_switch"]
