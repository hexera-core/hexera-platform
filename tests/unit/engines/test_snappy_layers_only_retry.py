# Responsibility: Verify CFDREQ-33 as amended in rulebook 116 - the relative retry adds only the layers to the first
# attempt's kept snapped mesh, rebuilds in full when that mesh is missing, changed or unreadable, and the kept mesh is
# removed once the attempt's choice is made; and a retry prices the near-wake boxes into its budget once.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshpipeline.engines import snapped_keep as SK

_METRES = """castellatedMesh true; snap true; addLayers true;
geometry { body.stl { type triSurfaceMesh; name body; } }
castellatedMeshControls { maxLocalCells 100; maxGlobalCells 100; }
snapControls { nSmoothPatch 3; }
addLayersControls { relativeSizes false; /* relativeOnRetry 0.002; */ layers { body { nSurfaceLayers 10; } }
  firstLayerThickness 0.0001; minThickness 0.00001; }
meshQualityControls { maxNonOrtho 65; minVol 1e-15; relaxed { maxNonOrtho 75; } }
"""
_RELATIVE = """castellatedMesh true; snap true; addLayers true;
geometry { body.stl { type triSurfaceMesh; name body; } }
castellatedMeshControls { maxLocalCells 100; maxGlobalCells 100; }
snapControls { nSmoothPatch 3; }
addLayersControls { relativeSizes true; /* plainWallCell 0.002; */ layers { body { nSurfaceLayers 10; } }
  firstLayerThickness 0.05; minThickness 0.005; }
meshQualityControls { maxNonOrtho 65; minVol 1.95e-18; relaxed { maxNonOrtho 75; } }
"""
_POLY = ("points", "faces", "owner", "neighbour", "boundary")


