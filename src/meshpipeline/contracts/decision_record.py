# Responsibility: Keep each run's "why" record: every decision the run made on its own, the written rule (docs/RULES.md) behind it, and the inputs that rule read.
# Owns: the record's entry shape, the rulebook version, the pure rule functions for the decisions made by rule (engine pick, engine switch, delivered attempt, the verdict), the replay that re-decides them from their recorded inputs, and the plain sentences an operator reads.
# Boundaries: pure over plain data. It decides nothing for a run: the deciding code stays where it is, and this module names the rule and can re-derive the outcome. A record that cannot be built never touches a verdict.
# Collaborates with: application/why_record (builds the record from a run's final state), application/run_reproducibility (writes it as the `why` section), application/review_delivery (cites the DEL rule), api/v1/admin_ops and devtools/why.py (read it).
"""THE WHY RECORD.

Each decision is one entry:

    stage         which decision ("engine_pick", "switch", "delivered", ...)
    decided       what was decided ("engine", "attempt")
    value         the outcome (an engine name, an attempt number)
    rule          the rule ID in docs/RULES.md, e.g. "ENG-4"
    also          the other rules that shaped the choice (ENG-1: only able engines were ranked)
    rules_version the rulebook version the rule was read from
    inputs        the measured or declared facts the rule read, by name
    alternatives  what else was on the table
    source        rule | user | ai-lever | guard-clock
    words         the decision in one plain sentence

THE REPLAY. Every stage with a pure rule function here can be decided again from its recorded
inputs alone (`replay`). A record whose replay disagrees with itself was not made by the rule it
names. That is the proof the rulebook describes the code, and the CI checks it.

THE HOOK FOR LATER DECISIONS. A decision made by a rule elsewhere (an engine's plan, a ladder
lever, a verdict) is added with `cite(job_id, ...)` as it is made; the run record collects it with
`cited_for(job_id)`. The rule ID it names must be in force in the rulebook (CI checks every ID the
code cites).
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

#: docs/RULES.md "Rulebook version". Raised whenever a rule changes what it decides.
RULES_VERSION = "11"

SOURCE_RULE = "rule"
SOURCE_USER = "user"
SOURCE_AI_LEVER = "ai-lever"
SOURCE_GUARD_CLOCK = "guard-clock"
SOURCES = (SOURCE_RULE, SOURCE_USER, SOURCE_AI_LEVER, SOURCE_GUARD_CLOCK)

STAGE_ENGINE_PICK = "engine_pick"
STAGE_SWITCH = "switch"
STAGE_DELIVERED = "delivered"
STAGE_VERDICT = "verdict"


@dataclass(frozen=True)
class Entry:
    stage: str
    decided: str
    value: Any
    rule: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    alternatives: Sequence[Any] = ()
    source: str = SOURCE_RULE
    words: str = ""
    rules_version: str = RULES_VERSION
    #: the other rules that shaped the choice (the candidates it was made among)
    also: Sequence[str] = ()

    def as_dict(self) -> dict:
        d = asdict(self)
        d["inputs"] = dict(self.inputs)
        d["alternatives"] = list(self.alternatives)
        d["also"] = list(self.also)
        return d


# #
# ENG: which engine the run was approved with
# #

#: engine_source values (pipeline/engine_fallback's provenance words)
_USER_NAMED = frozenset({"user_direct", "dispute"})
_CONFIRMED = "suggested_confirmed"


def engine_pick(inputs: Mapping[str, Any]) -> tuple[str, str]:
    """(the engine, the rule) the run's first engine follows from.

    inputs: engine_source; approved (the engine the run started on); recommended (the ranking's
    first engine, '' when nothing was ranked); measured (the run's geometry was measured, so the
    ranking read the lab record for its shape class - without it the ranking is no measured
    evidence); own_format (the candidates that read the upload's own format themselves, in
    ladder order)."""
    source = str(inputs.get("engine_source") or "")
    approved = str(inputs.get("approved") or "")
    recommended = str(inputs.get("recommended") or "")
    own = [str(e) for e in (inputs.get("own_format") or ()) if e]
    measured = inputs.get("measured", True) is True
    if source in _USER_NAMED:
        return approved, "ENG-6"
    if own and approved == own[0]:
        return approved, "ENG-7"
    if measured and recommended and approved == recommended:
        # the recommendation's own rule: the home engine (ENG-2), a record that clearly beat it
        # (ENG-3), or the ranked record (ENG-4)
        rule = str(inputs.get("recommended_rule") or "")
        return approved, rule if rule in ("ENG-2", "ENG-3") else "ENG-4"
    if source == _CONFIRMED:
        return approved, "ENG-9"
    # nobody named an engine and nothing was ranked: the run's own selection took the declared
    # default for its flow (pipeline/engine_select)
    return approved, "ENG-8"


#: The candidates an engine is picked among are the able ones (ENG-1).
ENGINE_PICK_ALSO = ("ENG-1",)
#: The engine a run moves to is the next one in the measured ranking (ENG-5).
SWITCH_ALSO = ("ENG-5",)


def engine_pick_source(rule: str) -> str:
    return SOURCE_USER if rule in ("ENG-6", "ENG-9") else SOURCE_RULE


# #
# SW: whether a run moved to another engine
# #

#: THE SWITCH RULES, by the opening words of engine_fallback.decide's `why`. Matched by prefix, so
#: a `why` that names an engine or a count still maps. tests/unit/hygiene/test_rulebook.py reads
#: every `Decision(why=...)` in engine_fallback.py and fails when one has no rule here.
#: INTEGRATION POINT (D1): when engine_fallback.Decision carries its own `rule`, this table goes.
SWITCH_RULES: tuple[tuple[str, str], ...] = (
    ("this engine's mesh of this shape came out broken", "SW-1"),
    ("this engine could not build a mesh", "SW-2"),
    ("the same failure repeated here", "SW-3"),
    ("the shared attempts go to the untried engine", "SW-4"),
    ("a measured check failed - this engine's own levers are tried first", "SW-5"),
    ("a retry here fits and still leaves every untried rung an attempt", "SW-6"),
    ("a review never moves the run to another engine", "SW-7"),
    ("the engine is not ours to change", "SW-8"),
    ("another engine cannot change this failure", "SW-9"),
    ("no untried engine delivers the approved mesh", "SW-10"),
    ("no untried engine meshes ", "SW-11"),
    ("no regular attempt is left", "SW-12"),
    ("too little time left for a ", "SW-13"),
    ("the fallback ladder is switched off", "SW-14"),
    ("the last attempt did not fail", "SW-15"),
    ("the system's own pick made no usable mesh", "SW-16"),
)
#: the switch rules a clock decides
_CLOCK_SWITCH_RULES = frozenset({"SW-13"})


def switch_rule(why: str) -> str:
    """The SW rule behind one of engine_fallback.decide's reasons ('' when it has none)."""
    text = str(why or "").lstrip()
    for prefix, rule in SWITCH_RULES:
        if text.startswith(prefix):
            return rule
    return ""


