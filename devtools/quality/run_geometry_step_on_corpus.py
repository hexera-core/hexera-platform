#!/usr/bin/env python3
# Responsibility: Run real corpus parts through THIS platform's geometry-agent step with the switch on, and print what the builder is handed.
# Boundaries: a developer harness. Every stage below is the platform's own function, called the way the product calls
#              it; this file supplies the customer's turn and the two database reads, and computes nothing itself.
#
# WHY IT EXISTS. `geometry_step.py` is the platform's step 5 and 6, and the only thing that proves it is a real
# part going in one end and the builder's own message coming out the other. Unit tests pin the seams; this
# measures the whole of it, on files the customer's CAD system wrote.
#
#     python devtools/quality/run_geometry_step_on_corpus.py --cases venturi_orifice__venturi_orifice_001 \
#         --out DIR [--print CASE] [--provider reference]
#
# WHAT IS THE PRODUCT'S AND WHAT IS THIS FILE'S, because a harness that quietly does the work it is measuring
# proves nothing:
#
#   the product's   `geometry_measurement.measure_local_file`   the upload's own measurement of the bytes
#                   `geometry_survey.compose`                   step 2, the measurement composed for the brief
#                   `geometry_survey.open_now` / `answered`     step 4, the questions and what an answer does
#                   `geometry_step.plan_the_part`               step 5, and the question step 6 raises
#                   `geometry_step.builder_handoff`             step 7, through `contract.deliver`
#                   `cad.regions.planner_inputs_for_state`      the call the BUILDER makes, run as it is written
#                   `snappy.planner.plan_with_accounting`       the builder's message, composed by the platform
#
#   this file's     the customer's answers, looked up in the corpus generator's own brief (SIMULATED: nothing
#                   below is a person's answer, and every row says so)
#                   the two stored reads `planner_inputs_for_state` makes, answered from memory instead of
#                   Postgres, which is the same seam `check_vision_off_is_byte_identical.py` patches
#                   the planner model, which is NOT called: the message it would be handed is captured
#
# THE LOOK DOES NOT RUN HERE. `measure_local_file` stores `not_attempted`, and step 3 is a worker. Every row
# says `look: not_attempted`, and the closed ends that only the look names are therefore absent, which is a
# known hole in the product and not one this harness introduces.
#
# DETERMINISTIC unless `--provider` names a model. With `reference`, the package's own stand-in policy plans
# and the ledger rows say `heuristic`; every number below is then reproducible from the bytes.
#
# RUN ON 2026-09-22, 49 parts, `--provider reference`, the platform at `049137d`:
#   planned 49 of 49, every one `submitted`; reached the builder with the plan 49 of 49
#   representations wall_shell 21, external 13, fluid_domain 13, annular_fluid 2 (and `unknown` on a run
#     with `--purpose internal_cfd`, which is the only way this corpus reaches that word)
#   the typed block carried the survey, intake's write-up, the flow patches and the plan's envelope on all 49
#   the envelope's source was `tools.estimate_builder_cells` on all 49
#   the ledger's stages were the same seven on all 49
#   the customer's brief survived the planner's 2,000-character read on 46 of 49
#   one plan: median 0.21 s, longest 4.13 s
#   the third intake fired on none of them; `--cap 750000` on manifold_001 puts it, and section "The
#     geometry agent's step" of docs/reference/configuration.md says why that is where it lives
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
# the repository root as well as `src`: `tests._geometry_support.prepared_surface` is the one place the
# platform builds the surface record the planner takes, and this harness must hand it the same one
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

#: Rehaan's own 322-case export, read only: one directory per case with the generator's `expected.json`,
#: which names the STEP file, the brief the customer would have written, and the port rows that are the
#: ANSWER KEY the simulated customer looks its replies up in.
CASES_DIR = Path(os.environ.get("GEOMETRY_CORPUS_CASES",
                                r"C:\Users\rehaa\hexera-platform-v2\gz_complete\eval\corpus_export_cases"))

