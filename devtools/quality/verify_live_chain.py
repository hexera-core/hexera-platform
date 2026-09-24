#!/usr/bin/env python3
# Responsibility: The verifier's own end-to-end read of the Surveyor chain on the live platform code path, written
#                 independently of `run_geometry_step_on_corpus.py` so that two tools have to agree before a claim stands.
# Boundaries: a developer harness. Every stage is the platform's own function called the way the product calls it.
#             This file supplies the customer's turn, the two stored reads, and a RECORDED look; it computes nothing itself.
#
# WHY A SECOND TOOL. The builders' harness is the only witness to gap A, and a harness that shares a blind spot with
# the thing it measures proves nothing. Ten times on this project a ruler had the same hole as its subject. So this
# one walks the same seams by hand, takes its own hashes, and differs from theirs in the two places theirs cannot see:
#
#   1. THE LOOK RUNS HERE. Their harness stores `look: not_attempted` on every row and says so; the `look_block`
#      branch of the step was therefore never exercised by any run, on any part. This file replays a REAL recorded
#      look - gpt-5.6-luna, the shipped `panels_stops` prompt, 17 views, with the render manifest's own drawn table -
#      through `geometry_vision.attach_look`, which is the function the look worker calls. No model is called: the
#      words were drawn from disk, so the run stays deterministic and repeatable.
#   2. THE PACKAGE'S OWN SWITCHES ARE ARMABLE. `--fluid-side` and `--measured-stops` set the two environment
#      switches gaps B and C sit behind, so this file can ask whether B and C reach the BUILDER and not only the
#      package's own eval.
#
#     python devtools/quality/verify_live_chain.py --cases tee_wye__tee_wye_001 --out DIR \
#         [--look off|replay] [--fluid-side] [--measured-stops] [--answer-fluid-side] \
#         [--cap N] [--take hold|raise|skip] [--purpose P] [--print CASE] [--provider reference]
#
# WHAT IS THE PRODUCT'S
#   geometry_measurement.measure_local_file     the upload's measurement of the real bytes
#   geometry_vision.attach_look                 step 3 landing on the stored document
#   geometry_survey.compose / carry_answers     step 2, composed for the customer's words
#   geometry_survey.open_now / answered         step 4, the questions and what an answer does
#   geometry_step.plan_the_part                 step 5, and the question step 6 raises
#   geometry_step.builder_handoff               step 7, through contract.deliver
#   cad.regions.planner_inputs_for_state        the call the snappy driver makes
#   snappy.planner.plan_with_accounting         the builder's message, composed by the platform
#
# WHAT IS THIS FILE'S
#   the customer's answers (SIMULATED from the corpus generator's answer key; every row says so)
#   the recorded look (REPLAYED from eval/vision/results/<run>/looks; no model call)
#   the two stored reads `planner_inputs_for_state` makes, answered from memory instead of Postgres
#   the planner model, which is NOT called: the message it would be handed is captured
#
# DETERMINISTIC unless --provider names a model.
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

CASES_DIR = Path(os.environ.get(
    "GEOMETRY_CORPUS_CASES", r"C:\Users\rehaa\hexera-platform-v2\gz_complete\eval\corpus_export_cases"))

#: Recorded looks, read only. `w_ctl` is the SHIPPED arm: prompt variant `panels_stops`, 17 views, gpt-5.6-luna,
#: the render manifest's drawn table on all 92 of its cases.
LOOKS_DIR = Path(os.environ.get(
    "GEOMETRY_RECORDED_LOOKS", r"C:\Users\rehaa\hexera-platform-v2\gz_complete\eval\vision\results\w_ctl\looks"))

OWNER = "verifier-live-chain"
SOURCE_PREFIX: tuple[str, str] = ("", "")
_BUDGET_LINE = re.compile(r"^Mesh budget:.*$", re.M)


