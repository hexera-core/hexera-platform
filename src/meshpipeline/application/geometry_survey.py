# Responsibility: Run the Surveyor's half of the chain on the platform: compose the survey for what the customer said, put its questions, keep their answers.
# Owns: the chain order, the survey row's contents, the check that an answer is the customer's, and the port-role gate at submission.
# Boundaries: every geometry number and every question is the measurement package's; this module computes none of them.
# Collaborates with: application/geometry_measurement.py for the row it reads, agents/intake for the conversation, cad/regions.py for the builder's block.
from __future__ import annotations

import hashlib
import logging
import re
import uuid as _uuid
from datetime import UTC, datetime
from typing import Any

import meshpipeline.settings.policy as polcfg

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
#   Step 5 is not on this platform. The agent's reasoning pass is not called here; the builder's own
#   planner decides what to do, and it is handed the survey as facts it may not re-decide.
#
#   Step 6 is the budget trade, released only when every step-4 question is settled, and put once.
#
#   Step 7 is `cad.regions.agent_block_for_state`, which puts `survey` into the block the planner
#   serialises after `request_txt[:2000]`.
CHAIN: tuple[tuple[str, str], ...] = (
    ("intake", "the customer states purpose, budget and boundary conditions"),
    ("measure", "the stored measurement is composed for what they said"),
    ("look", "the look is taken with the purpose and representation that decided"),
    ("intake", "the Surveyor's questions are put and the answers stored with who gave them"),
    ("geometry", "the builder's planner decides what to do from facts it may not re-decide"),
    ("intake", "the budget trade, and only it, once the step-4 questions are settled"),
    ("builder", "the survey rides in the typed block after the request cut"),
)

#: The row's own name, written into every state this module stores.
SURVEY_STATE_SCHEMA = "meshpipeline.geometry_survey.v1"

#: Stages a row moves through. `surveyed`: composed, nothing put yet. `asking`: step-4 questions put.
#: `trade`: the budget trade put. `settled`: nothing open that anybody will be asked.
STAGE_SURVEYED, STAGE_ASKING, STAGE_TRADE, STAGE_SETTLED = "surveyed", "asking", "trade", "settled"

#: Who an answer came from. The CUSTOMER, and never their account id: the `source` of a confirmed
#: claim rides into the planner's prompt, and a provider has no business reading who the customer
#: is. The principal is stored beside the answer in this platform's own row instead.
CUSTOMER = "customer"
#: The package's own word for a default nobody typed. It becomes an `assumed` claim, and on a role or
#: a budget the package refuses it outright, which leaves the question open. That is the point.
DEFAULT_TAKEN = "default_taken"

#: How a question is routed. `intake` is step 4. `trade` is step 6. `application` is a question this
#: platform already asks in its own words and owns the answer to: the unit, through
#: `agents/intake/unit_clarification.py` and the interpretation it records. `advisory` is reported
#: and never put, because its answer changes nothing.
ROUTE_INTAKE, ROUTE_TRADE, ROUTE_APPLICATION, ROUTE_ADVISORY = "intake", "trade", "application", "advisory"

#: The customer's own words, kept for recomposition. Bounded, because a pasted spec sheet is not a
#: brief and the budget and the carve sentence are always near the top of one.
BRIEF_MAX_CHARS = 8000


class SurveyError(RuntimeError):
    """A call this module refuses, with a sentence the model can act on. Never reaches a customer."""


def survey_enabled() -> bool:
    """All three gates, read together every time, so a test that sets one cannot arm the chain."""
    return bool(polcfg.GEOMETRY_MEASUREMENT_ENABLED and polcfg.GEOMETRY_REPORT_READERS_ENABLED
                and polcfg.GEOMETRY_SURVEY_ENABLED)


# -------------------------------------------------------------------------------------------------
# the package, imported the first time something asks
# -------------------------------------------------------------------------------------------------

def _package():
    from geometry_agent.agent import hexera
    from geometry_agent.contract import asking, build, deliver, given, intake, marks, survey
    from geometry_agent.facts.schema import GeometryFacts
    return {"hexera": hexera, "asking": asking, "build": build, "deliver": deliver, "given": given,
            "intake": intake, "marks": marks, "survey": survey, "GeometryFacts": GeometryFacts}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def brief_digest(brief: str | None) -> str:
    return hashlib.sha256(str(brief or "").encode("utf-8")).hexdigest()[:16]


# -------------------------------------------------------------------------------------------------
# STEP 2: the measurement, composed for what the customer said
# -------------------------------------------------------------------------------------------------