OWNER = "corpus-harness-owner"

#: `expected.json` names the STEP file by the absolute path the export was written on. Under WSL, which
#: is where the platform's own dependencies live, that path is the same file under `/mnt/c`, so
#: `--source-prefix FROM=TO` rewrites the head of it. It maps a path to the SAME BYTES on the same disk;
#: the sha256 is still taken from the file that is opened, so a mapping to anything else measures
#: something else and the row says which file it read.
SOURCE_PREFIX: tuple[str, str] = ("", "")

#: The corpus generator writes one budget sentence into every brief, and `--cap` rewrites that sentence.
#: THE ONLY THING IT CHANGES IS A NUMBER THE CUSTOMER WROTE. The geometry is the corpus part's, the
#: measurement is the platform's, and the budget is as real a customer input as the one it replaces: it
#: is where the third intake lives, because that question exists exactly when the plan's envelope is over
#: a budget the measurement's own forecast was under, and the export's briefs all state 2,000,000 where
#: the whole corpus forecasts less.
_BUDGET_LINE = re.compile(r"^Mesh budget:.*$", re.M)


def _mapped(source: str) -> str:
    """The corpus STEP file's path on THIS machine."""
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


def _arm(provider: str) -> None:
    """Every gate the step sits under, set the way an operator would set them in `.env`."""
    import meshpipeline.settings.policy as polcfg
    polcfg.GEOMETRY_MEASUREMENT_ENABLED = True
    polcfg.GEOMETRY_REPORT_READERS_ENABLED = True
    polcfg.GEOMETRY_SURVEY_ENABLED = True
    polcfg.GEOMETRY_AGENT_STEP_ENABLED = True
    polcfg.GEOMETRY_AGENT_STEP_PROVIDER = provider


# -------------------------------------------------------------------------------------------------
# step 2: the bytes, measured once and kept
# -------------------------------------------------------------------------------------------------

def measured(case: str, exp: dict, cache: Path) -> dict:
    """The stored measurement document, from `measure_local_file`, cached by sha and purpose.

    The cache is this harness's, not the product's: measuring 300 MB of STEP again for every run is the
    whole cost of a run, and the document is a pure function of the bytes and the purpose.
    """
    from meshpipeline.application.geometry_measurement import measure_local_file
    src = Path(_mapped(exp["source"]))
    if not src.is_file():
        raise FileNotFoundError(f"{case}: the corpus STEP file is not on this machine: {src}")
    sha = _sha256_of(src)
    purpose = str(exp.get("purpose") or "internal_cfd")
    keep = cache / f"{sha}.{purpose}.json"
    if keep.is_file():
        return json.loads(keep.read_text(encoding="utf-8"))
    doc = measure_local_file(
        src, purpose=purpose, unit=str(exp.get("unit_declared") or "") or None,
        source={"sha256": sha, "size_bytes": src.stat().st_size,
                "original_filename": src.name, "suffix_hint": src.suffix})
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text(json.dumps(doc), encoding="utf-8")
    return doc


def source_ref(exp: dict, doc: dict, case: str):
    from meshpipeline.contracts.geometry_source import GeometrySourceRef
    src = dict(doc.get("source") or {})
    return GeometrySourceRef(
        source_id=hashlib.sha256(case.encode()).hexdigest()[:32], owner_id=OWNER,
        object_key=f"uploads/{case}", sha256=str(src.get("sha256") or doc.get("source_sha256") or ""),
        size_bytes=int(src.get("size_bytes") or 1), original_filename=str(src.get("original_filename") or "part.step"),
        suffix_hint=str(src.get("suffix_hint") or ".step"))


# -------------------------------------------------------------------------------------------------
# step 4: the simulated customer
# -------------------------------------------------------------------------------------------------