def _mapped(source: str) -> str:
    head, tail = SOURCE_PREFIX
    text = str(source)
    if head and text.lower().startswith(head.lower()):
        return (tail + text[len(head):]).replace("\\", "/")
    return text


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


#: An ISO-8601 instant, which is the ONE thing in this chain that legitimately differs between two runs of
#: the same configuration: `geometry_survey._now()` stamps every answer with the time it was given, and the
#: provenance line the builder reads quotes it ("question role_inlet answered by customer at ..."). Two runs
#: of this file a minute apart gave the same 20,515-character message and two different digests for exactly
#: that reason. `planner_message_chars`, which is what the other harness reports, cannot see a digest move
#: at all; a digest that moves on the clock cannot be compared between runs. So both are taken.
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")


def _sha(payload: Any) -> str:
    """My own digest, over the bytes a reader would read: a string as itself, a dict canonically."""
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    else:
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _sha_stable(payload: Any) -> str:
    """The same digest with every instant masked, so two runs of one configuration can be compared."""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(_INSTANT.sub("<at>", text).encode("utf-8")).hexdigest()[:16]


def _arm(provider: str) -> None:
    """The model that plans. There are no package stages left to arm.

    This used to set two platform settings and assert that `policy.arm_the_package` had put them into the
    package's own GEOMETRY_AGENT_* variables, because a tool that wrote those variables by hand was blind to
    whether the platform's settings reached the package at all. Both readers are now deleted inside the
    package: the closed-end pass and the fluid-side reading run on every measurement, the two platform
    settings are retired, and a tool that still set them would be measuring a state this code cannot produce.
    """
    import meshpipeline.settings.policy as polcfg
    polcfg.GEOMETRY_AGENT_STEP_PROVIDER = provider


# -------------------------------------------------------------------------------------------------
# step 2: the bytes, measured once
# -------------------------------------------------------------------------------------------------

def measured(case: str, exp: dict, cache: Path, *, tag: str) -> dict:
    """`measure_local_file` on the real bytes, cached by sha, purpose AND the switches that change it.

    `tag` is in the key because `GEOMETRY_AGENT_MEASURED_STOPS` changes what `facts` carries; a cache keyed
    on the bytes alone would hand a stops-off document to a stops-on run and prove the opposite of the truth.
    """
    src = Path(_mapped(exp["source"]))
    if not src.is_file():
        raise FileNotFoundError(f"{case}: the corpus STEP file is not on this machine: {src}")
    sha = _sha256_of(src)
    purpose = str(exp.get("purpose") or "internal_cfd")
    keep = cache / f"{sha}.{purpose}.{tag}.json"
    if keep.is_file():
        return json.loads(keep.read_text(encoding="utf-8"))
    # imported HERE and not above, because the interpreter that can open STEP (trimesh, OCP) and the one
    # that has the database driver are two different ones on this machine: the first fills this cache and
    # the second runs the chain off it, and the second must not need the measurement's dependencies.
    from meshpipeline.application.geometry_measurement import measure_local_file
    doc = measure_local_file(
        src, purpose=purpose, unit=str(exp.get("unit_declared") or "") or None,
        source={"sha256": sha, "size_bytes": src.stat().st_size,
                "original_filename": src.name, "suffix_hint": src.suffix})
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text(json.dumps(doc), encoding="utf-8")
    return doc


def source_ref(doc: dict, case: str):
    from meshpipeline.contracts.geometry_source import GeometrySourceRef
    src = dict(doc.get("source") or {})
    return GeometrySourceRef(
        source_id=hashlib.sha256(case.encode()).hexdigest()[:32], owner_id=OWNER,
        object_key=f"uploads/{case}", sha256=str(src.get("sha256") or doc.get("source_sha256") or ""),
        size_bytes=int(src.get("size_bytes") or 1),
        original_filename=str(src.get("original_filename") or "part.step"),
        suffix_hint=str(src.get("suffix_hint") or ".step"))