#: the failure kinds (engine_fallback's classes) each moving rule can follow: no mesh or a broken
#: one is an ENGINE failure; a repeated or shared-attempt move follows a FIXABLE one
_MOVES_ON = {"SW-1": ("engine",), "SW-2": ("engine",), "SW-3": ("fixable",), "SW-4": ("fixable",),
             "SW-16": ("engine", "fixable")}


def switch(inputs: Mapping[str, Any]) -> tuple[str, str]:
    """(the engine the attempt ran on, the rule). inputs: why, kind (the failure class), from, to.

    The recorded facts must fit the rule: a move needs a failure kind that rule moves on and a
    different engine to move to, so a record whose reason and facts disagree has no rule ('')."""
    rule = switch_rule(str(inputs.get("why") or ""))
    if rule in _MOVES_ON:
        kind, frm, to = str(inputs.get("kind") or ""), inputs.get("from"), inputs.get("to")
        if kind not in _MOVES_ON[rule] or not to or to == frm:
            return str(to or ""), ""
        return str(to), rule
    return str(inputs.get("from") or ""), rule


def switch_source(rule: str) -> str:
    return SOURCE_GUARD_CLOCK if rule in _CLOCK_SWITCH_RULES else SOURCE_RULE


# #
# DEL: which valid mesh the run delivered
# #

