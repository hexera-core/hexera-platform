# Responsibility: Run the geometry agent's step of the chain on the platform: plan the part from the stored survey and the customer's answers, raise the one question only the plan can raise, and hand the builder both write-ups.
# Owns: when the step runs, what it keeps on the survey row, the third intake's question, the job ledger's rows, and the builder's handoff while the step is on.
# Boundaries: every number, every plan and every contract is the measurement package's (`chain.job`, `contract`, `ask.trade`); this module computes none of them and measures nothing again.
# Collaborates with: application/geometry_survey.py for the row, agents/intake/executor.py for the submission, cad/regions.py and engines/snappy/drivers.py for the builder.
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import geometry_survey as gs

logger = logging.getLogger(__name__)

# STEPS 5 AND 6 OF THE CHAIN, AND WHERE THEY HAPPEN ON THIS PLATFORM.
#
#   5  geometry   the geometry agent decides the flow patches and where the cells go, from the
#                 Surveyor's facts and the customer's confirmations. It never re-decides what the
#                 geometry IS: `chain.job.plan` hands it `agent.loop.ChainGiven`, and the loop refuses to
#                 start on a context that reads the part differently from the survey and sends back any
#                 plan that contradicts the survey or a confirmation.
#   6  intake     a third time, ONLY for a question that did not exist at step 4. The canonical one is
#                 the budget trade against what THIS PLAN costs on the builder's own sizing
#                 (`ask.trade`), and `contract.intake.binds_late` refuses any topic the survey had.
#
# WHY AT THE SUBMISSION AND NOT IN THE PIPELINE GRAPH. The graph (`pipeline/graph.py`) runs after the
# customer has approved and left: `node_intake` short-circuits there, and no node can put a question
# to anybody. Step 6 needs the customer, so step 5 has to run while they are still in the
# conversation, after their step-4 answers and before the builder. The last moment that is true is
# `submit_requirements`, which is also the first moment the fidelity and the engine are settled. So
# the plan is made there, once per set of answers, and the builder reads it from the survey row.
#
# FAIL-OPEN, AND IT SAYS WHY. Anything that goes wrong here (no model configured, the loop runs out,
# the plan is sent back three times, a contract refuses a handoff, the survey on the row is not what
# its own inputs compose to) is stored on the row as `status: failed` with the sentence, logged, and
# the job then runs exactly as it does with GEOMETRY_AGENT_STEP_ENABLED off: no third question, the
# builder's request and typed block untouched. The builder's side says the same sentence again when
# it falls back (`engines/snappy/drivers._planner_inputs`).

#: The step's own name, written into every record this module stores on the survey row.
STEP_SCHEMA = "meshpipeline.geometry_step.v1"
LATE_SCHEMA = "meshpipeline.geometry_step.late.v1"
PLANNED, FAILED = "planned", "failed"

#: The package's fidelity words, which are the platform's (`pipeline/enums.MeshFidelity`) one for one.
FIDELITIES: tuple[str, ...] = ("draft", "standard", "max")

#: The providers the step accepts by name. `reference` is the package's deterministic stand-in.
PROVIDERS: tuple[str, ...] = ("deepseek", "deepinfra", "anthropic", "generic", "auto", "reference")

#: Extra wall clock the thread is waited for past the loop's own budget: the deterministic tools the
#: loop runs between model calls are not inside that budget's model-call bound.
THREAD_GRACE_S = 120.0


class StepRefused(RuntimeError):
    """The step cannot be used for this job, and the sentence says why. Never reaches a customer."""


def step_enabled() -> bool:
    """Every gate under the step, read together every time."""
    return bool(polcfg.GEOMETRY_AGENT_STEP_ENABLED) and gs.survey_enabled()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _package() -> dict[str, Any]:
    from geometry_agent.agent import catalog, hexera
    from geometry_agent.agent.loop import LoopConfig
    from geometry_agent.agent.schema import GeometryPlan
    from geometry_agent.chain import handover as chandover
    from geometry_agent.chain import job as cj
    from geometry_agent.chain import resolution, schema
    from geometry_agent.chain.bridge import KIND_TEACHES
    from geometry_agent.chain.store import JobLedger
    from geometry_agent.contract import brief, build, deliver, given, intake, marks, survey
    return {"catalog": catalog, "hexera": hexera, "LoopConfig": LoopConfig, "GeometryPlan": GeometryPlan,
            "handover": chandover, "job": cj, "resolution": resolution, "schema": schema,
            "KIND_TEACHES": KIND_TEACHES, "JobLedger": JobLedger, "brief": brief, "build": build,
            "deliver": deliver, "given": given, "intake": intake, "marks": marks, "survey": survey}


# -------------------------------------------------------------------------------------------------
# which plan belongs to which answers
# -------------------------------------------------------------------------------------------------

def _late_id(state: dict | None) -> str:
    late = (state or {}).get("late")
    return str(late.get("id") or "") if isinstance(late, dict) else ""


