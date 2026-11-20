# Responsibility: Hold the corpus runner's per-file isolation to its promise: a file whose repair
#                 kills the process (a native segfault in OCC's ShapeFix, seen on a real ABC part)
#                 becomes a `crashed` row naming the signal and the stage, and the run goes on.
# Boundaries: hermetic - `_one` is stubbed; the crash is a real SIGSEGV the forked child sends
#             itself. POSIX only (fork).
from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fork isolation is POSIX only")


def _load():
    spec = importlib.util.spec_from_file_location(
        "repair_corpus_isolation_under_test",
        REPO / "devtools" / "validation" / "repair_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


corpus = _load()


def _files(tmp_path, *names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(n.encode())
        out.append(p)
    return out


def _stub(meshes_before):
    def one(path, scratch, *, stage=None, **_):
        if stage:
            stage("probe_before")
            stage("repair", meshes_before=meshes_before)
        if path.name.startswith("boom"):
            os.kill(os.getpid(), signal.SIGSEGV)
        return {"file": path.name, "outcome": "repaired", "meshes_before": False,
                "mesh_kind_before": "other", "meshes_after": True, "mesh_kind_after": ""}
    return one


def test_a_segfaulting_file_becomes_a_crashed_row_and_the_run_continues(monkeypatch, tmp_path):
    monkeypatch.setattr(corpus, "_one", _stub(meshes_before=False))
    rows = corpus.run_files(_files(tmp_path, "a.step", "boom.step", "c.step"), tmp_path,
                            mesh_probe=True, force_repair=True, isolate=True)
    assert [r["file"] for r in rows] == ["a.step", "boom.step", "c.step"]
    crashed = rows[1]
    assert crashed["outcome"] == "crashed"
    assert crashed["crash"]["signal"] == signal.SIGSEGV
    assert crashed["crash"]["stage"] == "repair"
    assert "sha256" in crashed
    assert rows[0]["outcome"] == rows[2]["outcome"] == "repaired"
    summary = corpus.meshing_summary(rows)
    # did not mesh before: a crash is not a break, it is its own count
    assert summary["newly_broken"] == 0 and summary["crashed"] == 1
    assert summary["newly_meshed"] == 2


def test_a_crash_on_a_file_that_meshed_before_counts_as_newly_broken(monkeypatch, tmp_path):
    monkeypatch.setattr(corpus, "_one", _stub(meshes_before=True))
    rows = corpus.run_files(_files(tmp_path, "boom.step"), tmp_path,
                            mesh_probe=True, force_repair=True, isolate=True)
    summary = corpus.meshing_summary(rows)
    assert summary["newly_broken"] == 1 and summary["crashed"] == 1


def test_a_run_with_no_crash_keeps_its_summary_unchanged():
    row = {"outcome": "repaired", "meshes_before": False, "meshes_after": True,
           "mesh_kind_before": "x", "mesh_kind_after": ""}
    assert "crashed" not in corpus.meshing_summary([row])


def test_isolation_returns_the_childs_row_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(corpus, "_one", _stub(meshes_before=False))
    isolated = corpus.run_files(_files(tmp_path, "a.step"), tmp_path, isolate=True)
    inline = corpus.run_files(_files(tmp_path, "a.step"), tmp_path)
    assert isolated == inline


def test_collect_does_not_call_a_clean_control_with_a_crash_untouched():
    spec = importlib.util.spec_from_file_location(
        "v3_collect_under_test_crash", REPO / "devtools" / "validation" / "v3_eval" / "collect.py")
    collect = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = collect
    spec.loader.exec_module(collect)
    rows = [{"file": "a.step", "outcome": "crashed", "operations": []}]
    out = collect.clean_untouched(rows, {"meshing": {"newly_broken": 0, "crashed": 1}})
    assert out["untouched"] is False and out["crashed_files"] == ["a.step"]


def test_run_arm_counts_model_calls_from_the_file_children_append_to(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "v3_run_arm_under_test_totals", REPO / "devtools" / "validation" / "v3_eval" / "run_arm.py")
    run_arm = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = run_arm
    spec.loader.exec_module(run_arm)
    sink = run_arm.TelemetryFile(tmp_path / "model-calls.jsonl")
    pid = os.fork()
    if pid == 0:  # a forked child's call reaches the file, not the parent's counter
        sink.path.write_text('{"estimated_cost_usd": 0.5}\n{"estimated_cost_usd": 0.25}\n')
        os._exit(0)
    os.waitpid(pid, 0)
    assert sink.calls == 0
    assert sink.totals() == (2, 0.75)


def test_a_crashed_clean_part_is_not_counted_byte_unchanged_by_collect():
    spec = importlib.util.spec_from_file_location(
        "v3_collect_under_test_unchanged",
        REPO / "devtools" / "validation" / "v3_eval" / "collect.py")
    collect = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = collect
    spec.loader.exec_module(collect)
    rep = {"files": 100, "clean": {"files_with_kept_ops": ["a.step"],
                                   "crashed_files": ["b.step", "a.step"]}}
    assert collect.clean_unchanged(rep) == 98   # a and b; a counted once
