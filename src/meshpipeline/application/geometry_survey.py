# Responsibility: Run the Surveyor's half of the chain on the platform: compose the survey for what the customer said, put its questions, keep their answers.
# Owns: the chain order, the survey row's contents, the check that an answer is the customer's, and the port-role gate at submission.
# Boundaries: every geometry number and every question is the measurement package's; this module computes none of them.
# Collaborates with: application/geometry_measurement.py for the row it reads, agents/intake for the conversation, cad/regions.py for the builder's block.
from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import uuid as _uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# THE CHAIN, IN REHAAN'S ORDER, and it is not up for debate.
#
#   1  intake     the customer says what the part is for, their budget and their boundary
#                 conditions. FIRST, because the brief changes the representation: the same file is a
#                 wall to carve inside or the fluid itself depending on what they say.
#   2  measure    deterministic geometry.
#   3  look       after the measurement, always, because the measurement decides what is drawn.
#   4  intake     armed with the survey. The Surveyor's abstentions are the questions.
#   5  geometry   decides flow patches and where the cells go from those confirmations. It never
#                 re-decides what the geometry is.
#   6  intake     a third time, only for a question that did not exist at 4: the budget trade.
#   7  builder    intake's write-up and the geometry's, with the survey riding in the typed block
#                 after the request cut.
#
# WHERE EACH STEP IS ON THIS PLATFORM, so the order is a fact of the code and not of a comment.
#
#   The upload reads the BYTES: `geometry_measurement.on_upload` runs the instrument once and stores
#   what it read, keyed by the sha256. That is the expensive half of step 2, and it is a pure function
#   of the file, so doing it before anyone speaks costs the customer nothing and decides nothing.
#   With the survey on, the upload no longer queues the look: a look taken before the customer has
#   said what the part is for reads every part as internal flow, and an external body comes back
#   with invented ports (`geometry_agent.vision.look`, the representation note).
#
#   `survey_the_part` is the boundary between step 1 and step 2. Intake calls it once the customer
#   has said what the analysis is for; this module then composes the measurement FOR that purpose,
#   those words and those ports (`hexera.report_measured` over the stored facts, milliseconds, no
#   geometry is re-measured), queues the look with the purpose and representation the customer's
#   words decided (step 3), and hands intake the Surveyor's questions (step 4).
#
#   The look worker recomposes the survey when the look lands, so the questions only the look can
#   raise (a mouth the measurement did not find) arrive before the conversation ends.
#
#   Step 5 is `application/geometry_step.py`: the geometry agent plans the part at submission, from
#   the stored survey and the customer's answers, through the package's chain. It decides what to DO
#   and never re-decides what the geometry IS. When it cannot plan, the builder's own planner decides
#   and is handed the survey as facts it may not re-decide, and the reason is on the row.
#
#   Step 6 is the budget trade, released only when every step-4 question is settled, and put once. A
#   trade costed on the geometry agent's PLAN is the third intake (`ROUTE_LATE`), and only when the
#   survey had no budget question of its own.
#
#   Step 7 is `cad.regions.planner_inputs_for_state`, which hands the planner the geometry agent's
#   write-up in front of `request_txt[:2000]` and its typed block, carrying `survey`, after the cut.
#   When there is no plan to hand over it is `cad.regions.agent_block_for_state` instead, which is the
#   measurement's own block and no write-up, and the reason that happened is logged and recorded.
#
#   THREE BLOCKS REACH THE BUILDER AND ALL THREE NAME THE LOOK'S STATE. `geometry_step.builder_handoff`
#   when the agent planned the job, `builder_block` here when it did not, and the measurement's own
#   block in `cad/regions` when no survey was stored for these bytes. `with_the_look_state` is the one
#   writer for all three: `survey.looked` is a boolean over four states, so without it a look that
#   FAILED and a look nobody took are the same block, and only the first of the three used to say so.
CHAIN: tuple[tuple[str, str], ...] = (
    ("intake", "the customer states purpose, budget and boundary conditions"),
    ("measure", "the stored measurement is composed for what they said"),
    ("look", "the look is taken with the purpose and representation that decided"),
    ("intake", "the Surveyor's questions are put and the answers stored with who gave them"),
    ("geometry", "the geometry agent decides what to DO from facts it may not re-decide"),
    ("intake", "the budget trade, and only it, once the step-4 questions are settled"),
    ("builder", "the survey rides in the typed block after the request cut"),
)

#: The row's own name, written into every state this module stores.
SURVEY_STATE_SCHEMA = "meshpipeline.geometry_survey.v1"

#: Stages a row moves through. `surveyed`: composed, nothing put yet. `asking`: step-4 questions put.
#: `trade`: the budget trade put. `settled`: nothing open that anybody will be asked.
STAGE_SURVEYED, STAGE_ASKING, STAGE_TRADE, STAGE_SETTLED = "surveyed", "asking", "trade", "settled"
#: The row is waiting on the third intake's question. Only reachable with the geometry agent's step on.
STAGE_LATE = "third_intake"

#: Who an answer came from. The CUSTOMER, and never their account id: the `source` of a confirmed
#: claim rides into the planner's prompt, and a provider has no business reading who the customer
#: is. The principal is stored beside the answer in this platform's own row instead.
CUSTOMER = "customer"
#: The package's own word for a default nobody typed. It becomes an `assumed` claim, and on a role or
#: a budget the package refuses it outright, which leaves the question open. That is the point.
DEFAULT_TAKEN = "default_taken"

#: WHAT THE ANSWER'S `note` SAYS when the customer accepted the platform's proposal rather than naming the
#: option themselves. It is the CUSTOMER's answer - they said go to a proposal that was shown to them - and
#: not a `DEFAULT_TAKEN`, which is nobody answering; the note is what lets a reader of the row tell the two
#: apart afterwards without changing what either one means to a consumer.
ACCEPTED_THE_PROPOSAL = "accepted the setup the application proposed"

#: How a question is routed. `intake` is step 4. `trade` is step 6. `application` is a question this
#: platform already asks in its own words and owns the answer to: the unit, through
#: `agents/intake/unit_clarification.py` and the interpretation it records. `advisory` is reported
#: and never put, because its answer changes nothing.
ROUTE_INTAKE, ROUTE_TRADE, ROUTE_APPLICATION, ROUTE_ADVISORY = "intake", "trade", "application", "advisory"
#: The third intake: a question that did not exist until the geometry agent planned the part, raised by
#: `application/geometry_step.py` and nowhere else. It is not one of the survey's uncertainties, which is
#: the point: the package refuses a late question on any topic the survey already had.
ROUTE_LATE = "late"
#: The `stage` an answer to a late question carries, so the step-4 handoff never reads it as one of its own.
LATE_STAGE = "third"
#: The consequence tier the third intake sits at, in `ask.schema.TIERS`' own words. A cell ceiling is
#: `resolution`: it decides how finely the part is meshed and never which face carries what.
LATE_TIER = "resolution"

# HOW MANY MOUTHS ONE ROW OF THE BUILDER'S BLOCK NAMES. The block has two lists that grow with the mouth
# count and the package caps neither: an unsettled row's `subjects`, one entry per mouth the question named,
# and a confirmed answer's `applies_to`, one entry per mouth that answer placed. What
# `deliver.SURVEY_BLOCK_ITEMS` caps is the number of ROWS, not the length of a row's list.
#
# MEASURED on bend_elbow_001, deterministically, with nothing answered: the block is 2,117 characters with two
# unplaced mouths and 8.0 characters longer for each further mouth, so it crosses `deliver.SURVEY_BLOCK_MAX`
# between 200 mouths (3,595 characters) and 300. Past the ceiling `deliver.survey_block` refuses,
# `builder_block` catches the refusal and returns None, and the planner is then handed
# `hexera.planner_block`'s own survey, which is composed with no intake: on a part with a few hundred mouths
# the builder silently lost every role the customer had confirmed. `ask.intake` names a measured part with
# 1,231 open mouths beside its wall-clock ceiling, so this is not a hypothetical size.
#
# WHY THE SUBJECTS ARE SHORTENED AND NOT THE ROW. The rule the package states is that a block which can be CUT
# can lose the one thing it was carrying, and that is about a block silently truncated in a prompt. Twenty ids
# and a sentence saying how many more there are loses no finding: the row still names the question, the count
# is in the `why` the finder wrote, and the full list is on the survey itself, which the platform keeps. No
# survey at all, which is what happened before, loses every one of them.
#
# IT IS APPLIED ALWAYS, not only when the block is too big, so one part's block does not change shape because
# another mouth was measured. No stored fixture has a row naming more than seven mouths, so nothing in the
# product's measured behaviour moves.
def block_list_max() -> int:
    """How many mouths one unsettled row names before it starts counting, read off the package.

    IT USED TO BE A NUMBER HERE, and shortening the survey before handing it over. `hexera._survey_unsettled`
    rewrites every row's `why` from `SURVEY_UNSETTLED_WHY`, so the sentence this side wrote saying how many
    were left out was thrown away again the moment the block was composed: the list was short and the count
    was gone. The package caps its own rows now (`hexera.SURVEY_SUBJECTS_MAX`) and states the remainder in
    the row it keeps, which is one implementation instead of two disagreeing.
    """
    return int(_package()["hexera"].SURVEY_SUBJECTS_MAX)

#: WHAT HAPPENED TO THE LOOK, and there are four answers, not two. `survey.looked` is a BOOLEAN, so every
#: one of these but the first reaches the builder as the same False, and the product rule is that a look that
#: FAILED is never the same as a look that found a clear passage. It is not the same as one that was never
#: taken either, and neither is the same as one still running, which is the race: the look is queued as a
#: worker (`_queue_the_look`) and the planner can reach the survey before the worker writes anything, so the
#: plan is made with no eyes and, until this, said nothing about it.
LOOK_OK, LOOK_PENDING, LOOK_FAILED, LOOK_NONE = "ok", "pending", "failed", "not_attempted"

#: EVERY STATE THE LOOK CAN BE IN, as one name. It exists so that nothing has to keep a list of them in
#: step by hand: a test that enumerates this tuple fails the day a fifth state is added, where a test
#: that hand-lists three of them goes on passing and says nothing about the fourth. That is how
#: `look_queued` came to have no column - the round trip was checked against a list somebody wrote out.
LOOK_STATES: tuple[str, ...] = (LOOK_OK, LOOK_PENDING, LOOK_FAILED, LOOK_NONE)

#: The outcomes `_queue_the_look` can report, and which of them mean a worker is on its way. `skipped` is
#: not one: the queue refused it and nothing will write a reading, so the row is `not_attempted` and not
#: pending. Named here because `look_state` and the row that stores the answer must agree about it.
LOOK_QUEUED, LOOK_CACHED, LOOK_SKIPPED = "queued", "cached", "skipped"
LOOK_ON_ITS_WAY: tuple[str, ...] = (LOOK_QUEUED, LOOK_CACHED)

#: What each state means for the `seen` half of the builder's block, in a sentence the builder reads. It is
#: the same sentence in the row and in the block, because two wordings of one fact drift.
LOOK_BECAUSE: dict[str, str] = {
    LOOK_PENDING: ("no look has landed on this part yet: the render worker was queued and has not written "
                   "its reading, so this survey is the measurement alone and every claim a look would make "
                   "is absent because nothing has looked yet, not because there was nothing to see"),
    LOOK_FAILED: ("the look of this part FAILED and no reading came back, so every claim a look would make "
                  "is absent because the reader did not answer. A failed look is not a clear passage and "
                  "nothing here may be read as one"),
    LOOK_NONE: ("no look has been taken of this part, so this survey is the measurement alone. Every claim "
                "a look would make is absent because nothing looked, not because there was nothing there"),
}

#: WHAT THE PACKAGE'S OWN `survey["look_state"]` SAYS FOR EACH OF THESE, when it says anything.
#:
#: The agent composes `look_state` into the survey block beside `looked`, with three values and no
#: `pending`: a look in flight is a state of the platform's QUEUE, which the stored document cannot see, and
#: the package refuses to claim it. The wheel this image ships (`vendor/wheels/`) WRITES THE KEY as of agent
#: 0428ad41, so the comparison below is live on every normal job rather than dormant. It was not: the wheel
#: vendored before that was 14 agent commits behind and wrote no `look_state` at all, so an absent key still
#: has to mean THIS BLOCK DOES NOT SAY - a stored row measured by an older image has none.
#:
#: WHY THE COMPARISON EXISTS. Once both are there they are two facts about one thing, written from two
#: sources: the package reads the stored document, this module reads the ROW, which is the document plus the
#: queue's answer. They can only disagree when the row is stale against the document, which is the very thing
#: `cad/regions._surveyed_block` recomposes to prevent - so a disagreement is that recomposition not having
#: happened, and the builder must not be handed both answers and left to pick.
PACKAGE_LOOK_STATE: dict[str, str] = {LOOK_OK: "ok", LOOK_FAILED: "failed", LOOK_NONE: "not_attempted",
                                      LOOK_PENDING: "not_attempted"}

#: The customer's own words, kept for recomposition. Bounded, because a pasted spec sheet is not a
#: brief and the budget and the carve sentence are always near the top of one.
BRIEF_MAX_CHARS = 8000

#: WHICH FINDER PUTS THE QUESTIONS. ONE, and it is the package's own step-4 finder:
#: `geometry_agent.ask.intake.ask_intake`. It is the one with the four settle cuts (somebody already
#: answered it; the answer changes nothing; it is the same question; it is below the cap), the
#: consequence ranking over `ask.schema.TIERS`, the `asked_before` tie-break inside a tier, the
#: five-question cap and the wall-clock backstop that turned a 900 s hang on a 1,231-mouth part into an
#: answer. `ask.say` writes the sentence a customer reads and `ask.record` writes the row that turns
#: their answer into a label.
#:
#: WHAT THIS PLATFORM USED TO DO INSTEAD, and it is why this constant is written down. `composition`
#: called `contract.build.survey_from` with no `uncertainties=`, so the package fell back to
#: `contract.asking.uncertainties_from`, its own simpler finder, and `question_views` rendered THAT with
#: `contract.asking.questions_from`, the identity map. Measured on the five stored fixtures, the product
#: path had no cap, no ranking, no tie-break, none of `ask.say`'s wording, no `ask.record` row, and two
#: kinds it cannot raise at all: `dispatch_refusal`, which is the TOP of the tier order and decides
#: whether the job runs, and `flow_direction`, the only question the external path has. `block_boss_sharp`
#: is the proof: the finder puts `q_dispatch`, `q_port_roles`, `q_unit`, and the old path put `unit` and
#: two role questions and never mentioned that the file would be refused before it was meshed.
#:
#: `contract.asking` still renders a question from an uncertainty (`questions_from`, the words and the
#: evidence) and that is all it is used for here. Its `uncertainties_from` is not called on this path and
#: must not be: two finders is two question lists, and the survey's uncertainty list and the question list
#: are the same object by construction or they are nothing.
QUESTION_FINDER = "geometry_agent.ask.intake.ask_intake"

#: The row's own name for the finder's decisions, stored beside the survey. The survey's uncertainty list
#: cannot carry them: `contract.survey.Uncertainty` forbids extra keys, deliberately, because a tier or a
#: cap decision is not a thing the Surveyor measured. So they ride here, keyed by question id.
ASKING_SCHEMA = "meshpipeline.geometry_survey.asking.v1"


class SurveyError(RuntimeError):
    """A call this module refuses, with a sentence the model can act on. Never reaches a customer."""


# -------------------------------------------------------------------------------------------------
# the package, imported the first time something asks
# -------------------------------------------------------------------------------------------------

def _package():
    from geometry_agent.agent import catalog, hexera
    from geometry_agent.ask import intake as ask_intake
    from geometry_agent.ask import say as ask_say
    from geometry_agent.ask import schema as ask_schema
    from geometry_agent.ask import trade as ask_trade
    from geometry_agent.chain import job as ask_job
    from geometry_agent.contract import asking, build, deliver, given, intake, marks, survey
    from geometry_agent.facts.schema import GeometryFacts
    from geometry_agent.reconcile import joint as reconcile_joint
    return {"hexera": hexera, "asking": asking, "build": build, "deliver": deliver, "given": given,
            "intake": intake, "marks": marks, "survey": survey, "GeometryFacts": GeometryFacts,
            "catalog": catalog, "ask": ask_intake, "ask_schema": ask_schema, "ask_job": ask_job,
            "ask_trade": ask_trade, "ask_say": ask_say, "reconcile": reconcile_joint}


def _reconciled(pkg: dict, facts, representation: str, look: Any, case: str):
    """STEP 4 ON THE PLATFORM'S OWN PATH: the measurement checking the look and the look checking the
    measurement, on the document this composition was just built from. None when there is no pair.

    WHY IT IS HERE AND NOT AT `geometry_vision.attach_look`. The bytes are held there and nowhere else, so a
    profile cut across the 41 stations is only affordable there. But the OUTPUT of step 4 is two live objects
    (`verdict.Reconciliation` and the monitor's alerts) and its one consumer is `ask.intake.ask_intake`, which
    is called here. Storing it on the row in between would mean a second spelling of both objects and a
    reconstruction of them on the way back, which is two more things to keep in step with the package. So step
    4 runs where its consumer is, and pays for it: with no triangles in hand the 41-station profile REFUSES
    (`profile.FlowProfile.refused_by == "no_mesh"`) and every claim and signal behind the axial channel goes
    silent rather than guessing. `joint.Joint.readers` carries that absence, so a count of nought
    contradictions is not read as agreement.

    NEVER RAISES. Step 4 is pure addition: this platform composed surveys without it for four rounds, so a
    fault in it costs the composition its reconciliation and nothing else.
    """
    # WHAT THIS CALL MAY NAME IS WHAT THE VENDORED WHEEL CARRIES, and that is not a matter of taste. The image
    # installs `vendor/wheels/`, built from ONE recorded agent commit, so a name the agent's master has and the
    # wheel does not is an AttributeError or a TypeError inside the `except` below: step 4 would be skipped in
    # silence, on every job, with one warning line behind it. That is this round's own failure shape one level
    # in, and it was real: the wheel vendored before this change carried `reconcile/joint.py` without `for_job`
    # at all, so the subpackage staleness gate passed it.
    # `tests/unit/deploy/test_geometry_agent_distribution.AGENT_CALLS` now reads the wheel and checks the
    # functions and keywords this application names, against the WHEEL and never against a developer's
    # PYTHONPATH, which is the one environment where a stale wheel cannot be allowed to look fine.
    #
    # WHAT IS DELIBERATELY NOT PASSED: the customer's own flow direction (`stated_axis`). It reaches only the
    # gate on the 41-station profile, and that profile refuses here for want of the triangles whatever it is
    # told, so it would change nothing. It belongs in the same change as the bytes.
    try:
        return pkg["reconcile"].for_job(facts, representation, look if isinstance(look, dict) else None,
                                        case=case, path=None, mesh=None)
    except Exception as exc:                       # noqa: BLE001 - see the docstring
        logger.warning("geometry survey: step 4 could not be run (%s: %s); the questions are composed "
                       "without it", type(exc).__name__, exc)
        return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def brief_digest(brief: str | None) -> str:
    return hashlib.sha256(str(brief or "").encode("utf-8")).hexdigest()[:16]


