# Responsibility: Verify our gmsh mesh applies a returned STEP's hints only when the sidecar names the exact bytes being meshed, in the staged solid's unit, and is untouched without them.
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.engines.gmsh import staged_hints as H


def _step(tmp_path: Path, body: bytes = b"ISO-10303-21; part") -> Path:
    p = tmp_path / "part.step"
    p.write_bytes(body)
    return p


def _sidecar(tmp_path: Path, step: Path, *, sha: str | None = None, faces=None) -> Path:
    digest = sha if sha is not None else hashlib.sha256(step.read_bytes()).hexdigest()
    doc = {"schema": "crs-mesh-hints/1", "units": "mm", "step": {"name": step.name, "sha256": digest},
           "diagonal_mm": 100.0, "status": "verified",
           "faces": faces if faces is not None else [
               {"face": 3, "size_mm": 0.5, "why": "short_edge",
                "fingerprint": {"area_mm2": 200.0, "centroid": [10.0, 20.0, 30.0]}}]}
    side = step.with_name(step.name + ".mesh-hints.json")
    side.write_text(json.dumps(doc))
    return side


# STAGING (host side): the sha decides


def test_a_sidecar_for_these_bytes_is_staged_in_the_staged_solids_unit(tmp_path):
    step = _step(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    said = H.stage(ws, geometry_path=step, hints_path=_sidecar(tmp_path, step), to_metres=1e-3)
    assert said == {"staged": True, "why": "", "faces": 1}
    doc = H.read_staged(ws)
    assert doc is not None
    face = doc["faces"][0]
    # lengths times to_metres, areas times its square: the staged solid is in metres
    assert face["size_mm"] == pytest.approx(0.5e-3)
    assert face["fingerprint"]["area_mm2"] == pytest.approx(200.0e-6)
    assert face["fingerprint"]["centroid"] == pytest.approx([10e-3, 20e-3, 30e-3])
    assert doc["diagonal"] == pytest.approx(0.1)


def test_a_sidecar_for_other_bytes_is_never_staged(tmp_path):
    step = _step(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    said = H.stage(ws, geometry_path=step, hints_path=_sidecar(tmp_path, step, sha="0" * 64),
                   to_metres=1e-3)
    assert said["staged"] is False and "different STEP bytes" in said["why"]
    assert not (ws / H.HINTS_FILE).exists()


def test_no_sidecar_stages_nothing_and_clears_a_stale_copy(tmp_path):
    step = _step(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / H.HINTS_FILE).write_text("{}")         # carried from an earlier attempt
    assert H.stage(ws, geometry_path=step, hints_path="", to_metres=1.0)["staged"] is False
    assert not (ws / H.HINTS_FILE).exists()


def test_an_unreadable_sidecar_is_no_hint_never_a_failure(tmp_path):
    step = _step(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "something-else/9"}))
    said = H.stage(ws, geometry_path=step, hints_path=bad, to_metres=1.0)
    assert said["staged"] is False and said["why"].startswith("sidecar unreadable")
    assert not (ws / H.HINTS_FILE).exists()


def test_the_builder_stages_hints_from_the_run_and_only_for_matching_bytes(tmp_path, monkeypatch):
    from meshpipeline.agents.builder import attempt as A

    step = _step(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr("meshpipeline.cad.staging.staged_surface", lambda g, d: SimpleNamespace(
        consumed=SimpleNamespace(to_metres=1e-3)))
    geometry = SimpleNamespace(path=str(step))
    side = _sidecar(tmp_path, step)
    state = {"repair_attempt": {"returned": {"path": str(step), "source": "passthrough",
                                             "hints_path": str(side)}}}
    A._stage_mesh_hints(ws, geometry, state)
    assert H.read_staged(ws)["source_sha256"] == hashlib.sha256(step.read_bytes()).hexdigest()

    # the run meshes OTHER bytes (a promoted repair whose sidecar was for the passthrough): ignored
    other = tmp_path / "other.step"
    other.write_bytes(b"ISO-10303-21; different")
    A._stage_mesh_hints(ws, SimpleNamespace(path=str(other)), state)
    assert not (ws / H.HINTS_FILE).exists()

    # no returned record at all: nothing is done
    A._stage_mesh_hints(ws, geometry, {})
    assert not (ws / H.HINTS_FILE).exists()


# APPLYING (mesher side)


class _Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"gmsh.{name} touched with no hints staged")