#: The rule behind each position of application/review_delivery.rank_key, in order. The first
#: position that separates the delivered mesh from the runner-up is the rule that delivered it.
DELIVERY_RULES = ("DEL-1", "DEL-2", "DEL-3", "DEL-4", "DEL-5", "DEL-6", "DEL-7", "DEL-8",
                  "DEL-9", "DEL-10")
DELIVERY_ONLY_ONE = "DEL-0"


def _ordered(rank: Sequence[Any]) -> tuple:
    # the record keeps an unmeasured metric (inf in rank_key) as None
    return tuple(math.inf if v is None else v for v in rank)


def delivery_rule(best: Sequence[Any], runner_up: Sequence[Any]) -> str:
    """The DEL rule that put `best` ahead of `runner_up` (both rank_key tuples)."""
    for i, (x, y) in enumerate(zip(_ordered(best), _ordered(runner_up), strict=False)):
        if x != y:
            return DELIVERY_RULES[i] if i < len(DELIVERY_RULES) else DELIVERY_RULES[-1]
    return DELIVERY_RULES[-1]


def delivered(inputs: Mapping[str, Any]) -> tuple[int | None, str]:
    """(the attempt delivered, the rule). inputs: candidates, each {attempt, engine, rank}."""
    pool = [c for c in (inputs.get("candidates") or ()) if isinstance(c, Mapping)]
    if not pool:
        return None, ""
    if len(pool) == 1:
        return _int(pool[0].get("attempt")), DELIVERY_ONLY_ONE
    ranked = sorted(pool, key=lambda c: _ordered(c.get("rank") or ()))
    best, runner = ranked[0], ranked[1]
    return _int(best.get("attempt")), delivery_rule(best.get("rank") or (), runner.get("rank") or ())


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# #
# VER: pass, delivered with concerns, or fail - from measured limits only
# #

VER_FAIL = "VER-1"
VER_CONCERNS = "VER-2"
VER_CLEAN = "VER-3"
VER_WRONG_PROBLEM = "VER-4"

OUTCOME_CLEAN = "pass"
OUTCOME_CONCERNS = "concerns"
OUTCOME_FAIL = "fail"
OUTCOME_WRONG_PROBLEM = "wrong_problem"

#: VER-4's measured checks: each way a mesh can be a different problem than the one confirmed
#: (the review taxonomy's classes) -> the executor gates that measure it. A mesh one of these gates
#: refused never validates, so the run fails on the gate's own measured account; this table only
#: names that failure VER-4 instead of VER-1. A class with no gate has no measured check yet: a
#: review can never stand in for one (it is a note), and the class is listed so a check is built.
WRONG_PROBLEM_CHECKS: Mapping[str, tuple[str, ...]] = {
    "wrong_side_meshed": ("port_areas", "region_contract"),
    "missing_geometry": ("file_fidelity",),
    "wrong_scale": (),                # measured as a VER-2 concern (the believable-size row)
    "wrong_flow_setup": ("domain_extent", "port_areas"),
    "wrong_boundaries": ("patch_contract", "boundary_types", "port_areas"),
    "no_flow_path": (),
    "shape_changed": ("file_fidelity",),
    "junk_regions": ("regions_split",),   # one-region engines: a VER-2 concern (regions row)
    "requirement_absent": (),         # measured as a VER-2 concern (the requirement rows)
}
WRONG_PROBLEM_GATES = frozenset(g for gates in WRONG_PROBLEM_CHECKS.values() for g in gates)