# -------------------------------------------------------------------------------------------------
# STEP 2: the measurement, composed for what the customer said
# -------------------------------------------------------------------------------------------------

def compose(document: dict, *, purpose: str, brief: str | None = None,
            declared: list[dict] | None = None, engine: str | None = None,
            unit: str | None = None, scale_to_metres: float | None = None,
            unit_basis: str | None = None, cell_cap: int | None = None,
            inlet_ids: list[str] | None = None,
            confirmed_representation: str | None = None,
            flow_direction: str | None = None,
            asked_before: dict[str, int] | None = None) -> dict:
    """Step 2: the stored measurement composed for this purpose, these words and these ports.

    Nothing is measured again. `document["facts"]` is the instrument's reading of the bytes, stored
    at upload, and `hexera.report_measured` over it is the package's own composition: the
    representation, the forecast, the opening table and the planner block all come from there. The
    survey is `contract.build.survey_from` over that composition. Raises `SurveyError` when the
    document cannot be composed, which the callers turn into "no survey" and a conversation that runs
    exactly as it does with the survey off.

    `cell_cap` is a budget the customer CONFIRMED in the trade. Without one, the budget is the one
    they wrote, read by the package's `budget_from_brief`, and it rides as `stated`.

    `inlet_ids` are the mouths the customer called the inlet. The builder sizes from the largest
    declared inlet, so the block's `inlet_bore_m` and the forecast the budget trade is priced on are
    read off that mouth; composed before anybody had said which mouth is which, they were read off the
    widest one, which on the corpus cyclone is the outlet (108 mm against a 75 mm inlet, and a trade
    priced at 2.9M cells where the customer's inlet gives 4.8M). None reads the inlets out of the
    declared ports the customer named, bound to mouths the way the submission gate binds them.

    `confirmed_representation` is the representation a person CONFIRMED by answering the fluid-side
    question. None while nobody has, which is every composition until one is answered, and the whole path
    is dead unless the package's own `GEOMETRY_AGENT_FLUID_SIDE` raises that question at all.

    `flow_direction` is which way the customer said the flow comes at the body, as `+x` and the rest
    (`confirmed_flow_direction` reads it off their answer). None while nobody has answered.
    """
    made = composition(document, purpose=purpose, brief=brief, declared=declared, engine=engine, unit=unit,
                       scale_to_metres=scale_to_metres, unit_basis=unit_basis, cell_cap=cell_cap,
                       inlet_ids=inlet_ids, confirmed_representation=confirmed_representation,
                       flow_direction=flow_direction, asked_before=asked_before)
    composed, survey = made["composed"], made["survey"]
    brief_text, ports, cap, stated_cap = made["brief"], made["ports"], made["cap"], made["stated_cap"]
    inlet_ids = made["inlet_ids"]
    stored_look = document.get("look")
    look = stored_look if isinstance(stored_look, dict) else {}
    return {
        "schema": SURVEY_STATE_SCHEMA,
        "sha256": str(survey.source_sha256),
        "facts_sha256": str(survey.facts_sha256),
        "survey": survey.model_dump(mode="json"),
        #: the finder's own decisions for this composition: which questions it PUT, which it held and why,
        #: each one's tier and `ask.say`'s sentence. `question_views` reads it; nothing else may.
        "asking": made["asking"],
        "planner_block": composed.get("planner_block"),
        "composed_for": {
            "purpose": purpose, "engine": engine or "", "declared": ports,
            "unit": unit or "", "unit_basis": unit_basis or "", "scale_to_metres": scale_to_metres,
            "brief": brief_text, "brief_sha": brief_digest(brief_text),
            "cell_cap": cap, "cell_cap_kind": "confirmed" if cell_cap is not None else (
                "stated" if stated_cap is not None else ""),
            "representation": composed.get("representation"),
            "inlet_ids": sorted(inlet_ids),
            "look_status": str(look.get("status") or "not_attempted"),
            #: the representation a person confirmed and the side of the surface it means, so a reader of
            #: the row can tell a representation the geometry settled from one a person did. Absent where
            #: nobody answered, which keeps the row byte for byte what it was.
            **({"confirmed_representation": str(confirmed_representation),
                "fluid_side": str(made["fluid_side"])} if made["fluid_side"] else {}),
            #: WHICH WAY THE CUSTOMER SAID THE FLOW COMES AT THE BODY, so `composed_inputs` replays the
            #: composition this row is and `geometry_step` hands the same one answer to the model that its
            #: checker already vetoes the plan against. The answer to `q_flow_direction` can outlive the
            #: question on the row and be retired with it, and the row is then the only place it is left:
            #: that is what happened to `confirmed_representation` above, measured on block_boss_sharp,
            #: where a second composition read the answer back as None and inverted the meshed domain.
            #: Absent where nobody answered, which keeps the row byte for byte what it was.
            **({"flow_direction": str(flow_direction)} if flow_direction else {}),
            #: THE TIE-BREAK COUNTS THIS COMPOSITION WAS MADE WITH, so the recomposition
            #: `application/geometry_step._inputs` makes reproduces this survey exactly. `asked_before`
            #: breaks ties inside a tier, so it moves the ORDER of the ranked list and with it the order of
            #: the survey's uncertainties; replayed with different counts the recomposed survey is not the
            #: stored one field for field and the step refuses to plan against it. Absent when nobody passed
            #: any, which is every composition today, and the row is then byte for byte what it was.
            **({"asked_before": {str(k): int(v) for k, v in dict(asked_before).items()}}
               if asked_before else {}),
            #: WHICH QUESTIONS THIS COMPOSITION DOES NOT CARRY AND WHY (`_not_carried`), so a non-flow survey
            #: with nothing left on it cannot be read as a part with nothing left to settle. Absent for a flow
            #: purpose, and the row is then byte for byte what it was.
            **({"not_carried": dict(made["not_carried"])} if made["not_carried"] else {}),
            "composed_at": _now(),
        },
        "agent_git_sha": str((document.get("stamp") or {}).get("agent_git_sha") or ""),
    }


def composition(document: dict, *, purpose: str, brief: str | None = None,
                declared: list[dict] | None = None, engine: str | None = None,
                unit: str | None = None, scale_to_metres: float | None = None,
                unit_basis: str | None = None, cell_cap: int | None = None,
                inlet_ids: list[str] | None = None,
                confirmed_representation: str | None = None,
                flow_direction: str | None = None,
                asked_before: dict[str, int] | None = None) -> dict:
    """The package's own composition of the stored measurement, and the survey built from it.

    `compose` is this plus the row it stores. It is separate because the geometry agent's step needs
    the composed document itself (the opening table the agent's port rows are placed on, the survey
    record the ledger keeps), and a second spelling of the `report_measured` call would be a second
    thing to keep in step with this one. Raises `SurveyError`.

    `asked_before` is `{ask kind: times this topic was asked}`, and it breaks ties INSIDE a tier, lowest
    count first: at a fixed question budget it is the one change that makes the ledger learn faster, and it
    never moves a question across a tier. It is a caller's argument and not read off this row, because it
    moves the order of the ranked list and every recomposition of one survey has to reproduce the same
    order; the counts a composition used are stored in `composed_for` so `composed_inputs` replays them.
    """
    # NO PACKAGE SWITCHES TO ARM. `catalog` read the fluid side off the environment once and no longer does:
    # the reading is on for every composition, so there was nothing left for `policy.arm_the_package` to arm.
    if not isinstance(document, dict) or document.get("status") != "ok":
        raise SurveyError("there is no successful measurement to compose")
    facts_dump = document.get("facts")
    if not isinstance(facts_dump, dict) or not facts_dump:
        raise SurveyError("the stored measurement carries no facts, so it cannot be composed again")
    pkg = _package()
    brief_text = str(brief or "")[:BRIEF_MAX_CHARS]
    try:
        facts = pkg["GeometryFacts"].model_validate(facts_dump)
    except Exception as exc:                       # noqa: BLE001 - a foreign dump is not a measurement
        raise SurveyError(f"the stored facts do not validate: {type(exc).__name__}") from exc
    stated_cap, _line = pkg["survey"].budget_from_brief(brief_text)
    # AND WITH NEITHER, THE PLATFORM'S CEILING RATHER THAN THE PACKAGE'S COPY OF IT.
    #
    # `hexera.budget` does `cap = int(cell_cap) if cell_cap else MAX_CELLS_CAP`, so None was never
    # unbounded - it fell through to the package's own constant, 8,000,000. The platform's
    # CELL_HARD_LIMIT is also 8,000,000, so today they agree. They agree BY COINCIDENCE: two numbers
    # in two independently pinned distributions, and only one of them is env-overridable
    # (`optional_env("CELL_HARD_LIMIT", ...)`). Set CELL_HARD_LIMIT=4000000 in a deployment and the
    # agent keeps planning against eight million while the snappy driver clamps to four, with nothing
    # anywhere that notices.
    #
    # Passing it explicitly makes the platform's number the only one in play. `cell_cap_kind` below is
    # computed from `cell_cap` and `stated_cap`, never from this, so a ceiling stays what it is and
    # does not become a confirmation.
    cap = cell_cap if cell_cap is not None else stated_cap
    ports = [dict(p) for p in (declared or []) if isinstance(p, dict)]
    if inlet_ids is None:
        inlet_ids = _declared_inlets(document, ports)
    side = _side_of(pkg, facts, purpose, brief_text, ports, confirmed_representation)
    the_unit, the_basis = unit, unit_basis

    def measured(for_cap: int | None) -> dict:
        # THE CUSTOMER'S ANSWERED FLOW DIRECTION DECIDES ONE THING HERE AND IT IS NOT A SMALL ONE.
        # `report_measured` passes it to `coverage_block`, which passes it to `facts.coverage.flow_places`,
        # which is the only thing that can name an external body's downstream flat face its `blunt_base`
        # rather than a `flat_end` (coverage.py:444-450, `outward @ flow >= BASE_COS`). Until this argument
        # was threaded through, `composition` had no way to pass it and every composition this platform made
        # called `report_measured` with `flow_direction=None`. MEASURED on the stored
        # ahmed_variant_001_external_looked: with no direction the block's one place is
        # `{"kind": "flat_end", "what": "the body ends in a flat face here, across its long axis"}`, and with
        # the `+x` the customer answered it is `{"kind": "blunt_base", "... facing downstream"}` on the same
        # patch w3. The builder spends its budget in the block's own list order (`coverage.KIND_ORDER` ranks
        # blunt_base above flat_end), so the wake behind the body was refined as an anonymous flat face on
        # every external job, with the answer sitting on the row unread.
        return pkg["hexera"].report_measured(
            facts, the_unit, brief_text or None, purpose=purpose, engine=engine or None,
            declared=ports or None, cell_cap=for_cap, look=document.get("look"), fluid_side=side,
            scale_to_metres=scale_to_metres, unit_basis=the_basis, flow_direction=flow_direction,
            stamp={k: v for k, v in (document.get("stamp") or {}).items()
                   if k in ("agent_git_sha", "platform_sha")},
            inlet_ids=list(inlet_ids) or None)

    try:
        composed = measured(cap)
        # THE QUESTIONS ARE FOUND AGAINST THE BUDGET THE CUSTOMER WROTE, AND ONLY THE BUILDER HEARS THE ONE
        # THEY CONFIRMED. `ask.uncertainty.budget_uncertainty` reads `forecast.over_cap`, which is computed
        # against whatever cap the composition was given, so composing the finder's document with the CONFIRMED
        # cap un-asks the very question the customer just answered: the trade drops off the survey, and with it
        # the uncertainty their answer was bound to. Measured on bend_elbow_001 with a 100,000-cell brief and
        # the trade raised to 575,554: the recomposition dropped `q_budget`, `carry_answers` retired the
        # answer, `confirmed_cell_cap` went back to None, and `contract.deliver.survey_block` handed the
        # builder a block with `customer_cell_cap` 575,554 and no confirmed budget in it. Worse, `earlier` in
        # `chain.job.third_intake` no longer held `budget`, so the third intake was free to put the budget
        # question a second time, which is the one thing "asked ONCE" forbids (audit item 18).
        #
        # A confirmed cap is not a measurement of the part, so it belongs in `report_measured` for the block's
        # `customer_cell_cap` and the forecast the trade is priced on, and nowhere near the finder.
        asking_doc = composed if cap == stated_cap else measured(stated_cap)
        # STEP 4, BETWEEN THE LOOK AND THE QUESTIONS, and this is the only place a customer of this platform
        # can reach it. `chain.job.run_job` runs it for the package's own chain, and nothing here calls
        # `run_job`: this module composes its own document from `hexera.report_measured`. So the layer was
        # wired for the tests and the eval and not for an upload, which is the same defect one level up from
        # the pass-through parameters it replaced. A contradiction between the two sources needs no answer
        # key, which is what makes it the one quality signal that works on a file nobody has seen.
        joint = _reconciled(pkg, facts, str(composed.get("representation") or ""), document.get("look"),
                            str(document.get("source_sha256") or ""))
        r_alerts = joint.alerts if joint is not None else None
        r_found = joint.reconciliation if joint is not None else None
        # STEP 4'S FINDER, AND IT IS THE ONLY ONE (`QUESTION_FINDER`). `ask_intake` reads the composed
        # document, applies the four cuts, ranks what is left by consequence and puts at most
        # `ask.intake.MAX_ASKED`; `ask_job.contract_uncertainties` is the package's own adapter from its
        # `Asked` to the handoff's uncertainty list, the same one `chain/job.survey` uses, so the platform
        # and the chain compose one list from one finder rather than two lists from two.
        #
        # A QUESTION THE CUTS HELD IS STILL AN UNCERTAINTY. The adapter keeps every question this pipeline
        # chose not to put (below the cap, no consequence) so the builder is told it is unsettled, and drops
        # the ones somebody else already answered, which are not uncertainties any more.
        asked = pkg["ask"].ask_intake(asking_doc, declared=ports or None, brief=brief_text or None,
                                      asked_before=dict(asked_before or {}) or None,
                                      alerts=r_alerts, reconciliation=r_found)
        # A UNIT THE BRIEF STATED IS A UNIT THIS DOCUMENT HAS, and until this ran, neither half said so.
        # `ask.settle` reads "Geometry is in millimetres" and holds the unit question `settled_by='brief'`,
        # so the finder drops it - a question somebody answered is not an uncertainty. But nothing put the
        # answer on the document, so there was no unit MARK either, and `survey_from` refused the whole
        # handoff: "no unit mark and no unit question: a length with no unit is the 1,000x error". Correct
        # refusal, and the fix is to stop losing the answer rather than to soften it.
        #
        # `chain.job.brief_stated_facts` is the package's own reading of what the brief settled and the same
        # one the chain uses, so the platform does not get a second opinion about it. The basis is `brief`,
        # which `contract.build._part_marks` already maps to a STATED mark: the customer's word, ranked
        # below a confirmation and above the file's own guess. A unit the caller supplied wins, because that
        # one came from a person through `GeometryInterpretationRef`.
        if the_unit is None:
            _brief_unit = pkg["ask_job"].brief_stated_facts(asked).get("unit")
            if _brief_unit:
                the_unit, the_basis = str(_brief_unit), "brief"
                composed = measured(cap)
                asking_doc = composed if cap == stated_cap else measured(stated_cap)
                # Found again against the document that now carries the unit: the unit question is settled
                # either way, but its absence changes what the other questions cost, and the finder ranks
                # them by consequence.
                asked = pkg["ask"].ask_intake(asking_doc, declared=ports or None, brief=brief_text or None,
                                              asked_before=dict(asked_before or {}) or None,
                                              alerts=r_alerts, reconciliation=r_found)
        # THE FLOW QUESTIONS COME OFF A NON-FLOW SURVEY HERE, AND NOWHERE ELSE. It is one cut in one place, and
        # it has to be one place: `asking_row` and `contract_uncertainties` below both read this object, and the
        # survey's uncertainty list and the question list are the same list by construction or they are nothing
        # (`QUESTION_FINDER`). It is after the unit re-find above deliberately - `brief_stated_facts` reads
        # every question the finder found, including the ones this cut removes, and a unit the brief settled is
        # a unit whatever the purpose.
        not_carried = _not_carried(purpose, str(composed.get("engine") or ""))
        asked = _without_the_flow_questions(pkg, asked, purpose, str(composed.get("engine") or ""))
        # the package admits an engine nobody named as `assumed` (work/z-chain, `contract.marks.OWNERS`), so the
        # composed document goes to the survey as it is; the shim that stripped the engine is gone
        survey = pkg["build"].survey_from(
            composed, brief=brief_text or None, declared=ports or None,
            uncertainties=pkg["ask_job"].contract_uncertainties(asked, asking_doc))
    except SurveyError:
        # ALREADY THE SENTENCE THE CALLER SHOWS. `_without_the_flow_questions` raises one from inside this
        # block, and the `except Exception` below would wrap it as "the survey could not be composed:
        # SurveyError: <the whole sentence>" - a refusal that reads as an internal fault instead of as the
        # reason it is. Re-raised before the two handlers below rather than after, because both would match.
        raise
    except pkg["marks"].ContractError as exc:
        raise SurveyError(f"the survey refused its own handoff: {exc}") from exc
    except Exception as exc:                       # noqa: BLE001 - never a turn, never a mesh
        raise SurveyError(f"the survey could not be composed: {type(exc).__name__}: {exc}") from exc
    return {"composed": composed, "survey": survey, "facts": facts, "brief": brief_text, "ports": ports,
            "cap": cap, "stated_cap": stated_cap, "inlet_ids": list(inlet_ids), "fluid_side": side,
            "flow_direction": flow_direction, "not_carried": not_carried,
            "asked": asked, "asking": asking_row(asked, pkg), "joint": joint}