def plan_key(state: dict, fidelity: str) -> str:
    """What a plan was made FOR: the bytes, the inputs the survey was composed from, the customer's
    step-4 answers and the fidelity. A plan whose key is not the row's is a plan for another job and
    is never handed to a builder.

    THE BUDGET THE COMPOSITION CARRIES IS IN IT, not inferred from the answers. It was left out on the
    argument that a confirmed budget always arrives with an answer that is here anyway, and on the
    product's own paths that is true; but the whole composition the plan was measured against, its
    forecast included, is composed for that number, and a key that says it identifies the composition
    while leaving out a value the composition takes is a key that lies when anything ever composes with
    an explicit cap. It costs two fields to be true instead of true-for-now.

    Not in it: the third intake's answer, which comes after the plan by construction and must not
    invalidate it. That answer is never composed back into the survey, so it moves nothing here."""
    stored = state.get("composed_for")
    cf: dict[str, Any] = stored if isinstance(stored, dict) else {}
    inputs = {k: cf.get(k) for k in ("purpose", "engine", "declared", "unit", "unit_basis", "scale_to_metres",
                                     "brief_sha", "representation", "inlet_ids", "look_status",
                                     "cell_cap", "cell_cap_kind")}
    late_id = _late_id(state)
    answers = [[a.get("question_id"), a.get("subject"), a.get("value"), a.get("answered_by"), bool(a.get("skipped"))]
               for a in gs.live_answers(state)
               if a.get("stage") != gs.LATE_STAGE and (not late_id or a.get("question_id") != late_id)]
    return _sha({"source": state.get("sha256"), "facts": state.get("facts_sha256"), "for": inputs,
                 "answers": answers, "fidelity": fidelity})[:32]


# -------------------------------------------------------------------------------------------------
# the model
# -------------------------------------------------------------------------------------------------

def planner_client(provider: str | None = None) -> Any:
    """The geometry agent's model: GEOMETRY_AGENT_STEP_PROVIDER, or a refusal naming why there is none."""
    name = str(provider or polcfg.GEOMETRY_AGENT_STEP_PROVIDER or "deepseek").strip().lower()
    if name not in PROVIDERS:
        raise StepRefused(f"GEOMETRY_AGENT_STEP_PROVIDER is {name!r}, which is not one of {PROVIDERS}")
    if name == "reference":
        from geometry_agent.agent.reference import ReferencePolicy
        return ReferencePolicy()
    from geometry_agent.agent.client import ModelError, client_from_env
    try:
        return client_from_env(name)
    except ModelError as exc:
        raise StepRefused(f"no model is configured for the geometry agent on {name!r}: {exc}") from exc


def _is_reference(client: Any) -> bool:
    return type(client).__name__ == "ReferencePolicy"


def _model_of(client: Any) -> str:
    return str(getattr(client, "model", "") or type(client).__name__)


# -------------------------------------------------------------------------------------------------
# the handoffs, rebuilt from the row and checked by the package's own contracts
# -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Inputs:
    facts: Any
    composed: dict
    brief: Any
    survey: Any
    intake: Any
    stated: dict[str, str]
    look: dict | None
    engine: str
    ports: list[dict]


def _brief_rows(ports: list[dict], facts: Any, unit: str | None) -> list[dict]:
    """The customer's port rows in the brief's own shape: `role` for `type`, the file's own units for a
    point or a size given in millimetres (`catalog.in_file_units`), and nothing a port row may not carry.
    A row whose role the brief's vocabulary does not have is left out rather than renamed."""
    pkg = _package()
    rows = pkg["catalog"].in_file_units([dict(p) for p in ports], facts, unit) or []
    allowed, roles = pkg["brief"].DECLARED_KEYS, pkg["brief"].DECLARED_ROLES
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        role = str(r.get("type") or r.get("role") or "").strip().lower()
        if role and role not in roles:
            continue
        row = {k: v for k, v in r.items() if k in allowed and k not in ("type", "role") and v is not None}
        if role:
            row["role"] = role
        out.append(row)
    return out


def _stated_roles(document: dict, ports: list[dict]) -> dict[str, str]:
    """The customer's own port list bound to measured mouths, `{opening_id: role}`: their words, nobody
    asked. Bound one row at a time the way the submission gate binds them, and a mouth two rows claim
    keeps the first."""
    from meshpipeline.agents.intake.geometry_brief import bind_patches
    out: dict[str, str] = {}
    for p in ports:
        role = str(p.get("type") or p.get("role") or "").strip().lower()
        if role not in ("inlet", "outlet"):
            continue
        bound = bind_patches(document, [{**p, "type": role}]).get("bound") or []
        oid = str((bound[0] if bound else {}).get("opening_id") or "")
        if oid and oid not in out:
            out[oid] = role
    return out


def _inputs(state: dict, document: dict, *, fidelity: str, source_path: str = "") -> _Inputs:
    """Handoffs 0 to 2 as the package's types, rebuilt from the row. Raises `StepRefused`.

    THE SURVEY IS THE ONE THE CUSTOMER ANSWERED, and that is checked, not assumed: the stored inputs are
    composed again (`gs.composition`, the same call `compose` makes) and the survey they give must be the
    stored one, field for field. A survey this platform cannot reproduce is not planned against, because a
    plan against another survey than the one the answers were bound to is an answer about another file.
    The reproduction is then bound to a brief (`contract.survey.bound_to_brief`), which the stored survey,
    composed at upload time, never was.
    """
    if not isinstance(state, dict) or not isinstance(state.get("survey"), dict):
        raise StepRefused("there is no survey on the row to plan from")
    if not isinstance(document, dict) or document.get("status") != "ok":
        raise StepRefused("there is no successful measurement of these bytes to plan from")
    pkg = _package()
    kwargs = gs.composed_inputs(state)
    try:
        made = gs.composition(document, **kwargs)
    except gs.SurveyError as exc:
        raise StepRefused(f"the survey could not be composed again: {exc}") from exc
    stored = gs.survey_of(state)
    if made["survey"].model_dump(mode="json") != stored.model_dump(mode="json"):
        raise StepRefused("the survey on the row is not what its own inputs compose to, so the customer's answers "
                          "are bound to a survey this platform cannot reproduce; no plan is made against it")
    facts, composed = made["facts"], made["composed"]
    brief = pkg["brief"].brief_from(kwargs["purpose"], made["brief"],
                                    declared=_brief_rows(made["ports"], facts, kwargs.get("unit")),
                                    engine=kwargs.get("engine"), fidelity=fidelity)
    survey = pkg["build"].survey_from(composed, brief=made["brief"] or None, declared=made["ports"] or None,
                                      brief_sha256=brief.sha256)
    pkg["survey"].bound_to_brief(survey, brief.sha256)
    stored_look = document.get("look")
    look = stored_look if isinstance(stored_look, dict) and stored_look.get("status") == "ok" else None
    # THE PART ITSELF, ON THE FACTS THE LOOP GETS AND NOWHERE ELSE. The stored document carries
    # `source_path: ""` deliberately: it was measured from a temp file at upload that is long gone, and
    # a stale path in a stored document is a lie. The agent's own tools do open the part, though
    # (`tools.section_profile` and every other mesh tool go through `ctx.mesh()`, which is
    # `load_mesh(facts.source_path)`), so the loop is given a real local copy. It is put on AFTER the
    # composition and the survey comparison above, on this object only, so that everything composed
    # from the facts is composed from exactly the dump the stored survey was composed from.
    if source_path:
        facts = facts.model_copy(update={"source_path": str(source_path)})
    return _Inputs(facts=facts, composed=composed, brief=brief, survey=survey, intake=gs.intake_handoff(state),
                   stated=_stated_roles(document, made["ports"]), look=look,
                   engine=str(composed.get("engine") or "snappy"), ports=made["ports"])