class SimulatedCustomer:
    """Answers the Surveyor's questions from the corpus generator's own brief and port rows.

    NOTHING HERE IS A PERSON'S ANSWER, and every row this harness writes says so. The rules are the ones
    `eval/chain/run_chain.BriefCustomer` uses in the package, read from the package where they exist:

      a role question  the role of the port row that binds to that mouth (`ask.settle.bind_declared`,
                       the package's own binder), and `wall` for a mouth no row names when the brief
                       says every remaining surface is the wall
      the budget trade the brief's budget sentence is the question's INPUT, so the brief cannot also be
                       its answer: it is skipped, and the ledger records a default that stood
      anything else    skipped, for the same reason: the brief does not answer it
    """

    def __init__(self, exp: dict, document: dict):
        self.brief = str(exp.get("brief") or "")
        self.rows = [d for d in (exp.get("declared") or []) if isinstance(d, dict)]
        self.document = document

    def _bound(self) -> dict[str, str]:
        """`{opening_id: role}` from the port rows, through the package's own binder."""
        from geometry_agent.ask import settle
        table = [r for r in (self.document.get("openings") or []) if isinstance(r, dict)]
        diag = (self.document.get("bbox") or {}).get("diagonal_mm")
        got = settle.bind_declared(table, self.rows, diag) or {}
        return {str(k): str((v or {}).get("role") or "") for k, v in got.items() if v}

    def reply(self, view: dict) -> dict | None:
        """The arguments for `geometry_survey.answered`, or None to skip the question."""
        bound = self._bound()
        if view["about"] == "opening.role":
            if view["id"] == "role_count":
                for oid, role in sorted(bound.items()):
                    if oid in view["options"] and role:
                        return {"choice": oid, "role": role,
                                "words": f"{oid} is the {role}"}
                return None
            wanted = view["id"][len("role_"):] if view["id"].startswith("role_") else ""
            for oid in view["options"]:
                if bound.get(oid) == wanted:
                    return {"choice": oid, "words": f"{oid} is the {wanted}"}
            return None
        return None


def put_the_questions(state: dict, doc: dict, who: SimulatedCustomer, note: list[str]) -> dict:
    """Step 4 to exhaustion: every open question put once, answered or skipped, through the platform.

    A question the simulated customer cannot answer is SKIPPED, not defaulted, because a default that
    stood would be recorded as the customer having been asked and having declined to choose; skipping is
    what the product's own tool records for a customer who does not answer.
    """
    from meshpipeline.application import geometry_survey as gs
    seen: set[str] = set()
    while True:
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
                note.append(f"{view['id']}: skipped (the brief does not answer it)")
            else:
                kwargs.update({k: v for k, v in said.items() if k in ("choice", "role")})
                note.append(f"{view['id']}: {said.get('choice')}"
                            + (f" as {said['role']}" if said.get("role") else ""))
            state = gs.answered(state, doc, **kwargs)
    return state


def settle_the_trade(state: dict, doc: dict, note: list[str], take: str, *, route: str) -> dict:
    """A budget trade, put and settled. `take` is `hold`, `raise` or `skip`.

    `route` is `survey` for the survey's own trade, which is part of the customer's step-4 turn and has
    to be settled BEFORE the geometry agent plans, and `third` for the one only a plan can raise.
    """
    from meshpipeline.application import geometry_survey as gs
    wanted = gs.ROUTE_LATE if route == "third" else gs.ROUTE_TRADE
    for _ in range(4):
        now = [v for v in gs.open_now(state) if v["route"] == wanted]
        if not now:
            return state
        view = now[0]
        which = "the third intake" if view["route"] == gs.ROUTE_LATE else "the survey's trade"
        if take == "skip":
            note.append(f"{which} [{view['id']}]: skipped")
            state = gs.answered(state, doc, question_id=view["id"], words="I would rather not say",
                                latest_user_message="I would rather not say", skipped=True, principal=OWNER)
            continue
        option = view["options"][0 if take == "hold" else -1]
        words = f"{option}"
        note.append(f"{which} [{view['id']}]: {option}")
        state = gs.answered(state, doc, question_id=view["id"], choice=option, words=words,
                            latest_user_message=words, principal=OWNER)
    return state