def _not_carried(purpose: str, engine: str) -> dict[str, str]:
    """`{question kind: why this composition does not carry it}`, empty for a flow purpose.

    WITHOUT THIS A SETTLED SURVEY AND A CUT ONE ARE THE SAME ROW. `_without_the_flow_questions` removes the
    flow-shaped questions from a structural survey, and on five of the seven stored fixtures that leaves the
    uncertainty list EMPTY - which is the same row a part with nothing left to settle would have. Worse, the
    package's own reason for an empty list is a fixed sentence written for the flow case
    (`contract.build.survey_from`: "the unit is confirmed, no mouth needs a role for this purpose, and the
    forecast fits the stated budget"). MEASURED on transition_007_fluid composed for `structural` with no
    engine named: the list is empty, that sentence is written, and `forecast.over_cap` is True at 2,046,473
    cells against the customer's 2M - so the handoff says the forecast fits while the only forecast it has
    says it does not, and neither claim is about the mesher a structural job runs on. The sentence is the
    package's field, written by a file this platform may not edit and read by nothing on either side; this is
    the platform's own record of the same fact, in the one place the platform owns, and it is the difference
    between "nothing was left unsettled" and "these were not asked, and here is why".

    Absent from the row where nothing was cut, so a flow job's `composed_for` is byte for byte what it was.
    """
    if purpose in FLOW_PURPOSES:
        return {}
    out = {k: f"{purpose} is not a flow purpose" for k in sorted(FLOW_ONLY_QUESTION_KINDS)}
    if not engine_can_mesh(purpose, engine):
        out.update({k: (f"the forecast and the triage this survey carries are {engine or 'no engine'}'s, and "
                        f"{engine or 'no engine'} cannot produce the mesh {purpose} needs")
                    for k in sorted(ENGINE_DEPENDENT_QUESTION_KINDS)})
    return out


def _without_the_flow_questions(pkg: dict, asked: Any, purpose: str, engine: str) -> Any:
    """The finder's answer with every question this purpose and this engine cannot honestly answer removed.
    The finder's own answer untouched for a flow purpose. Raises `SurveyError`.

    THE ONE CUT THAT LETS A STRUCTURAL JOB HAVE A SURVEY AT ALL. `SURVEYED_PURPOSES` says why the purpose is
    surveyed now, `FLOW_ONLY_QUESTION_KINDS` and `ENGINE_DEPENDENT_QUESTION_KINDS` say why each kind goes;
    this is the mechanics. MEASURED on the six stored fixtures composed for `structural` with no engine
    named: the finder puts `q_port_roles` on all six and `q_budget` on transition_007_fluid as well, and
    after this cut it puts neither on any of them.

    `engine` is the engine the composition was MADE with (`composed["engine"]`), not the one the caller
    asked for. They differ on every job nobody has named an engine on: `hexera.report_measured` composes
    `eng = engine or "snappy"` whatever the purpose, so a structural composition carries snappy, and reading
    the caller's `None` here would have left the two engine-dependent questions in place on exactly the
    compositions whose numbers are another mesher's.

    ALL THREE LISTS, NOT JUST THE QUESTIONS, and that is the whole defect this function could have had.
    `chain.job.contract_uncertainties` walks `questions` and `held` first and then walks `uncertainties`
    again, adding every one no question covered as an `advisory` row. So cutting the question alone would
    have taken the role question off the customer's screen and put the same abstention in front of the
    builder as an advisory uncertainty about which mouth is the inlet - the flow-shaped thing carried after
    all, one list along, with the customer-facing half looking fixed.

    IT REFUSES ON ANY KIND THIS PLATFORM HAS NOT CLASSIFIED, in either direction. The three sets must cover
    `ask.schema.KINDS` exactly: a kind this module names and the package no longer has is a cut that silently
    stops cutting (rename `port_role` and a structural customer is asked which mouth is the inlet again, with
    this function in the path looking like it handled it), and a kind the package has added and this module
    has not classified is carried onto a non-flow survey because nobody decided it should be. Both are the
    same defect - a check whose blind spot is its subject's - and both refuse. A refusal costs a non-flow job
    its survey, which is exactly what it had before this change, and it costs a flow job nothing because the
    cut does not run for one.

    THE FINDER'S RANKING IS NOT RE-RUN over what is left, and that is deliberate. `ask.intake.ask_intake`
    ranks by consequence and puts at most `MAX_ASKED` (5); a question it HELD `below_the_cap` could in
    principle have been put in the slot a cut question vacated. Re-running the finder over a filtered
    candidate list would be a second finder with a second ranking, which is the one thing this path may not
    have. It costs nothing measurable here: over the six fixtures composed for `structural` the finder puts
    at most two questions of any kind and holds none `below_the_cap`, so the cap is never the binding
    constraint - and a question not put still rides to the builder as unsettled, which is a question
    missing rather than a wrong one answered.
    """
    if purpose in FLOW_PURPOSES:
        return asked
    classified = FLOW_ONLY_QUESTION_KINDS | ENGINE_DEPENDENT_QUESTION_KINDS | KEPT_QUESTION_KINDS
    known = frozenset(pkg["ask_schema"].KINDS)
    gone, new = sorted(classified - known), sorted(known - classified)
    if gone or new:
        raise SurveyError(
            f"the Surveyor cannot compose for {purpose!r}: it may only carry a question kind somebody has "
            f"judged honest for a purpose that is not about flow, and this platform and "
            f"`ask.schema.KINDS` do not agree on the list"
            + (f" - it names {', '.join(gone)}, which the package no longer has" if gone else "")
            + (f" - the package has {', '.join(new)}, which nobody has classified" if new else ""))
    drop = set(FLOW_ONLY_QUESTION_KINDS)
    if not engine_can_mesh(purpose, engine):
        drop |= set(ENGINE_DEPENDENT_QUESTION_KINDS)
    cut = [f"{q.id} ({q.kind})" for q in asked.questions if q.kind in drop]
    out = dataclasses.replace(
        asked,
        uncertainties=[u for u in asked.uncertainties if u.kind not in drop],
        questions=[q for q in asked.questions if q.kind not in drop],
        held=[q for q in asked.held if q.kind not in drop])
    if cut:
        logger.info("geometry survey: %s on %s does not carry %s", purpose, engine or "no engine",
                    ", ".join(cut))
    return out


def asking_row(asked: Any, pkg: dict) -> dict:
    """The finder's own decisions for one composition, keyed by question id, small enough to store.

    WHY THE ROW CARRIES THEM AT ALL. `contract.survey.Uncertainty` forbids extra keys, so a tier, a cap
    decision and `ask.say`'s sentence have nowhere to ride on the survey; and they are not measurements, so
    they should not. Without them `question_views` cannot put the five the finder ranked and hold the rest,
    which is the cap, and it renders `contract.asking._text_for` instead of the sentence `ask.say` wrote.

    `put` is in the finder's RANKED ORDER, which is the order a customer is asked in. `held` is only what
    this pipeline chose to hold (`ask.schema.OUR_CHOICE`): a question somebody else already answered is not
    on the survey's uncertainty list at all, so there is no view of it to hold.
    """
    ours = pkg["ask_schema"].OUR_CHOICE
    held = {q.id: {"settled_by": q.settled_by, "quote": q.settled_quote}
            for q in asked.held if q.settled_by in ours}
    return {
        "schema": ASKING_SCHEMA, "finder": QUESTION_FINDER, "case": str(asked.case or ""),
        "max_asked": int(pkg["ask"].MAX_ASKED),
        "put": [q.id for q in asked.questions],
        "held": held,
        "tier": {q.id: q.tier for q in [*asked.questions, *asked.held] if q.id in held or q.asked},
        #: `ask.say`'s own sentence, which is what a customer reads. `contract.asking.questions_from`
        #: renders an uncertainty into words too, and its words are the fallback: on a role question it
        #: says "I could not settle opening.role on ..." where `ask.say` says "I can see seven openings on
        #: this part. None of them is named in what you have sent."
        "text": {q.id: q.text for q in [*asked.questions, *asked.held] if q.id in held or q.asked},
        #: WHAT MAKES THE ANSWER A LABEL (`ask.record`). Stored for the questions that are PUT, because an
        #: answer only comes back for one of those, and it is the row the ledger keys the answer on.
        "record": {q.id: q.record for q in asked.questions if q.record},
        #: THE PLATFORM'S OWN PROPOSAL, STRUCTURED (`ask.schema.Question.default_roles`). `text` above carries
        #: the sentence the customer reads, and the sentence is the whole of the proposal for every question
        #: but the role one, where it names a mouth. `record_answer` records THIS map, never a mouth the model
        #: chose, when a customer accepts the proposal instead of naming the roles themselves - so the platform
        #: is answerable for what it proposed rather than trusting a model to repeat it back. Empty for a
        #: question whose default names no place, and for a row composed before the agent wrote the field.
        "proposal": {q.id: dict(getattr(q, "default_roles", None) or {})
                     for q in [*asked.questions, *asked.held] if getattr(q, "default_roles", None)},
    }


def _side_of(pkg: dict, facts: Any, purpose: str, brief: str, ports: list[dict],
             confirmed_representation: str | None) -> str | None:
    """The side of the surface a CONFIRMED representation means (`catalog.FLUID_SIDES`), or None.

    THE MAP IS THE PACKAGE'S, INVERTED, NEVER RESTATED HERE. `catalog.side_reading` carries `readings`,
    `{side: the representation that side gives}`, and it is the same table the question's own options were
    written from (`contract.asking._fluid_side_uncertainty`). So the side is the key whose reading is the
    word the customer's answer set. Empty `readings` means the geometry or the brief already settled the
    side, and then there is no question to have answered and nothing to pass on.

    None on anything unexpected rather than a guess: a representation no reading gives is a mismatch this
    function must not paper over, and `report_measured` with `fluid_side=None` is the composition the
    platform has always made.
    """
    if not confirmed_representation:
        return None
    try:
        readings = pkg["catalog"].side_reading(facts, purpose, brief or None,
                                              declared=ports or None).readings or {}
    except Exception as exc:                       # noqa: BLE001 - never worth losing a composition over
        logger.warning("geometry survey: the fluid side could not be read back (%s)", exc)
        return None
    for side, reading in readings.items():
        if str(reading) == str(confirmed_representation):
            return str(side)
    logger.warning("geometry survey: %r is not a representation either side reading gives (%s), so the "
                   "composition is made without it", confirmed_representation, sorted(readings.values()))
    return None


def confirmed_representation(state: dict | None) -> str | None:
    """The representation a PERSON confirmed, from the answers. None while nobody has.

    A default that stood is not one: `_status` reads it as `defaulted`, the handoff lists it unanswered and
    the flow path stays refused. That is the rule everywhere on this path and it is the one this field would
    be easiest to break.
    """
    said = [a for a in live_answers(state)
            if a.get("about") == "representation" and a.get("answered_by") == CUSTOMER
            and not a.get("skipped") and a.get("value")]
    return str(said[-1]["value"]) if said else None


def confirmed_flow_direction(state: dict | None) -> str | None:
    """Which way the CUSTOMER answered the flow comes at the body, as `+x` and the rest, or None.

    A default that stood is not one, for the reason `_status` gives: `ask.say.flow_direction` offers "unless
    you say otherwise the flow runs along +x", and a customer who let that stand has said nothing about the
    direction. `ask.intake` ranks the question at `domain` because the answer moves the domain box
    (`ask.consequence.FLOOR_FIELD["flow_direction"]`), so reading a default as an answer would hand the
    builder a box nobody chose.

    IT IS READ HERE AND NOT OFF A `Given`. `contract.given.customer_flow_direction` is the package's own
    reading of the same answer, and it needs a `Given`, which needs a survey - and the two callers of this
    are the composition that BUILDS the survey and a row that has not been planned yet. So the answer is read
    off the row with `contract.given.FLOW_DIRECTION_RE`, the package's one spelling of a direction in a
    person's words, and turned into the sign-and-letter form by the same two lines
    `chain.job.flow_axis_of` uses on the same regex. A phrase that names no direction - the question's own
    "it does not matter for this run" - is None rather than a guess, which is what that option means.
    """
    said = [a for a in live_answers(state)
            if a.get("about") == "flow_direction" and a.get("answered_by") == CUSTOMER
            and not a.get("skipped") and a.get("value")]
    if not said:
        return None
    hit = _package()["given"].FLOW_DIRECTION_RE.search(str(said[-1]["value"]))
    return f"{hit.group(1)}{hit.group(2).lower()}" if hit else None


def confirmed_inputs(state: dict | None) -> dict[str, Any]:
    """Every composition input a PERSON settled, spelled as the keyword arguments `compose` takes for them.

    THE ONE PLACE THAT KNOWS WHAT A RECOMPOSITION HAS TO CARRY, and it exists because three callers used to
    know it separately and each of them knew a different half. `recomposed` carried the budget, the inlet and
    the side; `for_submission` carried the budget and the inlet; `survey_the_part` carried the budget alone. So
    the SECOND call of `survey_the_part` - which intake makes the moment the customer says anything that moves
    the purpose or names a port - composed without the side the customer had confirmed, and the composition it
    stored was the one made before they answered.

    MEASURED on block_boss_sharp, a solid block with one bore, composed for internal_cfd. `q_fluid_side` is
    raised, the customer answers "the fluid flows through the bore; the part is the solid around it", the row
    is recomposed and its representation becomes `wall_shell`: the builder carves the passage out of the solid.
    Call `survey_the_part` again and the representation goes back to `annular_fluid` - the file itself read as
    the flow - and stays there. That is not a lost label, it is THE MESHED DOMAIN INVERTED: an internal passage
    becomes an external body, or the reverse, with nothing on the row that says so.

    AND THE ANSWER CANNOT BE READ BACK, which is why every one of these reads the ROW as well as the answers.
    Once the side is confirmed the composition settles it, `q_fluid_side` is no longer raised, and
    `carry_answers` retires the answer with the question it was bound to - correctly: the answer binds nothing
    now. From that moment `confirmed_representation(state)` is None and `composed_for` is the only place the
    customer's answer still exists. A reader that trusts the answers alone loses it on the first recomposition
    after the one that settled it.

    `inlet_ids` is the exception and is the answers alone: the mouths in `composed_for["inlet_ids"]` may have
    been derived from the customer's declared port rows (`_declared_inlets`) rather than answered, so they are
    not a confirmation and this function does not report them as one. `recomposed` falls back to them itself,
    because it is composing THIS composition again and may not drop a value it was made with.
    """
    before = dict((state or {}).get("composed_for") or {})
    row_cap = before.get("cell_cap") if before.get("cell_cap_kind") == "confirmed" else None
    return {"cell_cap": confirmed_cell_cap(state) or row_cap,
            "inlet_ids": named_inlets(state) or None,
            "confirmed_representation": (confirmed_representation(state)
                                         or before.get("confirmed_representation") or None),
            "flow_direction": confirmed_flow_direction(state) or before.get("flow_direction") or None}


def composed_inputs(state: dict) -> dict:
    """The arguments `compose` was called with for this row, read back off `composed_for`.

    `cell_cap` is the one the composition used: the customer's confirmed budget when there is one, and
    otherwise None, so the budget is read out of their words again exactly as it was the first time.

    IT IS NOT `confirmed_inputs` AND MUST NOT BECOME IT. This is the REPLAY: `geometry_step._inputs` composes
    these arguments again and refuses to plan unless the survey they give is the stored one field for field, so
    every value here has to be the one the stored composition was made with and never a fresher reading of the
    answers. `confirmed_inputs` is the other direction - what a new composition has to carry forward.
    """
    before = dict(state.get("composed_for") or {})
    return {"purpose": before.get("purpose") or "internal_cfd", "brief": before.get("brief") or "",
            "declared": before.get("declared") or None, "engine": before.get("engine") or None,
            "unit": before.get("unit") or None, "scale_to_metres": before.get("scale_to_metres"),
            "unit_basis": before.get("unit_basis") or None,
            "cell_cap": before.get("cell_cap") if before.get("cell_cap_kind") == "confirmed" else None,
            "inlet_ids": list(before.get("inlet_ids") or []),
            "confirmed_representation": before.get("confirmed_representation") or None,
            "flow_direction": before.get("flow_direction") or None,
            "asked_before": dict(before.get("asked_before") or {}) or None}


def _declared_inlets(document: dict, ports: list[dict]) -> list[str]:
    """The mouths the customer's own declared inlets bind to, by position or bore. Empty when none binds."""
    inlets = [p for p in ports if str(p.get("type") or p.get("role") or "").strip().lower() == "inlet"]
    if not inlets:
        return []
    from meshpipeline.agents.intake.geometry_brief import bind_patches
    bound = bind_patches(document, [{**p, "type": "inlet"} for p in inlets])
    return sorted({str(b["opening_id"]) for b in bound.get("bound") or [] if b.get("opening_id")})


def named_inlets(state: dict | None) -> list[str]:
    """The mouths the CUSTOMER answered are the inlet. Defaults and skips are not in it."""
    return sorted(o for o, role in confirmed_roles(state).items() if role == "inlet")