def _for_survey(composed: dict, engine: str | None) -> dict:
    """The composed document as `contract.build.survey_from` can take it today.

    ONE SHIM, and it is written down rather than hidden. `report_measured` records an engine nobody
    declared as `engine_source: "assumed"`, and `survey_from` turns that into an `assumed` mark on
    `engine`, whose owners are `confirmed` and `stated` only, so the whole survey raises
    `ContractError` on every document that did not name an engine. That is every upload. Nobody has
    stated an engine at that point, and the honest survey carries NO engine mark rather than an
    assumed one the contract refuses, so the two keys are left out when the customer named none. The
    fix belongs in `contract/build.py` and is in this branch's NEEDS FROM OTHERS; when it lands this
    leaves a document the package already accepts.
    """
    if engine:
        return composed
    return {k: v for k, v in composed.items() if k not in ("engine", "engine_source")}


def compose(document: dict, *, purpose: str, brief: str | None = None,
            declared: list[dict] | None = None, engine: str | None = None,
            unit: str | None = None, scale_to_metres: float | None = None,
            unit_basis: str | None = None, cell_cap: int | None = None) -> dict:
    """Step 2: the stored measurement composed for this purpose, these words and these ports.

    Nothing is measured again. `document["facts"]` is the instrument's reading of the bytes, stored
    at upload, and `hexera.report_measured` over it is the package's own composition: the
    representation, the forecast, the opening table and the planner block all come from there. The
    survey is `contract.build.survey_from` over that composition. Raises `SurveyError` when the
    document cannot be composed, which the callers turn into "no survey" and a conversation that runs
    exactly as it does with the survey off.

    `cell_cap` is a budget the customer CONFIRMED in the trade. Without one, the budget is the one
    they wrote, read by the package's `budget_from_brief`, and it rides as `stated`.
    """
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
    cap = cell_cap if cell_cap is not None else stated_cap
    ports = [dict(p) for p in (declared or []) if isinstance(p, dict)]
    try:
        composed = pkg["hexera"].report_measured(
            facts, unit, brief_text or None, purpose=purpose, engine=engine or None,
            declared=ports or None, cell_cap=cap, look=document.get("look"),
            scale_to_metres=scale_to_metres, unit_basis=unit_basis,
            stamp={k: v for k, v in (document.get("stamp") or {}).items()
                   if k in ("agent_git_sha", "platform_sha")})
        survey = pkg["build"].survey_from(_for_survey(composed, engine), brief=brief_text or None,
                                          declared=ports or None)
    except pkg["marks"].ContractError as exc:
        raise SurveyError(f"the survey refused its own handoff: {exc}") from exc
    except Exception as exc:                       # noqa: BLE001 - never a turn, never a mesh
        raise SurveyError(f"the survey could not be composed: {type(exc).__name__}: {exc}") from exc
    look = document.get("look") if isinstance(document.get("look"), dict) else {}
    return {
        "schema": SURVEY_STATE_SCHEMA,
        "sha256": str(survey.source_sha256),
        "facts_sha256": str(survey.facts_sha256),
        "survey": survey.model_dump(mode="json"),
        "planner_block": composed.get("planner_block"),
        "composed_for": {
            "purpose": purpose, "engine": engine or "", "declared": ports,
            "unit": unit or "", "unit_basis": unit_basis or "", "scale_to_metres": scale_to_metres,
            "brief": brief_text, "brief_sha": brief_digest(brief_text),
            "cell_cap": cap, "cell_cap_kind": "confirmed" if cell_cap is not None else (
                "stated" if stated_cap is not None else ""),
            "representation": composed.get("representation"),
            "look_status": str(look.get("status") or "not_attempted"),
            "composed_at": _now(),
        },
        "agent_git_sha": str((document.get("stamp") or {}).get("agent_git_sha") or ""),
    }


def recomposed(state: dict, document: dict, **changes: Any) -> dict:
    """The same survey composed again for the same customer with something new: the look landed, the
    purpose moved, the ports were declared, a budget was confirmed. Answers and `asked` are carried
    across for every question id the new survey still raises, and dropped for the rest, because an
    answer is bound to a question and a question that no longer exists binds nothing."""
    before = dict(state.get("composed_for") or {})
    kwargs = {"purpose": before.get("purpose") or "internal_cfd", "brief": before.get("brief") or "",
              "declared": before.get("declared") or None, "engine": before.get("engine") or None,
              "unit": before.get("unit") or None, "scale_to_metres": before.get("scale_to_metres"),
              "unit_basis": before.get("unit_basis") or None,
              "cell_cap": before.get("cell_cap") if before.get("cell_cap_kind") == "confirmed" else None}
    kwargs.update(changes)
    fresh = compose(document, **kwargs)
    return carry_answers(state, fresh)