# -------------------------------------------------------------------------------------------------
# step 3: the look, replayed
# -------------------------------------------------------------------------------------------------

def replayed_look(case: str, doc: dict) -> tuple[dict, dict] | tuple[None, dict]:
    """The stored document with a REAL recorded look attached, through the platform's own `attach_look`.

    The words, the model, the provider, the seconds and the drawn table are all read off the record; nothing
    here is written by me and no model is called. `views` is the COUNT in the record and not the list of view
    names, so the block's `views` list is empty and says so in the note; `trust.for_model` never reads it, so
    no model consumer sees the difference.
    """
    where = LOOKS_DIR / f"{case}.json"
    if not where.is_file():
        return None, {"look_replay": f"no recorded look on disk for {case}"}
    rec = json.loads(where.read_text(encoding="utf-8"))
    imp = dict(rec.get("impression") or {})
    if not imp:
        return None, {"look_replay": f"the recorded look for {case} carries no impression"}
    # fields the SHIPPED prompt does not ask for are not put in front of the platform. `w_ctl` is the shipped
    # arm so there are none; a fall-back run recorded with a killed arm would have them, and they go here.
    for killed in ("walk", "found", "checked_clear"):
        imp.pop(killed, None)
    from geometry_agent.agent import hexera

    from meshpipeline.application import geometry_vision as gv
    marks = [dict(m) for m in (rec.get("mark_places") or []) if isinstance(m, dict)]
    stops = [str(s) for s in (rec.get("stopped") or [])]
    block = hexera.look_block(
        imp, model=str(rec.get("model") or ""), provider=str(rec.get("provider") or ""),
        seconds=rec.get("seconds"), views=[],
        drawn={"marks": marks, "stops": stops} if (marks or stops) else None)
    note = {"look_replay": f"{LOOKS_DIR.parent.name}/{case}", "look_model": block["model"],
            "look_variant": str(rec.get("prompt_variant") or ""), "look_views_drawn": rec.get("views"),
            "look_prompt_sha256": str(rec.get("prompt_sha256") or ""),
            "look_marks": len(marks), "look_stops": stops}
    return gv.attach_look(doc, block), note


# -------------------------------------------------------------------------------------------------
# step 4: the simulated customer. Mine, written from the answer key, not taken from their harness.
# -------------------------------------------------------------------------------------------------

class KeyedCustomer:
    """Answers from the corpus generator's own `expected.json`. NOTHING HERE IS A PERSON'S ANSWER.

    role questions   the role of the port row that binds to that mouth, through `ask.settle.bind_declared`,
                     which is the package's own binder and the only honest way to read the key
    fluid_side       the side the generator built, when `--answer-fluid-side` is passed; otherwise skipped,
                     which is what a customer who says nothing produces
    everything else  skipped: the brief is the question's INPUT and cannot also be its answer
    """

    def __init__(self, exp: dict, document: dict, *, answer_side: bool):
        self.rows = [d for d in (exp.get("declared") or []) if isinstance(d, dict)]
        self.document = document
        self.answer_side = answer_side
        self.key_representation = str(exp.get("representation") or "")

    def _bound(self) -> dict[str, str]:
        from geometry_agent.ask import settle
        table = [r for r in (self.document.get("openings") or []) if isinstance(r, dict)]
        diag = (self.document.get("bbox") or {}).get("diagonal_mm")
        got = settle.bind_declared(table, self.rows, diag) or {}
        return {str(k): str((v or {}).get("role") or "") for k, v in got.items() if v}

    def reply(self, view: dict) -> dict | None:
        if view["about"] == "opening.role":
            bound = self._bound()
            if view["id"] == "role_count":
                for oid, role in sorted(bound.items()):
                    if oid in view["options"] and role:
                        return {"choice": oid, "role": role, "words": f"{oid} is the {role}"}
                return None
            wanted = view["id"][len("role_"):] if view["id"].startswith("role_") else ""
            for oid in view["options"]:
                if bound.get(oid) == wanted:
                    return {"choice": oid, "words": f"{oid} is the {wanted}"}
            return None
        if view["about"] == "representation" and view["id"] == "fluid_side" and self.answer_side:
            # The generator's own word for what the file contains says which of the two sides is the key's.
            # `carve` and `wall` (the corpus) and `through` (eval/hard_real) all mean the file is the metal
            # and the fluid is the bore; `is_fluid` and `around` mean the file is already the fluid. The
            # option picked is the QUESTION's own sentence, matched by the side it names and never invented
            # here: `contract.asking._fluid_side_uncertainty` writes one sentence per `catalog.FLUID_SIDES`
            # key, and this is the key that sentence belongs to.
            side = {"carve": "through", "wall": "through", "wall_shell": "through", "through": "through",
                    "is_fluid": "is_fluid", "around": "is_fluid"}.get(self.key_representation)
            if side is None:
                return None
            sentence = {"through": "the fluid runs through the bores; the part is the solid around it",
                        "is_fluid": "the part is the fluid itself, the volume the flow fills"}[side]
            for option in view["options"]:
                if option == sentence:
                    return {"choice": option, "words": option}
            return None
        return None