def recomposed(state: dict, document: dict, **changes: Any) -> dict:
    """The same survey composed again for the same customer with something new: the look landed, the
    purpose moved, the ports were declared, a budget was confirmed. Answers and `asked` are carried
    across for every question id the new survey still raises, and dropped for the rest, because an
    answer is bound to a question and a question that no longer exists binds nothing."""
    before = dict(state.get("composed_for") or {})
    kwargs: dict[str, Any] = {
        "purpose": before.get("purpose") or "internal_cfd", "brief": before.get("brief") or "",
        "declared": before.get("declared") or None, "engine": before.get("engine") or None,
        "unit": before.get("unit") or None, "scale_to_metres": before.get("scale_to_metres"),
        "unit_basis": before.get("unit_basis") or None,
        # the same tie-break counts, so a recomposition ranks the questions the way this row's did
        "asked_before": dict(before.get("asked_before") or {}) or None,
        # and everything a PERSON settled, read in the one place that knows the whole list
        # (`confirmed_inputs`), so this recomposition and the one `survey_the_part` makes cannot carry
        # different halves of the same customer's answers
        **confirmed_inputs(state)}
    # the mouths the customer's declared ports bind to, where they have answered no inlet themselves. It is
    # here and not in `confirmed_inputs` because a derived mouth is not a confirmation, and this is the one
    # caller composing THIS composition again, which may not drop a value it was made with
    kwargs["inlet_ids"] = kwargs["inlet_ids"] or before.get("inlet_ids")
    kwargs.update(changes)
    fresh = compose(document, **kwargs)
    return carry_answers(state, fresh)


def carry_answers(old: dict | None, fresh: dict) -> dict:
    if not old:
        return {**fresh, "asked": [], "answers": [], "stage": STAGE_SURVEYED}
    if old.get("sha256") and old.get("sha256") != fresh.get("sha256"):
        # a survey for other bytes. Nothing it was told is about this file.
        return {**fresh, "asked": [], "answers": [], "stage": STAGE_SURVEYED}
    # THE GEOMETRY AGENT'S HALF RIDES ACROSS a recomposition of the same bytes: its plan says which answers
    # it was made for, so a plan the new composition no longer matches is refused where it is read, and the
    # third intake's question stays, because it is asked once and an answer to it is still an answer
    fresh = {**fresh, **{k: old[k] for k in ("late", "geometry_step", "look_queued") if k in old}}
    ids = {q["id"] for q in question_views(fresh)}
    # NOTHING THE CUSTOMER SAID IS DELETED. An answer to a question the new survey no longer raises is
    # kept in the same append-only list, marked retired, and every reader skips it: it binds nothing
    # now, and it is still the record of what they were asked and what they said.
    answers = []
    for a in old.get("answers") or []:
        if not isinstance(a, dict):
            continue
        if a.get("retired") or a.get("question_id") in ids:
            answers.append(a)
        else:
            answers.append({**a, "retired": True, "retired_at": _now()})
    out = {**fresh, "asked": [q for q in (old.get("asked") or []) if q in ids], "answers": answers}
    out["stage"] = stage_of(out)
    return out


def live_answers(state: dict | None) -> list[dict]:
    """The answers still bound to a question the survey raises. Retired ones are the record, not input."""
    return [a for a in ((state or {}).get("answers") or []) if isinstance(a, dict) and not a.get("retired")]


# -------------------------------------------------------------------------------------------------
# STEP 4 AND STEP 6: the questions, which are the survey's uncertainties rendered
# -------------------------------------------------------------------------------------------------

def survey_of(state: dict):
    """The package's `SurveyHandoff`, rebuilt from the stored dump. Its own validators run again."""
    return _package()["survey"].SurveyHandoff.model_validate(dict(state.get("survey") or {}))


def _route(q) -> str:
    if q.about == "unit":
        return ROUTE_APPLICATION
    if q.effect == "advisory":
        return ROUTE_ADVISORY
    if q.about == "cell_budget":
        return ROUTE_TRADE
    return ROUTE_INTAKE


def asking_of(state: dict | None) -> dict:
    """The finder's decisions stored on this row, or an empty dict.

    Empty means a row composed before the finder was wired, and then every question the survey raises is
    put and none is ranked, which is what this platform did before. It is not silently the same thing: the
    view says `finder: ""` and `put: True` on everything, so a reader can tell an unranked row from a
    ranked one.
    """
    row = (state or {}).get("asking")
    return dict(row) if isinstance(row, dict) else {}


def question_views(state: dict) -> list[dict]:
    """Every question the survey raises, with its route, its tier, whether it is PUT and whether it is
    settled. Advisory and held ones included, so the list is the uncertainty list one for one and nothing
    is silently filtered.

    THE SENTENCE IS `ask.say`'S WHERE THERE IS ONE. The finder writes the words a customer reads and stores
    them on the row; `contract.asking.questions_from` renders an uncertainty into words as well and its
    rendering is the fallback, for a row composed before the finder was wired.

    `put` IS THE CAP. `ask.intake` ranks by consequence and puts at most `MAX_ASKED`; everything it held is
    here with `put: False` and `settled_by` naming which cut held it, because a held question is still an
    uncertainty the builder must be told about and an answer to it still overrules whatever held it.
    """
    pkg = _package()
    try:
        questions = pkg["asking"].questions_from(survey_of(state), include_advisory=True)
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry survey: the stored survey could not be read (%s)", exc)
        return []
    asking = asking_of(state)
    ranked = list(asking.get("put") or [])
    held = dict(asking.get("held") or {})
    tiers = dict(asking.get("tier") or {})
    said = dict(asking.get("text") or {})
    records = dict(asking.get("record") or {})
    proposals = dict(asking.get("proposal") or {})
    tier_why = dict(pkg["ask_schema"].TIER_WHY)
    answers = live_answers(state)
    out = []
    for q in questions:
        mine = [a for a in answers if a.get("question_id") == q.id]
        subjects = list(q.of.subjects or ([q.subject] if q.subject else []))
        settled = held.get(q.id) or {}
        view = {"id": q.id, "about": q.about, "text": str(said.get(q.id) or q.text),
                "options": list(q.options),
                "subjects": subjects, "effect": q.effect, "default": q.default,
                #: THE SAME DEFAULT AS A MAP, `{mouth: role}`, written by `ask.say.port_roles` from the same
                #: reading the sentence in `default` is written from. It is what `record_answer` records when a
                #: customer accepts the proposal rather than naming the roles, and it is `{}` on every question
                #: whose default names no place.
                "proposal": dict(proposals.get(q.id) or {}),
                "evidence": list(q.evidence), "route": _route(q),
                "why": q.of.why, "options_from": q.of.options_from,
                #: THE CAP AND THE RANKING, from the finder that made them. `put` False is a question the
                #: finder held: it is never asked, it still rides to the builder as unsettled, and an answer
                #: to it is still accepted, because a customer volunteering one overrules a cut.
                "put": (q.id in ranked) if asking else True,
                "held_because": str(settled.get("settled_by") or ""),
                "held_quote": str(settled.get("quote") or ""),
                "tier": str(tiers.get(q.id) or ""),
                "tier_why": tier_why.get(str(tiers.get(q.id) or ""), ""),
                #: WHAT TURNS THE ANSWER INTO A LABEL (`ask.record`), for the questions that are put. It is
                #: the row the ledger keys an answer on, and it never existed on this path before.
                "record": dict(records.get(q.id) or {}),
                "finder": str(asking.get("finder") or ""),
                #: WHAT EACH OPTION SETS, paired by position with `options`. The uncertainty writes it so a
                #: consumer never has to read a machine value back out of the sentence a person was shown:
                #: the fluid-side question's options are two sentences and its values are the two
                #: representations they mean. Without this the stored answer was the sentence, and the
                #: sentence became the value of the `representation` fact the whole chain reads.
                "option_values": [str(v) for v in (q.of.option_values or [])],
                #: the evidence as the package's own marks, kind and value, for the one reader that
                #: needs a number back out of a question: the budget trade's two options
                "marks": [pkg["marks"].as_json(m, compact=True) for m in q.of.evidence]}
        view["status"] = _status(view, mine)
        out.append(view)
    # THE ORDER A CUSTOMER IS ASKED IN IS THE FINDER'S RANKING, not the uncertainty list's order. The ranking
    # is lexicographic over `ask.schema.TIERS` (dispatch, boundary, domain, resolution, none) with the
    # `asked_before` tie-break inside a tier, and `ask.intake` has already sorted `put` into it. A held
    # question keeps its place after every put one, so `open_now[0]` is always the worst consequence open.
    order = {qid: i for i, qid in enumerate(ranked)}
    out.sort(key=lambda v: (order.get(v["id"], len(order)), v["id"]))
    late = late_view(state)
    if late is not None:
        out.append(late)
    return out


def late_view(state: dict | None) -> dict | None:
    """The third intake's question as a view, `status` and all, or None. Only when a plan raised one:
    a row whose step did not plan carries no `late` and nothing here runs.

    It carries its own status because it is read on its own as often as it is read through
    `question_views`: `submission_problems` and `at_submission` both ask this function whether the
    question has been put. A view whose status only one of its two readers filled in is a view whose
    other reader crashes, which is what happened before this line moved here.
    """
    late = (state or {}).get("late")
    if not isinstance(late, dict) or not late.get("id"):
        return None
    stored = late.get("envelope")
    env: dict[str, Any] = stored if isinstance(stored, dict) else {}
    view = {"id": str(late["id"]), "about": "cell_budget", "text": str(late.get("text") or ""),
            "options": [str(o) for o in (late.get("options") or [])], "subjects": [],
            "effect": "changes_mesh", "default": late.get("default"),
            "evidence": [f"the plan's envelope {env.get('cells_high')} cells ({env.get('source')})",
                         f"your stated budget {env.get('cap')} cells"],
            #: the third intake is a budget question: its default names a number, never a mouth
            "proposal": {},
            "route": ROUTE_LATE, "why": str(late.get("because") or ""),
            "options_from": "the stated budget and the builder emulator's envelope for this plan",
            #: the same keys every other view carries, so a reader never has to know which kind of view it
            #: holds. The third intake is always put (that is what raising it means) and it is not one of the
            #: finder's questions, so it has no tier of the finder's and no `ask.record` row
            "put": True, "held_because": "", "held_quote": "", "tier": LATE_TIER,
            "tier_why": _package()["ask_schema"].TIER_WHY.get(LATE_TIER, ""),
            "record": {}, "finder": "",
            #: the two numbers the options were written from, in the marks `_trade_envelope` reads, so an
            #: answer is settled from them and never parsed back out of the sentence
            "marks": [{"field": "cell_budget", "kind": "stated", "value": env.get("cap")},
                      {"field": "forecast.cells_high", "kind": "measured", "value": env.get("cells_high")}]}
    view["status"] = _status(view, [a for a in live_answers(state) if a.get("question_id") == view["id"]])
    return view


#: HOW A ROLE QUESTION IS ANSWERED, and there are two shapes because there are two kinds of role question.
#: `MOUTH_FOR_ROLE` asks "which of these mouths is the inlet": its id names the role, its options are mouth
#: ids, and one answer settles it. `ROLE_PER_MOUTH` asks "which of these carries the incoming flow, and which
#: are outlets": its options are ROLES, the mouths it names are in `subjects`, and it is settled only when
#: every mouth it named has a role. `ask.say.port_roles` writes the second and `contract.asking` writes the
#: first, so the platform reads both rather than the one its old finder happened to produce.
MOUTH_FOR_ROLE, ROLE_PER_MOUTH = "mouth_for_role", "role_per_mouth"

_ROLE_FOR_ONE = re.compile(r"^role_(inlet|outlet)$")


def _role_shape(view: dict) -> str:
    """Which of the two role shapes this view is, or `""` when it is not a role question at all."""
    if view.get("about") != "opening.role":
        return ""
    return MOUTH_FOR_ROLE if _ROLE_FOR_ONE.match(str(view.get("id") or "")) else ROLE_PER_MOUTH


def _status(view: dict, answers: list[dict]) -> str:
    """`open`, `answered`, `skipped` or `defaulted`.

    A default that stood is NOT an answer. It is its own status, and every consumer treats it as
    open: a role or a budget nobody confirmed is a question still unsettled, and a question still
    unsettled is what rides to the builder under `unsettled`.
    """
    confirmed = [a for a in answers if a.get("answered_by") == CUSTOMER and not a.get("skipped")]
    if _role_shape(view) == ROLE_PER_MOUTH:
        # one mouth at a time, and settled only when every mouth it named has a role from the customer
        named = {a.get("subject") for a in confirmed}
        if set(view["subjects"]) and set(view["subjects"]) <= named:
            return "answered"
    elif confirmed:
        return "answered"
    if any(a.get("skipped") for a in answers):
        return "skipped"
    if any(a.get("answered_by") == DEFAULT_TAKEN for a in answers):
        return "defaulted"
    return "open"


SETTLED = ("answered", "skipped")


def open_now(state: dict) -> list[dict]:
    """The questions to put NOW, in the chain's order.

    Step 4 first: every intake-routed question not yet settled. Only when none is left does the budget
    trade appear, and it appears once: a trade that was put and answered or skipped is never put
    again. A default that stood leaves a question open, so it is put again rather than read as a yes.

    A QUESTION THE FINDER HELD IS NEVER HERE. `put` False is the cap and the two other cuts this pipeline
    makes for itself; a held question is reported to the builder as unsettled and is not put to a person.
    That is what makes `ask.intake.MAX_ASKED` a fact of the product rather than of a test.
    """
    views = [v for v in question_views(state) if v["put"]]
    step4 = [v for v in views if v["route"] == ROUTE_INTAKE and v["status"] not in SETTLED]
    if step4:
        return step4
    trade = [v for v in views if v["route"] == ROUTE_TRADE and v["status"] not in TRADE_PUT]
    if trade:
        return trade
    # the third intake last, and put once exactly as the trade is: there is none unless the geometry
    # agent's step planned the part and its plan raised one
    return [v for v in views if v["route"] == ROUTE_LATE and v["status"] not in TRADE_PUT]


#: The budget trade is put ONCE. A customer who let its default stand has not confirmed a budget, so the
#: question stays unsettled and rides to the builder as such, but it is not put to them a second time.
TRADE_PUT = (*SETTLED, "defaulted")


def stage_of(state: dict) -> str:
    """Which stage the row is at. Only questions that are PUT can hold it open: a row whose one remaining
    question the finder held is `settled`, because nobody will ever be asked it."""
    views = [v for v in question_views(state) if v["put"]]
    step4 = [v for v in views if v["route"] == ROUTE_INTAKE]
    trade = [v for v in views if v["route"] == ROUTE_TRADE]
    if any(v["status"] not in SETTLED for v in step4):
        return STAGE_ASKING if any(v["id"] in (state.get("asked") or []) for v in step4) else STAGE_SURVEYED
    if any(v["status"] not in TRADE_PUT for v in trade):
        return STAGE_TRADE
    # a row whose third-intake question is still open is not settled, and the row says so rather than
    # reporting the word that means every question has been put. There is none with the step off
    if any(v["status"] not in TRADE_PUT for v in views if v["route"] == ROUTE_LATE):
        return STAGE_LATE
    return STAGE_SETTLED


def mark_asked(state: dict, views: list[dict]) -> dict:
    asked = list(state.get("asked") or [])
    for v in views:
        if v["id"] not in asked:
            asked.append(v["id"])
    out = {**state, "asked": asked}
    out["stage"] = stage_of(out)
    return out


# -------------------------------------------------------------------------------------------------
# the answers, with who gave them
# -------------------------------------------------------------------------------------------------

_SPACE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACE.sub(" ", str(text or "").strip().lower())


def said_by_customer(words: str, latest_user_message: str, earlier: Any = None) -> bool:
    """The quote is in what the customer just wrote, or is a standing delegation they wrote earlier.

    The first shape is the one engine selection already asks for and is unchanged: a model that cannot
    quote the customer did not hear them say it.

    THE SECOND SHAPE EXISTS BECAUSE THE FIRST MADE A DELEGATION EXPIRE, and a submission depend on
    whether the model used it in time. MEASURED on ahmed_variant_001, four flow conversations with the
    same script ("internal cfd, air through it", "you decide everything", then "go"):

      - two recorded the port roles on the turn "you decide everything" WAS the latest message, and
        submitted in five and six turns;
      - two did not, and could never submit. `role_problems` refuses every port role a customer has
        not confirmed, the delegation was no longer quotable, and the runs ended with "submit rejected
        - port roles the customer has not confirmed" twice over and the turn cap. The customer was
        told "the system will not accept the port roles unless you write them yourself".

    Same script, same part, opposite outcome, decided by which turn the model chose to record on. So a
    standing instruction was being read as a one-turn utterance.

    NARROW ON PURPOSE. Only a DEFERRAL may be quoted from an earlier message - "you decide", "your
    call", "whatever's best" - because a deferral is the one kind of thing that does not expire: it
    says do not ask me again, and it is still true three turns later. Everything else must still be in
    their latest message, so "no, not that one" from two turns ago can never be spent as agreement to
    something else. This is the same argument `engine_selection.choice_deferred` already makes for the
    engine, where a delegation likewise survives the turn it was given on.
    """
    quote = _norm(words)
    if not quote:
        return False
    if quote in _norm(latest_user_message):
        return True
    from meshpipeline.agents.intake.recommendation import choice_deferred
    if not choice_deferred(words):
        return False
    return any(quote in _norm(str(m)) for m in (earlier or ()))


