#!/usr/bin/env python3
# Responsibility: Prove by hashing that the look and the survey change nothing until they are switched on.
# Boundaries: a repository gate; it runs the real intake turn and the real planner read in four checkouts and
#              compares digests. It calls no provider, touches no database and writes nothing outside a
#              temporary directory.
#
# WHY A GATE AND NOT A UNIT TEST. The claim is about what the models are handed BEFORE and AFTER a commit,
# so it can only be checked by producing it in the older checkout as well. A test in this checkout can
# assert that no `look` or `survey` key is present; it cannot assert that the bytes are the ones the old
# code produced, because the old code is not importable from here.
#
# THROUGH THE REAL PATHS, NOT BESIDE THEM. The first version of this gate rendered `render_block(doc)` and
# `block_for_document(doc)` directly, so it could not see anything that happens on the way to them: which
# tools intake is offered, whether `node_intake` composes the block it always composed, and whether the
# driver's own read of the stored row hands the planner the block it always handed it. The survey adds a
# tool list, a prompt block and a planner key on exactly those paths. So the harness now runs
# `node_intake` on a state built by `api/v1/chat._build_intake_state`, the one the chat route builds, with
# the model call captured, and asks the snappy driver's own `_agent_block` for the planner's key. The
# stored rows it reads are answered from the document named below and nothing reaches a network.
#
# WHAT IS COMPARED: the intake system prompt, the exact tool list intake is offered, and the planner's
# user message. The planner's two inputs are taken from the driver's OWN read for the checkout under
# test - `_planner_inputs` where it exists, `_agent_block` where it does not - because the geometry
# agent's step changes the request as well as the block, and a gate that read only the block would
# report "identical" for the half of the change it never looked at.
#
#   A  every geometry gate off, this checkout against `main`
#   B  GEOMETRY_MEASUREMENT_ENABLED and GEOMETRY_REPORT_READERS_ENABLED on, the look and the survey
#      off, against the base of the measurement work
#   C  B with GEOMETRY_VISION_ENABLED on, the survey off, against the commit that added the look. The
#      stored look is `not_attempted`, so it describes nothing
#   D  C with GEOMETRY_SURVEY_ENABLED on, against the same commit. This one MUST DIFFER, in the tool
#      list and the intake prompt: it is the check that the harness can see the survey at all. A ruler
#      that reports "identical" for a change it cannot see is how this project has been misled before
#   E  the survey's whole chain on and GEOMETRY_AGENT_STEP_ENABLED off, against `3ba42c0`, the first
#      complete Surveyor. Not against an empty row: the row it reads is a conversation answered to the
#      end with the geometry agent's PLAN already on it, because "the step changes nothing until it is
#      set" is only worth saying where there is something for it to change
#   F  E with GEOMETRY_AGENT_STEP_ENABLED on. This one MUST DIFFER, in the planner's message, which is
#      where the step's change lands: the agent's write-up in front of the request and its handoff as
#      the block. Same reason as D
#
# ONE DELIBERATE CHANGE IS HELD APART, and it is the only one. work/complete rewrote the planner's note on
# the measured block (`planner._AGENT_BLOCK_NOTE`): the places are now described as geometry in the order
# the part needs them, and the two sentences that predicted the mesh ("stair-stepped cells", "where prism
# layers collapse") are gone, because the Surveyor names places and never predicts a mesh. B and C read that
# note, so for them the working copy is run with the REFERENCE commit's note put back (`GATE_NOTE_FROM`):
# every other byte of the planner's message must still be what the reference produced. A still runs with
# nothing put back, and every gate off still reaches the note not at all.
#
# The document is a REAL stored measurement rather than a shape somebody typed out:
# `vision_off_gate_measurement.json` beside this file is what the measurement package wrote for
# `annular_001.step` of the corpus export, stamped the way `application/geometry_measurement.py` stamps a
# row, with `look` at `not_attempted` and the raw `facts` dump dropped. `--document` overrides it.
#
#   python devtools/quality/check_vision_off_is_byte_identical.py
#
# Where `git archive` cannot run from the interpreter that has the dependencies, extract the commits by
# hand and name them:
#
#   git archive main    | tar -x -C /tmp/main
#   git archive 91bb74b | tar -x -C /tmp/base
#   git archive bae0fb2 | tar -x -C /tmp/look
#   python devtools/quality/check_vision_off_is_byte_identical.py \
#       --tree main=/tmp/main --tree 91bb74b=/tmp/base --tree bae0fb2=/tmp/look
#
# Run on 2026-09-18 on feat/complete-surveyor at a0aab26: A, B and C byte for byte, D different as it must be.
#   A  intake_system 0dccfc492c1bf82f   intake_tools 557b7c1b95c6d46e   planner_user 16e44b932da08e8b
#   B  intake_system 858a67570d6f0895   intake_tools 557b7c1b95c6d46e   planner_user 855866e292055abb
#   C  intake_system 858a67570d6f0895   intake_tools 557b7c1b95c6d46e   planner_user 855866e292055abb
#   D  intake_system 0c2a15887e7e7a3f   intake_tools 9cb0145c2b4c5e72   planner_user 855866e292055abb
# The A and B digests are the ones the first version of this gate recorded on 2026-09-15, so the node
# path produces the same bytes the direct render did.
#
# Run on 2026-09-22 on feat/surveyor-geometry-step, with E and F and with the planner's two inputs taken
# from the driver's own read. A, B, C and E byte for byte; D and F different as they must be.
#   A  intake_system 0dccfc492c1bf82f   intake_tools 557b7c1b95c6d46e   planner_user 16e44b932da08e8b
#   B  intake_system 858a67570d6f0895   intake_tools 557b7c1b95c6d46e   planner_user 855866e292055abb
#   C  intake_system 858a67570d6f0895   intake_tools 557b7c1b95c6d46e   planner_user 855866e292055abb
#   D  intake_system 0c2a15887e7e7a3f   intake_tools 9cb0145c2b4c5e72   planner_user 33ba4e6a760b8d88
#   E  intake_system 7392ea8106df56b9   intake_tools 9cb0145c2b4c5e72   planner_user fa052d8f4050f316
#   F  intake_system 7392ea8106df56b9   intake_tools 9cb0145c2b4c5e72   planner_user 7476234daccd37ac
# Run again on 2026-09-22 after the geometry step was given the customer's file and its envelope was
# priced at the handoff. Same verdicts, and A, B, C and E the same digests a third time:
#   A  intake_system 0dccfc492c1bf82f   intake_tools 557b7c1b95c6d46e   planner_user 16e44b932da08e8b
#   B  intake_system 858a67570d6f0895   intake_tools 557b7c1b95c6d46e   planner_user 855866e292055abb
#   C  intake_system 858a67570d6f0895   intake_tools 557b7c1b95c6d46e   planner_user 855866e292055abb
#   D  intake_system 0c2a15887e7e7a3f   intake_tools 9cb0145c2b4c5e72   planner_user 33ba4e6a760b8d88
#   E  intake_system 7392ea8106df56b9   intake_tools 9cb0145c2b4c5e72   planner_user fa052d8f4050f316
#   F  intake_system 7392ea8106df56b9   intake_tools 9cb0145c2b4c5e72   planner_user fc858827cc94579c
# Run twice back to back, every digest identical both passes, so this gate is reproducible.
#
# WHY F's DIGEST MOVED, 7476234daccd37ac to fc858827cc94579c, and why no fact did. The handoff no
# longer reads `plan_envelope` off the row; it prices it again against the budget the customer settled
# on. On this row, which has no trade, every value is the same - checked field by field on Windows and
# under WSL, both IDENTICAL - and the only difference in the whole message is the KEY ORDER of that one
# dict: alphabetical when it came off the row, because the tracked fixture was written with sorted
# keys, and the envelope type's own order now. Two rulers missed it. Dict equality ignores key order,
# and so does the corpus harness's `planner_message_chars`, which a reordering leaves untouched. Only
# this gate, which hashes the bytes, could see it. The order is now the code's rather than the
# storage's, which is the reproducible one: Postgres JSONB does not preserve key order at all.
#
# RUN THE MEASUREMENT PACKAGE FROM ITS CHECKOUT, not from a copy of its `src`. Copying `src` alone to a
# local disk to make this gate faster changed what the builder is told: `learn/store.py` takes its ROOT
# from `Path(__file__).parents[3]`, so the fitted cell correction lives OUTSIDE `src`, and without it
# the basis line degrades from "calibration multiplier 1.01 was fitted against ..." to the bare
# "learn.calibration.cell_correction". The applied multiplier is 1.0 either way, so no number moves and
# no test fails; one sentence the planner reads does. The faster ruler was the wrong ruler.
#
# A, B and C are the same three digests as before, which is the point of them. D's planner_user moved
# against the 2026-09-18 run: the reference for D is `bae0fb2`, which predates the survey, and this
# branch's own history has since changed the block a survey composes (`3ba42c0` composes it from the
# inlet the customer names). E is the case that asks what THIS change moves, and it moves nothing.
# F moves only the planner's message, which is where the step's change lands: the write-up in front of
# the request and the handoff as the block. It leaves the tool list alone because the step adds no tool.
#
# HOW THIS RAN. On Windows the checkout is a git worktree whose `.git` names a Windows path, which the
# Linux git cannot follow, so the four commits were extracted on the Windows side and named with
# `--tree`; the gate itself ran under WSL Ubuntu-24.04 on the platform's own interpreter, with the
# measurement package's checkout on PYTHONPATH:
#
#   for rev in main 91bb74b bae0fb2 3ba42c0; do git archive $rev | tar -x -C /tmp/$rev; done
#   PYTHONPATH=/path/to/gz_complete/src python devtools/quality/check_vision_off_is_byte_identical.py #       --tree main=/tmp/main --tree 91bb74b=/tmp/91bb74b --tree bae0fb2=/tmp/bae0fb2 #       --tree 3ba42c0=/tmp/3ba42c0
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOCUMENT = Path(__file__).with_name("vision_off_gate_measurement.json")
#: E and F read a measurement that kept its facts and the survey row a conversation left on it.
#: `run_geometry_step_on_corpus.py` writes both; the tracked pair is venturi_orifice_001.
DEFAULT_STEP_DOCUMENT = ROOT / "tests" / "fixtures" / "geometry_survey" / "venturi_orifice_001.json"
DEFAULT_STEP_ROW = Path(__file__).with_name("geometry_step_gate_row.json")