def _finite(v: Any) -> bool:
    return not (isinstance(v, float) and not math.isfinite(v))


def limit_met(measured: Any, op: str, limit: Any) -> bool | None:
    """Does a measured value meet its limit? None when nothing (or nothing finite) was measured -
    an unmeasured row is listed as such and decides nothing."""
    if measured is None or not _finite(measured):
        return None
    if op == "empty":
        return not measured
    if op == "==":
        return bool(measured == limit)
    if isinstance(measured, bool):
        return None
    try:
        m, lim = float(measured), float(limit)
    except (TypeError, ValueError):
        return None
    if op == "<=":
        return m <= lim
    if op == ">=":
        return m >= lim
    if op == "<":
        return m < lim
    if op == ">":
        return m > lim
    return None


def _rows(inputs: Mapping[str, Any], key: str) -> list[Mapping]:
    return [r for r in (inputs.get(key) or ()) if isinstance(r, Mapping)]


def missed_limits(inputs: Mapping[str, Any]) -> list[dict]:
    """The production limits (VER-2) the measured numbers miss, each as measured vs limit."""
    return [dict(r) for r in _rows(inputs, "limits")
            if limit_met(r.get("measured"), str(r.get("op") or ""), r.get("limit")) is False]


def failed_gating(inputs: Mapping[str, Any]) -> list[dict]:
    """The gating rows (VER-1) the measured numbers fail."""
    return [dict(r) for r in _rows(inputs, "gating")
            if limit_met(r.get("measured"), str(r.get("op") or ""), r.get("limit")) is False]


def verdict(inputs: Mapping[str, Any]) -> tuple[str, str]:
    """(the outcome, the VER rule) from the measured numbers alone.

    inputs: valid (the executor validated the delivered mesh: every gate passed); failed_gate (the
    gate that refused the last attempt, '' when none); gating (rows: key, measured, op, limit);
    limits (the production limits for this engine and purpose, the same shape). Nothing a review
    said is read here: a review's notes are recorded beside the verdict, never in it."""
    if inputs.get("valid") is not True or failed_gating(inputs):
        gate = str(inputs.get("failed_gate") or "")
        if gate in WRONG_PROBLEM_GATES:
            return OUTCOME_WRONG_PROBLEM, VER_WRONG_PROBLEM
        return OUTCOME_FAIL, VER_FAIL
    if missed_limits(inputs):
        return OUTCOME_CONCERNS, VER_CONCERNS
    return OUTCOME_CLEAN, VER_CLEAN


#: the rules a verdict can name, by outcome
VERDICT_RULES = {OUTCOME_CLEAN: VER_CLEAN, OUTCOME_CONCERNS: VER_CONCERNS,
                 OUTCOME_FAIL: VER_FAIL, OUTCOME_WRONG_PROBLEM: VER_WRONG_PROBLEM}


# #
# the replay
# #

#: stage -> the pure rule function that decides it again from the recorded inputs
REPLAYERS: dict[str, Callable[[Mapping[str, Any]], tuple[Any, str]]] = {
    STAGE_ENGINE_PICK: engine_pick,
    STAGE_SWITCH: switch,
    STAGE_DELIVERED: delivered,
    STAGE_VERDICT: verdict,
}