# -------------------------------------------------------------------------------------------------
# step 7: the builder's own read, and the builder's own message
# -------------------------------------------------------------------------------------------------

async def through_the_row(state: dict) -> tuple[dict, str]:
    """The state as the DATABASE would give it back, and why not when it cannot be had here.

    THE REASON THIS IS NOT A PLAIN DICT. `GeometrySurveyRepository.record` writes a named list of keys
    and silently drops any other, so a state that round-trips through a harness holding it in memory can
    still lose half of itself in production: the geometry agent's plan and its third-intake question
    were both lost exactly that way, and nothing above the repository could see it, this harness least
    of all, because a double that keeps whatever it is handed agrees with every caller. So the state
    goes through the repository's own mapping here, against a session that touches no database.

    Where sqlalchemy is not installed - the measurement package's interpreter, which is the one that can
    read STEP - the mapping cannot run, and the run says so rather than quietly proving less.
    """
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
    """`cad.regions.planner_inputs_for_state`, the call the snappy driver makes, run as it is written.

    The two stored reads it makes are answered from this run instead of from Postgres, which is the seam
    `check_vision_off_is_byte_identical.py` patches for the same reason. Everything between them - the
    gate, the contract, the handoff, the fallback - is the product's.

    What the write GIVES BACK is the repository's own mapping of the state where that can run here, not
    the dict it was handed: see `through_the_row`.
    """
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
    """The builder's planner message, composed by the platform's own `plan_with_accounting`, captured.

    The planner MODEL is not called. What is checked is the message it would be handed, which is the
    whole of what this change moves.
    """
    from tests._geometry_support import prepared_surface

    import meshpipeline.engines.snappy.planner as planner
    from meshpipeline.contracts.model_inference import ModelRoundResult

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "input.stl").write_text("solid x\nendsolid x\n", encoding="utf-8")
    seen: dict[str, str] = {}

    async def call(messages, tools=None, tool_choice="none", job_id="", **_kw):
        seen["user"] = messages[1]["content"]
        return ModelRoundResult(assistant_text=json.dumps({"approach": "x", "max_cells": 1000}))

    # every seam the planner reaches out through, patched at the module it imports FROM, because that is
    # where `plan_with_accounting` looks them up: the surface analysis (this harness has no real STL), the
    # training record (a developer run writes none) and the model itself
    import meshpipeline.cad.analysis as analysis
    import meshpipeline.capture.logger as capture
    before_call = planner.llm_router.call_planner_model
    before_log = capture.TrainingLogger
    before_a, before_r = analysis.analyze_surface, analysis.recommend_refinement
    planner.llm_router.call_planner_model = call
    capture.TrainingLogger = lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None)
    analysis.analyze_surface = lambda _stl: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 6.0,
                                             "min_feature": 0.01}
    analysis.recommend_refinement = lambda _a, **_k: {"surface_level": 2, "feature_level": 3, "afford_level": 2}
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
# one case
# -------------------------------------------------------------------------------------------------

