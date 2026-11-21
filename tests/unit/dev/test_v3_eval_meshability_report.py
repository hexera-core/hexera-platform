# Responsibility: Verify the meshability wave's report: per-arm headlines from
#                 repair_corpus.meshability_summary, the per-file before/after table, the AI
#                 loop's marginal gain over the pass in BOTH directions, the clean control, the
#                 markdown, and that collect writes it only for rows scored with --meshability.
# Boundaries: hermetic - rows are written as JSON; no CAD, no gmsh, no cloud.
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
V3 = REPO / "devtools" / "validation" / "v3_eval"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mr = _load("meshability_report_under_test", V3 / "meshability_report.py")
rc = _load("repair_corpus_for_report", REPO / "devtools" / "validation" / "repair_corpus.py")
PASS, AI = "meshability_pass", "meshability_pass_ai"


def _row(name, *, before=False, default=False, hints=None, present=True, source="repaired",
         credit=None, gd=None):
    row = {"file": name, "outcome": "refused", "meshes_before": before,
           "returned_step": ({"present": True, "source": source, "hints": "none"} if present
                             else {"present": False}),
           "meshes_after_default": default if present else None,
           "meshes_after_hints": hints, "meshes_after_gmsh_default": gd,
           "meshes_before_gmsh_default": False}
    if credit:
        row["meshed_by"] = credit
    return row


def _units():
    base = [_row("a", default=True, credit={"by": "pass", "op": "resolve_sub_mesh_gap"}),
            _row("b"), _row("c", hints=True), _row("d", present=False)]
    ai = [_row("a", default=True, credit={"by": "pass", "op": "resolve_sub_mesh_gap"}),
          _row("b", default=True, credit={"by": "loop", "op": "fix_small_faces"}),
          _row("c"), _row("d", present=False)]
    clean = [_row(f"k{i}", before=True, default=True, source="passthrough") for i in range(3)]
    clean[0]["meshes_after_default"] = False
    return {(PASS, "failing"): base, (AI, "failing"): ai, (PASS, "clean"): clean,
            (AI, "clean"): clean}


def test_ai_marginal_counts_both_directions():
    rep = mr.build(_units(), summarise=rc.meshability_summary, comparison=(PASS, AI))
    m = rep["ai_marginal"]
    assert m["paired_files"] == 4
    assert m["default"] == {"ai_only": ["b"], "pass_only": [], "net": 1}
    # c meshes only with hints under the pass, not at all under the AI: the AI LOST it with hints
    assert m["with_hints"] == {"ai_only": ["b"], "pass_only": ["c"], "net": 0}


def test_headlines_and_files_per_arm():
    rep = mr.build(_units(), summarise=rc.meshability_summary, comparison=(PASS, AI))
    p = rep["arms"][PASS]["failing"]
    assert (p["meshes_default"], p["meshes_with_hints"], p["no_returned_step"]) == (1, 2, 1)
    assert rep["arms"][PASS]["files"]["d"]["returned"] is False
    assert rep["arms"][AI]["files"]["b"]["meshed_by"] == {"by": "loop", "op": "fix_small_faces"}
    c = rep["arms"][PASS]["clean"]
    assert c == {"files": 3, "still_meshes_default": 2, "newly_broken_default": 1,
                 "no_returned_step": 0, "valid_to_invalid": 0, "changed": 0}


def test_render_names_both_headlines_and_the_bug_row():
    md = mr.render(mr.build(_units(), summarise=rc.meshability_summary, comparison=(PASS, AI)))
    assert "**STEP meshes at default settings**" in md
    assert "**STEP meshes with the hints**" in md
    assert "NO STEP RETURNED (bug)" in md and "gmsh defaults (secondary)" in md
    assert "AI-only: `b`" in md and "net +1" in md
    assert "2/3 still mesh" in md


def test_no_marginal_without_both_arms():
    units = {(PASS, "failing"): [_row("a")]}
    rep = mr.build(units, summarise=rc.meshability_summary, comparison=(PASS, AI))
    assert "ai_marginal" not in rep
    assert "AI marginal" not in mr.render(rep)


def test_collect_writes_the_report_only_for_meshability_rows(tmp_path, monkeypatch):
    collect = _load("v3_collect_for_report", V3 / "collect.py")
    spec = collect.evalspec
    commit = "a" * 40

    def shard(root, arm, kind, rows):
        d = root / arm / kind / "shard-1-of-1"
        d.mkdir(parents=True)
        prov = {"commit": commit, "image": "img@sha256:" + "b" * 64, "image_commit": "c" * 40,
                "sample_sha256": ("s" if kind == "failing" else "c") * 64, "kind": kind,
                "arm": arm, "shard": "1/1", "exit_code": 0, "machine_type": "c2d-highcpu-4",
                "started_at": "2026-11-21T00:00:00Z", "ended_at": "2026-11-21T00:10:00Z",
                "wall_seconds": 600.0, "operations_catalogue": [], "model_calls": []}
        (d / "PROVENANCE.json").write_text(json.dumps(prov))
        (d / "rows.json").write_text(json.dumps(rows))
        (d / "DONE").write_text("0\n")

    monkeypatch.setattr(spec, "SAMPLE_SIZE", 4)
    monkeypatch.setattr(spec, "CLEAN_SIZE", 3)
    monkeypatch.setattr(collect, "seal", lambda entry, arm, kind, checked, o: o / "sealed")
    src, out = tmp_path / "src", tmp_path / "out"
    for (arm, kind), rows in _units().items():
        shard(src, arm, kind, rows)
    out.mkdir()
    assert collect.main(["--commit", commit, "--from", str(src), "--out", str(out)]) == 0
    rep = json.loads((out / "meshability.json").read_text())
    assert rep["ai_marginal"]["default"]["net"] == 1
    assert "STEP meshes at default settings" in (out / "meshability.md").read_text()