def accepts_a_proposal(latest_user_message: str, earlier: Any = None) -> bool:
    """Has the customer accepted a proposal that is on screen, or handed us the decision and not taken it back?

    TWO SHAPES, AND BOTH ARE READ OFF THEIR OWN MESSAGE rather than relayed by a model, which is the whole
    point: `record_answer`'s other paths let a model NAME the option, so a model quoting the bare word "go"
    could record any role it liked and satisfy the gate. This reads what they wrote.

      an acceptance   `engine_selection.affirms`, which is the reader already used for the one question the
                      application puts on screen itself. It refuses any message carrying a denial or a
                      hesitation anywhere in it, so "go, but o2 is the inlet" is not an acceptance of anything.
      a delegation    `recommendation.choice_deferred`, in their latest message or in an earlier one. A
                      delegation does not expire - that is `said_by_customer`'s own argument, measured - so
                      "everything else you decide" three turns ago still stands.

    IT IS NOT AN ANSWER TO THE QUESTION and it is not read as one. It is permission to record the PLATFORM'S
    proposal, which is why the value recorded is never the model's: see `_the_proposal_recorded`.
    """
    from meshpipeline.agents.intake.engine_selection import affirms
    from meshpipeline.agents.intake.recommendation import choice_deferred

    if affirms(latest_user_message):
        return True
    if choice_deferred(latest_user_message):
        return True
    return any(choice_deferred(str(m)) for m in (earlier or ()))


def _the_proposal_recorded(state: dict, view: dict, row: dict, *, latest_user_message: str,
                           earlier: Any, principal: str) -> dict:
    """The PLATFORM'S OWN proposal recorded as the customer's answer, one row per place it names.

    WHY THE PLATFORM'S AND NOT THE MODEL'S. The owner's design, in his words: "why even have the confidence
    score be an issue? like we anyway confirm it with the user so only what passes there is used to make the
    plan". The platform proposes, the customer's one "go" confirms the whole block, and the plan runs on that.
    The proposal is `ask.say.port_roles`' `default_roles` - the same reading its sentence is written from,
    which is what the mesher does when nobody answers - so what is recorded is what the customer was shown.

    AND IT IS STRICTLY SAFER THAN THE PATH IT REPLACES. Before this, a customer's "go" was recorded by the
    model calling `answer_survey_question` once per mouth with an option of its own choosing and their word
    "go" as the quote: the gate checked that the quote was theirs and that the option was one of the
    question's, and nothing checked that the option was the one they had been shown. Here the value comes
    from the platform, and the only thing the model contributes is the claim that they accepted - which
    `accepts_a_proposal` checks against their own message, and which can therefore fail on its own.

    IT IS THE SAME MOVE `engine_selection.answers_the_selection_question` ALREADY MAKES FOR THE ENGINE:
    "deferring to a choice we have already made and shown is accepting it", and there too the value taken is
    the one the application proposed and not a name the model wrote. A customer who disagrees corrects it -
    `_corrected` supersedes a confirmed role on their own later words - and the roles are named in the
    requirements block they confirm before anything is dispatched.
    """
    proposal = dict(view.get("proposal") or {})
    if not proposal:
        raise SurveyError(
            f"{view['id']!r} carries no proposal for the application to record, so an acceptance cannot "
            f"settle it: its default is {view.get('default')!r}, which names no place. Record what they "
            f"said with an option, or took_default if they left it to you")
    if not accepts_a_proposal(latest_user_message, earlier):
        raise SurveyError(
            "their latest message does not accept the proposal and they have not handed you the decision "
            "either, so the application may not record its own reading as their answer. Record the option "
            "they named, or put the question")
    for place in view["subjects"]:
        if place not in proposal:
            raise SurveyError(
                f"the proposal for {view['id']!r} does not place {place}, and a proposal that covers some of "
                f"the mouths it names is not one the application may record; it places "
                f"{sorted(proposal)}. Put the question")
    for place, role in sorted(proposal.items()):
        if place not in view["subjects"]:
            continue
        # the same option words and the same vocabulary as an answer the customer named, read through the one
        # function that knows both role shapes, so an accepted proposal and a stated role land identically
        at, value = _role_answer(view, place, role, "")
        option = _canonical_option(role, view["options"]) or role
        state = _corrected(state, view, at, value, principal)
        answer = {**row, "answered_by": CUSTOMER, "subject": at, "value": value, "option": option,
                  "note": ACCEPTED_THE_PROPOSAL}
        _check_with_the_package(answer)
        state = _append(state, answer)
    return state


def _canonical_option(choice: str, options: list[str]) -> str | None:
    wanted = _norm(choice)
    for option in options:
        if _norm(option) == wanted:
            return option
    return None


def record_answer(state: dict, *, question_id: str, choice: str = "", role: str = "",
                  words: str = "", latest_user_message: str = "", principal: str = "",
                  skipped: bool = False, took_default: bool = False, subject: str = "",
                  earlier_customer_messages: Any = None, accepted_proposal: bool = False) -> dict:
    """One answer, checked, appended. Raises `SurveyError` with a sentence for the model.

    WHAT MAKES IT THE CUSTOMER'S. The quote has to be in their latest message, and the choice has to
    be one of the question's own options (`ask/settle.py`'s rule: a phrase settles a question only by
    selecting one of its options). Nothing here maps free text onto an option.

    WHAT IT BECOMES. The package's `contract.intake.Answer`, built here and asked for its mark, so the
    ownership table decides whether a person may set the field at all. A role is the mouth the
    customer picked, with the role the question was about; a budget is the number the option names,
    taken from the question's own evidence marks rather than parsed out of the words.

    `subject` is WHICH MOUTH, for the one question that names several and whose options are the roles
    (`ask.say.port_roles`). It is the other half of that answer: the option says what the mouth is for and
    `subject` says which mouth, and neither alone is an answer.

    AN ANSWER TO A HELD QUESTION IS STILL AN ANSWER. Every cut this pipeline makes for itself leaves the
    question whole, and a customer who volunteers a role on a mouth the ranking held overrules the cut. What
    is refused is a question nobody may answer here at all: the unit, which this platform asks in its own
    words, and an advisory one, whose answer changes nothing.
    """
    views = {v["id"]: v for v in question_views(state)}
    view = views.get(question_id)
    if view is None:
        raise SurveyError(f"there is no survey question {question_id!r}; the questions are "
                          f"{sorted(views)}")
    if view["route"] not in (ROUTE_INTAKE, ROUTE_TRADE, ROUTE_LATE):
        raise SurveyError(f"{question_id!r} is not put through this tool" + (
            ": the application asks for the unit itself" if view["route"] == ROUTE_APPLICATION else ""))
    # ONLY A QUESTION THAT WAS PUT CAN HOLD THE TRADE OPEN, exactly as `open_now` and `stage_of` already read
    # it. This gate read every view, held ones included, and its two siblings filter on `put` - so a question
    # the finder HELD was invisible to the two functions that decide whether to ask the trade and visible to
    # the one that accepts the answer. The platform therefore PUT the trade and then refused the customer's
    # reply to it, every time, for good: `record_answer` is the only way an answer reaches the row, the held
    # question is never put to anybody (that is what `ask.intake.MAX_ASKED` means), so nothing could ever
    # settle it and nothing could ever settle the trade either.
    #
    # REACHABLE ON THE THIRD INTAKE TODAY, and measured there. The survey's own trade sits at the `resolution`
    # tier, which is the lowest `ask.schema.TIERS` puts, so the cap holds it before it holds any
    # intake-routed question and the two states cannot meet on it. The third intake's question is `put` by
    # construction (`late_view`), so on a part where the cap held one step-4 question - block_boss_sharp has
    # six questions - `open_now` returned the plan's budget question, `stage_of` said `third_intake`, and this
    # line refused the answer. Two siblings agreeing and one disagreeing about the same row.
    if view["route"] in (ROUTE_TRADE, ROUTE_LATE) and any(
            v["put"] and v["route"] == ROUTE_INTAKE and v["status"] not in SETTLED
            for v in views.values()):
        raise SurveyError("the budget trade is only put once every other survey question is settled")
    if view["route"] == ROUTE_LATE and view["status"] in TRADE_PUT:
        # ASKED ONCE. A late question that was answered, skipped or let default is not put again, and an
        # answer to it is not replaced by a second one
        raise SurveyError(f"{question_id!r} was already put and settled as {view['status']}; it is asked once")
    if not said_by_customer(words, latest_user_message, earlier_customer_messages):
        raise SurveyError("that quote is not in the customer's latest message. Quote their own words "
                          "exactly; if they did not answer, do not record an answer")
    row: dict[str, Any] = {"question_id": question_id, "about": view["about"], "at": _now(),
                           "words": str(words)[:500], "principal": str(principal or "")[:256],
                           "via": "intake_conversation"}
    if view["route"] == ROUTE_LATE:
        row["stage"] = LATE_STAGE
    if skipped:
        return _append(state, {**row, "answered_by": CUSTOMER, "skipped": True, "subject": "",
                               "value": None})
    if took_default:
        # Stored so the record shows the customer was asked and let the default stand. Never an
        # answer: `_status` reads it as `defaulted`, the IntakeHandoff lists it unanswered, and the
        # builder is told the question is still open.
        return _append(state, {**row, "answered_by": DEFAULT_TAKEN, "subject": "",
                               "value": view.get("default")})
    if accepted_proposal:
        # THEY ACCEPTED WHAT THE APPLICATION PROPOSED, so what is recorded is the application's own proposal
        # and not an option the model named. This is not `took_default`: a default that stood is nobody
        # answering, and on a role question `role_problems` treats it as a veto, so recommending it here would
        # make submission impossible. See `_the_proposal_recorded`.
        return _the_proposal_recorded(state, view, row, latest_user_message=latest_user_message,
                                      earlier=earlier_customer_messages, principal=principal)
    option = _canonical_option(choice, view["options"])
    if option is None:
        raise SurveyError(f"{choice!r} is not one of the options for {question_id!r}: "
                          f"{view['options']}")
    at, value, note = _subject_and_value(view, option, role, subject)
    # A CORRECTION SUPERSEDES THEIR OWN EARLIER WORDS, it is not refused. See `_corrected`: this used to raise
    # on any conflicting role, which locked a customer who named the wrong opening as the inlet into it for the
    # life of the job and told the model to do something the code then refused.
    state = _corrected(state, view, at, value, principal)
    answer = {**row, "answered_by": CUSTOMER, "subject": at, "value": value, "option": option,
              "note": note}
    _check_with_the_package(answer)
    return _append(state, answer)


def _subject_and_value(view: dict, option: str, role: str, subject: str = "") -> tuple[str, Any, str]:
    shape = _role_shape(view)
    if shape == MOUTH_FOR_ROLE:
        # "which of these mouths is the inlet": the id names the role and the option is the mouth
        return option, view["id"][len("role_"):], ""
    if shape == ROLE_PER_MOUTH:
        return (*_role_answer(view, option, role, subject), "")
    if view["about"] == "cell_budget":
        cap = _trade_cap(view, option)
        if cap is None:
            raise SurveyError(f"the budget trade cannot settle a cap from {option!r}: its numbers are "
                              f"{_trade_envelope(view)} and the option names neither holding nor raising")
        return "", cap, option
    values = view.get("option_values") or []
    index = view["options"].index(option)
    if view["about"] == "representation" and not values:
        # THE SIDE QUESTION FROM THE STEP-4 FINDER CARRIES NO PAIRED VALUES, because `ask.say.fluid_side`
        # writes its options as two sentences and keeps the map from a sentence back to a side in the package
        # (`ask.say.fluid_side_of`), where `contract.asking` paired them positionally instead. Read the map,
        # never a positional guess and never the sentence itself: stored as the sentence, the sentence became
        # the value of the `representation` fact the whole chain reads, and the geometry agent then refused
        # every plan on a part whose side was answered.
        side = _side_answer(view, option)
        if side is None:
            raise SurveyError(f"{option!r} names neither reading of this surface, so it settles no "
                              f"representation; the readings are {_side_readings(view)}")
        return "", side, option
    if index < len(values) and values[index]:
        # WHAT THE OPTION SETS, not the sentence the person was shown. The fluid-side question's two
        # options are sentences and its two values are the representations they mean, so the answer that
        # reaches `contract.marks` is a representation and not prose. It was the sentence before this line,
        # and `Given.representation` is a CONFIRMED field, so the sentence won over the measured word and
        # the geometry agent's own consistency check refused every plan on a part whose side was answered:
        # "the loop's context reads this part as 'annular_fluid' and the survey measured 'the fluid runs
        # through the bores; the part is the solid around it'". Answering made the job worse than saying
        # nothing. The words are kept in `note`, so nothing a person said is lost.
        return view["subjects"][0] if len(view["subjects"]) == 1 else "", str(values[index]), option
    return view["subjects"][0] if len(view["subjects"]) == 1 else "", option, ""


def _role_answer(view: dict, option: str, role: str, subject: str) -> tuple[str, str]:
    """`(the mouth, the role)` for a question that asks a role per mouth. Raises `SurveyError`.

    TWO QUESTIONS HAVE THIS SHAPE AND THEY LIST DIFFERENT THINGS. `ask.say.port_roles` puts ONE question
    naming every unplaced mouth and its options are the ROLES ("inlet", "outlet", "wall", "closed for this
    run"); `contract.asking`'s `role_count` lists the measured MOUTHS and the role is named beside the choice.
    Which it is is read off the question's own options rather than off its id, and the caller names the other
    half either way, so the answer that lands is always `subject = the mouth, value = the role`. Stored the
    other way round, `Given.role_of(mouth)` finds nothing and every role a customer confirmed reads as
    unsettled downstream.

    The role words are the package's: `ask.say`'s "closed for this run" is the customer's phrase and
    `chain.job.ROLE_WORD` is the package's own map onto the vocabulary `contract.marks` admits. It is read,
    never restated here.
    """
    pkg = _package()
    roles = pkg["asking"].ROLE_OPTIONS
    words = dict(pkg["ask_job"].ROLE_WORD)
    if option in view["subjects"]:
        mouth, said = option, _norm(role)
    else:
        mouth, said = str(subject or "").strip(), words.get(_norm(option), _norm(option))
        if mouth not in view["subjects"]:
            raise SurveyError(f"say which mouth the customer gave {option!r} to: one of "
                              f"{list(view['subjects'])}")
    wanted = words.get(said, said)
    if wanted not in roles:
        raise SurveyError(f"say which role the customer gave {mouth or option}: one of {list(roles)}")
    return mouth, wanted


def _record_measurements(view: dict) -> list[dict]:
    """Every `measurement` the finder put on this question's record rows (`ask.record.record_for`).

    It is where the machine values live for a question `ask.say` words as prose: the two side readings, the
    budget's two numbers. The row is the finder's own and this platform only reads it.
    """
    out = []
    for target in ((view.get("record") or {}).get("targets") or []):
        measured = target.get("measurement") if isinstance(target, dict) else None
        if isinstance(measured, dict):
            out.append(measured)
    return out


def _side_readings(view: dict) -> dict:
    """`{side: the representation that side gives}` for the fluid-side question, from its own record."""
    for measured in _record_measurements(view):
        readings = measured.get("readings")
        if isinstance(readings, dict) and readings:
            return {str(k): str(v) for k, v in readings.items()}
    return {}


def _side_answer(view: dict, option: str) -> str | None:
    """The REPRESENTATION one option of the fluid-side question sets, or None when it names neither side.

    Two steps, both the package's own: `ask.say.fluid_side_of` maps the sentence a person read back to the
    `catalog.FLUID_SIDES` key it means, and the question's own `readings` map that key to the representation
    that reading gives. `_side_of` inverts the same table on the way back into a composition, so the row
    stores a representation and the composition gets the side, with one table behind both.
    """
    readings = _side_readings(view)
    if not readings:
        return None
    bores = next((int(m.get("bores") or 0) for m in _record_measurements(view) if m.get("bores")), 1)
    side = _package()["ask_say"].fluid_side_of(option, bores)
    reading = readings.get(str(side)) if side else None
    return str(reading) if reading else None


def _trade_envelope(view: dict) -> dict | None:
    """`{"cap": the stated budget, "cells_high": the measured envelope}` for a budget question, or None.

    TWO PLACES CARRY THE NUMBERS BECAUSE TWO FINDERS WRITE THE QUESTION. `contract.asking` puts both on the
    uncertainty as evidence marks, and so does the third intake's own view; `ask.say.budget` puts them in the
    uncertainty's `measurement`, which reaches this platform through `ask.record`'s row. Both are read and
    neither is parsed out of the sentence a person was shown.
    """
    cap = high = None
    for mark in view.get("marks") or []:
        if mark.get("field") == "cell_budget":
            cap = mark.get("value")
        elif mark.get("field") == "forecast.cells_high":
            high = mark.get("value")
    for measured in _record_measurements(view):
        cap = measured.get("cell_cap") if cap is None else cap
        high = measured.get("cells_high") if high is None else high
    if isinstance(cap, int) and isinstance(high, int):
        return {"cap": int(cap), "cells_high": int(high)}
    return None


def _trade_cap(view: dict, option: str) -> int | None:
    """The budget ONE option sets, by the package's own rule and never by this module's own.

    `ask.trade.Trade.value_of` is that rule: an option that holds keeps the number the customer stated and one
    that raises takes the measured envelope. It is read rather than restated because the three questions that
    can arrive here word their options three ways ("hold 2,000,000", "hold the budget", "hold it and tell me
    what was lost") and an index into the list was wrong on the third the moment a third existed.
    """
    env = _trade_envelope(view)
    if env is None:
        return None
    trade = _package()["ask_trade"].Trade(id=str(view["id"]), kind="", text="", options=list(view["options"]),
                                          default="", default_is="operation", because="", envelope=env)
    return trade.value_of(option)


#: Why an answer was retired when the CUSTOMER changed it, rather than when the question it was bound to
#: stopped existing. Both are retirements - the row is the record and no reader takes either as input - and
#: they are different facts, so a reader of the row can tell "we stopped asking this" from "they said
#: something else". `carry_answers` writes the first and `_corrected` below writes this one.
SUPERSEDED = "the customer corrected it"