def late_handoff(state: dict) -> Any:
    """The third intake as `contract.intake.LateIntakeHandoff`, or None when the plan raised nothing.

    Only the customer's own answer is an `Answer`. A skip and a default that stood are `unanswered`: the
    budget admits no assumed claim, so a default here is the question still open, exactly as at step 4."""
    late = state.get("late")
    if not isinstance(late, dict) or not late.get("id"):
        return None
    pkg = _package()
    qid = str(late["id"])
    rows = [a for a in gs.live_answers(state) if a.get("question_id") == qid]
    asked = [qid] if (qid in (state.get("asked") or []) or rows) else []
    answers = [pkg["intake"].Answer(question_id=qid, about="cell_budget", value=a.get("value"),
                                    answered_by=gs.CUSTOMER, at=str(a.get("at") or _now()),
                                    note=str(a.get("option") or ""))
               for a in rows if a.get("answered_by") == gs.CUSTOMER and not a.get("skipped")][-1:]
    return pkg["intake"].LateIntakeHandoff(
        survey_source_sha256=str(state.get("sha256") or ""), survey_facts_sha256=str(state.get("facts_sha256") or ""),
        depends_on=str(late.get("depends_on") or ""),
        raised=[pkg["survey"].Uncertainty.model_validate(late.get("raised") or {})],
        asked=asked, answers=answers, unanswered=[q for q in asked if not answers])


def _given(inp: _Inputs, state: dict, *, with_late: bool) -> Any:
    pkg = _package()
    return pkg["given"].Given.of(inp.survey, inp.intake, late=late_handoff(state) if with_late else None,
                                 stated_roles=inp.stated)


# -------------------------------------------------------------------------------------------------
# the job ledger's rows, in the chain's own record types
# -------------------------------------------------------------------------------------------------

def _kind(about: str) -> str:
    try:
        return str(_package()["schema"].canonical_kind(about))
    except ValueError:
        return about


def _uncertainty_records(survey: Any, asked: set[str], texts: dict[str, str]) -> list[Any]:
    """The survey's uncertainties as the ledger's merged record: `known` is measured values only."""
    pkg = _package()
    out = []
    for u in survey.uncertainties:
        kind = _kind(u.about)
        subject = ",".join(u.subjects) or u.subject or "part"
        out.append(pkg["schema"].Uncertainty(
            id=f"{kind}:{subject}", kind=kind, subject=subject,
            known={str(m.source or m.field): m.value for m in u.evidence if m.kind == pkg["marks"].MEASURED},
            unsettled_because=u.why, ask=texts.get(u.id, ""), default=str(u.default or ""),
            default_from="the survey's own default" if u.default else "", teaches=pkg["KIND_TEACHES"].get(kind, ""),
            effect=u.effect, places=tuple(u.subjects), why=u.why, options=tuple(u.options),
            options_from=u.options_from, raised_at="second", asked=u.id in asked, settled_by=""))
    return out