def put_the_questions(state: dict, doc: dict, who: KeyedCustomer, note: list[str]) -> dict:
    """Step 4 to exhaustion. Every open intake question put once, answered or SKIPPED, through the platform."""
    from meshpipeline.application import geometry_survey as gs
    seen: set[str] = set()
    for _ in range(40):
        now = [v for v in gs.open_now(state) if v["route"] == gs.ROUTE_INTAKE and v["id"] not in seen]
        if not now:
            return state
        for view in now:
            seen.add(view["id"])
            said = who.reply(view)
            words = str((said or {}).get("words") or f"I cannot say for {view['id']}")
            kwargs: dict[str, Any] = {"question_id": view["id"], "words": words,
                                      "latest_user_message": words, "principal": OWNER}
            if said is None:
                kwargs["skipped"] = True
                note.append(f"{view['id']} [{view['about']}]: SKIPPED (the key does not answer it)")
            else:
                kwargs.update({k: v for k, v in said.items() if k in ("choice", "role")})
                note.append(f"{view['id']} [{view['about']}]: {said.get('choice')}"
                            + (f" as {said['role']}" if said.get("role") else ""))
            state = gs.answered(state, doc, **kwargs)
    return state


def settle_the_trade(state: dict, doc: dict, note: list[str], take: str, *, route: str) -> dict:
    from meshpipeline.application import geometry_survey as gs
    wanted = gs.ROUTE_LATE if route == "third" else gs.ROUTE_TRADE
    for _ in range(4):
        now = [v for v in gs.open_now(state) if v["route"] == wanted]
        if not now:
            return state
        view = now[0]
        which = "third intake" if view["route"] == gs.ROUTE_LATE else "the survey's own trade"
        if take == "skip":
            note.append(f"{which} [{view['id']}]: SKIPPED")
            state = gs.answered(state, doc, question_id=view["id"], words="I would rather not say",
                                latest_user_message="I would rather not say", skipped=True, principal=OWNER)
            continue
        option = view["options"][0 if take == "hold" else -1]
        note.append(f"{which} [{view['id']}]: {option}")
        state = gs.answered(state, doc, question_id=view["id"], choice=option, words=option,
                            latest_user_message=option, principal=OWNER)
    return state


# -------------------------------------------------------------------------------------------------
# step 7: the builder's own read, through the repository's own mapping
# -------------------------------------------------------------------------------------------------