def _corrected(state: dict, view: dict, subject: str, value: Any, principal: str) -> dict:
    """The row with the customer's earlier role for this mouth retired, when this answer corrects it.

    A CONFIRMED ROLE COULD NEVER BE CORRECTED, and that is what this replaces. `_refuse_conflict` raised on
    every conflicting role for a mouth, so a customer who named the wrong opening as the inlet was locked into
    it for the life of the job: `role_problems` then refuses any patch declared the other way, and the only way
    out was a new upload. Worse, the sentence it returned - "Ask them which it is before recording it as the
    outlet" - named a path that DOES NOT EXIST: ask them, hear the correction, try to record it, and the same
    refusal fires again on the answer that was the correction. An instruction the code cannot carry out is
    worse than no instruction, because the model spends the customer's turns following it.

    A CORRECTION IS NOT A DEFAULT AND IT IS NOT THE MODEL'S PROSE, which is the only reason this is allowed to
    supersede a confirmation. `record_answer` has already proved two things about it before this runs: the
    quote is in the customer's LATEST message (`said_by_customer`) and the choice is one of the question's own
    options (`_canonical_option`). So it is the customer's own words superseding their own earlier words, which
    is what a person changing their mind looks like, and house law 2 is about a value nobody confirmed.

    WHAT IS STILL REFUSED is an answer that would rewrite a confirmation recorded for a DIFFERENT account.
    The row keeps `principal` beside every answer and this is the only place two of them can meet on one mouth;
    one customer's confirmed boundary condition is not overwritten by another's session. Where either principal
    is unrecorded there is nothing to compare and the correction stands, which is every row written before the
    field was stored and every caller that passes none.

    NOTHING IS DELETED. The earlier answer stays in the append-only list marked retired, with `SUPERSEDED` and
    the value that replaced it, so the record still says what they were asked and what they said first. Every
    reader goes through `live_answers`, which skips a retired row, so `confirmed_roles`, `named_inlets`,
    `_status`, `intake_handoff` and the submission gate all see the correction and only the correction - one
    retirement, read by everything, rather than a second rule in each of them.
    """
    if view["about"] != "opening.role":
        return state
    mine, answers, corrected = str(principal or ""), [], False
    for a in state.get("answers") or []:
        if (isinstance(a, dict) and not a.get("retired") and a.get("about") == "opening.role"
                and a.get("answered_by") == CUSTOMER and not a.get("skipped")
                and a.get("subject") == subject and a.get("value") != value):
            said_by = str(a.get("principal") or "")
            if said_by and mine and said_by != mine:
                raise SurveyError(
                    f"{subject} is recorded as the {a.get('value')} on this job for another account "
                    f"({said_by}), and this answer is {mine}'s, so recording it would overwrite somebody "
                    f"else's confirmation. It is not recorded. Tell the customer that {subject} is down as "
                    f"the {a.get('value')} and that the account which set it has to be the one to change it")
            answers.append({**a, "retired": True, "retired_at": _now(),
                            "retired_because": SUPERSEDED, "superseded_by": str(value)})
            corrected = True
        else:
            answers.append(a)
    return {**state, "answers": answers} if corrected else state


def _check_with_the_package(answer: dict) -> None:
    pkg = _package()
    try:
        pkg["intake"].Answer(question_id=answer["question_id"], about=answer["about"],
                             subject=answer["subject"], value=answer["value"],
                             answered_by=answer["answered_by"], at=answer["at"]).as_mark()
    except pkg["marks"].ContractError as exc:
        raise SurveyError(f"the contract refuses that answer: {exc.problem}") from exc


def _append(state: dict, answer: dict) -> dict:
    out = {**state, "answers": list(state.get("answers") or []) + [answer]}
    out = mark_asked(out, [{"id": answer["question_id"]}])
    out["stage"] = stage_of(out)
    return out


def confirmed_roles(state: dict | None) -> dict[str, str]:
    """Mouth id to the role the CUSTOMER gave it. Defaults and skips are not in it."""
    out: dict[str, str] = {}
    for a in live_answers(state):
        if (isinstance(a, dict) and a.get("about") == "opening.role" and a.get("answered_by") == CUSTOMER
                and not a.get("skipped") and a.get("subject")):
            out[str(a["subject"])] = str(a.get("value") or "")
    return out


def confirmed_cell_cap(state: dict | None) -> int | None:
    """The budget the customer confirmed in the trade, or None. A stated budget is not this.

    The trade at step 6 of the SURVEY, costed on the measurement alone. A budget confirmed at the third
    intake, against the geometry agent's plan, is not composed back into the survey: that question is not
    one of the survey's, and the composition it would change is the one the plan was made against. It
    reaches the builder through the geometry agent's handoff instead (`application/geometry_step.py`)."""
    for a in reversed(live_answers(state)):
        if (isinstance(a, dict) and a.get("about") == "cell_budget" and a.get("answered_by") == CUSTOMER
                and not a.get("skipped") and isinstance(a.get("value"), int) and a.get("stage") != LATE_STAGE):
            return int(a["value"])
    return None


# -------------------------------------------------------------------------------------------------
# handoff 2 and handoff 3, in the package's own types
# -------------------------------------------------------------------------------------------------

def intake_handoff(state: dict):
    """What intake carries to the geometry agent: `contract.intake.IntakeHandoff`, built from the rows.

    Only the customer's own answers become `Answer`s. A default that stood and a skip are listed
    `unanswered`, because a question that was asked and not settled is a different thing from one
    never asked, and the package refuses a handoff that cannot tell them apart.
    """
    pkg = _package()
    # THE THIRD INTAKE IS NOT IN THIS HANDOFF. Its question is not one of the survey's, so an answer to it
    # here is an answer to a question the survey never raised, which `contract.intake.binds_to` refuses; it
    # travels in its own `LateIntakeHandoff` (`application/geometry_step.late_handoff`)
    late_id = str(((state.get("late") or {}) if isinstance(state.get("late"), dict) else {}).get("id") or "")
    asked = [q for q in (state.get("asked") or []) if not late_id or q != late_id]
    answers = []
    answered_ids: set[str] = set()
    for a in live_answers(state):
        if a.get("answered_by") != CUSTOMER or a.get("skipped"):
            continue
        if a.get("stage") == LATE_STAGE or (late_id and a.get("question_id") == late_id):
            continue
        answers.append(pkg["intake"].Answer(question_id=a["question_id"], about=a["about"],
                                            subject=str(a.get("subject") or ""), value=a.get("value"),
                                            answered_by=CUSTOMER, at=str(a.get("at") or _now()),
                                            note=str(a.get("note") or "")))
        answered_ids.add(a["question_id"])
    for q in answered_ids:
        if q not in asked:
            asked.append(q)
    return pkg["intake"].IntakeHandoff(
        survey_source_sha256=str(state.get("sha256") or ""),
        survey_facts_sha256=str(state.get("facts_sha256") or ""),
        asked=asked, answers=answers, unanswered=[q for q in asked if q not in answered_ids])


#: What the block says when step 5 could not plan the part and the row does not say why. The reason is
#: normally the step's own, and this is the one case where there is a failed step and nothing kept the
#: sentence - a row written before `plan_the_part` stored one, which is worth saying rather than dropping
#: the whole row over.
NO_PLAN_NO_REASON = "the step recorded no reason"


def with_no_plan_if_the_step_failed(block: dict | None, state: dict | None) -> dict | None:
    """The block, with one `unsettled` row saying step 5 made no plan for this part, and why. Never raises.

    THE BLOCK THE BUILDER READ WAS BYTE FOR BYTE THE BLOCK OF A JOB THE GEOMETRY AGENT NEVER RAN ON. When the
    step fails, `geometry_step.plan_the_part` stores `status: failed` with the reason, `builder_handoff` raises
    `StepRefused`, and `cad/regions.planner_inputs_for_state` falls back to this block and writes the reason to
    the job's record. So the platform knew, the record knew, the log knew - and the BUILDER, the one reader that
    acts on it, was told nothing at all. It could not tell a job whose plan was attempted and failed from a job
    where the step never ran.

    IT IS THE ROW AND NOT THE BLOCK THAT IS AT RISK. Everything on this path fails open, because a block with
    no sentence is where the builder already was and no block is worse; so a refusal of the row costs the row.

    THE REASON IS SAFE TO CARRY AND THAT IS CHECKED RATHER THAN ASSUMED. `deliver.with_no_plan` passes it
    through `deliver.safe_reason`, which proves the sentence against `refuse_outcome_claims` before writing it
    and substitutes `REFUSAL_UNQUOTABLE` when it would not pass - and the platform's own two extra rules
    (`check_the_survey_block`) cannot fire on it either, because `_refuse_builder_keys` reads dict KEYS and the
    reason is a value. `test_a_reason_that_predicts_the_mesh_still_reaches_the_builder` holds that, with the
    step's reason quoting every outcome phrase the rule knows.
    """
    # READ FROM THE MODULE THAT WRITES IT, never restated here: `geometry_step` owns the two words and a copy
    # on this side would go on matching `failed` on the day that module stops writing it. Imported inside the
    # function because `geometry_step` imports this one at its top.
    from meshpipeline.application.geometry_step import FAILED as STEP_FAILED

    step = state.get("geometry_step") if isinstance(state, dict) else None
    if not isinstance(step, dict) or str(step.get("status") or "") != STEP_FAILED:
        return block
    try:
        why = str(step.get("reason") or "").strip() or NO_PLAN_NO_REASON
        out = _package()["deliver"].with_no_plan(block, why)
        check_the_survey_block((out or {}).get("survey"))
        return out
    except Exception as exc:                       # noqa: BLE001 - the block is worth more than the row
        logger.warning("geometry survey: the builder is not told that no plan was made for this part (%s) - "
                       "it reads a block it cannot tell from one the geometry agent never ran on", exc)
        return block


def builder_block(state: dict | None) -> dict | None:
    """Step 7: the package's planner block composed for this customer, with the survey attached.

    `contract.deliver.attach` puts `survey` under the block, and `deliver.survey_block` has already
    run the package's own validator on it before it returns. Nothing is computed here. None when
    there is no state, or when the package refuses the pair, and the caller then hands the planner
    the measurement's own block exactly as it does with the survey off.

    A FAILED STEP IS SAID IN THE BLOCK, and this is the one place both routes to the builder pass through:
    `cad/regions._surveyed_block` ends here, and the other route (`_with_the_look_state`, taken when no
    survey was stored) has no `geometry_step` to read because that key lives on the survey ROW.
    """
    if not isinstance(state, dict) or not isinstance(state.get("planner_block"), dict):
        return None
    pkg = _package()
    try:
        given = pkg["given"].Given.of(survey_of(state), intake_handoff(state))
        block = pkg["deliver"].attach(dict(state["planner_block"]), given)
        return with_no_plan_if_the_step_failed(with_the_look_state(block, look_state(state)), state)
    except Exception as exc:                       # noqa: BLE001 - a plan is never failed for this
        logger.warning("geometry survey: the builder's block could not carry the survey (%s)", exc)
        return None
    finally:
        # `survey_refused` is what the package writes when it composed no survey and said why. It was
        # written and read nowhere, on either side: the caller then hands the planner the raw block,
        # which carries the reason in a key nobody looked at. The agent now reads it at
        # deliver.check_builder_handoff; this is the platform's half, so the reason reaches a log on the
        # machine that ran the job and not only a stored row.
        _block = state.get("planner_block") if isinstance(state, dict) else None
        _refused = _block.get("survey_refused") if isinstance(_block, dict) else None
        if _refused:
            logger.warning("geometry survey: the package composed no survey for this job: %s", _refused)


def check_the_survey_block(block: Any) -> None:
    """The package's own validator on a survey block, PLUS the two rules it does not run. Raises `SurveyError`.

    WHY THIS EXISTS, and it is the audit's central lesson in one function. `contract.deliver.check_survey_block`
    is the block's contract in executable form and it runs four rules: the required keys, every leaf a valid
    mark, no `seen` mark carrying a digit, and the ceiling. It does NOT run the other two,
    `contract.survey._refuse_builder_keys` and `_refuse_outcome_claims`, which are private to `contract/survey.py`
    and fire only inside a `SurveyHandoff`'s own validator. The look's free text is added to the block AFTER that
    validator has run (`hexera.planner_survey` puts `seen` and `at_places` on after `survey_from`), and that block
    is what reaches the builder whenever the geometry step did not run.

    MEASURED on the platform's own block for `ahmed_variant_001_external_looked`: `check_survey_block` accepts a
    `seen` row carrying the key `n_layers`, and accepts the value "the mesh will collapse here". The first is a
    builder control the Surveyor has no business naming; the second is a prediction of how the mesh turns out,
    which this handoff never makes. Both are refused here.

    The two word lists are the package's and are READ, never restated: `contract.survey.BUILDER_KEYS` and
    `OUTCOME_WORDS` are both public. The day the block fixer runs these inside `check_survey_block`, this
    function becomes a second call of the same rules and costs nothing.
    """
    pkg = _package()
    try:
        pkg["deliver"].check_survey_block(block)
    except pkg["marks"].ContractError as exc:
        raise SurveyError(f"the survey block breaks the package's own contract: {exc}") from exc
    keys, words = pkg["survey"].BUILDER_KEYS, pkg["survey"].OUTCOME_WORDS
    for where, found in _contract_breaks(block, keys, words):
        raise SurveyError(f"the survey block at {where or 'its root'} {found}; the Surveyor names places and "
                          f"measurements, never a builder setting, and never predicts how the mesh turns out")


def what_the_surveyor_may_not_say(node: Any) -> str:
    """The first place `node` names a builder control or predicts how the mesh turns out, or "".

    THE SAME TWO RULES `check_the_survey_block` runs, over any node rather than over a whole survey block, for
    the other thing this platform puts in front of the builder in words of its own: the refusal row
    `cad/regions.py` writes when a stage of the measurement did not run. That row's sentence is the platform's
    and the quote inside it is the measurement's, so both are checked here rather than trusted. The two word
    lists are the package's and are read, never restated (`contract.survey.BUILDER_KEYS` and `OUTCOME_WORDS`).
    """
    pkg = _package()
    keys, words = pkg["survey"].BUILDER_KEYS, pkg["survey"].OUTCOME_WORDS
    for where, found in _contract_breaks(node, keys, words):
        return f"{where or 'its root'} {found}"
    return ""