def _resolutions(state: dict, stated: dict[str, str]) -> list[Any]:
    """What came back for every survey question, as the chain's five types. A default is never an answer:
    a skip or a default that stood is `Defaulted`, a question never put is `Unasked`.

    ONE RESOLUTION PER FIELD, and an answered field never also carries an assumption. Two questions can
    be about the same mouth - "which is the inlet" and "which is the outlet" on a part with one mouth are
    both about it - and on 2 of 49 corpus parts the customer answered one and skipped the other, which
    put that mouth under `confirmed` and `assumed` at once and the package refused the whole handoff. It
    was right to: an answer and a default are two things. So an unanswered question speaks only for the
    subjects nothing has settled, and one whose subjects are all settled says nothing at all, because
    there is no field left for it to be about.
    """
    res = _package()["resolution"]
    at = _now()
    asked = set(state.get("asked") or [])
    late_id = _late_id(state)
    live = [a for a in gs.live_answers(state)
            if a.get("stage") != gs.LATE_STAGE and (not late_id or a.get("question_id") != late_id)]
    views = [v for v in gs.question_views(state) if v["route"] != gs.ROUTE_LATE]
    said_for = {v["id"]: [a for a in live if a.get("question_id") == v["id"]
                          and a.get("answered_by") == gs.CUSTOMER and not a.get("skipped")] for v in views}
    settled = {f"role:{oid}" for oid in stated}
    for v in views:
        settled |= {f"{_kind(v['about'])}:{a.get('subject') or ','.join(v['subjects']) or 'part'}"
                    for a in said_for[v["id"]]}
    out: list[Any] = []
    for v in views:
        kind = _kind(v["about"])
        rows = [a for a in live if a.get("question_id") == v["id"]]
        if said_for[v["id"]]:
            for a in said_for[v["id"]]:
                subject = a.get("subject") or ",".join(v["subjects"]) or "part"
                out.append(res.Answered(field=f"{kind}:{subject}", value=str(a.get("value")),
                                        question_id=v["id"], answered_by=gs.CUSTOMER,
                                        verbatim=str(a.get("words") or ""), at=str(a.get("at") or at)))
            continue
        left = [s for s in v["subjects"] if f"{kind}:{s}" not in settled]
        if v["subjects"] and not left:
            continue
        subject = ",".join(left) or "part"
        if rows or v["id"] in asked:
            out.append(res.Defaulted(field=f"{kind}:{subject}", assumed=str(v.get("default") or "nothing"),
                                     question_id=v["id"], default_from="the survey's own default; nobody confirmed it",
                                     at=at))
        else:
            out.append(res.Unasked(field=f"{kind}:{subject}", assumed=str(v.get("default") or "nothing"),
                                   why_not_asked=v["route"] if v["route"] != gs.ROUTE_INTAKE else "policy",
                                   default_from="the survey's own default", at=at))
    for oid, role in sorted(stated.items()):
        out.append(res.Stated(field=f"role:{oid}", brief_value=role, at=at,
                              quote=f"your port list names this mouth as {role}"))
    return out


def _intake_record(state: dict, inp: _Inputs, key: str) -> Any:
    schema = _package()["schema"]
    asked = set(state.get("asked") or [])
    views = [v for v in gs.question_views(state) if v["route"] != gs.ROUTE_LATE and v["id"] in asked]
    questions = tuple(schema.Question(id=schema.question_id(key, v["id"]), uncertainty_id=v["id"],
                                      topic=_kind(v["about"]), ask=v["text"], options=tuple(v["options"]),
                                      default=str(v.get("default") or ""), default_from="the survey's own default",
                                      effect=v["effect"], asked_at="") for v in views)
    return schema.Intake(asked=questions, resolutions=tuple(_resolutions(state, inp.stated)))


def _trade_record(state: dict, key: str) -> Any:
    """The third intake as the ledger's `trade` stage, or None when the plan raised nothing. Its one
    resolution: `Answered` when the customer chose, `Defaulted` when they skipped or let the default
    stand, `Unasked` when it was raised and never put."""
    late = state.get("late")
    if not isinstance(late, dict) or not late.get("id"):
        return None
    pkg = _package()
    schema, res = pkg["schema"], pkg["resolution"]
    qid = str(late["id"])
    rows = [a for a in gs.live_answers(state) if a.get("question_id") == qid]
    said = [a for a in rows if a.get("answered_by") == gs.CUSTOMER and not a.get("skipped")]
    at = str((rows[-1] if rows else {}).get("at") or _now())
    if said:
        r: Any = res.Answered(field="budget:cap", value=str(said[-1].get("option") or said[-1].get("value")),
                              question_id=qid, answered_by=gs.CUSTOMER, verbatim=str(said[-1].get("words") or ""), at=at)
    elif rows or qid in (state.get("asked") or []):
        r = res.Defaulted(field="budget:cap", assumed=str(late.get("default") or ""), question_id=qid,
                          default_from="the customer's own stated budget", at=at)
    else:
        r = res.Unasked(field="budget:cap", assumed=str(late.get("default") or ""), why_not_asked="policy",
                        default_from="the customer's own stated budget", at=at)
    question = schema.Question(id=schema.question_id(key, qid), uncertainty_id=qid, topic="budget",
                               ask=str(late.get("text") or ""), options=tuple(late.get("options") or ()),
                               default=str(late.get("default") or ""), default_from=str(late.get("default_is") or ""),
                               effect="changes_mesh", asked_at=at)
    return schema.Intake(asked=(question,), resolutions=(r,))


def _records(state: dict, inp: _Inputs, key: str, meta: dict) -> dict[str, Any]:
    """Every stage record the chain keeps for this job, built once, and intake's write-up in its record."""
    pkg = _package()
    cj, schema = pkg["job"], pkg["schema"]
    brief_rec = cj.brief_record(inp.brief)
    survey_rec = cj.survey_record(inp.composed, inp.brief, cj.representation_without_brief(inp.facts, inp.brief.purpose))
    look_rec = cj.look_record(inp.look)
    views = {v["id"]: v["text"] for v in gs.question_views(state)}
    records = _uncertainty_records(inp.survey, set(state.get("asked") or []), views)
    intake_rec = _intake_record(state, inp, key)
    base = schema.JobRecord(key=key, part_key=meta["part_key"], job_id=meta["job_id"], provenance=meta["provenance"],
                            brief=brief_rec, survey=survey_rec, look=look_rec, uncertainties=tuple(records),
                            intake=intake_rec)
    intake_rec = schema.Intake(asked=intake_rec.asked, resolutions=intake_rec.resolutions,
                               write_up=pkg["handover"].write_up(base))
    return {"brief": brief_rec, "survey": survey_rec, "look": look_rec, "uncertainty": records, "intake": intake_rec}