async def through_the_row(state: dict) -> tuple[dict, str]:
    """The state as the DATABASE would give it back. A harness double that keeps whatever it is handed
    agrees with every caller and is exactly how the plan and the late question were lost once before."""
    try:
        import uuid as _uuid

        from meshpipeline.persistence.repositories.geometry_survey_repository import (
            GeometrySurveyRepository,
            state_of,
        )
    except Exception as exc:                       # noqa: BLE001 - an absent driver is not a failed run
        return state, f"{type(exc).__name__}: {exc}"

    class _NoRowYet:
        async def execute(self, _statement):
            return self

        def scalar_one_or_none(self):
            return None

        def add(self, _row):
            return None

        async def flush(self):
            return None

    row = await GeometrySurveyRepository().record(
        _NoRowYet(), owner_id=OWNER, geometry_source_id=_uuid.uuid4(),
        sha256=str(state.get("sha256") or ""), state=state)
    return state_of(row), ""


async def what_the_builder_gets(state: dict, doc: dict, ref, request_txt: str) -> dict:
    import meshpipeline.cad.regions as regions
    from meshpipeline.application import geometry_survey as gs

    stored, no_row_mapping = await through_the_row(state)
    saved: dict[str, dict] = {"state": stored}

    async def _document(_ref, _digest):
        return doc

    async def _load(*_a, **_k):
        return saved["state"]

    async def _save(_owner, _source, new, **_k):
        saved["state"], _why = await through_the_row(new)
        return True

    before_doc, before_load, before_save = regions._stored_document, gs.load, gs.save
    regions._stored_document, gs.load, gs.save = _document, _load, _save
    try:
        pipeline_state = {"request_txt": request_txt, "geometry": {"ref": ref.to_payload()}}
        request, block, why = await regions.planner_inputs_for_state(pipeline_state)
    finally:
        regions._stored_document, gs.load, gs.save = before_doc, before_load, before_save
    return {"request": request, "block": block, "why": why, "state": saved["state"],
            "no_row_mapping": no_row_mapping}


async def planner_message(request: str, block: dict | None, workspace: Path, job_id: str) -> str:
    """The builder's planner message, composed by `plan_with_accounting`. The planner MODEL is not called."""
    from tests._geometry_support import prepared_surface

    import meshpipeline.engines.snappy.planner as planner
    from meshpipeline.contracts.model_inference import ModelRoundResult

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "input.stl").write_text("solid x\nendsolid x\n", encoding="utf-8")
    seen: dict[str, str] = {}

    async def call(messages, tools=None, tool_choice="none", job_id="", **_kw):
        seen["user"] = messages[1]["content"]
        seen["system"] = messages[0]["content"]
        return ModelRoundResult(assistant_text=json.dumps({"approach": "x", "max_cells": 1000}))

    import meshpipeline.cad.analysis as analysis
    import meshpipeline.capture.logger as capture
    before_call = planner.llm_router.call_planner_model
    before_log = capture.TrainingLogger
    before_a, before_r = analysis.analyze_surface, analysis.recommend_refinement
    planner.llm_router.call_planner_model = call
    capture.TrainingLogger = lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None)
    analysis.analyze_surface = lambda _stl: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 6.0,
                                             "min_feature": 0.01}
    analysis.recommend_refinement = lambda _a, **_k: {"surface_level": 2, "feature_level": 3,
                                                      "afford_level": 2}
    try:
        await planner.plan_with_accounting(
            surface=prepared_surface(workspace / "input.stl",
                                     source_id="11111111-1111-1111-1111-111111111111",
                                     interpretation_id="22222222-2222-2222-2222-222222222222"),
            workspace=workspace, job_id=job_id, request_txt=request, geometry_agent=block)
    finally:
        planner.llm_router.call_planner_model = before_call
        capture.TrainingLogger = before_log
        analysis.analyze_surface, analysis.recommend_refinement = before_a, before_r
    return seen.get("user", "")


# -------------------------------------------------------------------------------------------------
# one case, all seven steps
# -------------------------------------------------------------------------------------------------