async def run_case(case: str, out: Path, cache: Path, *, provider: str, take: str,
                   fidelity: str = "standard", cap: int = 0, purpose: str = "") -> dict:
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
        # what the CUSTOMER says the analysis is for, which is step 1 and decides the representation.
        # A solid bluff body whose owner says "internal flow" is read `unknown`, and that is the only
        # way this corpus reaches that word: every case's own purpose is the one its shape was cut for
        exp = {**exp, "purpose": purpose}
    doc = measured(case, exp, cache)
    if doc.get("status") != "ok":
        return {"case": case, "step": "measure", "status": doc.get("status"), "reason": doc.get("reason")}
    ref = source_ref(exp, doc, case)
    note: list[str] = []

    # step 2: composed for what the customer said. No ports are declared here, because the customer has
    # not named any in a form the platform owns: their roles are the Surveyor's questions, below.
    state = gs.carry_answers(None, gs.compose(
        doc, purpose=str(exp.get("purpose") or "internal_cfd"), brief=brief,
        engine=str(exp.get("engine") or "") or None, unit=str(exp.get("unit_declared") or "") or None))
    state = gs.mark_asked(state, gs.open_now(state))
    asked_at_four = [v["id"] for v in gs.open_now(state)]

    # STEP 4 IN FULL, the Surveyor's questions and then the survey's own budget trade, because both are
    # the customer's turn and step 5 plans from their answers (`geometry_step.not_yet`).
    state = put_the_questions(state, doc, SimulatedCustomer(exp, doc), note)
    state = settle_the_trade(state, doc, note, take, route="survey")
    waiting = gst.not_yet(state)

    # step 5, and the question step 6 raises
    planned_at = time.perf_counter()
    state = gst.plan_the_part(state, doc, fidelity=fidelity, job_id=case,
                              client=gst.planner_client(provider))
    plan_seconds = round(time.perf_counter() - planned_at, 2)
    step = dict(state.get("geometry_step") or {})
    late_before = dict(state.get("late") or {})
    state = settle_the_trade(state, doc, note, take, route="third")

    # step 7, through the builder's own read
    got = await what_the_builder_gets(state, doc, ref, brief)
    message = await planner_message(got["request"], got["block"], out / case / "workspace", case)

    where = out / case
    where.mkdir(parents=True, exist_ok=True)
    (where / "planner_message.txt").write_text(message, encoding="utf-8")
    (where / "request.txt").write_text(got["request"], encoding="utf-8")
    (where / "typed_block.json").write_text(json.dumps(got["block"], indent=1, default=str), encoding="utf-8")
    (where / "survey_row.json").write_text(json.dumps(got["state"], indent=1, default=str), encoding="utf-8")
    events = ((got["state"].get("geometry_step") or {}).get("ledger") or {}).get("events") or []
    (where / "ledger.jsonl").write_text(
        "\n".join(json.dumps(e, default=str) for e in events), encoding="utf-8")

    block = got["block"] or {}
    places = [p.get("kind") for p in (block.get("places") or []) if isinstance(p, dict)]
    return {
        "case": case, "family": exp.get("family"), "purpose": exp.get("purpose"), "fidelity": fidelity,
        "budget_the_customer_stated": cap or "the corpus brief's own",
        "representation": (state.get("composed_for") or {}).get("representation"),
        "look": str((doc.get("look") or {}).get("status") or ""),
        "asked_at_step_4": asked_at_four, "answers": note,
        "survey_still_waiting_when_it_planned": waiting,
        "status": step.get("status"), "reason": (step.get("reason") or "")[:300],
        "exit": step.get("exit"), "rounds": step.get("rounds"),
        "sent_back_by_the_survey": step.get("sent_back_by_the_survey"),
        "provider": step.get("provider"), "model": step.get("model"),
        "envelope_cells_high": (step.get("envelope") or {}).get("cells_high"),
        "envelope_cap": (step.get("envelope") or {}).get("cap"),
        "envelope_source": (step.get("envelope") or {}).get("source"),
        "flow_patches": len(step.get("flow_patches") or []),
        "third_intake": bool(late_before), "third_intake_id": late_before.get("id", ""),
        "trade_skipped_because": step.get("trade_skipped_because", ""),
        "step_used_by_the_builder": not got["why"], "why_not": got["why"][:300],
        "the_row_mapping_ran": not got["no_row_mapping"], "no_row_mapping": got["no_row_mapping"][:200],
        "request_chars": len(got["request"]), "prefix_chars": len(got["request"]) - len(brief) - 2,
        "brief_survives_the_2000_cut": bool(brief) and brief.strip()[-30:] in got["request"][:2000],
        "typed_keys": sorted(block.keys()),
        "typed_places": places,
        "typed_customer_cell_cap": block.get("customer_cell_cap"),
        "typed_plan_envelope": bool(block.get("plan_envelope")),
        "typed_flow_patches": len(block.get("flow_patches") or []),
        "typed_intake_write_up_chars": len(str((block.get("intake") or {}).get("write_up") or "")),
        "typed_survey": bool(block.get("survey")),
        "planner_message_chars": len(message),
        "ledger_stages": [e.get("event") for e in events],
        "plan_seconds": plan_seconds, "seconds": round(time.perf_counter() - started, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, help="comma separated case directory names")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default="", help="where measured documents are kept (default: --out/.measured)")
    ap.add_argument("--provider", default="reference",
                    help="GEOMETRY_AGENT_STEP_PROVIDER for the run; `reference` is deterministic")
    ap.add_argument("--take", default="skip", choices=("hold", "raise", "skip"),
                    help="what the simulated customer does with a budget trade")
    ap.add_argument("--source-prefix", default="", metavar="FROM=TO",
                    help="rewrite the head of the STEP paths in expected.json, for running the same "
                         "corpus from another mount, as WSL mounts the Windows disk under /mnt/c")
    ap.add_argument("--purpose", default="",
                    help="the purpose the customer states, instead of the corpus case's own")
    ap.add_argument("--cap", type=int, default=0,
                    help="rewrite the brief's own budget sentence to this many cells: the customer's "
                         "number, and the only thing it changes")
    ap.add_argument("--fidelity", default="standard", choices=("draft", "standard", "max"),
                    help="the mesh fidelity the customer asked for at submission, which the geometry "
                         "agent plans against")
    ap.add_argument("--print", default="", help="print this case's builder message and typed block in full")
    args = ap.parse_args()

    if args.source_prefix:
        global SOURCE_PREFIX
        head, _, tail = args.source_prefix.partition("=")
        SOURCE_PREFIX = (head, tail)
    _arm(args.provider)
    import geometry_agent
    print(f"geometry_agent {geometry_agent.__file__}")
    import meshpipeline
    print(f"meshpipeline   {meshpipeline.__file__}")

    out = Path(args.out).resolve()
    cache = Path(args.cache).resolve() if args.cache else out / ".measured"
    rows = []
    for case in [c.strip() for c in args.cases.split(",") if c.strip()]:
        try:
            row = asyncio.run(run_case(case, out, cache, provider=args.provider, take=args.take,
                                       fidelity=args.fidelity, cap=args.cap, purpose=args.purpose))
        except Exception as exc:                   # noqa: BLE001 - one case never stops the run
            row = {"case": case, "status": "harness_failed", "reason": f"{type(exc).__name__}: {exc}"}
        rows.append(row)
        print(f"{row.get('case'):48s} {str(row.get('representation') or '-'):14s} "
              f"{str(row.get('status')):9s} exit={row.get('exit')} rounds={row.get('rounds')} "
              f"env={row.get('envelope_cells_high')}/{row.get('envelope_cap')} "
              f"trade={row.get('third_intake')} builder_used={row.get('step_used_by_the_builder')} "
              f"msg={row.get('planner_message_chars')} {row.get('plan_seconds')}s")
        if row.get("reason"):
            print(f"    reason: {row['reason']}")
        if row.get("why_not"):
            print(f"    the builder did not use it: {row['why_not']}")
        if row.get("no_row_mapping"):
            print(f"    NOTE: the row mapping did not run here, so what the database keeps is not "
                  f"proved by this run: {row['no_row_mapping']}")

    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    if args.print:
        where = out / args.print
        print("\n" + "=" * 100 + f"\nWHAT THE BUILDER RECEIVES FOR {args.print}\n" + "=" * 100)
        print("\n--- 1. the request the planner reads, in full (the planner cuts it at 2,000) ---\n")
        print((where / "request.txt").read_text(encoding="utf-8"))
        print("\n--- 2. the typed block, in full (a dict key, added AFTER the cut) ---\n")
        print((where / "typed_block.json").read_text(encoding="utf-8"))
        print("\n--- 3. the builder's planner message, in full ---\n")
        print((where / "planner_message.txt").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