class _Ledger:
    """The chain's own `JobLedger`, writing to the survey row: every event is kept in `events`, and
    appended as JSONL too when GEOMETRY_AGENT_LEDGER_PATH names a file."""

    def __init__(self, meta: dict[str, str]):
        pkg = _package()
        path = str(polcfg.GEOMETRY_AGENT_LEDGER_PATH or "")
        self._file = bool(path)
        self.ledger = pkg["JobLedger"](Path(path or "geometry_agent_ledger.unused.jsonl"))
        self.key = self.ledger.open_job(meta["part_key"], meta["job_id"], provenance=meta["provenance"])
        self.events: list[dict] = []

    def put(self, stage: str, value: Any, at: str = "") -> None:
        self.events.append(json.loads(json.dumps(self.ledger.put(self.key, stage, value, at=at), default=str)))

    def flush(self) -> None:
        if self._file:
            try:
                self.ledger.flush()
            except OSError as exc:              # the row keeps every event; the file is a copy
                logger.warning("geometry step: the ledger file could not be written (%s)", exc)


def _ledger_meta(state: dict, facts_sha: str, job_id: str, provenance: str) -> dict[str, str]:
    return {"part_key": str(facts_sha or state.get("facts_sha256") or state.get("sha256") or ""),
            "job_id": str(job_id or str(state.get("sha256") or "")[:12]), "provenance": provenance}


def _append_events(state: dict, events: list[dict]) -> dict:
    step = dict(state.get("geometry_step") or {})
    ledger = dict(step.get("ledger") or {})
    ledger["events"] = [*(ledger.get("events") or []), *events]
    step["ledger"] = ledger
    return {**state, "geometry_step": step}


def _reopen(state: dict) -> _Ledger | None:
    meta = ((state.get("geometry_step") or {}).get("ledger") or {}).get("meta")
    if not isinstance(meta, dict) or not meta.get("part_key"):
        return None
    return _Ledger(meta)


# -------------------------------------------------------------------------------------------------
# step 5, and the question step 6 puts
# -------------------------------------------------------------------------------------------------

class _NotYet:
    """The customer, at the moment the trade is raised: not asked yet. The chain's `third_intake` is run
    for the envelope and the question it would put; the answer comes in a later turn."""

    def reply(self, question: Any) -> None:
        return None


def plan_the_part(state: dict, document: dict, *, fidelity: str = "standard", job_id: str = "",
                  client: Any = None, source_path: str = "") -> dict:
    """Step 5, and the question step 6 puts. Returns the row with `geometry_step` and, when the plan
    raised one, `late`. NEVER RAISES: a failure is stored as `status: failed` with its reason, and the
    job then runs exactly as it does with the step off.

    A FAILED STEP ASKS NOBODY ANYTHING. A question an earlier plan raised and that was never put is
    dropped here (`_with_late(..., None, None)`), because there is no plan left to price it against and
    the customer would be held at the submission for an envelope this platform can no longer produce.
    One already put stays put, with whatever they said: it is asked once.

    `source_path` is a local copy of the customer's own file, for the agent's tools. Empty is allowed
    and plans exactly as before it existed; a tool that then needs the part fails, and that fails open
    like anything else here."""
    fid = fidelity if fidelity in FIDELITIES else "standard"
    key = plan_key(state, fid)
    try:
        return _plan(state, document, key=key, fidelity=fid, job_id=job_id, client=client,
                     source_path=source_path)
    except Exception as exc:                       # noqa: BLE001 - the step is never worth the job
        reason = str(exc) if isinstance(exc, StepRefused) else f"{type(exc).__name__}: {exc}"
        logger.warning("geometry step: no plan for this submission, the job runs without it - %s", reason[:500])
        state = _with_late(state, None, None)
        step = {**(state.get("geometry_step") or {}), "schema": STEP_SCHEMA, "status": FAILED, "for": key,
                "fidelity": fid, "reason": reason[:1000], "at": _now()}
        for stale in ("plan", "envelope", "flow_patches", "unconfirmed_roles"):
            step.pop(stale, None)
        return {**state, "geometry_step": step}