def carry_answers(old: dict | None, fresh: dict) -> dict:
    if not old:
        return {**fresh, "asked": [], "answers": [], "stage": STAGE_SURVEYED}
    if old.get("sha256") and old.get("sha256") != fresh.get("sha256"):
        # a survey for other bytes. Nothing it was told is about this file.
        return {**fresh, "asked": [], "answers": [], "stage": STAGE_SURVEYED}
    ids = {q["id"] for q in question_views(fresh)}
    out = {**fresh,
           "asked": [q for q in (old.get("asked") or []) if q in ids],
           "answers": [a for a in (old.get("answers") or []) if a.get("question_id") in ids],
           "dropped_answers": [a for a in (old.get("answers") or []) if a.get("question_id") not in ids]
           + list(old.get("dropped_answers") or [])}
    out["stage"] = stage_of(out)
    return out


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


def question_views(state: dict) -> list[dict]:
    """Every question the survey raises, as `contract.asking.questions_from` renders it, with its
    route and whether it is settled. Advisory ones included, so the list is the uncertainty list one
    for one and nothing is silently filtered."""
    pkg = _package()
    try:
        questions = pkg["asking"].questions_from(survey_of(state), include_advisory=True)
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry survey: the stored survey could not be read (%s)", exc)
        return []
    answers = [a for a in (state.get("answers") or []) if isinstance(a, dict)]
    out = []
    for q in questions:
        mine = [a for a in answers if a.get("question_id") == q.id]
        subjects = list(q.of.subjects or ([q.subject] if q.subject else []))
        view = {"id": q.id, "about": q.about, "text": q.text, "options": list(q.options),
                "subjects": subjects, "effect": q.effect, "default": q.default,
                "evidence": list(q.evidence), "route": _route(q),
                "why": q.of.why, "options_from": q.of.options_from,
                #: the evidence as the package's own marks, kind and value, for the one reader that
                #: needs a number back out of a question: the budget trade's two options
                "marks": [pkg["marks"].as_json(m, compact=True) for m in q.of.evidence]}
        view["status"] = _status(view, mine)
        out.append(view)
    return out