def _case(ws: Path, shm: str) -> Path:
    (ws / "system").mkdir(parents=True, exist_ok=True)
    (ws / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    (ws / "system" / "snappyHexMeshDict").write_text(shm)
    (ws / "system" / "blockMeshDict").write_text("blocks ( hex (0 1 2 3 4 5 6 7) (10 10 10) );\n")
    (ws / "system" / "controlDict").write_text("startTime 0; deltaT 1; writeFormat ascii; writeCompression off;\n")
    (ws / "constant" / "triSurface" / "body.stl").write_text("solid body\nendsolid body\n")
    return ws


def _mesh(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for f in _POLY:
        (d / f).write_text("x\n")


class _Fake:
    """run_guarded stand-in: makes each stage's outputs and records what snappyHexMesh was given."""

    def __init__(self, snappy_rc=(0,)):
        self.calls: list[str] = []
        self.snappy: list[tuple[str, str]] = []
        self._rc = list(snappy_rc)

    def __call__(self, cmd, *a, **k):
        line = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        ws = Path(k["cwd"])
        self.calls.append(line)
        rc = 0
        if "decomposePar" in line:
            for n in range(2):
                _mesh(ws / f"processor{n}" / "constant" / "polyMesh")
        elif "snappyHexMesh" in line:
            self.snappy.append(((ws / "system" / "snappyHexMeshDict").read_text(),
                                (ws / "system" / "controlDict").read_text()))
            rc = self._rc.pop(0) if self._rc else 0
            if rc == 0:
                for n in range(2):
                    if "-overwrite" in line:
                        _mesh(ws / f"processor{n}" / "constant" / "polyMesh")
                    else:                       # each phase in its own time directory
                        for t in ("1", "2", "3"):
                            _mesh(ws / f"processor{n}" / t / "polyMesh")
                            (ws / f"processor{n}" / t / "polyMesh" / "phase").write_text(t)
        elif "reconstructParMesh" in line or "blockMesh" in line:
            if "reconstructParMesh" in line:
                ph = ws / "processor0" / "constant" / "polyMesh" / "phase"
                self.reconstructed = ph.read_text() if ph.is_file() else None
            _mesh(ws / "constant" / "polyMesh")

        class _Done:
            returncode = rc
        return _Done()


@pytest.fixture
def native(monkeypatch):
    from meshpipeline.engines.snappy import native as N
    monkeypatch.setattr(N, "_foam_version", lambda *a, **k: "v2412")
    monkeypatch.setattr(N, "_rank_count", lambda: 2)
    monkeypatch.setattr(N, "_measure", lambda *a, **k: None)
    monkeypatch.setattr(N, "_export_volume_timed", lambda *a, **k: 0.0)
    return N


def _first_attempt(tmp_path, native, monkeypatch) -> Path:
    ws = _case(tmp_path / "job" / "attempt_1", _METRES)
    fake = _Fake()
    monkeypatch.setattr(native, "run_guarded", fake)
    native._run_snappy_local(ws)
    return ws, fake


def test_an_attempt_that_may_be_retried_relative_keeps_its_snapped_mesh(tmp_path, native, monkeypatch):
    ws, fake = _first_attempt(tmp_path, native, monkeypatch)
    # ONE run as authored, its phases kept (no -overwrite), written in binary
    assert len(fake.snappy) == 1
    (d1, c1), = fake.snappy
    assert d1 == _METRES and "writeFormat binary;" in c1
    snappy_line = next(c for c in fake.calls if "snappyHexMesh" in c)
    assert "-overwrite" not in snappy_line and "-parallel" in snappy_line
    # the snapped phase is kept with its key, the authored controlDict is back, nothing is left at the root
    assert "writeFormat ascii;" in (ws / "system" / "controlDict").read_text()
    rec = json.loads((ws / SK.KEEP_DIR / SK.KEY_FILE).read_text())
    assert rec["nproc"] == 2 and rec["key"] == SK.snap_key(ws)
    assert (ws / SK.KEEP_DIR / "processor1" / "constant" / "polyMesh" / "phase").read_text() == "2"
    assert fake.reconstructed == "3", "the layered phase is the mesh reconstructParMesh joins"
    assert not list(ws.glob("processor*"))
    assert SK.read_reuse(ws)["path"] == "full" and SK.read_reuse(ws)["kept"]["bytes"] > 0


def test_a_plain_attempt_runs_snappy_once_and_keeps_nothing(tmp_path, native, monkeypatch):
    ws = _case(tmp_path / "job" / "attempt_1", _METRES.replace(" /* relativeOnRetry 0.002; */", ""))
    fake = _Fake()
    monkeypatch.setattr(native, "run_guarded", fake)
    native._run_snappy_local(ws)
    assert len(fake.snappy) == 1 and "addLayers true;" in fake.snappy[0][0]
    assert not (ws / SK.KEEP_DIR).exists() and SK.read_reuse(ws) is None


def _retry(tmp_path, shm: str = _RELATIVE) -> Path:
    ws = _case(tmp_path / "job" / "attempt_2", shm)
    (ws / "gate_facts.json").write_text(json.dumps({"gate": "layer_stack", "facts": {"relative_next": True}}))
    return ws


def test_the_relative_retry_asks_for_a_layers_only_run_on_the_kept_mesh(tmp_path, native, monkeypatch):
    first, _ = _first_attempt(tmp_path, native, monkeypatch)
    ws = _retry(tmp_path)
    plan = SK.adopt(ws)
    assert plan["path"] == "layers_only" and plan["from"] == "attempt_1"
    assert (ws / SK.LAYERS_ONLY_FILE).is_file() and not (first / SK.KEEP_DIR).exists()
    fake = _Fake()
    monkeypatch.setattr(native, "run_guarded", fake)
    out = native._run_snappy_local(ws)
    assert not any("decomposePar" in c for c in fake.calls), "the kept decomposition is the one used"
    assert len(fake.snappy) == 1
    assert "castellatedMesh false; snap false; addLayers true;" in fake.snappy[0][0]
    assert "relativeSizes true" in fake.snappy[0][0]               # the retry's own layer stage
    assert out["layer_reuse"]["path"] == "layers_only"
    assert not (ws / SK.KEEP_DIR).exists() and not list(ws.glob("processor*"))
    assert (ws / "system" / "snappyHexMeshDict").read_text() == _RELATIVE


def test_the_retry_rebuilds_in_full_when_no_mesh_was_kept(tmp_path, native, monkeypatch):
    ws = _retry(tmp_path)
    plan = SK.adopt(ws)
    assert plan == {"path": "full", "rule": "CFDREQ-33", "why": "the first attempt kept no snapped mesh"}
    fake = _Fake()
    monkeypatch.setattr(native, "run_guarded", fake)
    out = native._run_snappy_local(ws)
    assert any("decomposePar" in c for c in fake.calls)
    assert len(fake.snappy) == 1 and "castellatedMesh true; snap true; addLayers true;" in fake.snappy[0][0]
    assert out["layer_reuse"]["path"] == "full"


def test_the_retry_rebuilds_in_full_when_its_castellation_differs(tmp_path, native, monkeypatch):
    first, _ = _first_attempt(tmp_path, native, monkeypatch)
    ws = _retry(tmp_path, _RELATIVE.replace("maxGlobalCells 100", "maxGlobalCells 200"))
    plan = SK.adopt(ws)
    assert plan["path"] == "full" and "castellation or snapping" in plan["why"]
    assert not (first / SK.KEEP_DIR).exists(), "the unused kept mesh is removed"


def test_the_retry_rebuilds_in_full_when_the_kept_mesh_is_incomplete(tmp_path, native, monkeypatch):
    first, _ = _first_attempt(tmp_path, native, monkeypatch)
    (first / SK.KEEP_DIR / "processor0" / "constant" / "polyMesh" / "faces").unlink()
    plan = SK.adopt(_retry(tmp_path))
    assert plan["path"] == "full" and "incomplete" in plan["why"]


def test_an_unreadable_kept_mesh_falls_back_to_the_whole_mesh(tmp_path, native, monkeypatch):
    _first_attempt(tmp_path, native, monkeypatch)
    ws = _retry(tmp_path)
    assert SK.adopt(ws)["path"] == "layers_only"
    fake = _Fake(snappy_rc=(1, 0))                 # the layers-only run fails; the full run then builds
    monkeypatch.setattr(native, "run_guarded", fake)
    out = native._run_snappy_local(ws)
    assert len(fake.snappy) == 2 and any("decomposePar" in c for c in fake.calls)
    assert "castellatedMesh true; snap true; addLayers true;" in fake.snappy[1][0]
    assert out["layer_reuse"]["path"] == "full" and "exited 1" in out["layer_reuse"]["why"]
    assert out["rc"] == 0


def test_the_kept_mesh_is_removed_once_the_choice_is_made(tmp_path, native, monkeypatch):
    ws, _ = _first_attempt(tmp_path, native, monkeypatch)
    SK.settle(ws, refused_gate="layer_stack", facts={"relative_next": True})
    assert (ws / SK.KEEP_DIR).is_dir(), "kept for the relative retry"
    SK.settle(ws, refused_gate="layer_stack", facts={})
    assert not (ws / SK.KEEP_DIR).exists()
    ws2, _ = _first_attempt(tmp_path / "b", native, monkeypatch)
    SK.settle(ws2, refused_gate=None, facts=None)              # its gates passed
    assert not (ws2 / SK.KEEP_DIR).exists()


def test_the_kept_mesh_is_the_first_thing_left_out_of_an_archive_over_the_ceiling(tmp_path, native, monkeypatch):
    from meshpipeline.engines import return_scope as RS
    ws, _ = _first_attempt(tmp_path, native, monkeypatch)
    rec = RS.scope_return(ws, ceiling=1)
    assert rec["snapped_mesh_left_out"]["dir"] == SK.KEEP_DIR + "/"
    assert not (ws / SK.KEEP_DIR).exists() and (ws / "constant" / "polyMesh" / "owner").is_file()


def test_a_retry_inherits_the_plans_budget_not_the_raised_one(tmp_path):
    from meshpipeline.engines.snappy import drivers as D
    a1 = tmp_path / "attempt_1"
    a2 = tmp_path / "attempt_2"
    a1.mkdir()
    a2.mkdir()
    # the render raised 4,000,000 for the near wake and the near-body box; the plan memory keeps both
    (a1 / ".last_plan.json").write_text(json.dumps({"max_cells": 4_345_373, D._PLAN_MAX_CELLS: 4_000_000}))
    assert D._inherit_durable_plan_fields({}, a2)["max_cells"] == 4_000_000
    # an older memory without the plan's own budget is read as before
    (a1 / ".last_plan.json").write_text(json.dumps({"max_cells": 4_345_373}))
    assert D._inherit_durable_plan_fields({}, a2)["max_cells"] == 4_345_373