def replay(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every entry decided again from its recorded inputs. Returns the disagreements, in words
    ([] when the record agrees with its rules). An entry whose stage has no rule function here is
    a decision cited by its own code (`cite`), and is checked by that code's own tests."""
    out: list[str] = []
    for i, e in enumerate(entries):
        fn = REPLAYERS.get(str(e.get("stage") or ""))
        if fn is None:
            continue
        value, rule = fn(e.get("inputs") or {})
        if rule != e.get("rule") or value != e.get("value"):
            out.append(f"entry {i} ({e.get('stage')}): recorded {e.get('value')!r} by "
                       f"{e.get('rule')}, the rule gives {value!r} by {rule}")
    return out


def cited_rules(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every rule ID the entries name, their `also` rules included."""
    out: list[str] = []
    for e in entries:
        out.append(str(e.get("rule") or ""))
        out.extend(str(r) for r in (e.get("also") or ()))
    return out


# #
# the hook for decisions made elsewhere
# #

_PER_JOB = 400
_JOBS = 64
_LOCK = threading.Lock()
_CITED: OrderedDict[str, list[dict]] = OrderedDict()


def cite(job_id: str, *, stage: str, decided: str, value: Any, rule: str,
         inputs: Mapping[str, Any] | None = None, alternatives: Sequence[Any] = (),
         source: str = SOURCE_RULE, words: str = "") -> None:
    """Add one decision to this process's record for `job_id`, as it is made. Never raises: a
    record that cannot be kept never changes the decision."""
    if not job_id:
        return
    try:
        entry = Entry(stage=stage, decided=decided, value=value, rule=rule,
                      inputs=dict(inputs or {}), alternatives=tuple(alternatives),
                      source=source if source in SOURCES else SOURCE_RULE, words=words).as_dict()
    except Exception:  # noqa: BLE001 - a record never touches a decision
        return
    with _LOCK:
        log = _CITED.setdefault(job_id, [])
        _CITED.move_to_end(job_id)
        if len(log) < _PER_JOB:
            log.append(entry)
        while len(_CITED) > _JOBS:
            _CITED.popitem(last=False)


def cited_for(job_id: str) -> list[dict]:
    with _LOCK:
        return list(_CITED.get(job_id, []))


# #
# the words
# #

def sentences(why: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> list[str]:
    """The record as plain sentences, one per decision: "Engine snappy, rule ENG-4: ..."."""
    entries = why.get("decisions") if isinstance(why, Mapping) else why
    out = []
    for e in entries or ():
        if not isinstance(e, Mapping):
            continue
        head = f"{str(e.get('decided') or e.get('stage') or '').capitalize()} {e.get('value')}"
        rule = str(e.get("rule") or "no rule")
        out.append(f"{head}, rule {rule}: {e.get('words') or ''}".rstrip(": "))
    return out


def record(entries: Sequence[Entry | Mapping[str, Any]]) -> dict:
    """The `why` section of a run's reproducibility record."""
    rows = [e.as_dict() if isinstance(e, Entry) else dict(e) for e in entries]
    return {"rules_version": RULES_VERSION, "decisions": rows, "sentences": sentences(rows)}


__all__ = ["DELIVERY_ONLY_ONE", "DELIVERY_RULES", "REPLAYERS", "RULES_VERSION", "SOURCES",
           "SOURCE_AI_LEVER", "SOURCE_GUARD_CLOCK", "SOURCE_RULE", "SOURCE_USER",
           "STAGE_DELIVERED", "STAGE_ENGINE_PICK", "STAGE_SWITCH", "SWITCH_ALSO", "SWITCH_RULES",
           "ENGINE_PICK_ALSO", "Entry",
           "cite", "cited_for", "cited_rules", "delivered", "delivery_rule", "engine_pick",
           "engine_pick_source", "record", "replay", "sentences", "switch", "switch_rule",
           "switch_source",
           "OUTCOME_CONCERNS", "OUTCOME_FAIL", "OUTCOME_CLEAN", "OUTCOME_WRONG_PROBLEM",
           "STAGE_VERDICT", "VERDICT_RULES", "VER_CONCERNS", "VER_FAIL", "VER_CLEAN",
           "VER_WRONG_PROBLEM", "WRONG_PROBLEM_CHECKS", "WRONG_PROBLEM_GATES", "failed_gating",
           "limit_met", "missed_limits", "verdict"]
