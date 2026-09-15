#!/usr/bin/env python3
# Responsibility: Prove by hashing that the look changes nothing until it is switched on.
# Boundaries: a repository gate; it renders two prompts in three checkouts and compares digests. It
#              calls no provider, touches no database and writes nothing outside a temporary directory.
#
# WHY A GATE AND NOT A UNIT TEST. The claim is about two prompts BEFORE and AFTER a commit, so it can
# only be checked by rendering them in the older checkout as well. A test in this checkout can assert
# that no `look` key is present, which `tests/unit/engines/test_planner_look_block.py` and
# `tests/unit/agents/intake/test_intake_geometry_look.py` do; it cannot assert that the bytes are the
# ones the old code produced, because the old code is not importable from here.
#
# WHAT IS COMPARED, and they are the only two prompts this branch can reach:
#
#   A  both gates off, this checkout against `main`
#      No measurement is stored at all, so the intake system prompt and the mesh planner's user
#      message must be byte for byte what they were before the measurement work existed.
#
#   B  GEOMETRY_MEASUREMENT_ENABLED on and GEOMETRY_VISION_ENABLED off, this checkout against the
#      base of the measurement work
#      A measurement IS stored and both prompts carry it. With the look switched off, the stored
#      `look` block is `look_block(None)`, the planner block carries no `look` key, and both prompts
#      must be byte for byte what the measurement alone produced.
#
# The document for B is a REAL stored measurement rather than a shape somebody typed out:
# `vision_off_gate_measurement.json` beside this file is what the measurement package wrote for
# `annular_001.step` of the corpus export, stamped the way `application/geometry_measurement.py`
# stamps a row, with `look` at `not_attempted` and the raw `facts` dump dropped (nothing either prompt
# renders reads it). `--document` overrides it with any other stored row.
#
#   python devtools/quality/check_vision_off_is_byte_identical.py
#
# Where `git archive` cannot run from the interpreter that has the dependencies (a worktree whose
# `.git` file names a path in another operating system's spelling is the case this was written on),
# extract the two commits by hand and name them:
#
#   git archive main    | tar -x -C /tmp/main
#   git archive 91bb74b | tar -x -C /tmp/base
#   python devtools/quality/check_vision_off_is_byte_identical.py \
#       --tree main=/tmp/main --tree 91bb74b=/tmp/base
#
# Run on 2026-09-15: every prompt byte for byte, both cases.
#   A  intake_system 0dccfc492c1bf82f   planner_user 16e44b932da08e8b
#   B  intake_system 858a67570d6f0895   planner_user 855866e292055abb
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
#: and admission read it. Case B asks whether the look moved anything that commit settled.
MEASUREMENT_BASE = "91bb74b"

#: Rendered inside each checkout under that checkout's own unit-tier conftest, which pins the
#: settings, stubs the heavy dependencies and keeps the runtime roots off the working copy. Written
#: to tests/unit/ of a temporary extract, never of the working copy.
HARNESS = '''\
# Responsibility: Print the digest of the two prompts, in whichever checkout this lands in.
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import types
from pathlib import Path

_DOC = os.environ.get("GATE_DOC") or ""
DOC = json.loads(Path(_DOC).read_text(encoding="utf-8")) if _DOC else None
WS = Path(os.environ["GATE_WS"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _intake_system() -> str:
    from meshpipeline.agents.intake.agent import compose_intake_system

    system = compose_intake_system()
    try:
        from meshpipeline.agents.intake.geometry_brief import render_block
    except ImportError:
        return system                              # the checkout predates the geometry block
    return system + render_block(DOC)


def _agent_block():
    if DOC is None:
        return None
    from meshpipeline.contracts.geometry_agent_block import block_for_document

    return block_for_document(DOC)


def _planner_user(monkeypatch) -> str:
    import meshpipeline.engines.snappy.planner as planner
    from meshpipeline.contracts.model_inference import ModelRoundResult

    WS.mkdir(parents=True, exist_ok=True)
    (WS / "input.stl").write_text("solid x\\nendsolid x\\n")
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda _stl: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 6.0,
                                      "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda _a, **_k: {"surface_level": 2, "feature_level": 3, "afford_level": 2})
    monkeypatch.setattr(planner, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None),
                        raising=False)
    seen: dict = {}

    async def _fake_call(messages, tools=None, tool_choice="none", job_id="", **_kw):
        seen["user"] = messages[1]["content"]
        return ModelRoundResult(assistant_text=json.dumps({"approach": "x", "max_cells": 1000}))

    monkeypatch.setattr(planner.llm_router, "call_planner_model", _fake_call)
    from tests._geometry_support import prepared_surface

    kwargs = {"workspace": WS, "job_id": "gate-job", "request_txt": "mesh it",
              "surface": prepared_surface(WS / "input.stl",
                                          source_id="11111111-1111-1111-1111-111111111111",
                                          interpretation_id="22222222-2222-2222-2222-222222222222")}
    if "geometry_agent" in inspect.signature(planner.plan_with_accounting).parameters:
        kwargs["geometry_agent"] = _agent_block()
    asyncio.run(planner.plan_with_accounting(**kwargs))
    return seen["user"]


def test_emit(monkeypatch):
    intake = _intake_system()
    user = _planner_user(monkeypatch)
    print(f"GATE intake_system {_sha(intake)} {len(intake)}")
    print(f"GATE planner_user {_sha(user)} {len(user)}")
'''


def _extract(rev: str, into: Path, supplied: dict[str, Path]) -> Path:
    """The named commit as a plain directory.

    `--tree rev=path` wins when it is given. It exists because this checkout can be a worktree whose
    `.git` file names a path in another operating system's spelling, and then `git archive` cannot run
    from the interpreter that has the dependencies. Extracting by hand is then one command, and the
    comparison is the same comparison.
    """
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


def _digests(tree: Path, label: str, document: Path | None, python: str) -> dict[str, str]:
    harness = tree / "tests" / "unit" / "gate_vision_off.py"
    harness.write_text(HARNESS, encoding="utf-8")
    workspace = tree / ".gate-workspace"
    env = {"GATE_WS": str(workspace), "GATE_DOC": str(document) if document else "",
           "PYTHONPATH": "src"}
    run = subprocess.run([python, "-m", "pytest", str(harness.relative_to(tree)), "-s", "-q",
                          "-p", "no:randomly"],
                         cwd=tree, env={**os.environ, **env}, capture_output=True, text=True)
    out = {}
    for line in run.stdout.splitlines():
        if line.startswith("GATE "):
            _, name, digest, _length = line.split()
            out[name] = digest
    if len(out) != 2:
        print(run.stdout[-4000:], file=sys.stderr)
        raise SystemExit(f"{label}: the harness printed no digest")
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
        cases = [("A  both gates off", MAIN, None),
                 ("B  measurement on, vision off", MEASUREMENT_BASE, document)]
        for label, rev, doc in cases:
            old = _extract(rev, tmp_path / rev.replace("/", "_"), supplied)
            mine = _digests(here, f"{label} (working copy)", doc, args.python)
            theirs = _digests(old, f"{label} ({rev})", doc, args.python)
            for prompt in ("intake_system", "planner_user"):
                same = mine[prompt] == theirs[prompt]
                print(f"{'OK  ' if same else 'DIFF'} {label}: {prompt} "
                      f"{mine[prompt][:16]} vs {rev} {theirs[prompt][:16]}")
                if not same:
                    failures.append(f"{label}: {prompt}")
    if failures:
        print("\nthe look is not off when it is off: " + "; ".join(failures))
        return 1
    print("\nevery prompt compared is byte for byte what it was")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