def test_with_nothing_staged_the_mesher_touches_no_gmsh_state(tmp_path):
    field, record = H.apply(_Untouchable(), tmp_path, background=7)
    assert (field, record) == (7, None)


def _box_model(gmsh):
    gmsh.model.add("hints")
    gmsh.model.occ.addBox(0, 0, 0, 10, 20, 30)
    gmsh.model.occ.synchronize()
    tag = sorted(t for _d, t in gmsh.model.getEntities(2))[0]
    area = gmsh.model.occ.getMass(2, tag)
    c = gmsh.model.occ.getCenterOfMass(2, tag)
    return tag, area, list(c)


@pytest.fixture
def gmsh_session():
    gmsh = pytest.importorskip("gmsh")
    gmsh.initialize(interruptible=False)
    gmsh.option.setNumber("General.Terminal", 0)
    try:
        yield gmsh
    finally:
        gmsh.finalize()


def _stage_raw(ws: Path, faces: list, diagonal: float) -> None:
    (ws / H.HINTS_FILE).write_text(json.dumps({"schema": H.STAGED_SCHEMA, "diagonal": diagonal,
                                               "source_sha256": "abc", "faces": faces}))


def test_a_staged_hint_is_applied_to_its_face_under_the_plans_own_field(tmp_path, gmsh_session):
    gmsh = gmsh_session
    tag, area, centroid = _box_model(gmsh)
    diag = (10**2 + 20**2 + 30**2) ** 0.5
    # the sidecar numbered the face differently (gmsh's tags are not OCC's indices): matched by fingerprint
    _stage_raw(tmp_path, [{"face": 99, "size_mm": 0.25,
                           "fingerprint": {"area_mm2": area, "centroid": centroid}},
                          {"face": 5, "size_mm": 0.25,
                           "fingerprint": {"area_mm2": 1.0, "centroid": [500.0, 0.0, 0.0]}}], diag)
    plan = gmsh.model.mesh.field.add("Constant")
    gmsh.model.mesh.field.setNumber(plan, "VIn", 3.0)

    field, record = H.apply(gmsh, tmp_path, background=plan)

    assert record["applied"] == 1 and record["unmapped"] == [5]
    assert field not in (None, plan)
    # one Min over the plan's field and the hints' own Min: a hint only refines
    assert gmsh.model.mesh.field.getType(field) == "Min"
    assert plan in [int(f) for f in gmsh.model.mesh.field.getNumbers(field, "FieldsList")]


def test_a_model_read_at_another_scale_is_hinted_at_that_scale(tmp_path, gmsh_session):
    """MESH-4 reads the staged solid again at the CAD's millimetres: the staged metres times 1000."""
    gmsh = gmsh_session
    tag, area, centroid = _box_model(gmsh)
    k = 1000.0
    diag = (10**2 + 20**2 + 30**2) ** 0.5
    # staged as if this model were the staged solid at 1/k
    _stage_raw(tmp_path, [{"face": tag, "size_mm": 0.25 / k,
                           "fingerprint": {"area_mm2": area / k / k,
                                           "centroid": [c / k for c in centroid]}}], diag / k)
    field, record = H.apply(gmsh, tmp_path, scale=k, background=None)
    assert record["applied"] == 1 and record["unmapped"] == []
    assert field is not None
    assert gmsh.model.mesh.field.getNumber(
        [f for f in gmsh.model.mesh.field.list() if gmsh.model.mesh.field.getType(f) == "Constant"][0],
        "VIn") == pytest.approx(0.25)