def _plan(state: dict, document: dict, *, key: str, fidelity: str, job_id: str, client: Any,
          source_path: str = "") -> dict:
    pkg = _package()
    cj = pkg["job"]
    inp = _inputs(state, document, fidelity=fidelity, source_path=source_path)
    given = _given(inp, state, with_late=False)
    client = client if client is not None else planner_client()
    meta = _ledger_meta(state, inp.facts.sha256, job_id, "heuristic" if _is_reference(client) else "live")
    ledger = _Ledger(meta)
    recs = _records(state, inp, ledger.key, meta)
    for stage in ("brief", "survey", "look", "uncertainty", "intake"):
        ledger.put(stage, recs[stage])
    config = pkg["LoopConfig"](wall_budget_s=float(polcfg.GEOMETRY_AGENT_STEP_TIMEOUT_SECONDS) or None)
    result = cj.plan(inp.facts, inp.brief, given, inp.composed, client, look_block=inp.look, config=config)
    sent_back = sum(1 for t in result.trace if t.get("rejection_kind") == "given")
    step: dict[str, Any] = {"schema": STEP_SCHEMA, "for": key, "at": _now(), "fidelity": fidelity,
                            "engine": inp.engine,
                            "provider": "reference" if _is_reference(client) else str(
                                polcfg.GEOMETRY_AGENT_STEP_PROVIDER or ""),
                            "model": _model_of(client), "exit": result.exit, "rounds": result.rounds,
                            "sent_back_by_the_survey": sent_back, "representation": given.representation,
                            "brief_sha256": inp.brief.sha256,
                            # A NEW PLAN STARTS A NEW RECORD ON THE ROW. The events of a plan this row no
                            # longer carries describe a job that no longer exists, and keeping them grew
                            # the row by a whole plan for every answer the customer changed. Measured over
                            # 49 corpus parts: one plan's events are a median 24 kB, and the row is a
                            # median 23 kB without the step and 72 kB with it. The durable append-only
                            # record, every plan kept, is GEOMETRY_AGENT_LEDGER_PATH. Nothing the customer
                            # said is in here: their answers live in `answers` and are never rewritten.
                            "ledger": {"meta": {**meta, "key": ledger.key}, "events": []}}
    if result.plan is None:
        ledger.put("plan", cj.plan_record(result, given, inp.facts, inp.brief, recs["survey"], "", inp.engine))
        ledger.flush()
        step["ledger"]["events"].extend(ledger.events)
        raise_with = f"the geometry agent submitted no plan ({result.exit}): {result.reason}"
        # no plan, so nothing prices a trade: a question raised earlier and never put goes with it
        failed = {**_with_late(state, None, None),
                  "geometry_step": {**step, "status": FAILED, "reason": raise_with[:1000]}}
        logger.warning("geometry step: %s", raise_with[:500])
        return failed
    plan = result.plan
    plan_dict = plan.model_dump(mode="json")
    env, trade, late, _resolutions_unused, skipped = cj.third_intake(inp.survey, plan, inp.facts, inp.brief, given,
                                                                     _NotYet(), engine=inp.engine)
    unit = str(given.value("unit") or plan.unit.assumed)
    write_up = pkg["hexera"].builder_plan_block(plan, inp.facts, engine=inp.engine, cell_cap=given.budget,
                                                representation=given.representation, unit=unit,
                                                brief=inp.brief.text or None, request_txt=inp.brief.text or None)
    ledger.put("plan", cj.plan_record(result, given, inp.facts, inp.brief, recs["survey"], str(write_up.get("text") or ""),
                                      inp.engine))
    if trade is not None:
        ledger.put("uncertainty", [*recs["uncertainty"], cj.trade_uncertainty(trade)])
    ledger.flush()
    step["ledger"]["events"].extend(ledger.events)
    step.update({"status": PLANNED, "reason": "", "plan": plan_dict, "envelope": env.as_dict(),
                 "trade": "raised" if trade is not None else "", "trade_skipped_because": skipped,
                 "flow_patches": pkg["deliver"].flow_patches(plan_dict.get("patches") or [], given),
                 "unconfirmed_roles": pkg["given"].unconfirmed_roles(plan_dict, given)})
    logger.info("geometry step: planned (%s, %d rounds, %d sent back by the survey), envelope %s, trade %s",
                result.exit, result.rounds, sent_back, env.cells_high, step["trade"] or skipped)
    return _with_late({**state, "geometry_step": step}, trade, late)


def _with_late(state: dict, trade: Any, late: Any) -> dict:
    """The third intake's question on the row. ASKED ONCE: a question already put stays exactly as it was
    put, whatever a later plan costs; one raised and not yet put is replaced by the latest plan's, or
    removed when the latest plan fits."""
    old = state.get("late") if isinstance(state.get("late"), dict) else None
    if old is not None:
        qid = str(old.get("id") or "")
        put = qid in (state.get("asked") or []) or any(a.get("question_id") == qid for a in gs.live_answers(state))
        if put:
            return state
    out = {k: v for k, v in state.items() if k != "late"}
    if trade is None or late is None:
        return out
    raised = late.raised[0]
    out["late"] = {"schema": LATE_SCHEMA, "id": trade.id, "kind": trade.kind, "text": trade.text,
                   "options": list(trade.options), "default": trade.default, "default_is": trade.default_is,
                   "because": trade.because, "envelope": dict(trade.envelope),
                   "raised": raised.model_dump(mode="json"), "depends_on": late.depends_on, "raised_at": _now()}
    return out


def note_late_answer(state: dict) -> dict:
    """The third intake's answer, written to the job ledger as the chain writes it: the `trade` stage,
    with its one resolution. Never raises; a ledger that cannot be written leaves the answer recorded."""
    try:
        ledger = _reopen(state)
        rec = _trade_record(state, ledger.key) if ledger is not None else None
        if ledger is None or rec is None:
            return state
        ledger.put("trade", rec)
        ledger.flush()
        return _append_events(state, ledger.events)
    except Exception as exc:                       # noqa: BLE001 - the answer is recorded either way
        logger.warning("geometry step: the third intake's answer did not reach the ledger (%s)", exc)
        return state


def not_yet(state: dict | None) -> list[str]:
    """The survey's own questions that have not been put, which step 5 waits for. Empty when none.

    The Surveyor's questions and the survey's own budget trade both belong to the customer's turn, and
    the geometry agent plans from their answers. A question still to be put is an answer the plan would
    not have. `open_now` already returns exactly those, in the chain's order, and a question that was
    PUT and skipped or left to its default is not one of them: the customer has had it, and a default
    that stood rides to the builder as the open question it is.
    """
    return [f"{v['route']}:{v['id']}" for v in gs.open_now(state or {})
            if v["route"] in (gs.ROUTE_INTAKE, gs.ROUTE_TRADE)]


def submission_problems(state: dict | None) -> list[str]:
    """Why the submission waits: the plan raised a question and it has not been put. Empty otherwise."""
    view = gs.late_view(state)
    if view is None or view["status"] in gs.TRADE_PUT:
        return []
    return [f"the geometry agent has planned the part and its plan raises ONE more question, put once: "
            f"[{view['id']}] {view['text']} options: {', '.join(view['options'])}; the default is "
            f"{view['default']}. Put it to the customer in these words or close to them, record what they say "
            f"with answer_survey_question (skipped or took_default if they decline or leave it to you), then "
            f"submit again"]


