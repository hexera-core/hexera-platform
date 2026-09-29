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

#: contracts.failure_cause values (when the executor recorded one) -> class. Read as plain
#: strings so this module does not depend on that vocabulary landing first.
_CAUSE_CLASS: dict[str, str] = {
    "engine_crashed": ENGINE,
    "geometry_rejected": ENGINE,
    "mesh_quality": FIXABLE,
    "under_resolved": FIXABLE,
    "cell_budget": FIXABLE,
    "not_solvable": FIXABLE,
    "patch_not_captured": FIXABLE,
    "contract_mismatch": NEVER,
    "boundary_type": NEVER,
    "domain_extent": NEVER,
    "region_split": NEVER,
}
#: The executor's failed-gate key -> class, for a failure recorded without a cause. A gate that
#: can mean several things (manifest_valid) is read as the milder class: never an immediate switch.
_GATE_CLASS: dict[str, str] = {
    "finalize": ENGINE,
    "geometry": ENGINE,
    "manifest_valid": FIXABLE,
    "quality_floor": FIXABLE,
    "sicn_floor": FIXABLE,
    "resolution_floor": FIXABLE,
    "solvability": FIXABLE,
    "patch_contract": NEVER,
    "region_contract": NEVER,
    "boundary_types": NEVER,
    "domain_extent": NEVER,
    "regions_split": NEVER,
    "interfaces": NEVER,
}

#: What each failure means, said to the user. Short and plain: the full account of the failure is
#: the terminal message's; this is the half-sentence that says why the engine was left.
_REASON_BY_CAUSE: dict[str, str] = {
    "engine_crashed": "it stopped before it finished the mesh",
    "geometry_rejected": "it cannot take this geometry as it is",
    "mesh_quality": "its cells came out too badly shaped to use",
    "under_resolved": "it could not fit enough cells across the narrowest passage",
    "cell_budget": "its mesh came out bigger than one job allows",
    "not_solvable": "a trial solve on its mesh did not converge",
    "patch_not_captured": "it lost one of your boundaries",
}
_REASON_BY_GATE: dict[str, str] = {
    "finalize": _REASON_BY_CAUSE["engine_crashed"],
    "geometry": _REASON_BY_CAUSE["geometry_rejected"],
    "quality_floor": _REASON_BY_CAUSE["mesh_quality"],
    "sicn_floor": "its tetrahedra came out too badly shaped to use",
    "resolution_floor": _REASON_BY_CAUSE["under_resolved"],
    "solvability": _REASON_BY_CAUSE["not_solvable"],
    "manifest_valid": "its mesh failed a basic validity check",
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
    cause = str(state.get("executor_failure_cause", "") or "")
    gate = str(state.get("executor_failed_gate", "") or "")
    if cause in _CAUSE_CLASS:
        return Failure(_CAUSE_CLASS[cause], cause,
                       _REASON_BY_CAUSE.get(cause) or _REASON_BY_GATE.get(gate, _REASON_GENERIC))
    if gate:
        return Failure(_GATE_CLASS.get(gate, FIXABLE), gate, _REASON_BY_GATE.get(gate, _REASON_GENERIC))
    # no gate named at all: the executor had nothing to validate - the builder produced no mesh
    return Failure(ENGINE, "no_mesh", _REASON_BY_CAUSE["engine_crashed"])


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
    n = int(state.get("retry_count", 0) or 0)
    if n > 0 and not any(int(a.get("attempt", -1)) == n for a in attempts):
        entry: dict[str, Any] = {"attempt": n, "engine": str(state.get("engine") or "")}
        entry.update(failure.as_dict() if failure else {"kind": "passed"})
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
#: bars, the trial solve, and the review of layer coverage. Only these earn a fewer-layers offer.
_LAYER_SHAPED = frozenset({"mesh_quality", "quality_floor", "manifest_valid", "not_solvable",
                           "solvability", "review"})


def _review_faulted_layers(state: Mapping) -> bool:
    return any(isinstance(f, Mapping) and f.get("passed") is False
               and "layer" in str(f.get("axis_key") or "").lower()
               for f in (state.get("reviewer_axis_findings") or []))


def _fewer_layers(state: Mapping, engine: str, failure: Failure) -> dict | None:
    from meshpipeline.engines.registry import get_spec
    lr = layer_request(str(state.get("request_txt", "") or ""),
                       str(state.get("review_brief_txt", "") or ""))
    dm = get_spec(engine).delivered_mesh
    if (failure.cause not in _LAYER_SHAPED or not lr.count or lr.count < 2
            or dm is None or not dm.prism_layers):
        return None
    if failure.cause == "review" and not _review_faulted_layers(state):
        return None                 # the review faulted something fewer layers would not fix
    fewer = max(1, lr.count // 2)
    return {"kind": "fewer_layers", "engine": engine, "same_contract": False,
            "layers_from": lr.count, "layers_to": fewer,
            "changes": [f"{fewer} near-wall layers instead of {lr.count}"],
            "reply": f"use {fewer} layers",
            "text": (f"{engine_label(engine)} could not finish this mesh with the {lr.count} "
                     f"near-wall layers you asked for: {failure.reason}. Fewer layers usually "
                     f"fixes this. I can build the same mesh with {fewer} layers instead - same "
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
    rec["offer"] = (None if (succeeded or system_failure)
                    else offer(state, rec))
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
