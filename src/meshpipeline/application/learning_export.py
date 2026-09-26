# Responsibility: Turn one FINISHED job into the durable record the geometry agent's learning loop ingests.
# Owns: the export envelope's shape, which rows it is built from, and the delegation flag on every answer.
# Boundaries: a reader. It writes no platform row, runs no model, measures no geometry and judges nothing;
#             the verdicts and the split are the learning loop's (geometry_agent.learn.ingest_platform).
#             It selects no adapter: the object store is injected, and only the CLI entry point composes one.
# Collaborates with: persistence models for the rows, an injected ObjectStore for the delivered payload,
#                    agents.intake.recommendation for the one predicate that says a quote was a delegation.
"""THE SIGNALS ONLY A REAL JOB PRODUCES, written down before the job is forgotten.

`geometry_agent.learn` already ingests the corpus export and a recorded live evaluation run. Neither produces
the signals that are worth the most and cost nothing, because both are us talking to ourselves:

  - whether the CUSTOMER CORRECTED the proposal. The survey row already carries both halves - the question
    finder's per-place proposal in `asking.record` (`geometry_agent.ask.record.record_for`) and the customer's
    own choice in `answers` - and `answer: null` sits in every stored record row because nobody ever joined
    them. That join is this module's first job.
  - the REVIEWER's verdict and the quality gate's measured numbers.
  - the cells the builder DELIVERED against the cells the agent forecast.
  - what the LOOK said, beside the representation the job actually settled on.

WHY THE ENVELOPE IS PLATFORM VOCABULARY AND NOT AN OutcomeRecord. `geometry_agent` is a standalone package and
must never import `meshpipeline` (tests/unit/hygiene/test_architecture_boundaries.py). The dependency only runs
one way, so this module could import the agent's record schema - and deliberately does not. Two reasons, both
measured:

  1. THE RECORD NEEDS THINGS THIS PROCESS DOES NOT HAVE. A record's `split`, its labels and its `facts_sha256`
     come from the agent's own eval artefacts (`eval/split/export_split.json`, `eval/labels/export_labels.json`)
     and from `learn.store.facts_sha256`, which hashes the canonical measurement JSON. None of those is in the
     API image. Composing a record here would mean shipping half of one and calling it whole.
  2. THE TWO PACKAGES SPELL `facts_sha256` DIFFERENTLY AND IT WOULD HAVE BEEN SILENT. On this platform
     `geometry_surveys.facts_sha256` is the FILE's digest - `hexera.py:4563` reads it straight out of
     `facts["sha256"]` - and every row in the local database has `sha256 == facts_sha256`, verified on 8 rows.
     In `learn` the same name means the sha256 of the measurement's canonical JSON with `timings` removed
     (`learn/store.facts_sha256`), which is a different number and is what a record's file name and job key are
     built from. An exporter that filled the agent's field from the platform's column would key every record on
     the file instead of on the measurement, and a re-measurement that changed the answer would be invisible -
     the exact failure `ask.record.LEDGER_REQUIREMENTS` names for that column. So the envelope carries the
     platform's own two digests under the platform's own names (`source_sha256`, `facts_sha256_platform`) and
     the ingester derives the learn-side one itself, from the measurement document it is handed.

WHAT THIS MODULE DOES DECIDE, because only this side can: whether an answer's quote was a STANDING DELEGATION.
`geometry_survey.said_by_customer` accepts a quote from an earlier message only when
`agents.intake.recommendation.choice_deferred` says it is a deferral, so that function is the authority on
whether a stored answer rests on a choice or on "you decide". The value stored for a delegated answer is this
platform's own pick wearing the customer's authority; counting it as a confirmation would report agreement on
exactly the jobs where the customer said nothing about the substance. The flag is written here, beside the
answer, and the ingester refuses to score a row that does not carry it.

AND TWO MORE READINGS OF THE SAME WORDS, for the same reason and by the same readers (`quote_reading`): whether
the quote carries a DENIAL, and whether it NAMES the mouth the row placed. Both were missing and the loop was
guessing at them with substring tests of its own:

  - a quote the customer used to REFUSE an option contains that option's word, so "o4 is not the inlet" passed
    the loop's own "the words name the answer" gate and was published as the customer choosing `inlet`. Ground
    truth asserting the customer chose what they refused teaches the opposite of what happened.
  - a delegation that places one mouth and leaves the rest ("o5 is the inlet and o6 the outlet, the rest you
    decide") read as a BLANKET delegation, and the two mouths they had named were vetoed along with the rest.

`geometry_survey.accepts_a_proposal` and `_the_proposal_recorded` already decide both of those questions, with
`engine_selection._DENY` and `geometry_survey._mentions`, and they decide them for the same words at the moment
the answer is taken. So the loop is handed their reading instead of forming a second one. A second opinion about
what a message means is the defect this chain keeps producing, and here it decides where an inlet goes.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EXPORT_SCHEMA = "meshpipeline.learning_export.v1"

#: Only a job that reached a terminal state has an outcome. `pending` and `running` are refused by name rather
#: than exported with empty halves: a record whose reviewer block is empty because the review has not happened
#: is indistinguishable from one whose review failed, and the ingester would count both as "no verdict".
TERMINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed")

#: The logical artifact that carries the delivered mesh's own numbers. The quality block is written by the
#: pipeline from the engine's manifest (`render.mesh_facts.quality`, `application.viewer_payload`) and is the
#: only durable place the DELIVERED cell count exists - `simulation_jobs.final_result` carries the verdict and
#: not a single metric, verified on job 489fa1ab.
VIEWER_DATA_KEY = "viewer_data"

COUNTERFACTUAL_SCHEMA = "meshpipeline.learning_export.without_the_look.v1"

#: The one look status the counterfactual is a real comparison for. A look that failed or was never attempted
#: composes the same document either way, and calling that "the look changed nothing" would count a deployment
#: with no reader as evidence that the reader is not needed. It is a copy of `geometry_survey.LOOK_OK` and
#: `tests/unit/application/test_learning_export.py` pins the two together, because this module may not import
#: that one at module scope (the package import is deliberately lazy here).
LOOK_OK = "ok"

#: THE FIELDS THE TWO COMPOSITIONS ARE COMPARED ON, and the list is closed on purpose.
#:
#: A diff over the whole composed document would report the LOOK ITSELF as a difference - `composed["look"]`
#: and `planner_block["look"]` are the look block copied through, so a blanket comparison says "the look
#: changed the look" on every job that had one and the number would be 100 percent by construction. These are
#: the fields that are a PROPOSAL: what the platform would hand the customer and the builder. Each one is
#: extracted by name in `proposal_of`, so a field added to the composition is not silently added to this
#: measurement.
PROPOSAL_FIELDS: tuple[str, ...] = (
    "representation", "fluid_side",
    "forecast.cells_low", "forecast.cells_high", "forecast.over_cap", "forecast.tier",
    "planner_block.representation", "planner_block.inlet_opening_id", "planner_block.inlet_bore_m",
    "planner_block.smallest_port_min_dim_m", "planner_block.agent_forecast_cells",
    "planner_block.customer_cell_cap", "planner_block.places",
    "questions.put", "warnings", "uncertainties",
)

#: The per-place and per-question fields, which cannot be listed above because their names carry an id.
#: `openings[<id>].role` is spelled exactly as `ask.record.record_for` spells a target's `field`, and as
#: `learn.schema.AnswerOutcome.field` therefore spells it, so the ingester can join a delta to the label for
#: the same field without a translation table.
PROPOSAL_FIELD_PREFIXES: tuple[str, ...] = ("openings[", "questions[")

_SLUG = re.compile(r"[^a-z0-9]+")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds")
    return str(value)


def _enum(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def case_name(original_filename: str, source_sha256: str) -> str:
    """The case name a platform job carries into the learning loop.

    PREFIXED `platform__` ON PURPOSE. `learn.store.record_paths` picks a record's side of the corpus split by
    matching its case name against the split file's two lists, so a platform case must be a name that is on
    neither. The prefix guarantees it whatever the customer called their file, including a file whose stem is
    exactly a corpus case name. The sha's first eight characters keep two different parts with the same
    filename apart.
    """
    stem = _SLUG.sub("_", Path(str(original_filename or "part")).stem.lower()).strip("_") or "part"
    return f"platform__{stem}__{str(source_sha256 or '')[:8]}"


def _delegated(words: str) -> bool:
    from meshpipeline.agents.intake.recommendation import choice_deferred
    return bool(choice_deferred(str(words or "")))


#: The name of the reading below, written on every row that carries it, so a record says WHO decided what the
#: customer's words meant. Nothing downstream may hold a second opinion about it.
QUOTE_READER = "meshpipeline.application.geometry_survey"


def _refuses(words: str) -> bool:
    """Does this quote carry a denial, by the SAME reader `accepts_a_proposal` consults?

    `geometry_survey.accepts_a_proposal` reads a denial with `engine_selection._DENY` over
    `engine_selection._words`, and takes a standing delegation back when it finds one, for the reason its own
    comment gives: "a customer's refusal was read as permission to record OUR proposal". The learning loop
    needs the same reading for the same words and it must not be a second one - `_norm(words)` there and a
    substring test here would diverge silently and the first divergence would be a fabricated label.
    """
    from meshpipeline.agents.intake.engine_selection import _DENY, _norm, _words
    return any(w in _DENY for w in _words(_norm(str(words or ""))))


def _names_its_subject(words: str, subject: Any) -> bool | None:
    """Do the customer's words NAME the mouth this row placed? None when the row places no mouth.

    `geometry_survey._mentions` is the reader `_the_proposal_recorded` already uses to hand a message back when
    it names one of the mouths the question named: "a message that names one of these mouths is not an
    acceptance of a reading of all of them". A delegation that names a mouth is the same shape - the customer
    left the REST to us and placed that one themselves - and the loop may not veto the place they named.
    """
    place = str(subject or "").strip()
    if not place:
        return None
    from meshpipeline.application.geometry_survey import _mentions
    return bool(_mentions(str(words or ""), place))


def quote_reading(answer: dict[str, Any]) -> dict[str, Any]:
    """What the customer's words MEAN, decided here because only this side holds the readers that decide it.

    THREE READINGS, ALL OF THEM THE APPLICATION'S OWN, none of them re-derived anywhere else:

      `delegated`          `recommendation.choice_deferred`, the predicate `said_by_customer` used to accept
                           the quote in the first place. A delegation's stored value is this platform's pick.
      `refuses`            the denial reader `accepts_a_proposal` consults. "o4 is not the inlet" contains the
                           word "inlet" and is not a choice of it; a loop that reads it as one publishes the
                           customer choosing what they refused.
      `names_its_subject`  `geometry_survey._mentions`, the reader that hands a message back when it names a
                           mouth. It is what tells a BLANKET delegation ("everything else you decide") from a
                           delegation that placed this mouth itself ("o5 is the inlet, the rest you decide").

    WHY IT IS WRITTEN HERE AND NOT READ THERE. The learning loop is a separate package and cannot import these
    readers; a copy of them beside it would be a third opinion about what a message means, which is the defect
    this chain keeps producing (`geometry_survey.accepts_a_proposal`'s own comment: "a second opinion about
    what 'no' means is a second thing to keep in step, and the first divergence would be silent and in a
    mesh"). So the reading rides on the row, named, and the ingester consumes it and decides nothing.
    """
    words = str(answer.get("words") or "")
    return {"reader": QUOTE_READER, "delegated": _delegated(words), "refuses": _refuses(words),
            "names_its_subject": _names_its_subject(words, answer.get("subject"))}


def answer_rows(answers: Any) -> list[dict[str, Any]]:
    """The survey's stored answers with the application's own reading of each quote added.

    `delegated` stays at the top level, where it has always been and where `learn.schema.AnswerOutcome` reads
    it, and `quote` carries the whole reading including that same flag - ONE call, written twice, so the
    ingester can check the two agree and refuse a row where they do not. Two fields that could disagree about
    one truth is a fact that lies; two copies of one call cannot.

    Nothing else is changed, nothing is dropped and no row is reordered - a retired row travels too, because
    the record still has to say what the customer said FIRST and what replaced it.
    """
    out: list[dict[str, Any]] = []
    for a in answers if isinstance(answers, list) else []:
        if not isinstance(a, dict):
            continue
        reading = quote_reading(a)
        out.append({**a, "delegated": reading["delegated"], "quote": reading})
    return out


def quality_from_payload(payload: Any) -> dict[str, Any]:
    """The delivered mesh's own numbers, as the pipeline wrote them, copied and not recomputed.

    `criteria` is flattened to `{key: {measured, ok_when, passed, gating}}` because that is the only place
    several of the metrics exist: on job 489fa1ab the top-level block carries `cell_count` 258540, `engine`,
    `mesh_units` and `production_grade`, and `max_non_ortho` 64.9193, `layer_coverage_pct` 94.5776,
    `wall_faces` 8846, `skew_fraction` 0.0 and `fatal` [] are criteria rows.
    """
    if not isinstance(payload, dict):
        return {}
    _q = payload.get("quality")
    q: dict = _q if isinstance(_q, dict) else {}
    criteria: dict[str, Any] = {}
    for c in q.get("criteria") or []:
        if isinstance(c, dict) and c.get("key"):
            criteria[str(c["key"])] = {"measured": c.get("measured"), "ok_when": c.get("ok_when"),
                                       "passed": c.get("passed"), "gating": c.get("gating")}
    _review = payload.get("review")
    review: dict = _review if isinstance(_review, dict) else {}
    return {"cell_count": q.get("cell_count"), "engine": q.get("engine"), "mesh_units": q.get("mesh_units"),
            "production_grade": q.get("production_grade"), "criteria": criteria,
            "review_verdict": review.get("verdict"), "attempt_reviewed": review.get("attempt_reviewed"),
            "rebuild_required": review.get("rebuild_required"),
            "mesh_available": payload.get("mesh_available")}


# -------------------------------------------------------------------------------------------------
# WHAT THE PLATFORM WOULD HAVE PROPOSED WITHOUT THE LOOK
# -------------------------------------------------------------------------------------------------
# THE VALUE OF THE LOOK, ON EVERY JOB, FOR NOTHING. The measurement runs at upload whatever happens and the
# look rides on the same document (`geometry_measurements.document["look"]`), and `geometry_survey.composition`
# reads that block as an INPUT. So for any finished job the composition can be run again with the look taken
# out, and the difference between the two compositions is the look's whole contribution to what the customer
# and the builder were handed. No second arm, no second model call, no corpus run: `composition` measures
# nothing and calls nothing, it composes the stored facts.
#
# BOTH SIDES ARE RECOMPUTED, AND THAT IS THE POINT. The obvious version of this compares the STORED row against
# a fresh composition with the look removed - and it is wrong, because the stored row was composed by the code
# that was deployed when the job ran and the fresh one by the code running now. Every difference between two
# agent builds would be counted as the look's work. So both halves are composed here, in one process, under one
# build, and the only thing that differs between them is the look.
#
# AND THE CHECK IS A DIFFERENT CHECK. `replay_mismatch` compares the WITH-the-look recomposition against the
# fields the stored row itself carries (`stored_proposal`). That comparison can fail where the diff cannot: a
# composition whose replay does not reproduce the row is a composition about some other code version, and then
# the delta list is not about the look and says so instead of being published.


def _as_text(value: Any) -> str | None:
    """One field's value as a comparable string. None stays None, because absent is not a value."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, sort_keys=True, default=str)


def _places(block: Any) -> str | None:
    """The builder's place list as `kind:id`, sorted. The coordinates are left out: they are the measurement's
    and cannot move with the look, and a float inside a compared string is noise looking for a difference."""
    if not isinstance(block, dict) or not isinstance(block.get("places"), list):
        return None
    return ",".join(sorted(f"{p.get('kind')}:{p.get('id')}" for p in block["places"] if isinstance(p, dict)))


def _roles_proposed(asking: dict) -> dict[str, str | None]:
    """`{openings[<id>].role: the role this composition would have taken unasked}`.

    Read off `asking["record"][<qid>]["targets"]`, which is `ask.record.record_for`'s row and the ONE place the
    platform's per-place proposal is written down. The key is the target's own `field`, unchanged, so a delta
    and the label for the same field are the same string on both sides of the export.
    """
    out: dict[str, str | None] = {}
    for row in (asking.get("record") or {}).values():
        if not isinstance(row, dict):
            continue
        for tgt in row.get("targets") or []:
            if isinstance(tgt, dict) and tgt.get("field") and tgt.get("place"):
                out[str(tgt["field"])] = _as_text(tgt.get("proposed"))
    return out


def proposal_of(made: dict) -> dict[str, str | None]:
    """One composition's PROPOSAL as a flat `{field: value}` map, over `PROPOSAL_FIELDS` and nothing else.

    `made` is `geometry_survey.composition`'s return value. Every field is pulled by name and nothing is
    walked, so the look block the composition carries as an input cannot leak into the comparison.
    """
    composed = made.get("composed") or {}
    _forecast = composed.get("forecast")
    forecast: dict = _forecast if isinstance(_forecast, dict) else {}
    _planner = composed.get("planner_block")
    planner: dict = _planner if isinstance(_planner, dict) else {}
    # Bound once and annotated so the narrowing is PROVABLE. Written as
    # `made.get("asking") if isinstance(made.get("asking"), dict) else {}` it is correct at runtime
    # and unprovable to a reader: two calls, and nothing says they return the same object. The mypy
    # ratchet caught it as `Any | dict | None` reaching `_roles_proposed(asking: dict)`.
    _asking = made.get("asking")
    asking: dict = _asking if isinstance(_asking, dict) else {}
    uncertainties = getattr(made.get("survey"), "uncertainties", None) or []
    out: dict[str, str | None] = {
        "representation": _as_text(composed.get("representation")),
        "fluid_side": _as_text(composed.get("fluid_side")),
        "forecast.cells_low": _as_text(forecast.get("cells_low")),
        "forecast.cells_high": _as_text(forecast.get("cells_high")),
        "forecast.over_cap": _as_text(forecast.get("over_cap")),
        "forecast.tier": _as_text(forecast.get("tier")),
        "planner_block.representation": _as_text(planner.get("representation")),
        "planner_block.inlet_opening_id": _as_text(planner.get("inlet_opening_id")),
        "planner_block.inlet_bore_m": _as_text(planner.get("inlet_bore_m")),
        "planner_block.smallest_port_min_dim_m": _as_text(planner.get("smallest_port_min_dim_m")),
        "planner_block.agent_forecast_cells": _as_text(planner.get("agent_forecast_cells")),
        "planner_block.customer_cell_cap": _as_text(planner.get("customer_cell_cap")),
        "planner_block.places": _places(planner),
        #: WHICH QUESTIONS THE CUSTOMER WAS PUT, IN THE FINDER'S OWN ORDER. This is the field that caught the
        #: one real difference in the first batch: on job c17d3523 the look raised `q_look_dispute` and the
        #: composition without it put `q_port_roles` alone. A per-job boolean would have said "the look changed
        #: something" and thrown away WHAT.
        "questions.put": _as_text(list(asking.get("put") or [])),
        "warnings": _as_text(sorted({str(w.get("kind")) for w in (composed.get("warnings") or [])
                                     if isinstance(w, dict) and w.get("kind")})),
        "uncertainties": _as_text(sorted(str(getattr(u, "id", "")) for u in uncertainties)),
    }
    out.update(_roles_proposed(asking))
    for qid in (asking.get("put") or []):
        row = (asking.get("record") or {}).get(str(qid))
        out[f"questions[{qid}].default"] = _as_text(row.get("default")) if isinstance(row, dict) else None
    return out


def stored_proposal(survey: Any) -> dict[str, str | None]:
    """The same map for the fields the STORED row itself carries. The replay's own check.

    Deliberately a SUBSET: the row keeps `composed_for.representation`, its `planner_block` and its `asking`
    row, and not the whole composed document. Every field it can supply is compared, and a field it cannot is
    absent here rather than filled with a guess.
    """
    comp = dict(getattr(survey, "composed_for", None) or {})
    _planner = getattr(survey, "planner_block", None)
    planner: dict = _planner if isinstance(_planner, dict) else {}
    _asking = getattr(survey, "asking", None)
    asking: dict = dict(_asking) if isinstance(_asking, dict) else {}
    out: dict[str, str | None] = {
        "representation": _as_text(comp.get("representation")),
        "planner_block.representation": _as_text(planner.get("representation")),
        "planner_block.inlet_opening_id": _as_text(planner.get("inlet_opening_id")),
        "planner_block.inlet_bore_m": _as_text(planner.get("inlet_bore_m")),
        "planner_block.agent_forecast_cells": _as_text(planner.get("agent_forecast_cells")),
        "planner_block.customer_cell_cap": _as_text(planner.get("customer_cell_cap")),
        "planner_block.places": _places(planner),
        #: ONLY WHERE THE ROW HAS AN ASKING BLOCK. `_as_text([])` is `"[]"`, a real value, so a row composed
        #: before the question finder was wired would claim it put no questions and every replay would be
        #: reported unfaithful against a claim the row never made.
        **({"questions.put": _as_text(list(asking.get("put") or []))} if asking else {}),
    }
    out.update(_roles_proposed(asking))
    return {k: v for k, v in out.items() if v is not None}


def deltas(with_look: dict[str, str | None], no_look: dict[str, str | None]) -> list[dict[str, Any]]:
    """One row per field, INCLUDING every field the look left alone.

    A job where the look changed nothing is as informative as one where it changed everything, and a per-job
    boolean throws that away: it cannot say which field the look earns its cost on, and it cannot say that a
    field the look never moves is a field the look is not needed for. Every row carries `changed`, so a reader
    counts it either way round.
    """
    rows = []
    for field in sorted(set(with_look) | set(no_look)):
        a, b = with_look.get(field), no_look.get(field)
        rows.append({"field": field, "with_look": a, "without_look": b, "changed": a != b})
    return rows


_SHORT_SHA = re.compile(r"\+g([0-9a-f]{7,40})")


def same_build(version: str, stamped: str) -> bool | None:
    """Whether the build that recomposed is the build the job was measured under. None when it cannot be said.

    NONE, NOT FALSE, WHEN EITHER SIDE HAS NO COMMIT IN IT. R2 turns on whether two numbers come from one build,
    and "we cannot tell" is a third answer: a wheel built outside `deploy/vendor_geometry_agent.sh` has no
    `+g<sha>` segment, and a measurement stamped with a bare git sha has no version. Reporting False there
    would read as "a different build", which is a claim neither side supports.
    """
    a, b = _SHORT_SHA.search(str(version or "")), _SHORT_SHA.search(str(stamped or ""))
    if a and b:
        one, two = a.group(1), b.group(1)
        return one.startswith(two) or two.startswith(one)
    if not version or not stamped:
        return None
    #: a bare sha on one side and a version on the other: comparable only if one contains the other
    if len(str(stamped)) >= 7 and str(stamped) in str(version):
        return True
    return None if not (a or b) else False


def _agent_version() -> str:
    try:
        import importlib.metadata as md
        return str(md.version("hexera-geometry-agent"))
    except Exception:                              # noqa: BLE001 - a missing stamp is recorded, never invented
        return ""


def without_the_look(document: Any, survey: Any) -> dict[str, Any]:
    """What this job's survey WOULD have proposed from the measurement alone, beside what it did propose.

    Computed, never guessed. `computed` False carries the reason in `why_not` and NO field list, because a
    comparison that could not be made is a missing comparison and an empty one reads as agreement.
    """
    from meshpipeline.application.geometry_survey import composed_inputs, composition
    started = time.perf_counter()
    doc = document if isinstance(document, dict) else {}
    _look = doc.get("look")
    look: dict = _look if isinstance(_look, dict) else {}
    _stamp = doc.get("stamp")
    stamp: dict = _stamp if isinstance(_stamp, dict) else {}
    out: dict[str, Any] = {
        "schema": COUNTERFACTUAL_SCHEMA,
        "computed": False,
        "why_not": "",
        "look_status": str(look.get("status") or "not_attempted"),
        #: R2. The counterfactual is composed by the build that EXPORTS, which is not always the build that
        #: measured the job, and two batches composed under different builds are not one population. Both
        #: stamps travel, and `same_build_as_the_job` says in one field whether they are the same.
        "recomposed_under": {"agent_version": _agent_version(),
                             "platform_sha": str(stamp.get("platform_sha") or "")},
        "job_measured_under": {"agent_git_sha": str(stamp.get("agent_git_sha") or ""),
                               "facts_schema_version": stamp.get("facts_schema_version")},
        "same_build_as_the_job": None,
        "inputs": {},
        "replay_faithful": None,
        #: how many fields the stored row could check the replay on. NONE CHECKED IS NOT FAITHFUL: a row that
        #: carries no comparable field cannot vouch for the replay, and reporting `replay_faithful: true` there
        #: would be a check that always passes, which is the one thing a check may not be.
        "replay_checked": 0,
        "replay_mismatch": [],
        "fields": [],
        "seconds": None,
    }
    if survey is None:
        out["why_not"] = "there is no survey row for this job, so there is no composition to run again"
        return out
    if doc.get("status") != "ok" or not isinstance(doc.get("facts"), dict) or not doc.get("facts"):
        out["why_not"] = "the stored measurement cannot be composed, so neither half can be recomputed"
        return out
    if not look or str(look.get("status")) != "ok":
        out["why_not"] = (f"the look on this job is {look.get('status') or 'absent'}, so the composition with "
                          f"the look and the composition without it are the same composition")
        return out
    inputs = composed_inputs({"composed_for": dict(getattr(survey, "composed_for", None) or {})})
    out["inputs"] = json.loads(json.dumps(inputs, default=str))
    try:
        a = composition(doc, **inputs)
        b = composition({k: v for k, v in doc.items() if k != "look"}, **inputs)
    except Exception as exc:                       # noqa: BLE001 - an export never fails on this block
        out["why_not"] = f"the composition could not be run again ({type(exc).__name__}: {exc})"[:300]
        return out
    with_look, no_look = proposal_of(a), proposal_of(b)
    stored = stored_proposal(survey)
    checked = sorted(f for f in stored if f in with_look)
    mismatch = [f for f in checked if with_look[f] != stored[f]]
    version = out["recomposed_under"]["agent_version"]
    out.update({
        "computed": True,
        "replay_faithful": None if not checked else not mismatch,
        "replay_checked": len(checked),
        "replay_mismatch": [{"field": f, "stored": stored[f], "replayed": with_look[f]} for f in mismatch],
        "fields": deltas(with_look, no_look),
        "seconds": round(time.perf_counter() - started, 3),
        "same_build_as_the_job": same_build(version, out["job_measured_under"]["agent_git_sha"]),
    })
    return out


def envelope(*, job: Any, source: Any, interpretation: Any, measurement: Any, survey: Any,
             viewer_payload: Any = None, quality_unavailable: str = "",
             counterfactual: dict[str, Any] | None = None) -> dict[str, Any]:
    """One finished job as the learning loop's input document. A pure function of the rows it is handed.

    EVERY HALF THAT IS MISSING SAYS SO BY NAME. A job with no survey row, no measurement, no interpretation or
    no delivered payload is still exported, with that half null and the reason in `absent`, because a job that
    ran without a survey is a real population and dropping it would make the batch describe only the jobs that
    went well. `absent` is what the ingester reports and what the runbook counts.
    """
    fr = job.final_result if isinstance(getattr(job, "final_result", None), dict) else {}
    doc = measurement.document if measurement is not None and isinstance(measurement.document, dict) else {}
    _stamp = doc.get("stamp")
    stamp: dict = _stamp if isinstance(_stamp, dict) else {}
    step = survey.geometry_step if survey is not None and isinstance(survey.geometry_step, dict) else None
    absent: list[str] = []
    for name, row in (("geometry_source", source), ("interpretation", interpretation),
                      ("measurement", measurement), ("survey", survey)):
        if row is None:
            absent.append(f"no {name} row for this job")
    if survey is not None and step is None:
        absent.append("the geometry agent's step never ran on this job (geometry_step is null)")
    if viewer_payload is None:
        absent.append(quality_unavailable or f"the {VIEWER_DATA_KEY} payload was not read")
    # A COUNTERFACTUAL THAT COULD NOT BE COMPUTED ON A JOB THAT HAD A LOOK IS A MISSING HALF, and a replay
    # that does not reproduce the row is worse than missing, because its delta list would be read as the
    # look's work. Both are named here so the runbook's "read `absent` first" covers them.
    cf = counterfactual if isinstance(counterfactual, dict) else {}
    if cf and not cf.get("computed") and cf.get("look_status") == LOOK_OK:
        absent.append(f"the without-the-look composition could not be computed: {cf.get('why_not')}")
    if cf.get("computed") and cf.get("replay_faithful") is False:
        absent.append("the with-the-look recomposition does not reproduce the stored survey row, so this "
                      "job's without-the-look deltas are not about the look")

    src_sha = str(getattr(source, "sha256", "") or "")
    return {
        "schema": EXPORT_SCHEMA,
        "exported_at": _now(),
        "case": case_name(getattr(source, "original_filename", "") or "", src_sha),
        "absent": absent,
        "job": {
            "job_id": str(job.id),
            "status": _enum(job.status),
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "ended_at": _iso(job.ended_at),
            "current_attempt": job.current_attempt,
            "failed_reason": _enum(job.failed_reason),
            # THE VERDICT AND THE METRICS COME FROM TWO PLACES AND THAT IS NOT AN OVERSIGHT.
            # `final_result` is the durable terminal verdict and carries no metric at all; the metrics are in
            # the delivered payload. A record that reported one as the other would be a quality claim with no
            # measurement behind it.
            "final_result": dict(fr),
        },
        "source": None if source is None else {
            "geometry_source_id": str(source.id),
            "original_filename": source.original_filename,
            "suffix_hint": source.suffix_hint,
            "source_sha256": src_sha,
            "size_bytes": source.size_bytes,
            "bytes_available": bool(getattr(source, "bytes_available", False)),
        },
        "interpretation": None if interpretation is None else {
            "unit": interpretation.unit, "scale_to_metres": interpretation.scale_to_metres,
            "basis": interpretation.basis, "evidence": interpretation.evidence,
        },
        "measurement": None if measurement is None else {
            "status": measurement.status, "reason": measurement.reason, "purpose": measurement.purpose,
            #: THE PLATFORM'S OWN NAME FOR THE FILE DIGEST. See the module docstring: this is not the
            #: learning loop's `facts_sha256` and must never be copied into that field.
            "facts_sha256_platform": measurement.sha256,
            "facts_schema_version": measurement.facts_schema_version,
            "agent_git_sha": measurement.agent_git_sha,
            "measure_seconds": measurement.measure_seconds,
            "platform_sha": stamp.get("platform_sha"),
            "measurement_revision": stamp.get("measurement_revision"),
            #: the whole `hexera.report_measured` document, including its `facts` block. The ingester rebuilds
            #: GeometryFacts from it and derives the learn-side measurement digest itself.
            "document": doc,
        },
        "survey": None if survey is None else {
            "survey_id": str(survey.id),
            "session_id": None if survey.session_id is None else str(survey.session_id),
            "stage": survey.stage,
            "state_schema": survey.state_schema,
            "source_sha256": survey.sha256,
            "facts_sha256_platform": survey.facts_sha256,
            "agent_git_sha": survey.agent_git_sha,
            "look_queued": survey.look_queued,
            "composed_for": dict(survey.composed_for or {}),
            "asking": dict(survey.asking or {}),
            "asked": list(survey.asked or []),
            "answers": answer_rows(survey.answers),
            "late": dict(survey.late) if isinstance(survey.late, dict) else None,
            "planner_block": dict(survey.planner_block) if isinstance(survey.planner_block, dict) else None,
            "created_at": _iso(survey.created_at),
            "updated_at": _iso(survey.updated_at),
        },
        # The step, minus its ledger's events: the events are a median 24 kB per plan and every fact the
        # learning loop reads off them is already in `plan`, `envelope` and the answers.
        "step": None if step is None else {k: v for k, v in step.items() if k != "ledger"},
        "quality": quality_from_payload(viewer_payload),
        #: WHAT THE SAME SURVEY WOULD HAVE PROPOSED WITH NO LOOK, computed (see `without_the_look`). None on a
        #: caller that did not ask for it; the ingester reads `computed` and `why_not` and never an empty list.
        "look_counterfactual": counterfactual,
    }


# -------------------------------------------------------------------------------------------------
# reading the rows
# -------------------------------------------------------------------------------------------------

async def _rows_for(db: Any, job: Any) -> tuple[Any, Any, Any, Any]:
    from sqlalchemy import select

    from meshpipeline.persistence.models import (
        GeometryInterpretationRow,
        GeometryMeasurement,
        GeometrySurvey,
    )
    source = job.geometry_source
    survey = measurement = interp = None
    if job.geometry_source_id is not None:
        survey = (await db.execute(select(GeometrySurvey).where(
            GeometrySurvey.geometry_source_id == job.geometry_source_id))).scalars().first()
        measurement = (await db.execute(select(GeometryMeasurement).where(
            GeometryMeasurement.geometry_source_id == job.geometry_source_id))).scalars().first()
    if job.geometry_interpretation_id is not None:
        interp = (await db.execute(select(GeometryInterpretationRow).where(
            GeometryInterpretationRow.id == job.geometry_interpretation_id))).scalars().first()
    return source, interp, measurement, survey


async def _viewer_payload(db: Any, job_id: Any, store: Any) -> tuple[Any, str]:
    """The delivered payload, or None and the reason. Never raises: a job whose bytes are gone is still a job."""
    from sqlalchemy import select

    from meshpipeline.persistence.models import Artifact
    if store is None:
        return None, "no object store was given to the exporter"
    row = (await db.execute(select(Artifact).where(
        Artifact.job_id == job_id, Artifact.logical_key == VIEWER_DATA_KEY))).scalars().first()
    if row is None:
        return None, f"the job delivered no {VIEWER_DATA_KEY} artifact"
    try:
        return json.loads(store.get_bytes(object_key=row.storage_key)), ""
    except Exception as exc:                       # noqa: BLE001 - a missing object is a fact, not a failure
        return None, f"the {VIEWER_DATA_KEY} object could not be read ({type(exc).__name__})"


async def export_job(db: Any, job_id: str, *, store: Any = None) -> dict[str, Any]:
    """One job's envelope. Raises ValueError when the job does not exist or has not finished."""
    from sqlalchemy import select

    from meshpipeline.persistence.models import SimulationJob
    job = (await db.execute(select(SimulationJob).where(SimulationJob.id == job_id))).scalars().first()
    if job is None:
        raise ValueError(f"no job {job_id}")
    status = _enum(job.status)
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"job {job_id} is {status}, not finished; the terminal statuses are "
                         f"{list(TERMINAL_STATUSES)}")
    source, interp, measurement, survey = await _rows_for(db, job)
    payload, why = await _viewer_payload(db, job.id, store)
    # THE ONE PIECE OF COMPUTATION THIS MODULE DOES. Everything else here is a read; this recomposes the survey
    # twice (with the look and without it) to measure what the look was worth on this job. It is affordable
    # because `geometry_survey.composition` runs no model and measures no geometry - it composes the stored
    # facts - and it never raises: `without_the_look` returns `computed: False` with the reason.
    doc = measurement.document if measurement is not None and isinstance(measurement.document, dict) else {}
    return envelope(job=job, source=source, interpretation=interp, measurement=measurement, survey=survey,
                    viewer_payload=payload, quality_unavailable=why,
                    counterfactual=without_the_look(doc, survey))


