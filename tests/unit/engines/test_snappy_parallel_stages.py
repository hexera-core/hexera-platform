# Responsibility: Verify a parallel snappy run fails closed - residue, an unknown non-zero or a stale mesh is no pass.
from __future__ import annotations

import re

from meshpipeline.engines.snappy import parallel_stages as ps
from meshpipeline.engines.snappy.parallel_stages import REQUIRED_POLYMESH, StageResult, classify


def _stage(name, rc=0, timed_out=False, authoritative=True):
    return StageResult(name=name, command=name, log=f"{name}.log", rc=rc,
                       timed_out=timed_out, dt_s=1.0, authoritative=authoritative)


def _complete_polymesh(ws):
    poly = ws / "constant" / "polyMesh"
    poly.mkdir(parents=True, exist_ok=True)
    for f in REQUIRED_POLYMESH:
        (poly / f).write_text("x\n")           # non-empty
    return ws


def _parallel_ok():
    return [_stage("decomposePar"), _stage("snappyHexMesh"), _stage("reconstructParMesh")]


# 1
def test_all_three_stages_zero_with_complete_mesh_is_success(tmp_path):
    _complete_polymesh(tmp_path)
    v = classify(tmp_path, _parallel_ok())
    assert v["rc"] == 0 and v["timed_out"] is False
    assert v["failing_stage"] is None and v["benign"] is None


# 2
def test_decomposePar_failure_is_isolated(tmp_path):
    stages = [_stage("decomposePar", rc=1)]
    v = classify(tmp_path, stages)
    assert v["rc"] == 1 and v["failing_stage"] == "decomposePar"


# 3
def test_mpirun_failure_with_incomplete_output_fails(tmp_path):
    # no polyMesh written - snappyHexMesh aborted
    stages = [_stage("decomposePar"), _stage("snappyHexMesh", rc=139)]
    v = classify(tmp_path, stages)
    assert v["rc"] == 139 and v["failing_stage"] == "snappyHexMesh"
    assert v["polymesh"]["complete"] is False


# 4
def test_reconstructParMesh_failure_is_isolated(tmp_path):
    stages = [_stage("decomposePar"), _stage("snappyHexMesh"), _stage("reconstructParMesh", rc=1)]
    v = classify(tmp_path, stages)
    assert v["rc"] == 1 and v["failing_stage"] == "reconstructParMesh"


# 5
def test_all_stages_zero_but_checkmesh_fails_is_rejected(tmp_path):
    _complete_polymesh(tmp_path)
    v = classify(tmp_path, _parallel_ok(), checkmesh_ok=lambda: False)
    assert v["rc"] == -3 and v["failing_stage"] == "checkMesh"


# 6
def test_a_reproduced_benign_nonzero_is_reclassified_only_when_fully_verified(tmp_path, monkeypatch):
    cond = ps.BenignCondition(
        stage="reconstructParMesh", code=1, reason="known benign v2412 reconstruct warning",
        foam_versions=("v2412",), pattern=re.compile(r"benign-marker"))
    monkeypatch.setattr(ps, "_BENIGN_PARALLEL_CONDITIONS", (cond,))
    _complete_polymesh(tmp_path)
    stages = [_stage("decomposePar"), _stage("snappyHexMesh"), _stage("reconstructParMesh", rc=1)]
    logs = {"reconstructParMesh": "... benign-marker ...\n"}

    v = classify(tmp_path, stages, foam_version="v2412", logs=logs, checkmesh_ok=lambda: True)
    assert v["rc"] == 0 and v["benign"] and v["benign"]["stage"] == "reconstructParMesh"
    assert v["benign"]["rc"] == 1 and v["benign"]["foam_version"] == "v2412"

    # gate 1: wrong toolchain version → NOT benign
    assert classify(tmp_path, stages, foam_version="v2312", logs=logs,
                    checkmesh_ok=lambda: True)["rc"] == 1
    # gate 2: checkMesh fails → NOT benign
    assert classify(tmp_path, stages, foam_version="v2412", logs=logs,
                    checkmesh_ok=lambda: False)["rc"] == 1
    # gate 3: incomplete mesh → NOT benign
    (tmp_path / "constant" / "polyMesh" / "owner").unlink()
    assert classify(tmp_path, stages, foam_version="v2412", logs=logs,
                    checkmesh_ok=lambda: True)["rc"] == 1


# 7
def test_an_unknown_nonzero_with_complete_looking_output_fails_closed(tmp_path):
    _complete_polymesh(tmp_path)
    stages = [_stage("decomposePar"), _stage("snappyHexMesh"), _stage("reconstructParMesh", rc=1)]
    v = classify(tmp_path, stages, foam_version="v2412", checkmesh_ok=lambda: True)
    assert v["rc"] == 1 and v["failing_stage"] == "reconstructParMesh" and v["benign"] is None


# 8
def test_timeout_with_processor_dirs_left_behind_fails(tmp_path):
    (tmp_path / "processor0").mkdir(parents=True)
    (tmp_path / "processor1").mkdir(parents=True)
    stages = [_stage("decomposePar"), _stage("snappyHexMesh", timed_out=True, rc=-1)]
    v = classify(tmp_path, stages)
    assert v["rc"] == -1 and v["timed_out"] is True and v["failing_stage"] == "snappyHexMesh"
    assert v["residue"] == ["processor0", "processor1"]


# extra guards
def test_all_zero_but_processor_residue_is_not_success(tmp_path):
    _complete_polymesh(tmp_path)
    (tmp_path / "processor0").mkdir()
    v = classify(tmp_path, _parallel_ok())
    assert v["rc"] == -3 and v["failing_stage"] == "cleanup"


def test_a_stale_mesh_from_a_prior_attempt_is_not_counted(tmp_path):
    _complete_polymesh(tmp_path)
    import os
    old = 1_000.0
    for f in REQUIRED_POLYMESH:
        os.utime(tmp_path / "constant" / "polyMesh" / f, (old, old))
    v = classify(tmp_path, _parallel_ok(), attempt_started=1_000_000.0)
    assert v["rc"] == -3 and "stale" in v["note"]


def test_the_production_benign_registry_is_empty_strict_by_default():
    assert ps._BENIGN_PARALLEL_CONDITIONS == ()