#: The commit each configuration is compared against.
MAIN = "main"
#: The measurement work's own base: the commit that stored a measurement and let intake, the planner
#: and admission read it.
MEASUREMENT_BASE = "91bb74b"
#: The commit that added the look behind its own flag.
LOOK_BASE = "bae0fb2"
#: The first complete Surveyor: measurement, look and survey, and no geometry agent's step. The
#: reference for E and F, which ask what the step's own switch moves.
SURVEYOR_BASE = "3ba42c0"

_MEASURED = "GEOMETRY_MEASUREMENT_ENABLED,GEOMETRY_REPORT_READERS_ENABLED"
_SURVEYED = _MEASURED + ",GEOMETRY_VISION_ENABLED,GEOMETRY_SURVEY_ENABLED"
PROMPTS = ("intake_system", "intake_tools", "planner_user")

#: Run inside each checkout under that checkout's own unit-tier conftest, which pins the settings, stubs
#: the heavy dependencies and keeps the runtime roots off the working copy. Written to tests/unit/ of a
#: temporary copy, never of the working copy.
HARNESS = r'''# Responsibility: Print the digests of what the intake model and the mesh planner are handed, through the real paths.
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import types
from pathlib import Path
from types import SimpleNamespace

_DOC = os.environ.get("GATE_DOC") or ""
_ROW = os.environ.get("GATE_ROW") or ""
_NOTE_FROM = os.environ.get("GATE_NOTE_FROM") or ""
DOC = json.loads(Path(_DOC).read_text(encoding="utf-8")) if _DOC else None
#: A STORED SURVEY ROW, when the case supplies one: the row a customer's answered conversation leaves
#: behind, carrying the geometry agent's plan. Without it `geometry_survey.load` answers None, which is
#: what every case before the step existed wanted.
ROW_STATE = json.loads(Path(_ROW).read_text(encoding="utf-8")) if _ROW else None
FLAGS = [f for f in (os.environ.get("GATE_FLAGS") or "").split(",") if f]
WS = Path(os.environ["GATE_WS"])
SHA = str((DOC or {}).get("source_sha256") or "a" * 64)
ROW = SimpleNamespace(id="11111111-1111-4111-8111-111111111111", owner_id="owner-gate",
                      object_key="uploads/11111111-1111-4111-8111-111111111111", sha256=SHA,
                      size_bytes=173563, original_filename="part.step", suffix_hint=".step")
MESSAGES = [{"role": "assistant", "content": "Geometry received. What are you meshing this for?"},
            {"role": "user", "content": "Internal flow of water through this part at 2 m/s. "
                                        "Mesh budget: keep the total cell count under 2 million cells."}]


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _flags(monkeypatch):
    import meshpipeline.settings.policy as polcfg
    for name in FLAGS:
        if hasattr(polcfg, name):
            monkeypatch.setattr(polcfg, name, True)


def _stored_reads(monkeypatch):
    import meshpipeline.cad.regions as regions

    async def stored(_ref, _digest):
        return DOC

    async def nothing(_ref):
        return None

    if hasattr(regions, "_stored_document"):
        monkeypatch.setattr(regions, "_stored_document", stored)
    if hasattr(regions, "_analysis_from_object_store"):
        monkeypatch.setattr(regions, "_analysis_from_object_store", nothing)
    try:
        from meshpipeline.application import geometry_survey
    except ImportError:
        return

    async def survey_row(*a, **k):
        return ROW_STATE

    async def stored_row(*a, **k):
        return True

    monkeypatch.setattr(geometry_survey, "load", survey_row)
    # the step writes the handover back to the row; in here that write goes nowhere
    if hasattr(geometry_survey, "save"):
        monkeypatch.setattr(geometry_survey, "save", stored_row)


def _state():
    from meshpipeline.api.v1 import chat
    session = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222", messages=list(MESSAGES), geometry_source=ROW,
        request_txt="", review_brief_txt="", domain="", mesh_engine="", engine_params={},
        intake_patches=[], dimensionality="", purpose="", input_kind="")
    return chat._build_intake_state(session, "owner-gate")


def _intake(monkeypatch):
    import meshpipeline.agents.intake.agent as agent
    from meshpipeline.contracts.model_inference import ModelRoundResult

    seen = {}

    async def call(**kw):
        seen.setdefault("system", kw["messages"][0]["content"])
        seen.setdefault("tools", json.dumps(kw.get("tools"), sort_keys=True))
        return ModelRoundResult(assistant_text="What fluid is it?", finish_reason="stop")

    monkeypatch.setattr(agent.llm_router, "call_intake_model", call)
    asyncio.run(agent.node_intake(_state()))
    return seen["system"], seen["tools"]


def _planner(monkeypatch):
    import meshpipeline.engines.snappy.drivers as drivers
    import meshpipeline.engines.snappy.planner as planner
    from meshpipeline.contracts.model_inference import ModelRoundResult

    WS.mkdir(parents=True, exist_ok=True)
    (WS / "input.stl").write_text("solid x\nendsolid x\n")
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda _stl: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 6.0,
                                      "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda _a, **_k: {"surface_level": 2, "feature_level": 3, "afford_level": 2})
    monkeypatch.setattr(planner, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None), raising=False)
    seen = {}

    async def call(messages, tools=None, tool_choice="none", job_id="", **_kw):
        seen["user"] = messages[1]["content"]
        return ModelRoundResult(assistant_text=json.dumps({"approach": "x", "max_cells": 1000}))

    monkeypatch.setattr(planner.llm_router, "call_planner_model", call)
    if _NOTE_FROM:
        # the reference commit's note, read out of its source, so the one deliberate change is held apart
        import ast
        tree = ast.parse(Path(_NOTE_FROM).read_text(encoding="utf-8"))
        note = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", "") == "_AGENT_BLOCK_NOTE" for t in n.targets))
        monkeypatch.setattr(planner, "_AGENT_BLOCK_NOTE", note)
    from tests._geometry_support import prepared_surface

    kwargs = {"workspace": WS, "job_id": "gate-job", "request_txt": "mesh it",
              "surface": prepared_surface(WS / "input.stl",
                                          source_id="11111111-1111-1111-1111-111111111111",
                                          interpretation_id="22222222-2222-2222-2222-222222222222")}
    if "geometry_agent" in inspect.signature(planner.plan_with_accounting).parameters:
        # THROUGH THE DRIVER'S OWN READ, whichever one this checkout has. Before the geometry agent's
        # step the driver read `_agent_block(state)` and passed the state's request through untouched;
        # with the step it reads both from `_planner_inputs`, which is where the write-up in front of
        # the request is decided. A gate that kept calling `_agent_block` here would be blind to the
        # half of the change that lands on the request, which is most of it.
        st = _state()
        st["request_txt"] = kwargs["request_txt"]
        if hasattr(drivers, "_planner_inputs"):
            got = asyncio.run(drivers._planner_inputs(st))
            kwargs["request_txt"], kwargs["geometry_agent"] = got[0], got[1]
            # AND THE THIRD VALUE, which this gate used to drop on the floor. `_planner_inputs` returns the
            # reason the step was NOT used, and dropping it is how case F could report "the planner's message
            # did not move" without being able to say that the step had fallen back to the step-off values.
            # A ruler that cannot tell "nothing changed" from "nothing ran" is the one blind spot this file
            # exists to avoid, so the reason is printed and `_digests` passes it through.
            if len(got) > 2 and got[2]:
                print(f"WHY the step was not used: {got[2]}")
        else:
            kwargs["geometry_agent"] = asyncio.run(drivers._agent_block(st))
    asyncio.run(planner.plan_with_accounting(**kwargs))
    return seen["user"]


def test_emit(monkeypatch):
    _flags(monkeypatch)
    _stored_reads(monkeypatch)
    system, tools = _intake(monkeypatch)
    user = _planner(monkeypatch)
    print(f"GATE intake_system {_sha(system)} {len(system)}")
    print(f"GATE intake_tools {_sha(tools)} {len(tools)}")
    print(f"GATE planner_user {_sha(user)} {len(user)}")
'''