async def run_case(case: str, out: Path, cache: Path, *, provider: str, take: str, look: str,
                   answer_side: bool,
                   fidelity: str, cap: int, purpose: str) -> dict:
    from meshpipeline.application import geometry_step as gst
    from meshpipeline.application import geometry_survey as gs

    started = time.perf_counter()
    exp = json.loads((CASES_DIR / case / "expected.json").read_text(encoding="utf-8"))
    brief = str(exp.get("brief") or "")
    if cap:
        brief, swaps = _BUDGET_LINE.subn(
            f"Mesh budget: keep the total cell count under {cap:,} cells.", brief)
        if not swaps:
            return {"case": case, "status": "harness_failed",
                    "reason": "this brief states no mesh budget, so there is none to replace"}
    if purpose:
        exp = {**exp, "purpose": purpose}

    # STEP 1 and 2: the upload and its measurement of the real bytes
    # BOTH PACKAGE STAGES ALWAYS RUN, so there is one shape of measurement and one cache key for it. The
    # tag used to carry the two switches; they were retired with the stages they selected, and the two
    # `args` that fed it were deleted from the parser while these lines still read them, so every case
    # ended `harness_failed` on an AttributeError before it measured anything.
    doc = measured(case, exp, cache, tag="stagesOn")
    if doc.get("status") != "ok":
        return {"case": case, "step": "measure", "status": doc.get("status"), "reason": doc.get("reason")}
    step_file = Path(_mapped(exp["source"]))
    ref = source_ref(doc, case)
    facts = doc.get("facts") or {}
    row: dict[str, Any] = {
        "case": case, "family": exp.get("family"), "purpose": exp.get("purpose"),
        "measured_openings": len(doc.get("openings") or []),
        "measured_passage_ends": len(facts.get("passage_ends") or []) if facts.get(
            "passage_ends") is not None else None,
        "document_has_passage_ends_key": "passage_ends" in doc,
        "document_fluid_side": doc.get("fluid_side"),
    }

    # STEP 3: the look, replayed from a real recorded one
    row["look_asked_for"] = look
    if look == "replay":
        attached, note = replayed_look(case, doc)
        row.update(note)
        if attached is not None:
            doc = attached
    row["look_status_on_the_document"] = str((doc.get("look") or {}).get("status") or "")

    # STEP 2 composed for the customer's words, with the look in it if it landed
    state = gs.carry_answers(None, gs.compose(
        doc, purpose=str(exp.get("purpose") or "internal_cfd"), brief=brief,
        engine=str(exp.get("engine") or "") or None, unit=str(exp.get("unit_declared") or "") or None))
    state = gs.mark_asked(state, gs.open_now(state))
    row["representation_composed"] = (state.get("composed_for") or {}).get("representation")
    row["look_status_in_the_composition"] = (state.get("composed_for") or {}).get("look_status")
    row["asked_at_step_4"] = [f"{v['id']}[{v['about']}]" for v in gs.open_now(state)]
    before_places = [p.get("kind") for p in
                     ((state.get("planner_block") or {}).get("places") or []) if isinstance(p, dict)]
    row["places_before_any_answer"] = before_places

    # STEP 4 in full: the Surveyor's questions, then the survey's own budget trade
    note: list[str] = []
    state = put_the_questions(state, doc, KeyedCustomer(exp, doc, answer_side=answer_side), note)
    state = settle_the_trade(state, doc, note, take, route="survey")
    row["answers"] = note
    row["representation_after_the_answers"] = (state.get("composed_for") or {}).get("representation")
    row["survey_still_waiting_when_it_planned"] = gst.not_yet(state)

    # STEP 5, and the question step 6 raises
    planned_at = time.perf_counter()
    state = gst.plan_the_part(state, doc, fidelity=fidelity, job_id=case,
                             client=gst.planner_client(provider), source_path=str(step_file))
    row["plan_seconds"] = round(time.perf_counter() - planned_at, 2)
    step = dict(state.get("geometry_step") or {})
    late_before = dict(state.get("late") or {})
    state = settle_the_trade(state, doc, note, take, route="third")

    # STEP 7: the builder's own read and the builder's own message
    got = await what_the_builder_gets(state, doc, ref, brief)
    message = await planner_message(got["request"], got["block"], out / case / "workspace", case)

    where = out / case
    where.mkdir(parents=True, exist_ok=True)
    (where / "planner_message.txt").write_text(message, encoding="utf-8")
    (where / "request.txt").write_text(got["request"], encoding="utf-8")
    (where / "typed_block.json").write_text(
        json.dumps(got["block"], indent=1, default=str, sort_keys=True), encoding="utf-8")
    (where / "survey_row.json").write_text(
        json.dumps(got["state"], indent=1, default=str, sort_keys=True), encoding="utf-8")
    events = ((got["state"].get("geometry_step") or {}).get("ledger") or {}).get("events") or []
    (where / "ledger.jsonl").write_text(
        "\n".join(json.dumps(e, default=str, sort_keys=True) for e in events), encoding="utf-8")

    block = got["block"] or {}
    places = [p.get("kind") for p in (block.get("places") or []) if isinstance(p, dict)]
    ends = [p for p in (block.get("places") or [])
            if isinstance(p, dict) and str(p.get("found_by") or "").endswith("passage_ends")]
    row.update({
        "status": step.get("status"), "reason": (step.get("reason") or "")[:300],
        "exit": step.get("exit"), "rounds": step.get("rounds"),
        "provider": step.get("provider"), "model": step.get("model"),
        "envelope_cells_high": (step.get("envelope") or {}).get("cells_high"),
        "envelope_cap": (step.get("envelope") or {}).get("cap"),
        "envelope_source": (step.get("envelope") or {}).get("source"),
        "flow_patches": len(step.get("flow_patches") or []),
        "third_intake": bool(late_before), "third_intake_id": late_before.get("id", ""),
        "step_used_by_the_builder": not got["why"], "why_not": got["why"][:300],
        "the_row_mapping_ran": not got["no_row_mapping"], "no_row_mapping": got["no_row_mapping"][:200],
        "request_chars": len(got["request"]),
        "brief_survives_the_2000_cut": bool(brief) and brief.strip()[-30:] in got["request"][:2000],
        "typed_keys": sorted(block.keys()),
        "typed_places": places,
        "typed_places_from_measured_ends": [p.get("kind") for p in ends],
        "typed_survey_keys": sorted((block.get("survey") or {}).keys()),
        "typed_look_present": bool(block.get("look")),
        "typed_look_keys": sorted((block.get("look") or {}).keys()),
        "typed_customer_cell_cap": block.get("customer_cell_cap"),
        "typed_plan_envelope": block.get("plan_envelope"),
        "typed_flow_patches": len(block.get("flow_patches") or []),
        "ledger_stages": [e.get("event") for e in events],
        "planner_message_chars": len(message),
        # MY OWN HASHES, over the bytes a reader reads. `_stable` masks the instants a real answer
        # legitimately carries, and is the one to compare two runs of one configuration with.
        "sha_request": _sha(got["request"]),
        "sha_typed_block": _sha(got["block"]),
        "sha_planner_message": _sha(message),
        "sha_request_stable": _sha_stable(got["request"]),
        "sha_typed_block_stable": _sha_stable(got["block"]),
        "sha_planner_message_stable": _sha_stable(message),
        "seconds": round(time.perf_counter() - started, 2),
    })
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default="")
    ap.add_argument("--provider", default="reference")
    ap.add_argument("--take", default="skip", choices=("hold", "raise", "skip"))
    ap.add_argument("--look", default="off", choices=("off", "replay"))
    ap.add_argument("--looks-dir", default="")
    # `--fluid-side` AND `--measured-stops` ARE GONE. They set two platform settings which armed two variables
    # the measurement package no longer reads: both stages run on every measurement, so the flags selected
    # nothing and a run that passed one was measuring a state this code cannot produce.
    ap.add_argument("--answer-fluid-side", action="store_true",
                    help="the simulated customer answers the fluid-side question as the key would")
    ap.add_argument("--source-prefix", default="", metavar="FROM=TO")
    ap.add_argument("--purpose", default="")
    ap.add_argument("--cap", type=int, default=0)
    ap.add_argument("--fidelity", default="standard", choices=("draft", "standard", "max"))
    ap.add_argument("--print", default="")
    args = ap.parse_args()

    if args.source_prefix:
        global SOURCE_PREFIX
        head, _, tail = args.source_prefix.partition("=")
        SOURCE_PREFIX = (head, tail)
    if args.looks_dir:
        global LOOKS_DIR
        LOOKS_DIR = Path(args.looks_dir)
    _arm(args.provider)

    # THE PYTHON TRAP, asserted rather than trusted: the venv resolves both packages to the LIVE trees
    # unless my two `src` directories lead PYTHONPATH, and a run that resolved the wrong one measures
    # somebody else's tree.
    import geometry_agent

    import meshpipeline
    print(f"geometry_agent {geometry_agent.__file__}")
    print(f"meshpipeline   {meshpipeline.__file__}")
    from geometry_agent.learn import store as learn_store
    print(f"learn.store    ROOT={learn_store.ROOT}")
    # The two package stages are not switches any more, so there is nothing to print about them: they run.
    print(f"switches       look={args.look} provider={args.provider}")

    out = Path(args.out).resolve()
    cache = Path(args.cache).resolve() if args.cache else out / ".measured"
    rows = []
    for case in [c.strip() for c in args.cases.split(",") if c.strip()]:
        try:
            row = asyncio.run(run_case(
                case, out, cache, provider=args.provider, take=args.take, look=args.look,
                answer_side=args.answer_fluid_side, fidelity=args.fidelity, cap=args.cap,
                purpose=args.purpose))
        except Exception as exc:                   # noqa: BLE001 - one case never stops the run
            import traceback
            row = {"case": case, "status": "harness_failed",
                   "reason": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()[-1200:]}
        rows.append(row)
        print(f"{row.get('case'):46s} {str(row.get('representation_composed') or '-'):14s} "
              f"{str(row.get('status')):9s} look={row.get('look_status_on_the_document') or '-'} "
              f"ends={row.get('measured_passage_ends')} "
              f"env={row.get('envelope_cells_high')}/{row.get('envelope_cap')} "
              f"late={row.get('third_intake')} used={row.get('step_used_by_the_builder')} "
              f"msg={row.get('planner_message_chars')}/{row.get('sha_planner_message_stable')}")
        if row.get("reason"):
            print(f"    reason: {row['reason']}")
        if row.get("traceback"):
            print(f"    {row['traceback']}")
        if row.get("why_not"):
            print(f"    the builder did not use it: {row['why_not']}")
        if row.get("no_row_mapping"):
            print(f"    NOTE the row mapping did not run here: {row['no_row_mapping']}")

    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    if args.print:
        where = out / args.print
        print("\n" + "=" * 100 + f"\nWHAT THE BUILDER RECEIVES FOR {args.print}\n" + "=" * 100)
        print("\n--- 1. the request the planner reads, in full (the planner cuts it at 2,000) ---\n")
        print((where / "request.txt").read_text(encoding="utf-8"))
        print("\n--- 2. the typed block, in full (a dict argument, after the cut) ---\n")
        print((where / "typed_block.json").read_text(encoding="utf-8"))
        print("\n--- 3. the builder's planner message, in full ---\n")
        print((where / "planner_message.txt").read_text(encoding="utf-8"))
        print("\n--- 4. the job ledger ---\n")
        print((where / "ledger.jsonl").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