def the_bytes_again(source_ref: Any, workspace: str, job_id: str = "") -> str:
    """A local copy of the customer's own file, for the geometry agent's OWN tools. "" when there is none.

    WHY THE STEP NEEDS IT AT ALL. The stored measurement keeps `source_path: ""` on purpose, so the row
    never holds a path that stopped existing when the upload's temp directory went. But the agent opens
    the part itself: `tools.section_profile` and every other mesh tool go through `ctx.mesh()`, which is
    `load_mesh(Path(facts.source_path))`, and the renderers take the same field. A step that hands the
    loop an empty path therefore works only for as long as the model does not reach for one.

    WHAT HID IT. The corpus proof of this step ran on the package's stand-in policy, which calls no tool
    that opens the part, so every one of those runs passed with the path empty. The first live run put
    `section_profile` on the second part it tried and the whole step fell open with "unsupported format
    ''". The ruler had the same blind spot as the thing it was measuring.

    THE SAME RETRIEVAL THE LOOK USES, and for the same reason: `fetch_verified_bytes` checks the bytes
    against the sha the row names and keeps the file's own suffix, which is what the loader reads.

    NEVER RAISES. No bytes means the loop runs as it did before this function existed.
    """
    try:
        from meshpipeline.application.geometry_materializer import fetch_verified_bytes
        return str(fetch_verified_bytes(source_ref, workspace=workspace, job_id=f"geometry-step:{job_id}"))
    except Exception as exc:                       # noqa: BLE001 - the plan is attempted either way
        logger.warning("geometry step: the uploaded geometry could not be retrieved for the agent's own tools "
                       "(%s); the plan is attempted without it, and a tool that needs the part will fail it", exc)
        return ""


async def at_submission(*, owner_id: str, session_id: str, source_ref: Any, state: dict | None,
                        document: dict | None, fidelity: str | None = None, client: Any = None) -> dict | None:
    """Step 5 at the submission: the plan for these answers, made now when there is none. NEVER RAISES
    and never blocks the submission on a failure: a failed step is stored and the job runs without it.

    One attempt per set of answers. A failure is not retried for the same answers, because every retry
    is another run of the loop inside the customer's submission turn; a changed answer makes a new key."""
    if not step_enabled() or not isinstance(state, dict) or not isinstance(document, dict) or source_ref is None:
        return state
    if not_yet(state):
        # STEP 5 IS AFTER STEP 4, and the survey's own budget trade is part of step 4's turn. Planning
        # against a survey whose budget is still open buys a plan the next answer throws away: confirming
        # a budget composes the measurement again, which moves `plan_key`, and the builder then refuses
        # the stored plan as one made for another job. Measured on the corpus before this guard existed:
        # of ten parts run end to end, the four whose survey still had its trade open were the four the
        # builder fell back on. Nothing is stored here, because this is "not yet" and not a failure: the
        # next submission, once the question has been put, plans.
        logger.info("geometry step: not planning yet, the survey still has questions to put")
        return state
    fid = fidelity if fidelity in FIDELITIES else "standard"
    stored = state.get("geometry_step")
    step: dict[str, Any] = stored if isinstance(stored, dict) else {}
    if step.get("for") == plan_key(state, fid) and step.get("status") in (PLANNED, FAILED):
        return state
    budget = float(polcfg.GEOMETRY_AGENT_STEP_TIMEOUT_SECONDS or 0)
    # `ignore_cleanup_errors`: what the timeout below ends is the WAITING, not the thread, so on a
    # timeout the loop can still be holding this file open. A temp directory left behind is worth less
    # than a submission turn that dies while tidying one up.
    with tempfile.TemporaryDirectory(prefix="geometry-step-bytes-", ignore_cleanup_errors=True) as workspace:
        new = await _planned(state, document, source_ref, workspace,
                             fid=fid, step=step, budget=budget, session_id=session_id, client=client)
    view = gs.late_view(new)
    if view is not None and view["status"] not in gs.TRADE_PUT:
        # it is put in the refusal the submission returns, which is the model's next instruction
        new = gs.mark_asked(new, [view])
    await gs.save(owner_id, str(source_ref.source_id), new, session_id=session_id)
    return new


async def _planned(state: dict, document: dict, source_ref: Any, workspace: str, *, fid: str, step: dict,
                   budget: float, session_id: str, client: Any) -> dict:
    """One attempt at the plan, inside the directory the customer's file was fetched into."""
    local = the_bytes_again(source_ref, workspace, session_id)
    try:
        work = asyncio.to_thread(plan_the_part, state, document, fidelity=fid, job_id=session_id, client=client,
                                 source_path=local)
        return await (asyncio.wait_for(work, timeout=budget + THREAD_GRACE_S) if budget > 0 else work)
    except Exception as exc:                       # noqa: BLE001 - a timeout included
        reason = f"the geometry agent's step did not finish inside its budget: {type(exc).__name__}: {exc}"
        logger.warning("geometry step: %s", reason)
        return {**state, "geometry_step": {**step, "schema": STEP_SCHEMA, "status": FAILED,
                                           "for": plan_key(state, fid), "fidelity": fid, "reason": reason,
                                           "at": _now()}}


# -------------------------------------------------------------------------------------------------
# step 7: both write-ups, the Surveyor inside the typed block
# -------------------------------------------------------------------------------------------------