def _contract_breaks(node: Any, keys, words, path: str = ""):
    """Every place a block carries a builder control or an outcome claim, walked the way the package walks it."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}".lstrip(".")
            if str(key) in keys:
                yield here, f"carries the builder control {key!r}"
            yield from _contract_breaks(value, keys, words, here)
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            yield from _contract_breaks(value, keys, words, f"{path}[{i}]")
    elif isinstance(node, str):
        low = node.lower()
        for word in words:
            if word in low:
                yield path, f"says {word!r}"


def with_the_look_state(block: dict | None, look: str) -> dict | None:
    """The block, with the look's state named in the one channel the block has for it. Raises `SurveyError`.

    `look` is one of `LOOK_STATES`, read by the caller from whatever it has: `look_state(row)` where there is
    a survey row, `look_state_of_document(document)` where there is only the stored measurement. It is a
    string and not a row because the three callers hold three different things and the sentence is the same.

    WHY IT IS AN `unsettled` ROW. `deliver.survey_block` carries `looked`, a boolean, and three of the four
    states it can stand for are False: never taken, still running, and FAILED. A builder reading `looked:
    false` beside `seen: {}` cannot tell "nothing looked" from "the look found nothing", and the product rule
    is that a failed look is never a clear passage. `unsettled` is the block's own channel for what nobody
    settled, named, and a look that did not happen is exactly that, so the state goes there in words rather
    than into a fifth boolean nobody would read.

    IT GOES FIRST and the list keeps its length. `SURVEY_BLOCK_ITEMS` is the package's cap on this list and
    appending past it would break the package's own policy from outside; the look changes how every other row
    in the block reads, so it is the row that keeps its place.

    IT IS PUBLIC, AND THAT IS THE FIX FOR THE PATH EVERY NORMAL JOB TAKES. It used to be private to
    `builder_block`, which is the block the planner gets when the geometry agent produced NO plan. The block a
    planned job gets is `geometry_step.builder_handoff`'s typed block, composed through the same
    `deliver.attach` and handed over without ever passing here. MEASURED over the stored fixtures with the
    reference planner: every planned part carried `looked: false` and not one sentence about the look, while
    the same part on the no-plan path carried one. So a look that FAILED and a look nobody took were the same
    block to the builder on exactly the path almost every job takes. All THREE paths a block reaches the
    builder by go through this function now - `geometry_step.builder_handoff` when the agent planned the job,
    `builder_block` when it did not, and `cad/regions._with_the_look_state` when there is no survey row at all
    - so there is one wording and one place it can go wrong.

    AND IT REFUSES TWO ANSWERS. Where the block already carries the package's own `look_state`, this checks it
    against `look` and raises rather than putting a sentence beside a key that says something else. See
    `PACKAGE_LOOK_STATE` for which of the platform's four states each of the package's three stands for, and
    why `pending` maps onto `not_attempted` rather than being a disagreement.
    """
    if not isinstance(block, dict) or not isinstance(block.get("survey"), dict):
        return block
    said = block["survey"].get("look_state")
    if said is not None and said != PACKAGE_LOOK_STATE.get(look):
        raise SurveyError(f"the measurement package's block says the look was {said!r} and this row says "
                          f"{look!r}: two facts about one look, and the builder cannot tell which to read. "
                          f"The row is composed from the document plus the queue's answer, so this is a row "
                          f"that was not recomposed after the look's status changed")
    because = LOOK_BECAUSE.get(look)
    if because is None:
        return block
    survey = dict(block["survey"])
    rows = [{"about": "look", "subjects": [], "why": because},
            *[r for r in (survey.get("unsettled") or []) if r.get("about") != "look"]]
    survey["unsettled"] = rows[:_package()["deliver"].SURVEY_BLOCK_ITEMS]
    out = {**block, "survey": survey}
    # the whole contract, again, on the block this platform actually hands over. A row that takes it over its
    # ceiling is refused here rather than cut in the prompt, which is the whole point of the rule
    check_the_survey_block(survey)
    return out


# -------------------------------------------------------------------------------------------------
# the gate at submission: a port role the customer did not confirm does not reach port_declaration,
# and neither does a file the customer asked us to send back
# -------------------------------------------------------------------------------------------------

#: WHAT THE CUSTOMER'S ANSWER TO THE DISPATCH QUESTION MEANS, in the package's own word for it
#: (`ask.say.DISPATCH_WORDS`). `send_back` is the branch that says there is no job.
SEND_BACK = "send_back"


def dispatch_refusals(state: dict | None) -> list[str]:
    """Why this file may not be dispatched, in sentences for the model. Empty means it may.

    THE ONE QUESTION WHOSE ANSWER DECIDES WHETHER THERE IS A JOB AT ALL, and until this function nothing read
    it. `ask.intake` ranks it first of every question this product asks (`ask.schema.TIERS`: "the answer
    decides whether the job runs at all; nothing below this matters if it does not"), `ask.say.dispatch` puts
    it in these words - "I do not think this file will get through the mesher as it stands ... I can send it
    back to you now rather than spend the run finding out. Shall I?" - and `record_answer` stores the
    customer's yes as a confirmed claim on `dispatch`. Then the submission went through, the job was
    dispatched, and the run was spent on a file the product had just offered to send back.
    Measured on tests/_fixtures/manifold_short.stl for internal_cfd: `hexera.triage` predicts
    `engines/snappy/drivers.py:540`, which refuses an STL for an internal build before the mesher starts, so
    the question is raised on EVERY STL uploaded for internal flow, and answering it changed nothing.
    The builder's block carried `confirmed: {"dispatch": {"value": "send it back now"}}` while the mesh ran.
    `hexera.triage` states `enforced: False` and `contract.marks.OWNERS` says why: the prediction is a
    measurement of the file against THIS platform's rules and the decision is the customer's. Nothing in the
    package can stop a job, so the platform is where the decision has to be read - here, at the last moment
    before `submit_requirements` authorises anything.

    WHICH BRANCH THEY CHOSE IS READ OFF THE PACKAGE (`ask.say.dispatch_of`) and never off the position of an
    option or a sentence restated on this side, for the reason `_trade_cap` gives about the budget's three
    wordings. An answer this platform cannot map to a branch is refused rather than read as consent: the
    question is "shall I send it back", and anything that is not the option meaning "try it anyway" is not a
    yes to meshing. A package too old to be asked at all is the one case that passes, because then this
    platform cannot tell the two branches apart and inventing a refusal would be a guess of its own.

    A DEFAULT IS NOT A CONFIRMATION HERE EITHER, in the other direction: only the customer's own answer is
    read, so a question they skipped or let default blocks nothing. It rides to the builder as unsettled,
    which is what `_status` and the survey block already do with it.
    """
    if not isinstance(state, dict):
        return []
    views = [v for v in question_views(state) if v.get("about") == "dispatch"]
    if not views:
        return []
    try:
        read = _package()["ask_say"].dispatch_of
    except Exception as exc:                       # noqa: BLE001 - a package that cannot be asked is not a refusal
        logger.warning("geometry survey: the customer's dispatch answer cannot be read in this image (%s), so "
                       "the submission is judged without it", exc)
        return []
    answers = live_answers(state)
    problems: list[str] = []
    for v in views:
        mine = [a for a in answers if a.get("question_id") == v["id"]
                and a.get("answered_by") == CUSTOMER and not a.get("skipped")]
        if not mine:
            continue
        said = mine[-1]
        chose = read(str(said.get("option") or said.get("value") or ""))
        if chose == SEND_BACK:
            problems.append(
                f"the customer answered survey question {v['id']} and asked for this file to be sent back "
                f"rather than meshed: {v['why']}. Do not submit it. Tell them what has to change about the "
                f"file, and submit only if they tell you to run it as it stands")
        elif chose is None:
            problems.append(
                f"the customer's answer to survey question {v['id']} is {said.get('option') or said.get('value')!r}, "
                f"which names neither branch of the question that decides whether this file is meshed at all, so "
                f"it cannot be read as permission to mesh it. Put the question again and record one of "
                f"{v['options']}")
    return problems


def _roled(patches: Any) -> list[dict]:
    return [p for p in (patches or []) if isinstance(p, dict)
            and str(p.get("type") or p.get("role") or "").strip().lower() in ("inlet", "outlet")]


def role_problems(state: dict | None, document: dict | None, patches: Any) -> list[str]:
    """Why these patches may not be submitted, in sentences for the model. Empty means they may.

    `agents/intake/admission_token.py` copies every declared inlet and outlet into `port_declaration`
    and `engines/port_binding.py` binds a boundary condition from each. So this is the last place a
    role can be stopped before it becomes a boundary condition on a real job.

    THE RULE. Where the survey raised a role question, a patch may carry that role only on the mouth
    the CUSTOMER picked for it: bound by position or bore to a measured mouth, and that mouth's
    confirmed role equal to the patch's. An open, skipped or defaulted role question is a refusal,
    and so is a patch that binds to no measured mouth, because a role that cannot be tied to a mouth
    cannot be tied to the customer's answer either. Where the survey raised no role question, the
    customer's brief already named as many ports as the file has mouths, the roles are theirs, and
    the platform's binder matches them as it always has.
    """
    if not isinstance(state, dict) or not _roled(patches):
        return []
    role_views = [v for v in question_views(state) if v["about"] == "opening.role"]
    if not role_views:
        return []
    problems: list[str] = []
    for v in role_views:
        # NAME THE WAY OUT THAT EXISTS, and there are two. A customer who named the roles is recorded with the
        # option they named; a customer who accepted the setup, or handed you the decision, is recorded with
        # the application's OWN proposal (`accepted_proposal`). The refusal used to name only the first, so on
        # every job where the customer said "go" the model's only lever was to ask the roles again - after it
        # had already asked "shall I go?" - which is the round trip this refusal caused most often.
        settle = (f"record it with answer_survey_question: the option they named, or accepted_proposal when "
                  f"they accepted the setup you showed them or left the decision to you (the application then "
                  f"records its own reading, {v.get('default')!r})")
        if v["status"] == "open":
            problems.append(f"the customer has not answered survey question {v['id']} (which of "
                            f"{', '.join(v['subjects'])} is which): {settle}")
        elif v["status"] in ("skipped", "defaulted"):
            problems.append(f"the customer did not confirm survey question {v['id']}, so no port may carry "
                            f"that role: {settle}, or leave the port out")
    if problems:
        return problems
    confirmed = confirmed_roles(state)
    from meshpipeline.agents.intake.geometry_brief import bind_patches
    for patch in _roled(patches):
        role = str(patch.get("type") or patch.get("role") or "").strip().lower()
        bound = bind_patches(document, [patch])
        opening = (bound.get("bound") or [{}])[0].get("opening_id") if bound.get("bound") else None
        name = str(patch.get("name") or "a patch")
        if not opening:
            problems.append(f"{name} ({role}) does not bind to a measured mouth by its position or bore, so "
                            f"its role cannot be checked against what the customer said")
        elif confirmed.get(opening) != role:
            said = confirmed.get(opening)
            problems.append(f"{name} is declared {role} and binds to {opening}, which the customer called "
                            f"{'the ' + said if said else 'nothing'}")
    return problems


# -------------------------------------------------------------------------------------------------
# the rows, and the calls the conversation and the workers make
# -------------------------------------------------------------------------------------------------

def _brief_of(messages: Any) -> str:
    """The customer's own words so far. Their messages only: intake's write-up is intake's, not theirs."""
    said = [str(m.get("content") or "") for m in (messages or [])
            if isinstance(m, dict) and m.get("role") == "user"]
    return "\n\n".join(s for s in said if s.strip())[:BRIEF_MAX_CHARS]


async def load(owner_id: str, source_id: str, *, sha256: str) -> dict | None:
    """The stored survey for this upload, or None. Refuses a row for other bytes. Never raises."""
    try:
        from meshpipeline.persistence.repositories.geometry_survey_repository import (
            GeometrySurveyRepository,
            state_of,
        )
        from meshpipeline.persistence.session import get_db

        async with get_db() as db:
            row = await GeometrySurveyRepository().for_source(
                db, owner_id=owner_id, geometry_source_id=_uuid.UUID(str(source_id)))
        state = state_of(row)
    except Exception as exc:                       # noqa: BLE001
        logger.info("geometry survey: no stored survey for source_id=%s (%s)", source_id, exc)
        return None
    if state is None:
        return None
    if not sha256 or state.get("sha256") != str(sha256).lower():
        logger.warning("geometry survey: the stored survey describes other bytes - source_id=%s", source_id)
        return None
    return state


async def save(owner_id: str, source_id: str, state: dict, *, session_id: str = "") -> bool:
    try:
        from meshpipeline.persistence.repositories.geometry_survey_repository import (
            GeometrySurveyRepository,
        )
        from meshpipeline.persistence.session import get_db

        async with get_db() as db:
            await GeometrySurveyRepository().record(
                db, owner_id=owner_id, geometry_source_id=_uuid.UUID(str(source_id)),
                sha256=str(state.get("sha256") or ""), state=state,
                session_id=_uuid.UUID(session_id) if session_id else None)
            await db.commit()
        return True
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry survey: the row could not be written - source_id=%s: %s", source_id, exc)
        return False


async def _interpretation(owner_id: str, source_id: str) -> tuple[str | None, float | None, str | None]:
    """The unit THIS PLATFORM recorded for the bytes, or nothing. The same authority the measurement
    uses: a scale is never taken from a model or from this module."""
    try:
        from meshpipeline.application.geometry_measurement import _interpretation_for
        from meshpipeline.persistence.session import get_db

        async with get_db() as db:
            unit, scale = await _interpretation_for(db, owner_id, _uuid.UUID(str(source_id)))
        return unit, scale, ("user_confirmed" if unit else None)
    except Exception:                              # noqa: BLE001
        return None, None, None


async def survey_the_part(*, owner_id: str, session_id: str, source_ref, document: dict,
                          purpose: str, messages: Any, declared: list[dict] | None = None,
                          engine: str | None = None) -> dict:
    """Steps 1 to 4 on the platform, at the moment intake has heard what the part is for.

    Composes the survey for the customer's purpose, their words so far and any ports they already
    named; carries across every answer the new survey still has a question for; stores it; queues the
    look with the purpose and representation that decided (step 3) when it has not been taken; and
    returns the state with the step-4 questions marked put. Raises `SurveyError` for the model.
    """
    if purpose not in SURVEYED_PURPOSES:
        raise SurveyError(f"the Surveyor composes for flow purposes only ({', '.join(SURVEYED_PURPOSES)}); "
                          f"for this one carry on as usual")
    unit, scale, basis = await _interpretation(owner_id, source_ref.source_id)
    old = await load(owner_id, source_ref.source_id, sha256=source_ref.sha256)
    # EVERYTHING THE CUSTOMER HAS ALREADY SETTLED, and it used to be the budget alone. Intake calls this
    # function again whenever the customer says something that moves the purpose or names a port, and the
    # composition it made was the one from before they had answered anything but the trade: the side they
    # confirmed was dropped and could not be read back, because the answer is retired with the question it
    # settled and `composed_for` is then the only place it exists (`confirmed_inputs`). On block_boss_sharp
    # that took a confirmed `wall_shell` - carve the passage out of the solid - back to `annular_fluid`, the
    # file itself read as the flow, permanently. The meshed domain, inverted, by a second call.
    fresh = compose(document, purpose=purpose, brief=_brief_of(messages), declared=declared,
                    engine=engine if engine in PACKAGE_ENGINES else None, unit=unit,
                    scale_to_metres=scale, unit_basis=basis, **confirmed_inputs(old))
    if fresh["sha256"] != str(source_ref.sha256):
        raise SurveyError("the stored measurement describes other bytes than this session's upload")
    state = carry_answers(old, fresh)
    state = mark_asked(state, open_now(state))
    # STEP 3 IS QUEUED AND ITS ANSWER IS KEPT. The look is a worker and the planner can reach this row before
    # it lands, so the row records that one is on the way; without it the builder cannot tell a survey waiting
    # for eyes from one that will never have any (`look_state`, and audit item 15)
    state = _noted_queue(state, _queue_the_look(source_ref.source_id, owner_id, document))
    await save(owner_id, source_ref.source_id, state, session_id=session_id)
    return state


#: The purposes whose survey is ABOUT FLOW. Every question about a mouth's role, the way the flow comes at
#: the body and which side of the surface is the fluid means something for these three and nothing for a
#: bracket under load, so the distinction is a tuple rather than a comment: `_without_the_flow_questions`
#: reads it, and the day a fifth purpose is added it lands in one of these two lists by hand and not by
#: default.
FLOW_PURPOSES: tuple[str, ...] = ("internal_cfd", "external_cfd", "conjugate_heat_transfer")

#: The purposes the Surveyor composes for, which is now EVERY purpose this platform has
#: (`engines/purposes.PURPOSES`) and every purpose the package's own vocabulary has
#: (`facts.schema.Purpose`). There is no separate thermal purpose: thermal IS
#: `conjugate_heat_transfer`, which was surveyed already, so `structural` is the whole of what was
#: missing.
#:
#: IT USED TO BE THE THREE FLOW PURPOSES AND THE REASON WRITTEN HERE WAS STALE. It said a structural
#: purpose "would be asked which mouth is the inlet (`contract.asking.ROLES_NEEDED` defaults to inlet and
#: outlet for any purpose it does not name)". `ROLES_NEEDED.get(purpose, ())` returns the EMPTY tuple for a
#: purpose it does not name and `contract.asking._role_uncertainties` then raises nothing - and in any case
#: `contract.asking.uncertainties_from` is not the finder on this path and must not be (`QUESTION_FINDER`).
#: The live finder is `ask.intake.ask_intake`, and the role question it puts comes from
#: `ask.uncertainty.port_role_uncertainties`, which gates on the mouths THE BUILDER WOULD CUT
#: (`ask.uncertainty.builder_ports`) and not on the purpose at all. So the gate's conclusion was right and
#: its citation named a module that cannot cause it: MEASURED over the six stored fixtures composed for
#: `structural`, `ask_intake` puts `q_port_roles` on every one of them, in the words "Which of these
#: carries the incoming flow, and which are outlets?", on bend_elbow_001 and on a solid block alike.
#:
#: WHAT GENERALISES AND WHAT DOES NOT, measured on those six fixtures rather than argued. What a structural
#: job gets from this change: the look runs at all (nothing queued one for a purpose the Surveyor did not
#: compose for, so a structural part was never seen); the openings and their sizes, the thin clusters, the
#: sharp and knife edges, the symmetry planes and the thickness, which are fields of `GeometryFacts` and
#: carry no flow in them; the unit question, which is the 1,000x error and belongs to no purpose; and the
#: survey block the builder reads, composed for the purpose the customer actually said instead of the
#: `internal_cfd` the upload assumed (`geometry_measurement.DEFAULT_PURPOSE`).
#:
#: What does NOT generalise, and is cut rather than carried: `FLOW_ONLY_QUESTION_KINDS`. And what this
#: module cannot fix from here, stated so it is not mistaken for something that works: `report_measured`
#: composes `eng = engine or "snappy"` (`hexera.py:3154`) whatever the purpose, although
#: `catalog.default_engine` answers `gmsh` for `structural`, so a structural composition nobody named an
#: engine for carries `engine: snappy`, `engine_source: assumed`, and a `representation` out of a table
#: with no structural reading in it. Passing `gmsh` from here would be worse, not better: the document
#: records a caller's engine as `engine_source: "declared"`, and a default nobody confirmed recorded as a
#: declaration is the second house law. So the forecast and the triage stay a CFD engine's, and the two
#: questions that rest on them are the two this module refuses to put for a structural job.
SURVEYED_PURPOSES: tuple[str, ...] = (*FLOW_PURPOSES, "structural")

#: The question kinds that mean nothing outside `FLOW_PURPOSES` WHATEVER ELSE IS TRUE, in `ask.schema.KINDS`'
#: own words, with the reason each one goes. `_without_the_flow_questions` drops them from the finder's
#: questions AND from the survey's uncertainty list, so a structural job is neither asked them nor handed
#: them as unsettled.
#:
#:   `port_role`         its branches are inlet, outlet, wall and closed-for-this-run, and a structural
#:                       contract's roles are fixed, load, contact and free (`hexera.PURPOSE_ROLES`). The
#:                       measurement measures no restraint and the look reads none, so there is no honest
#:                       question here to re-word - only a wrong one to stop putting. MEASURED: this is the
#:                       one the finder puts on all six stored fixtures composed for `structural`, in the
#:                       words "Which of these carries the incoming flow, and which are outlets?".
#:   `flow_direction`    `ask.uncertainty.flow_direction_uncertainty` returns nothing unless the
#:                       representation is `external`, which only `external_cfd` produces
#:                       (`catalog._read_representation`), so this cannot fire today. It is named anyway:
#:                       the list says which questions MAY NOT be put for a non-flow purpose, and a rule
#:                       that only lists what currently happens to fire is a rule that stops being true.
#:   `fluid_side`        same: `catalog.side_reading` returns empty `readings` for any purpose outside
#:                       `INTERNAL_PURPOSES`, so `fluid_side.open` is False and the question cannot fire.
#:                       Which side of a surface is the fluid is not a question about a solid body.
FLOW_ONLY_QUESTION_KINDS: frozenset[str] = frozenset({"port_role", "flow_direction", "fluid_side"})

#: The question kinds whose SUBJECT generalises to any purpose and whose EVIDENCE is one particular mesher's.
#: They are cut when the composition's engine cannot produce the mesh this purpose needs (`engine_can_mesh`),
#: and kept when it can - so the cut is about the ENGINE and not about the purpose, because that is the true
#: reason and a rule stated as "structural has no budget question" would be false the moment the customer
#: confirms the engine their structural job actually runs on.
#:
#:   `budget_envelope`   a cell budget is a cell budget for any mesher; the number it is compared against is
#:                       not. `hexera.forecast` calls `tools.estimate_builder_cells`, which branches on the
#:                       engine: `_estimate_gmsh` reads the solid volume and the bbox diagonal off
#:                       `engines/gmsh/driver.py`'s own sizing, and everything else is `_estimate_internal`,
#:                       the snappy emulator that sizes off an inlet bore and counts a near band, layers and
#:                       a FLUID VOLUME (`engines/snappy/snappy_runner.py`). MEASURED on
#:                       transition_007_fluid composed for `structural`: with the engine nobody named
#:                       (snappy) the forecast is 2,046,473 cells and `over_cap` is True against the
#:                       customer's 2M, and with `gmsh` - the only engine this platform will run a
#:                       structural job on - it is 60,891 and fits. That trade would have asked a customer
#:                       to raise their budget over a figure 34x too big for the mesher that will run.
#:   `dispatch_refusal`  `hexera.triage` predicts named platform raises for a (purpose, engine) PAIR. A
#:                       refusal predicted for a pair this platform will not dispatch at all is not a
#:                       prediction about this job - and its answer is the one that decides whether there is
#:                       a job (`dispatch_refusals`, `ask.schema.TIERS`' top tier), so getting it from the
#:                       wrong engine is the most expensive place to be wrong.
ENGINE_DEPENDENT_QUESTION_KINDS: frozenset[str] = frozenset({"budget_envelope", "dispatch_refusal"})

#: NOT CUT for any purpose, and each for its own reason: `unit`, which belongs to no purpose and is the
#: 1,000x error; `unlabelled_mouth`, which is the look reporting an OPENING the measurement did not find and
#: says nothing about what flows through it; `look_dispute`, which is two readings of one place disagreeing
#: and is about the place, not the flow; `opening_unseen` and `identity`, which are recorded and advisory and
#: were never asked for any purpose.
#:
#: IT IS NOT A COMMENT, IT IS THE HALF OF THE CUT A READER CANNOT CHECK. These three sets must cover
#: `ask.schema.KINDS` exactly, and `_without_the_flow_questions` checks that they do before it cuts anything.
#: The failure mode of a cut written as "drop these five kinds" is the day the package adds a SIXTH: it is
#: carried onto a structural survey by default, nobody decided that it should be, and the cut sitting in the
#: path makes it look decided. A kind nobody has classified is a kind this platform cannot say is honest for
#: a purpose that is not about flow, so a non-flow job gets no survey until somebody puts it in one of these
#: two lists - which is exactly where that job was before any of this existed.
KEPT_QUESTION_KINDS: frozenset[str] = frozenset({
    "unit", "unlabelled_mouth", "look_dispute", "opening_unseen", "identity"})


def engine_can_mesh(purpose: str, engine: str) -> bool:
    """Can this engine produce the mesh this purpose needs, by THIS PLATFORM's own compatibility gate?

    `engines/purposes.is_compatible` over `engines/registry.get_spec`, which is the same pair
    `agents/intake/validation.py` refuses a submission with, so the Surveyor and the submission gate cannot
    disagree about which engine a job runs on. It is here rather than restated because the answer decides
    whether two of the survey's questions rest on the right mesher's numbers
    (`ENGINE_DEPENDENT_QUESTION_KINDS`) and whether the third intake has a trade to price
    (`geometry_step._trade_is_priced_on_this_job`).

    FALSE ON ANYTHING IT CANNOT READ, which is the careful direction: an unknown engine name or a registry
    that will not answer means this platform cannot say the forecast is the right mesher's, and a question
    not put is worth more than a question priced on another engine's mesh. Never raises.
    """
    try:
        from meshpipeline.engines import registry, purposes as engine_purposes

        if purpose not in engine_purposes.PURPOSES or not engine:
            return False
        return bool(engine_purposes.is_compatible(registry.get_spec(str(engine)), str(purpose)))
    except Exception as exc:                       # noqa: BLE001 - see the docstring
        logger.info("geometry survey: cannot tell whether %r meshes %r (%s), so the questions that rest on "
                    "the forecast are not put", engine, purpose, exc)
        return False

#: The engines the package's forecast knows by these names. Any other confirmed engine is left out of
#: the composition rather than passed as a word the forecast would read as its default.
PACKAGE_ENGINES = ("snappy", "cfmesh", "gmsh")


async def for_submission(*, owner_id: str, session_id: str, source_ref, document: dict,
                         purpose: str, messages: Any, state: dict | None,
                         engine: str | None = None) -> dict | None:
    """The survey the submission is checked against. Composed here when intake never called
    `survey_the_part`, or when the purpose it was composed for has moved.

    THE PATCHES ARE NEVER THE DECLARATION. They are what the intake model wrote, and the package reads
    a declared port as the customer's `stated` claim: where the declared count matches the mouths it
    asks nothing, so composing with the patches as the declaration let a model's own inlet and outlet
    through the gate with no question put and nothing confirmed (bend_elbow_001, once the flange
    shoulders stopped counting as mouths). The ports declared here are only the ones `survey_the_part`
    was given as the customer's own words; where it never ran there are none, the Surveyor raises its
    role questions, and the patches are checked against the answers. Raises `SurveyError`; the caller
    fails open.
    """
    if purpose not in SURVEYED_PURPOSES:
        return None
    before = dict((state or {}).get("composed_for") or {})
    stale = state is None or before.get("purpose") != purpose
    if not stale:
        return state
    unit, scale, basis = await _interpretation(owner_id, source_ref.source_id)
    fresh = compose(document, purpose=purpose, brief=_brief_of(messages),
                    declared=[dict(p) for p in _roled(before.get("declared"))] or None,
                    engine=engine if engine in PACKAGE_ENGINES else None, unit=unit,
                    scale_to_metres=scale, unit_basis=basis, **confirmed_inputs(state))
    out = mark_asked(carry_answers(state, fresh), [])
    # the look has not been taken if intake never surveyed; take it now, for this purpose, so the
    # builder still gets it when it lands before the planner runs, and keep the queue's answer so a plan
    # made before it lands says which of the four look states it was made in
    out = _noted_queue(out, _queue_the_look(source_ref.source_id, owner_id, document))
    await save(owner_id, source_ref.source_id, out, session_id=session_id)
    return out


def look_state(state: dict | None) -> str:
    """One of `LOOK_OK`, `LOOK_PENDING`, `LOOK_FAILED`, `LOOK_NONE` for the row's own composition.

    THE STORED DOCUMENT CANNOT TELL PENDING FROM NEVER. Before the worker writes anything the document
    carries no `look` at all, which reads as `not_attempted` whether one was queued a second ago or never
    queued at all. So the queue's own answer is kept on the row (`look_queued`) and read here: queued and
    nothing written is PENDING, and pending is the state the race produces.
    """
    status = str(((state or {}).get("composed_for") or {}).get("look_status") or LOOK_NONE)
    if status == LOOK_OK:
        return LOOK_OK
    if status == LOOK_FAILED:
        return LOOK_FAILED
    if str((state or {}).get("look_queued") or "") in LOOK_ON_ITS_WAY or status == LOOK_PENDING:
        return LOOK_PENDING
    if status != LOOK_NONE:
        # AN UNKNOWN STORED STATUS IS READ AS A LOOK THAT FAILED, which is the careful direction and the
        # direction the package reads it (`hexera._look_state`). It replaces branches for `"refused"` and
        # `"error"`, two values NOTHING HAS EVER WRITTEN: `hexera.look_block` raises on any value outside
        # `LOOK_STATES` and is the package's only composer of a look block, the shipped wheel writes only `ok`
        # and `failed` from `vision/look.py` (its `refused` occurrences are MEASUREMENT statuses in `facts/`),
        # and `git log -S` over the whole agent history finds neither as a look status in any version. A branch
        # for a specific value nothing writes is worth less than a careful reading of every value nothing
        # writes, and reading a stored status this side does not know as "nobody looked" was the one direction
        # that turns a look into silence.
        return LOOK_FAILED
    return LOOK_NONE


def look_state_of_document(document: dict | None) -> str:
    """What the STORED MEASUREMENT alone says happened to the look: `LOOK_OK`, `LOOK_FAILED` or `LOOK_NONE`.

    NEVER `LOOK_PENDING`, and that is the honest answer rather than a missing case. The queue's own answer
    lives on the survey ROW (`look_queued`), so with no row there is nothing that can tell a look on its way
    from one never taken, and this does not claim to. It is read by the one path that has a document and no
    row: `cad/regions.agent_block_for_state`, when no survey was stored for these bytes and the builder is
    handed the measurement's own block.
    """
    look = (document or {}).get("look")
    status = str((look or {}).get("status") or "") if isinstance(look, dict) else ""
    if status == LOOK_OK:
        return LOOK_OK
    # NO LOOK BLOCK AND NO STATUS IN IT IS THE ONE THING THAT MEANS NOBODY LOOKED. Anything else that is not
    # `ok` and not the package's own `not_attempted` is read as a look that FAILED, for the reason `look_state`
    # gives above: the same careful direction the package reads it, in place of branches for two values
    # (`"refused"`, `"error"`) that no version of the package has ever written.
    if not status or status == LOOK_NONE:
        return LOOK_NONE
    return LOOK_FAILED


def _noted_queue(state: dict, outcome: str) -> dict:
    """The queue's answer on the row, so `look_state` can tell pending from never attempted."""
    out = {**state, "look_queued": str(outcome or "")}
    out["stage"] = stage_of(out)
    return out


def _the_queue_has_been_answered(state: dict) -> dict:
    """The row with `look_queued` dropped, for the one caller that knows a worker has WRITTEN.

    WHY THIS EXISTS, and it is the same collapse from the other side. `look_queued` is the only thing that can
    tell a look on its way from one nobody took, because the document reads as `not_attempted` in both cases,
    and `carry_answers` deliberately carries it across a recomposition so a recomposition for some OTHER reason
    (the purpose moved, ports were declared) does not lose a look in flight. But the look landing is itself a
    recomposition, and the queue's answer was carried across that one too.

    MEASURED in the built `api` image on a real part, in the configuration a deployment with no vision key
    runs: the worker writes `look_block(None, status="not_attempted", reason="the configured reader (openai,
    gpt-5.6-luna) has no key in this environment, so nothing looked")`, the row is recomposed for it, and
    `look_state` went on answering `pending` for good. So the builder was told "the render worker was queued
    and has not written its reading" about a reading that had arrived and said nobody looked. A look that will
    never come back, reported as one still coming, on every upload in that deployment.

    A FAILED look never had this, because `look_state` reads `failed` before it reads the queue. `ok` likewise.
    It is only `not_attempted` the queue's answer could overrule, which is exactly the state a missing reader
    produces, which is the state this platform is in until a key is set.
    """
    if "look_queued" not in state:
        return state
    return {k: v for k, v in state.items() if k != "look_queued"}


def _queue_the_look(source_id: str, owner_id: str, document: dict) -> str:
    """Step 3, queued at the step it belongs to. Never raises."""
    try:
        stored_look = document.get("look")
        look = stored_look if isinstance(stored_look, dict) else {}
        if look.get("status") == LOOK_OK:
            return LOOK_CACHED
        from meshpipeline.contracts.geometry_measurement import enqueue_look
        return LOOK_QUEUED if enqueue_look(str(source_id), owner_id) else LOOK_SKIPPED
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry survey: the look could not be queued - source_id=%s: %s", source_id, exc)
        return LOOK_SKIPPED


async def answer(*, owner_id: str, source_ref, question_id: str, choice: str = "", role: str = "",
                 words: str = "", latest_user_message: str = "", skipped: bool = False,
                 took_default: bool = False, document: dict | None = None, subject: str = "",
                 earlier_customer_messages: Any = None, accepted_proposal: bool = False) -> dict:
    """Record one answer and store it. When it confirms a budget, the planner block is composed again
    for that budget, so `customer_cell_cap` is the number the customer chose; when it names the inlet,
    it is composed again from that inlet, so the block's bore and the budget trade that follows are the
    customer's inlet's and not the widest mouth's. Raises `SurveyError`."""
    state = await load(owner_id, source_ref.source_id, sha256=source_ref.sha256)
    if state is None:
        raise SurveyError("there is no survey for this upload yet: call survey_the_part first")
    state = answered(state, document, question_id=question_id, choice=choice, role=role, words=words,
                     latest_user_message=latest_user_message, principal=owner_id,
                     skipped=skipped, took_default=took_default, subject=subject,
                     earlier_customer_messages=earlier_customer_messages,
                     accepted_proposal=accepted_proposal)
    await save(owner_id, source_ref.source_id, state)
    return state


def answered(state: dict, document: dict | None = None, *, question_id: str, choice: str = "",
             role: str = "", words: str = "", latest_user_message: str = "", principal: str = "",
             skipped: bool = False, took_default: bool = False, subject: str = "",
             earlier_customer_messages: Any = None, accepted_proposal: bool = False) -> dict:
    """One answer recorded into the row, with everything that follows from it. No database.

    `answer` is this plus the load and the store. It is a function of its own so that a caller which
    already holds the row - a test, the corpus harness that runs this chain on real parts - takes the
    same path a customer's answer takes, rather than a second spelling of it that can drift from this
    one. Raises `SurveyError`."""
    state = record_answer(state, question_id=question_id, choice=choice, role=role, words=words,
                          latest_user_message=latest_user_message, principal=principal,
                          skipped=skipped, took_default=took_default, subject=subject,
                          earlier_customer_messages=earlier_customer_messages,
                          accepted_proposal=accepted_proposal)
    late = late_view(state)
    if late is not None and late["id"] == question_id:
        # the third intake's answer goes to the job ledger as the `trade` stage, the way the chain writes it
        from meshpipeline.application import geometry_step
        state = geometry_step.note_late_answer(state)
    cap = confirmed_cell_cap(state)
    before = state.get("composed_for") or {}
    new_cap = cap is not None and before.get("cell_cap") != cap
    new_inlet = bool(named_inlets(state)) and named_inlets(state) != sorted(before.get("inlet_ids") or [])
    # A CONFIRMED SIDE IS A NEW COMPOSITION, and before this line it was not. The answer went onto the row,
    # the row's `representation` stayed the one measured before anybody had answered, and the geometry
    # agent's own check then refused the plan for contradicting the survey: on `F0_block_sharp`, a solid
    # block with one bore, answering the question turned a planned job into a refused one. Measured here,
    # not reasoned about.
    said_side = confirmed_representation(state)
    new_side = bool(said_side) and before.get("confirmed_representation") != said_side
    # AND SO IS A CONFIRMED FLOW DIRECTION, for the same reason one line up. The answer decides whether an
    # external body's downstream flat face is named its base in the block the builder reads
    # (`composition`'s `measured`), so an answer that is not composed back in is an answer that changes
    # nothing: on ahmed_variant_001_external_looked the block's one place stayed `flat_end` after the
    # customer had said the flow runs along +x, and the wake was refined as an anonymous flat end.
    said_flow = confirmed_flow_direction(state)
    new_flow = bool(said_flow) and before.get("flow_direction") != said_flow
    if (new_cap or new_inlet or new_side or new_flow) and isinstance(document, dict):
        try:
            state = recomposed(state, document, **({"cell_cap": cap} if cap is not None else {}))
        except SurveyError as exc:
            logger.warning("geometry survey: the confirmed answer could not be composed in (%s)", exc)
    return mark_asked(state, open_now(state))


async def recompose_after_look(source_id: str, owner_id: str, document: dict) -> str:
    """The look landed: compose the survey again with it, keeping every answer still bound. Never raises.

    AND THE QUEUE'S ANSWER IS SPENT HERE, which is the whole of `_the_queue_has_been_answered`. This is the
    one caller that knows a worker has WRITTEN, so it is the one place that can take the row out of `pending`.
    """
    try:
        sha = str(((document.get("source") or {}).get("sha256")) or document.get("source_sha256") or "")
        state = await load(owner_id, source_id, sha256=sha)
        if state is None:
            return "no_survey"
        fresh = _the_queue_has_been_answered(recomposed(state, document))
        await save(owner_id, source_id, fresh)
        return "recomposed"
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry survey: could not be recomposed after the look - source_id=%s: %s",
                       source_id, exc)
        return "skipped"