def write_envelope(env: dict[str, Any], out_dir: Path) -> Path:
    """`<out_dir>/<job id>.json`, keyed on the job so a second export of one job replaces its own file."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{env['job']['job_id']}.json"
    path.write_text(json.dumps(env, indent=1, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


async def export_finished(db: Any, out_dir: Path, *, store: Any = None, limit: int = 50,
                          statuses: tuple[str, ...] = TERMINAL_STATUSES,
                          job_ids: list[str] | None = None, log=logger.info) -> dict[str, Any]:
    """Every finished job, newest first, as one envelope each. Idempotent: re-running replaces the same files."""
    from sqlalchemy import select

    from meshpipeline.persistence.models import JobStatus, SimulationJob
    if job_ids:
        ids = list(job_ids)
    else:
        wanted = [JobStatus(s) for s in statuses]
        rows = (await db.execute(select(SimulationJob.id).where(SimulationJob.status.in_(wanted))
                                 .order_by(SimulationJob.created_at.desc()).limit(limit))).scalars().all()
        ids = [str(i) for i in rows]
    report: dict[str, Any] = {"schema": EXPORT_SCHEMA, "out_dir": str(out_dir), "jobs": len(ids),
                              "written": [], "refused": {}, "absent": {}}
    for jid in ids:
        try:
            env = await export_job(db, jid, store=store)
        except ValueError as exc:
            report["refused"][jid] = str(exc)
            continue
        path = write_envelope(env, out_dir)
        report["written"].append(str(path))
        if env["absent"]:
            report["absent"][jid] = env["absent"]
        log("learning export: %s -> %s%s", jid, path,
            f" ({len(env['absent'])} halves absent)" if env["absent"] else "")
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(prog="python -m meshpipeline.application.learning_export",
                                 description="Export finished jobs for geometry_agent.learn ingest "
                                             "--source platform.")
    ap.add_argument("--out", required=True, help="the directory the envelopes are written to")
    ap.add_argument("--job", action="append", default=None, help="one job id; repeatable")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--no-quality", action="store_true",
                    help="do not read the delivered payload from object storage")
    a = ap.parse_args(argv)

    async def go() -> dict[str, Any]:
        # THE COMPOSITION HAPPENS HERE AND NOWHERE ELSE IN THIS FILE. `application/` may not select a concrete
        # adapter (tests/unit/hygiene/test_architecture_boundaries.test_product_and_application_import_no_
        # concrete_adapters, which caught the first version of this module importing `build_object_store`), so
        # the exporter's own functions take the store by injection and only this `__main__` entry point asks
        # runtime composition for one, then reads it back through the neutral accessor.
        from meshpipeline.contracts.object_storage import get_object_store
        from meshpipeline.persistence.session import get_db
        from meshpipeline.runtime.composition import install_adapters
        store = None
        if not a.no_quality:
            install_adapters()
            store = get_object_store()
        async with get_db() as db:
            return await export_finished(db, Path(a.out), store=store, limit=a.limit, job_ids=a.job,
                                         log=lambda *p: print(p[0] % p[1:]))

    report = asyncio.run(go())
    print(json.dumps({k: v for k, v in report.items() if k != "written"}, indent=1))
    print(f"{len(report['written'])} envelopes in {report['out_dir']}")
    return 0 if report["written"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