def _extract(rev: str, into: Path, supplied: dict[str, Path]) -> Path:
    """The named commit as a plain directory. `--tree rev=path` wins when it is given."""
    if rev in supplied:
        return supplied[rev]
    into.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "archive", rev], cwd=ROOT, check=True, stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)
    return into


def _working_copy(into: Path) -> Path:
    """This checkout as it stands, uncommitted edits included, which is the whole point."""
    def _skip(_dir, names):
        return [n for n in names if n in {".git", "BETA", "__pycache__", ".venv", "workspaces"}]

    shutil.copytree(ROOT, into, ignore=_skip, dirs_exist_ok=True)
    return into


def _digests(tree: Path, label: str, document: Path | None, flags: str, python: str,
             note_from: Path | None = None, row: Path | None = None) -> dict[str, str]:
    harness = tree / "tests" / "unit" / "gate_vision_off.py"
    harness.write_text(HARNESS, encoding="utf-8")
    workspace = tree / ".gate-workspace"
    env = {"GATE_WS": str(workspace), "GATE_DOC": str(document) if document else "",
           "GATE_ROW": str(row) if row else "",
           "GATE_FLAGS": flags, "GATE_NOTE_FROM": str(note_from) if note_from else "",
           # the checkout's OWN src first, and then whatever the caller already had on the path: on a
           # machine where the measurement package is a checkout rather than an installed distribution,
           # replacing PYTHONPATH outright is a gate that silently runs every case with no package
           "PYTHONPATH": os.pathsep.join(["src", *( [os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [] )])}
    try:
        run = subprocess.run([python, "-m", "pytest", str(harness.relative_to(tree)), "-s", "-q",
                              "-p", "no:randomly", "-p", "no:cacheprovider"],
                             cwd=tree, env={**os.environ, **env}, capture_output=True, text=True)
    finally:
        harness.unlink(missing_ok=True)
    out = {}
    for line in run.stdout.splitlines():
        if line.startswith("GATE "):
            _, name, digest, _length = line.split()
            out[name] = digest
        elif line.startswith("WHY "):
            # printed, not collected: it is not part of the comparison, and a case whose digest did not move
            # because the step never ran has to say so where whoever reads the verdict will see it
            print(f"     {label}: {line[4:]}")
    if set(out) != set(PROMPTS):
        print(run.stdout[-4000:], file=sys.stderr)
        raise SystemExit(f"{label}: the harness did not print every digest")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--document", default=str(DEFAULT_DOCUMENT),
                    help="a stored geometry measurement document, as JSON")
    ap.add_argument("--step-document", default=str(DEFAULT_STEP_DOCUMENT),
                    help="the measurement E and F read: unlike --document it keeps its raw facts, "
                         "because the geometry agent's step composes the survey again from them")
    ap.add_argument("--step-row", default=str(DEFAULT_STEP_ROW),
                    help="the stored survey row E and F read: a conversation answered to the end, "
                         "with the geometry agent's plan on it")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--tree", action="append", default=[], metavar="REV=PATH",
                    help="use an already extracted checkout of REV instead of running git archive")
    args = ap.parse_args()

    supplied: dict[str, Path] = {}
    for pair in args.tree:
        rev, _, path = pair.partition("=")
        supplied[rev] = Path(path).resolve()

    document = Path(args.document).resolve()
    if not document.is_file():
        raise SystemExit(f"no stored measurement document at {document}")
    step_document, row = Path(args.step_document).resolve(), Path(args.step_row).resolve()
    for what, path in (("measurement", step_document), ("survey row", row)):
        if not path.is_file():
            raise SystemExit(f"no {what} for E and F at {path}")
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="vision-off-gate-") as tmp:
        tmp_path = Path(tmp)
        here = _working_copy(tmp_path / "working")
        cases = [("A  every gate off", MAIN, None, "", None, True, ""),
                 ("B  measurement on, look and survey off", MEASUREMENT_BASE, document, _MEASURED, None, True, ""),
                 ("C  measurement and look on, survey off", LOOK_BASE, document,
                  _MEASURED + ",GEOMETRY_VISION_ENABLED", None, True, ""),
                 ("D  survey on: the harness has to see it", LOOK_BASE, document,
                  _SURVEYED, None, False, "intake_tools"),
                 ("E  the survey's whole chain on, the step OFF, an answered row with a plan on it",
                  SURVEYOR_BASE, step_document, _SURVEYED, row, True, ""),
                 ("F  the same, the step ON: the harness has to see it",
                  SURVEYOR_BASE, step_document, _SURVEYED + ",GEOMETRY_AGENT_STEP_ENABLED", row,
                  False, "planner_user")]
        for label, rev, doc, flags, row_path, must_match, must_move in cases:
            old = _extract(rev, tmp_path / rev.replace("/", "_"), supplied)
            # B and C read the rewritten note; the reference's is put back so nothing else can hide
            # behind it. E and F are against a commit that already has it, so nothing is put back
            note = ((old / "src" / "meshpipeline" / "engines" / "snappy" / "planner.py")
                    if must_match and doc and rev in (MEASUREMENT_BASE, LOOK_BASE) else None)
            mine = _digests(here, f"{label} (working copy)", doc, flags, args.python, note, row_path)
            theirs = _digests(old, f"{label} ({rev})", doc, flags, args.python, None, row_path)
            for prompt in PROMPTS:
                same = mine[prompt] == theirs[prompt]
                print(f"{'SAME' if same else 'DIFF'} {label}: {prompt} "
                      f"{mine[prompt][:16]} vs {rev} {theirs[prompt][:16]}")
                if must_match and not same:
                    failures.append(f"{label}: {prompt}")
            if must_move and mine[must_move] == theirs[must_move]:
                failures.append(f"{label}: {must_move} did not move, so this harness cannot see what "
                                f"this case turns on and its other verdicts prove nothing")
    if failures:
        print("\nthe off state is not off, or the gate is blind: " + "; ".join(failures))
        return 1
    print("\nevery prompt is byte for byte what it was with its gates off, and the survey is visible when on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
