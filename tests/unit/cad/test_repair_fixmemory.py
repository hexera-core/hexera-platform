# Responsibility: Verify the fix memory's shadow is advisory in fact, not just in intent: off does
# no work, a broken snapshot or a failing prediction costs a row and never the run, and every
# prediction lands beside the verdict of the attempt it preceded.
from __future__ import annotations

import gzip
import json

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import fixmemory
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.fixmemory import ShadowScorer, _load, predict_kept
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import run
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.patchdesc import DESCRIPTOR_VERSION
from meshpipeline.cad.repair.plan import Candidate
from meshpipeline.cad.repair.session import RepairSession

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)


@pytest.fixture(autouse=True)
def _fresh_cache():
    fixmemory._CACHE.clear()
    yield
    fixmemory._CACHE.clear()


def _snapshot_file(tmp_path, *, version=DESCRIPTOR_VERSION, dims=3):
    payload = {
        "descriptor_version": version,
        "mean": [0.0] * dims, "sd": [1.0] * dims,
        "defects": [
            {"c": "small_edge", "v": [0.0] * dims, "ops": {"fix_small_edges": 1}},
            {"c": "small_edge", "v": [0.1] * dims, "ops": {"fix_small_edges": 1}},
            {"c": "small_edge", "v": [5.0] * dims, "ops": {"fix_small_edges": 0}},
        ],
        "priors": {"small_edge|fix_small_edges": 0.5},
    }
    path = tmp_path / "memory.json.gz"
    path.write_bytes(gzip.compress(json.dumps(payload).encode()))
    return str(path)


def test_prediction_follows_the_near_neighbours_and_shrinks_to_the_prior(tmp_path):
    snap = _load(_snapshot_file(tmp_path))
    near = predict_kept(snap, "small_edge", [0.05, 0.05, 0.05], "fix_small_edges")
    far = predict_kept(snap, "small_edge", [5.0, 5.0, 5.0], "fix_small_edges")
    assert near > 0.6          # beside two keeps
    assert far < 0.5           # beside the one failure
    # an op the memory never judged for this code answers with the prior default
    assert predict_kept(snap, "small_edge", [0.0] * 3, "sew_faces") == 0.5


def test_mismatched_descriptor_or_missing_file_disables_shadow_quietly(tmp_path):
    assert _load(_snapshot_file(tmp_path, version="hand-v0")) is None
    assert _load(str(tmp_path / "nowhere.json.gz")) is None


def _defect(fingerprint="edge:a"):
    return EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.error,
                        entity_type="edge", entity_index=1, message="x",
                        fingerprint=fingerprint)


class _Op:
    name = "fix_small_edges"
    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def targets_for(self, defect):
        return (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape="fixed")


class _OncePlanner:
    label = "once"

    def __init__(self):
        self._op = _Op()

    def propose(self, defects, history, *, shape):
        for d in defects:
            if d.code in self._op.answers and not history.was_ineffective(
                    d.fingerprint, self._op.name):
                return Candidate(op=self._op, targets=self._op.targets_for(d),
                                 defect_code=d.code, defect_fingerprint=d.fingerprint)
        return None


def _session():
    scans = [([_defect()], True), ([], True)]
    queue = list(scans)

    def scanner(_shape, valid=None):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return RepairSession("original", measurer=lambda _s: _SOUND, scanner=scanner)


def test_off_is_off_and_shadow_rows_join_predictions_to_verdicts(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RAG", "off")
    off = run("original", planner=_OncePlanner(), tolerance_mm=0.1, session=_session())
    assert off.shadow == ()

    # Shadow on, over a fake shape the kernel cannot read: the prediction FAILS, the row records
    # the failure, and the run's verdicts are byte-for-byte what off produced. That is the
    # fail-open contract, tested on its worst path rather than its best.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RAG", "shadow")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RAG_MEMORY_PATH", _snapshot_file(tmp_path))
    shadowed = run("original", planner=_OncePlanner(), tolerance_mm=0.1, session=_session())
    assert [v.to_dict() for v in shadowed.verdicts] == [v.to_dict() for v in off.verdicts]
    assert len(shadowed.shadow) == 1
    row = shadowed.shadow[0]
    assert row["op"] == "fix_small_edges"
    assert row["defect_code"] == "small_edge"
    assert row["status"] == shadowed.verdicts[0].status
    assert row["p_kept"] is None and "error" in row


def test_unreadable_snapshot_means_no_scorer_and_an_unchanged_run(monkeypatch, tmp_path):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RAG", "shadow")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RAG_MEMORY_PATH", str(tmp_path / "absent.gz"))
    result = run("original", planner=_OncePlanner(), tolerance_mm=0.1, session=_session())
    assert result.shadow == ()
    assert result.verdicts  # the repair itself still ran


def test_shadow_budget_stops_prediction_but_keeps_counting(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RAG_MEMORY_PATH", _snapshot_file(tmp_path))
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)
    scorer = ShadowScorer()
    scorer._spent_s = 1e9
    scorer.observe("shape", _defect(), "fix_small_edges")
    scorer.resolve("kept")
    assert scorer.close() == ({"op": "fix_small_edges", "defect_code": "small_edge",
                               "fingerprint": "edge:a", "p_kept": None,
                               "skipped": "shadow_budget", "status": "kept"},)