def _status(view: dict, answers: list[dict]) -> str:
    """`open`, `answered`, `skipped` or `defaulted`.

    A default that stood is NOT an answer. It is its own status, and every consumer treats it as
    open: a role or a budget nobody confirmed is a question still unsettled, and a question still
    unsettled is what rides to the builder under `unsettled`.
    """
    confirmed = [a for a in answers if a.get("answered_by") == CUSTOMER and not a.get("skipped")]
    if view["about"] == "opening.role" and view["id"] == "role_count":
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
    """
    views = question_views(state)
    step4 = [v for v in views if v["route"] == ROUTE_INTAKE and v["status"] not in SETTLED]
    if step4:
        return step4
    return [v for v in views if v["route"] == ROUTE_TRADE and v["status"] not in SETTLED]


def stage_of(state: dict) -> str:
    views = question_views(state)
    step4 = [v for v in views if v["route"] == ROUTE_INTAKE]
    trade = [v for v in views if v["route"] == ROUTE_TRADE]
    if any(v["status"] not in SETTLED for v in step4):
        return STAGE_ASKING if any(v["id"] in (state.get("asked") or []) for v in step4) else STAGE_SURVEYED
    if any(v["status"] not in SETTLED for v in trade):
        return STAGE_TRADE
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


def said_by_customer(words: str, latest_user_message: str) -> bool:
    """The quote is in what the customer just wrote. The same proof engine selection already asks for:
    a model that cannot quote the customer did not hear them say it."""
    quote = _norm(words)
    return bool(quote) and quote in _norm(latest_user_message)


def _canonical_option(choice: str, options: list[str]) -> str | None:
    wanted = _norm(choice)
    for option in options:
        if _norm(option) == wanted:
            return option
    return None


def record_answer(state: dict, *, question_id: str, choice: str = "", role: str = "",
                  words: str = "", latest_user_message: str = "", principal: str = "",
                  skipped: bool = False, took_default: bool = False) -> dict:
    """One answer, checked, appended. Raises `SurveyError` with a sentence for the model.

    WHAT MAKES IT THE CUSTOMER'S. The quote has to be in their latest message, and the choice has to
    be one of the question's own options (`ask/settle.py`'s rule: a phrase settles a question only by
    selecting one of its options). Nothing here maps free text onto an option.

    WHAT IT BECOMES. The package's `contract.intake.Answer`, built here and asked for its mark, so the
    ownership table decides whether a person may set the field at all. A role is the mouth the
    customer picked, with the role the question was about; a budget is the number the option names,
    taken from the question's own evidence marks rather than parsed out of the words.
    """
    views = {v["id"]: v for v in question_views(state)}
    view = views.get(question_id)
    if view is None:
        raise SurveyError(f"there is no survey question {question_id!r}; the questions are "
                          f"{sorted(views)}")
    if view["route"] not in (ROUTE_INTAKE, ROUTE_TRADE):
        raise SurveyError(f"{question_id!r} is not put through this tool" + (
            ": the application asks for the unit itself" if view["route"] == ROUTE_APPLICATION else ""))
    if view["route"] == ROUTE_TRADE and any(v["route"] == ROUTE_INTAKE and v["status"] not in SETTLED
                                            for v in views.values()):
        raise SurveyError("the budget trade is only put once every other survey question is settled")
    if not said_by_customer(words, latest_user_message):
        raise SurveyError("that quote is not in the customer's latest message. Quote their own words "
                          "exactly; if they did not answer, do not record an answer")
    row: dict[str, Any] = {"question_id": question_id, "about": view["about"], "at": _now(),
                           "words": str(words)[:500], "principal": str(principal or "")[:256],
                           "via": "intake_conversation"}
    if skipped:
        return _append(state, {**row, "answered_by": CUSTOMER, "skipped": True, "subject": "",
                               "value": None})
    if took_default:
        # Stored so the record shows the customer was asked and let the default stand. Never an
        # answer: `_status` reads it as `defaulted`, the IntakeHandoff lists it unanswered, and the
        # builder is told the question is still open.
        return _append(state, {**row, "answered_by": DEFAULT_TAKEN, "subject": "",
                               "value": view.get("default")})
    option = _canonical_option(choice, view["options"])
    if option is None:
        raise SurveyError(f"{choice!r} is not one of the options for {question_id!r}: "
                          f"{view['options']}")
    subject, value, note = _subject_and_value(view, option, role)
    _refuse_conflict(state, view, subject, value)
    answer = {**row, "answered_by": CUSTOMER, "subject": subject, "value": value, "option": option,
              "note": note}
    _check_with_the_package(answer)
    return _append(state, answer)


def _subject_and_value(view: dict, option: str, role: str) -> tuple[str, Any, str]:
    if view["about"] == "opening.role":
        if view["id"].startswith("role_") and view["id"] != "role_count":
            return option, view["id"][len("role_"):], ""
        roles = _package()["asking"].ROLE_OPTIONS
        wanted = _norm(role)
        if wanted not in roles:
            raise SurveyError(f"say which role the customer gave {option}: one of {list(roles)}")
        return option, wanted, ""
    if view["about"] == "cell_budget":
        index = view["options"].index(option)
        numbers = _trade_numbers(view)
        if numbers is None:
            raise SurveyError("the budget trade carries no numbers to settle it with")
        return "", numbers[index], option
    return view["subjects"][0] if len(view["subjects"]) == 1 else "", option, ""


def _trade_numbers(view: dict) -> tuple[int, int] | None:
    """(hold, raise) for the budget trade, from the uncertainty's own evidence marks.

    The option at index 0 holds the stated budget and the one at index 1 raises it to the measured
    envelope (`contract.asking._budget_uncertainty`). The numbers are the marks' values, which is
    what the options were written from, so nothing is parsed out of a sentence.
    """
    hold = raise_to = None
    for mark in view.get("marks") or []:
        if mark.get("field") == "cell_budget":
            hold = mark.get("value")
        elif mark.get("field") == "forecast.cells_high":
            raise_to = mark.get("value")
    if isinstance(hold, int) and isinstance(raise_to, int):
        return hold, raise_to
    return None


def _refuse_conflict(state: dict, view: dict, subject: str, value: Any) -> None:
    if view["about"] != "opening.role":
        return
    for a in state.get("answers") or []:
        if (a.get("about") == "opening.role" and a.get("answered_by") == CUSTOMER
                and not a.get("skipped") and a.get("subject") == subject and a.get("value") != value):
            raise SurveyError(f"the customer already said {subject} is the {a.get('value')}. Ask them "
                              f"which it is before recording it as the {value}")


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
    for a in (state or {}).get("answers") or []:
        if (isinstance(a, dict) and a.get("about") == "opening.role" and a.get("answered_by") == CUSTOMER
                and not a.get("skipped") and a.get("subject")):
            out[str(a["subject"])] = str(a.get("value") or "")
    return out


def confirmed_cell_cap(state: dict | None) -> int | None:
    """The budget the customer confirmed in the trade, or None. A stated budget is not this."""
    for a in reversed((state or {}).get("answers") or []):
        if (isinstance(a, dict) and a.get("about") == "cell_budget" and a.get("answered_by") == CUSTOMER
                and not a.get("skipped") and isinstance(a.get("value"), int)):
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
    asked = list(state.get("asked") or [])
    answers = []
    answered_ids: set[str] = set()
    for a in state.get("answers") or []:
        if not isinstance(a, dict) or a.get("answered_by") != CUSTOMER or a.get("skipped"):
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


def builder_block(state: dict | None) -> dict | None:
    """Step 7: the package's planner block composed for this customer, with the survey attached.

    `contract.deliver.attach` puts `survey` under the block, and `deliver.survey_block` has already
    run the package's own validator on it before it returns. Nothing is computed here. None when
    there is no state, or when the package refuses the pair, and the caller then hands the planner
    the measurement's own block exactly as it does with the survey off.
    """
    if not isinstance(state, dict) or not isinstance(state.get("planner_block"), dict):
        return None
    pkg = _package()
    try:
        given = pkg["given"].Given.of(survey_of(state), intake_handoff(state))
        return pkg["deliver"].attach(dict(state["planner_block"]), given)
    except Exception as exc:                       # noqa: BLE001 - a plan is never failed for this
        logger.warning("geometry survey: the builder's block could not carry the survey (%s)", exc)
        return None


# -------------------------------------------------------------------------------------------------
# the gate at submission: a port role the customer did not confirm does not reach port_declaration
# -------------------------------------------------------------------------------------------------

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
        if v["status"] == "open":
            problems.append(f"the customer has not answered survey question {v['id']} (which of "
                            f"{', '.join(v['subjects'])} is which): put it, and record their answer with "
                            f"answer_survey_question, before submitting")
        elif v["status"] in ("skipped", "defaulted"):
            problems.append(f"the customer did not confirm survey question {v['id']}, so no port may carry "
                            f"that role. Tell them the mesh needs it, or leave the port out")
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
    fresh = compose(document, purpose=purpose, brief=_brief_of(messages), declared=declared,
                    engine=engine if engine in PACKAGE_ENGINES else None, unit=unit,
                    scale_to_metres=scale, unit_basis=basis, cell_cap=confirmed_cell_cap(old))
    if fresh["sha256"] != str(source_ref.sha256):
        raise SurveyError("the stored measurement describes other bytes than this session's upload")
    state = carry_answers(old, fresh)
    state = mark_asked(state, open_now(state))
    await save(owner_id, source_ref.source_id, state, session_id=session_id)
    _queue_the_look(source_ref.source_id, owner_id, document)
    return state


#: The purposes the Surveyor composes for. The measurement package reasons about flow: a structural
#: purpose would be asked which mouth is the inlet (`contract.asking.ROLES_NEEDED` defaults to inlet
#: and outlet for any purpose it does not name), which is a wrong question on a part that has none.
SURVEYED_PURPOSES = ("internal_cfd", "external_cfd", "conjugate_heat_transfer")

#: The engines the package's forecast knows by these names. Any other confirmed engine is left out of
#: the composition rather than passed as a word the forecast would read as its default.
PACKAGE_ENGINES = ("snappy", "cfmesh", "gmsh")


def _port_signature(ports: Any) -> list[tuple[str, str]]:
    return sorted((str(p.get("name") or ""), str(p.get("type") or p.get("role") or "").lower())
                  for p in _roled(ports))


async def for_submission(*, owner_id: str, session_id: str, source_ref, document: dict,
                         purpose: str, patches: Any, messages: Any, state: dict | None,
                         engine: str | None = None) -> dict | None:
    """The survey the submission is checked against. Composed here when intake never called
    `survey_the_part`, or when the purpose or the ports it was composed for have moved.

    It is never recomposed merely because patches exist: where the survey raised role questions and
    the customer answered them, those answers are what the patches are checked AGAINST, and composing
    again with the patches as the declaration would retire the questions and drop the answers with
    them. Raises `SurveyError`; the caller fails open.
    """
    if purpose not in SURVEYED_PURPOSES:
        return None
    before = dict((state or {}).get("composed_for") or {})
    has_roles = bool(state) and any(v["about"] == "opening.role" for v in question_views(state))
    stale = (state is None or before.get("purpose") != purpose
             or (not has_roles and _port_signature(patches) != _port_signature(before.get("declared"))))
    if not stale:
        return state
    unit, scale, basis = await _interpretation(owner_id, source_ref.source_id)
    fresh = compose(document, purpose=purpose, brief=_brief_of(messages),
                    declared=[dict(p) for p in _roled(patches)] or None,
                    engine=engine if engine in PACKAGE_ENGINES else None, unit=unit,
                    scale_to_metres=scale, unit_basis=basis, cell_cap=confirmed_cell_cap(state))
    out = mark_asked(carry_answers(state, fresh), [])
    await save(owner_id, source_ref.source_id, out, session_id=session_id)
    # the look has not been taken if intake never surveyed; take it now, for this purpose, so the
    # builder still gets it when it lands before the planner runs
    _queue_the_look(source_ref.source_id, owner_id, document)
    return out


def _queue_the_look(source_id: str, owner_id: str, document: dict) -> str:
    """Step 3, queued at the step it belongs to. Never raises."""
    try:
        from meshpipeline.application import geometry_vision
        if not geometry_vision.look_enabled():
            return "off"
        look = document.get("look") if isinstance(document.get("look"), dict) else {}
        if look.get("status") == "ok":
            return "cached"
        from meshpipeline.contracts.geometry_measurement import enqueue_look
        return "queued" if enqueue_look(str(source_id), owner_id) else "skipped"
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry survey: the look could not be queued - source_id=%s: %s", source_id, exc)
        return "skipped"


async def answer(*, owner_id: str, source_ref, question_id: str, choice: str = "", role: str = "",
                 words: str = "", latest_user_message: str = "", skipped: bool = False,
                 took_default: bool = False, document: dict | None = None) -> dict:
    """Record one answer and store it. When it confirms a budget, the planner block is composed again
    for that budget, so `customer_cell_cap` is the number the customer chose. Raises `SurveyError`."""
    state = await load(owner_id, source_ref.source_id, sha256=source_ref.sha256)
    if state is None:
        raise SurveyError("there is no survey for this upload yet: call survey_the_part first")
    state = record_answer(state, question_id=question_id, choice=choice, role=role, words=words,
                          latest_user_message=latest_user_message, principal=owner_id,
                          skipped=skipped, took_default=took_default)
    cap = confirmed_cell_cap(state)
    if cap is not None and isinstance(document, dict) and (state.get("composed_for") or {}).get("cell_cap") != cap:
        try:
            state = recomposed(state, document, cell_cap=cap)
        except SurveyError as exc:
            logger.warning("geometry survey: the confirmed budget could not be composed in (%s)", exc)
    state = mark_asked(state, open_now(state))
    await save(owner_id, source_ref.source_id, state)
    return state


async def recompose_after_look(source_id: str, owner_id: str, document: dict) -> str:
    """The look landed: compose the survey again with it, keeping every answer still bound. Never raises."""
    if not survey_enabled():
        return "off"
    try:
        sha = str(((document.get("source") or {}).get("sha256")) or document.get("source_sha256") or "")
        state = await load(owner_id, source_id, sha256=sha)
        if state is None:
            return "no_survey"
        fresh = recomposed(state, document)
        await save(owner_id, source_id, fresh)
        return "recomposed"
    except Exception as exc:                       # noqa: BLE001
        logger.warning("geometry survey: could not be recomposed after the look - source_id=%s: %s",
                       source_id, exc)
        return "skipped"


__all__ = ["CHAIN", "CUSTOMER", "DEFAULT_TAKEN", "ROUTE_ADVISORY", "ROUTE_APPLICATION", "ROUTE_INTAKE",
           "ROUTE_TRADE", "STAGE_ASKING", "STAGE_SETTLED", "STAGE_SURVEYED", "STAGE_TRADE",
           "SURVEY_STATE_SCHEMA", "SurveyError", "answer", "builder_block", "carry_answers", "compose",
           "confirmed_cell_cap", "confirmed_roles", "intake_handoff", "load", "mark_asked", "open_now",
           "question_views", "recompose_after_look", "recomposed", "record_answer", "role_problems",
           "said_by_customer", "save", "stage_of", "survey_enabled", "survey_the_part"]