__all__ = ["ASKING_SCHEMA", "CHAIN", "CUSTOMER", "DEFAULT_TAKEN", "LATE_STAGE", "LATE_TIER",
           "PACKAGE_LOOK_STATE", "SEND_BACK", "block_list_max", "dispatch_refusals",
           "LOOK_BECAUSE", "LOOK_CACHED", "LOOK_FAILED", "LOOK_NONE", "LOOK_OK", "LOOK_ON_ITS_WAY",
           "LOOK_PENDING", "LOOK_QUEUED", "LOOK_SKIPPED", "LOOK_STATES", "NO_PLAN_NO_REASON",
           "QUESTION_FINDER", "ROUTE_ADVISORY", "ROUTE_APPLICATION",
           "ROUTE_INTAKE", "ROUTE_LATE", "ROUTE_TRADE", "STAGE_ASKING", "STAGE_LATE", "STAGE_SETTLED",
           "STAGE_SURVEYED", "STAGE_TRADE", "SUPERSEDED", "SURVEY_STATE_SCHEMA", "SurveyError",
           "answer", "answered",
           "asking_of", "asking_row", "builder_block", "carry_answers",
           "check_the_survey_block", "compose",
           "composed_inputs", "composition", "confirmed_cell_cap", "confirmed_flow_direction",
           "confirmed_inputs", "confirmed_representation",
           "confirmed_roles", "intake_handoff", "late_view",
           "live_answers", "load", "look_state", "look_state_of_document", "mark_asked", "named_inlets",
           "open_now",
           "question_views", "recompose_after_look", "recomposed", "record_answer", "role_problems",
           "said_by_customer", "save", "stage_of", "survey_the_part", "what_the_surveyor_may_not_say",
           "with_no_plan_if_the_step_failed", "with_the_look_state"]
