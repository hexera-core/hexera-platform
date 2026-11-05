# Responsibility: Verify every training producer names its occurrence and reaches the durable authority.
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.product_modes import set_modes

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"


def _producer_calls():
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                                    # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "log":
                continue
            receiver = ast.unparse(node.func.value)
            if "TrainingLogger" not in receiver and "tlogger" not in receiver:
                continue
            yield path, node


def test_every_training_producer_names_its_occurrence():
    unkeyed = [f"{p.relative_to(SRC.parent.parent)}:{c.lineno}"
               for p, c in _producer_calls()
               if not any(kw.arg == "op_id" for kw in c.keywords)]
    assert not unkeyed, (
        "these producers cannot reach the durable capture authority, so their content never "
        "becomes training data:\n  " + "\n  ".join(unkeyed))


def test_the_audit_can_actually_see_the_producers():
    found = list(_producer_calls())
    assert len(found) >= 12, f"only found {len(found)} producers - the walker is broken"


@pytest.mark.parametrize("relative_path, token", [
    ("pipeline/executor.py", "executor:"),           # scoped to the builder retry
    ("pipeline/engine_select.py", "engine-select"),  # fires once per run
    ("agents/builder/attempt_capture.py", "builder:"), # scoped to the attempt
    ("application/pipeline_run.py", "intake:"),      # position in the intake stream
])
def test_representative_producers_scope_identity_to_the_right_thing(relative_path, token):
    assert token in (SRC / relative_path).read_text(encoding="utf-8"), (
        f"{relative_path} no longer scopes its capture identity to '{token}'")


def test_no_production_code_resolves_a_corpus_file(capture_authority):
    banned = ("_events_path", "_quarantined_ops", "CONFLICT_RECORD", "_replay_disposition")
    offenders = [f"{p.relative_to(SRC.parent.parent)}: {name}"
                 for p in SRC.rglob("*.py")
                 for name in banned
                 if name in p.read_text(encoding="utf-8")]
    assert not offenders, offenders


def test_a_producer_reaches_the_authority_end_to_end(capture_authority, monkeypatch, tmp_path):
    from tests.capture_authority import CAPTURE_OWNER

    import meshpipeline.settings.runtime as rtcfg
    from meshpipeline.capture.logger import TrainingLogger

    monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path, raising=False)
    set_modes(monkeypatch, collection=True)
    TrainingLogger("j-end").log("executor_run", {"success": True}, op_id="executor:0")

    (row,) = capture_authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id="j-end")
    assert row["name"] == "executor_run" and row["payload"] == {"success": True}