def builder_handoff(state: dict, document: dict, *, request_txt: str) -> dict:
    """Step 7: what the builder receives, validated by `contract.deliver.check_builder_handoff` before it
    leaves. Raises `StepRefused` with the reason whenever the stored plan cannot be used for this job.

    `request_prefix` is the geometry agent's write-up, `hexera.request_assembly`'s block sized against the
    platform's own `request_txt` so the two together fit the planner's 2,000-character read, and repeating
    the patch names when it pushes them past the cut. `typed` is the planner block composed for the
    customer's resolved budget and named inlet, with the survey, intake's write-up, the flow patches and
    the plan's envelope, all after the cut.

    WHEN THE TWO STILL DO NOT FIT the assembly says so, and so does this: on 3 of 49 corpus parts the
    write-up shortens to its floor and the last 31 to 100 characters of the brief fall past the cut. The
    assembly's own reading of those lines is `no reader`, the physics tail, and the budget sentence among
    them reaches the planner through the typed block's `customer_cell_cap` instead. That is a judgement
    the package makes and this module does not second-guess; what it does is say it out loud, because a
    line of the customer's brief that the planner never read is not a thing to find out later."""
    step = state.get("geometry_step") if isinstance(state, dict) else None
    if not isinstance(step, dict):
        raise StepRefused("the geometry agent did not plan this job: no plan was made before it was submitted")
    if step.get("status") != PLANNED:
        raise StepRefused(f"the geometry agent's step failed at submission: {step.get('reason') or 'no reason kept'}")
    fidelity = str(step.get("fidelity") or "standard")
    if step.get("for") != plan_key(state, fidelity):
        raise StepRefused("the plan was made for answers or a survey that have changed since, so it describes "
                          "another job")
    pkg = _package()
    cj = pkg["job"]
    inp = _inputs(state, document, fidelity=fidelity)
    given = _given(inp, state, with_late=True)
    plan = pkg["GeometryPlan"].model_validate(step["plan"])
    kwargs = {**gs.composed_inputs(state), "cell_cap": given.budget,
              "inlet_ids": cj.named_inlets(given, inp.facts)}
    final = gs.composition(document, **kwargs)["composed"].get("planner_block")
    unit = str(given.value("unit") or plan.unit.assumed)
    assembly = pkg["hexera"].request_assembly(plan, inp.facts, str(request_txt or ""), inp.engine, given.budget,
                                              given.representation, unit, inp.brief.text or None)
    _say_what_the_cut_takes(assembly)
    meta = dict(((step.get("ledger") or {}).get("meta")) or {})
    key = str(meta.get("key") or "")
    recs = _records(state, inp, key, {"part_key": meta.get("part_key", ""), "job_id": meta.get("job_id", ""),
                                      "provenance": meta.get("provenance", "live")})
    record = pkg["schema"].JobRecord(key=key, part_key=str(meta.get("part_key") or ""),
                                     job_id=str(meta.get("job_id") or ""),
                                     provenance=str(meta.get("provenance") or "live"),
                                     intake=recs["intake"], trade=_trade_record(state, key))
    return pkg["deliver"].builder_handoff(
        final, given, geometry_write_up=assembly["block"], intake_block=pkg["handover"].intake_block(record),
        plan_envelope=dict(step.get("envelope") or {}) or None,
        patches=pkg["deliver"].flow_patches([p.model_dump(mode="json") for p in plan.patches], given))


def _say_what_the_cut_takes(assembly: dict) -> None:
    """What the planner's 2,000-character read does not reach, in the assembly's own words. Never raises."""
    try:
        if not assembly.get("request_truncated"):
            return
        uncovered = [str(d.get("line") or "") for d in (assembly.get("displaced_uncovered") or [])]
        displaced = [f"{d.get('line')} ({d.get('status')})" for d in (assembly.get("displaced") or [])]
        where = logger.warning if uncovered else logger.info
        where("geometry step: the write-up and the request are %d characters and the planner reads 2,000, "
              "so %d fall past the cut; displaced: %s%s", assembly.get("chars"), assembly.get("lost_chars"),
              "; ".join(displaced) or "nothing the assembly names",
              f"; NOT CARRIED: {'; '.join(uncovered)}" if uncovered else "")
    except Exception as exc:                       # noqa: BLE001 - a log line is never worth a handoff
        logger.debug("geometry step: the cut could not be described (%s)", exc)


def request_with_write_up(handoff: dict, request_txt: str) -> str:
    """The geometry agent's write-up in front of the request, a blank line between, as the package's own
    `request_assembly` puts it."""
    prefix = str(handoff.get("request_prefix") or "")
    return f"{prefix}\n\n{request_txt or ''}" if prefix else str(request_txt or "")


def record_handover(state: dict, handoff: dict) -> dict:
    """The `handover` stage, once per distinct handoff: the builder asks for it on every plan call, and a
    ledger row per call would be the same row many times. Never raises."""
    try:
        digest = _sha(handoff)[:32]
        step = state.get("geometry_step") or {}
        if step.get("handover_sha") == digest:
            return state
        ledger = _reopen(state)
        if ledger is None:
            return state
        ledger.put("handover", handoff)
        ledger.flush()
        out = _append_events(state, ledger.events)
        out["geometry_step"]["handover_sha"] = digest
        return out
    except Exception as exc:                       # noqa: BLE001 - the builder is handed it either way
        logger.warning("geometry step: the handover did not reach the ledger (%s)", exc)
        return state


__all__ = ["FAILED", "FIDELITIES", "LATE_SCHEMA", "PLANNED", "PROVIDERS", "STEP_SCHEMA", "StepRefused", "at_submission",
           "builder_handoff", "late_handoff", "not_yet", "note_late_answer", "plan_key", "plan_the_part",
           "planner_client", "record_handover", "request_with_write_up", "step_enabled", "submission_problems",
           "the_bytes_again"]
