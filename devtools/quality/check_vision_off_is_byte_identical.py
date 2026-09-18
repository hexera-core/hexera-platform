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
# user message.
#
#   A  every geometry gate off, this checkout against `main`
#   B  GEOMETRY_MEASUREMENT_ENABLED and GEOMETRY_REPORT_READERS_ENABLED on, the look and the survey
#      off, against the base of the measurement work
#   C  B with GEOMETRY_VISION_ENABLED on, the survey off, against the commit that added the look. The
#      stored look is `not_attempted`, so it describes nothing
#   D  C with GEOMETRY_SURVEY_ENABLED on, against the same commit. This one MUST DIFFER, in the tool
#      list and the intake prompt: it is the check that the harness can see the survey at all. A ruler
#      that reports "identical" for a change it cannot see is how this project has been misled before
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
# Run on 2026-09-18 on feat/complete-surveyor: A, B and C byte for byte, D different as it must be.
#   A  intake_system 0dccfc492c1bf82f   intake_tools 557b7c1b95c6d46e   planner_user 16e44b932da08e8b
#   B  intake_system 858a67570d6f0895   intake_tools 557b7c1b95c6d46e   planner_user 855866e292055abb
#   C  intake_system 858a67570d6f0895   intake_tools 557b7c1b95c6d46e   planner_user 855866e292055abb
#   D  intake_system a6493c2a2c77c153   intake_tools 9cb0145c2b4c5e72   planner_user 855866e292055abb
# The A and B digests are the ones the first version of this gate recorded on 2026-09-15, so the node
# path produces the same bytes the direct render did.
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

#: The commit each configuration is compared against.
MAIN = "main"
#: The measurement work's own base: the commit that stored a measurement and let intake, the planner
#: and admission read it.
MEASUREMENT_BASE = "91bb74b"
#: The commit that added the look behind its own flag.
LOOK_BASE = "bae0fb2"

_MEASURED = "GEOMETRY_MEASUREMENT_ENABLED,GEOMETRY_REPORT_READERS_ENABLED"
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
DOC = json.loads(Path(_DOC).read_text(encoding="utf-8")) if _DOC else None
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

    async def no_survey(*a, **k):
        return None

    monkeypatch.setattr(geometry_survey, "load", no_survey)


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
    from tests._geometry_support import prepared_surface

    kwargs = {"workspace": WS, "job_id": "gate-job", "request_txt": "mesh it",
              "surface": prepared_surface(WS / "input.stl",
                                          source_id="11111111-1111-1111-1111-111111111111",
                                          interpretation_id="22222222-2222-2222-2222-222222222222")}
    if "geometry_agent" in inspect.signature(planner.plan_with_accounting).parameters:
        kwargs["geometry_agent"] = asyncio.run(drivers._agent_block(_state()))
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


def _digests(tree: Path, label: str, document: Path | None, flags: str, python: str) -> dict[str, str]:
    harness = tree / "tests" / "unit" / "gate_vision_off.py"
    harness.write_text(HARNESS, encoding="utf-8")
    workspace = tree / ".gate-workspace"
    env = {"GATE_WS": str(workspace), "GATE_DOC": str(document) if document else "",
           "GATE_FLAGS": flags, "PYTHONPATH": "src"}
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
    if set(out) != set(PROMPTS):
        print(run.stdout[-4000:], file=sys.stderr)
        raise SystemExit(f"{label}: the harness did not print every digest")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--document", default=str(DEFAULT_DOCUMENT),
                    help="a stored geometry measurement document, as JSON")
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
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="vision-off-gate-") as tmp:
        tmp_path = Path(tmp)
        here = _working_copy(tmp_path / "working")
        cases = [("A  every gate off", MAIN, None, "", True),
                 ("B  measurement on, look and survey off", MEASUREMENT_BASE, document, _MEASURED, True),
                 ("C  measurement and look on, survey off", LOOK_BASE, document,
                  _MEASURED + ",GEOMETRY_VISION_ENABLED", True),
                 ("D  survey on: the harness has to see it", LOOK_BASE, document,
                  _MEASURED + ",GEOMETRY_VISION_ENABLED,GEOMETRY_SURVEY_ENABLED", False)]
        for label, rev, doc, flags, must_match in cases:
            old = _extract(rev, tmp_path / rev.replace("/", "_"), supplied)
            mine = _digests(here, f"{label} (working copy)", doc, flags, args.python)
            theirs = _digests(old, f"{label} ({rev})", doc, flags, args.python)
            for prompt in PROMPTS:
                same = mine[prompt] == theirs[prompt]
                print(f"{'SAME' if same else 'DIFF'} {label}: {prompt} "
                      f"{mine[prompt][:16]} vs {rev} {theirs[prompt][:16]}")
                if must_match and not same:
                    failures.append(f"{label}: {prompt}")
            if not must_match and mine["intake_tools"] == theirs["intake_tools"]:
                failures.append(f"{label}: the survey is on and the tool list did not move, so this "
                                f"harness cannot see the survey and its other verdicts prove nothing")
    if failures:
        print("\nthe off state is not off, or the gate is blind: " + "; ".join(failures))
        return 1
    print("\nevery prompt is byte for byte what it was with its gates off, and the survey is visible when on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
