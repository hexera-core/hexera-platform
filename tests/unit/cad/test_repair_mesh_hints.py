# Responsibility: Pin the mesh-hints policy - matching, seeding, the bounded refinement loop, the
#                 sidecar contract and the two-number probe - with fakes for OCC and gmsh.
# Boundaries: hermetic. The real kernels and the STEP round trip are the native tier's.
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from meshpipeline.cad.repair import bounded_child, mesh_hints, mesh_probe
from meshpipeline.cad.repair.mesh_clash import Clash
from meshpipeline.cad.repair.mesh_probe import MeshFailure, ProbeResult

DIAG = 100.0


def _row(face, area, centre, shortest=2.0):
    x, y, z = centre
    return {"face": face, "surface": "plane", "area_mm2": area, "centroid": [x, y, z],
            "bbox": [x - 1, y - 1, z - 1, x + 1, y + 1, z + 1], "shortest_edge_mm": shortest}


TABLE = [_row(1, 10.0, (0, 0, 0)), _row(2, 20.0, (5, 0, 0), shortest=None),
         _row(3, 30.0, (0, 5, 0), shortest=4.0), _row(4, 40.0, (0, 0, 5))]


def _props(row):
    return mesh_hints._key(row)


# --- matching ----------------------------------------------------------------------------------

def test_same_face_needs_area_and_centre():
    a = (10.0, 1.0, 2.0, 3.0)
    assert mesh_hints.same_face(a, (10.0 * (1 + 1e-7), 1.0, 2.0, 3.0 + 1e-6), diagonal_mm=DIAG)
    assert not mesh_hints.same_face(a, (10.1, 1.0, 2.0, 3.0), diagonal_mm=DIAG)
    assert not mesh_hints.same_face(a, (10.0, 1.0, 2.0, 3.1), diagonal_mm=DIAG)


def test_match_prefers_its_own_number_then_the_one_unique_match():
    props = {r["face"]: _props(r) for r in TABLE}
    assert mesh_hints.match(2, _props(TABLE[1]), props, diagonal_mm=DIAG) == 2
    # gmsh dropped a face, so tags shifted: the fingerprint still finds it.
    shifted = {k - 1: v for k, v in props.items() if k > 1}
    assert mesh_hints.match(3, _props(TABLE[2]), shifted, diagonal_mm=DIAG) == 2
    # Two identical surfaces and the own number matches neither: refused, never guessed.
    twins = {7: _props(TABLE[0]), 8: _props(TABLE[0])}
    assert mesh_hints.match(1, _props(TABLE[0]), twins, diagonal_mm=DIAG) is None


class _FakeGmsh:
    """Just enough of gmsh's model for `apply_in_gmsh`."""

    def __init__(self, props):
        self.props = props
        self.fields: list[tuple] = []
        self.background = None
        model = SimpleNamespace()
        model.getEntities = lambda dim: [(2, t) for t in sorted(props)]
        model.occ = SimpleNamespace(getMass=lambda d, t: props[t][0],
                                    getCenterOfMass=lambda d, t: props[t][1:])
        field = SimpleNamespace(add=self._add, setNumbers=self._set, setNumber=self._set,
                                setAsBackgroundMesh=self._bg)
        model.mesh = SimpleNamespace(field=field)
        self.model = model

    def _add(self, kind):
        self.fields.append((kind, {}))
        return len(self.fields)

    def _set(self, tag, name, value):
        self.fields[tag - 1][1][name] = value

    def _bg(self, tag):
        self.background = tag


def _hint(row, size=1.0, face=None):
    return {"face": row["face"] if face is None else face, "size_mm": size,
            "fingerprint": {k: row[k] for k in ("surface", "area_mm2", "centroid", "bbox")}}


def test_apply_in_gmsh_sets_a_constant_field_per_matched_surface():
    gmsh = _FakeGmsh({1: _props(TABLE[0]), 2: _props(TABLE[2])})   # face 2 absent in gmsh
    applied, unmapped = mesh_hints.apply_in_gmsh(
        gmsh, [_hint(TABLE[0], 0.5), _hint(TABLE[1]), _hint(TABLE[2], 0.25)], diagonal_mm=DIAG)
    assert (applied, unmapped) == (2, [2])
    constants = [f for kind, f in gmsh.fields if kind == "Constant"]
    assert [(c["SurfacesList"], c["VIn"]) for c in constants] == [([1], 0.5), ([2], 0.25)]
    assert gmsh.fields[gmsh.background - 1][0] == "Min"


def test_apply_in_gmsh_with_nothing_matched_sets_no_background():
    gmsh = _FakeGmsh({1: _props(TABLE[3])})
    assert mesh_hints.apply_in_gmsh(gmsh, [_hint(TABLE[0])], diagonal_mm=DIAG) == (0, [1])
    assert gmsh.background is None


# --- seeding -----------------------------------------------------------------------------------

def _defect(code, box_of, **measurements):
    r = box_of
    return SimpleNamespace(code=SimpleNamespace(value=code), entity_type="face",
                           location={"bbox_min": r["bbox"][:3], "bbox_max": r["bbox"][3:]},
                           measurements=measurements, fingerprint="fp")


def test_seed_sizes_mesh_defects_only_and_respects_floor_and_gap():
    defects = [_defect("mesh_overlapping_facets", TABLE[0]),
               _defect("mesh_edge_through_face", TABLE[2], clash_gap_mm=0.5,
                       clash_kind="crossing"),
               _defect("mesh_other", TABLE[1]),
               _defect("small_face", TABLE[3])]      # not a mesh defect: not sized
    hints, unplaced = mesh_hints.seed_from_defects(defects, TABLE, diagonal_mm=DIAG)
    assert unplaced == 0
    assert sorted(hints) == [1, 2, 3]
    assert (hints[1].size_mm, hints[1].why) == (1.0, mesh_hints.WHY_SHORT_EDGE)
    assert (hints[3].size_mm, hints[3].why) == (0.5, mesh_hints.WHY_SUB_MESH_GAP)
    assert hints[3].source["clash_kind"] == "crossing"
    # Face 2's only edge is below the floor: a located face at half the default size.
    assert hints[2].why == mesh_hints.WHY_LOCATED_FACE
    assert hints[2].size_mm == pytest.approx(DIAG / mesh_hints.DEFAULT_DIVISOR / 2)


def test_a_defect_whose_box_matches_no_face_is_unplaced():
    far = _row(9, 1.0, (50, 50, 50))
    _hints, unplaced = mesh_hints.seed_from_defects(
        [_defect("mesh_other", far)], TABLE, diagonal_mm=DIAG)
    assert unplaced == 1


def test_seed_size_is_clamped_between_floor_and_default():
    floor, default = DIAG * mesh_hints.FLOOR_FRACTION, DIAG / mesh_hints.DEFAULT_DIVISOR
    assert mesh_hints._seed_size({"shortest_edge_mm": 1e-9}, default_mm=default,
                                 floor_mm=floor)[0] == floor
    assert mesh_hints._seed_size({"shortest_edge_mm": 1e6}, default_mm=default,
                                 floor_mm=floor)[0] == default


# --- the refinement loop -----------------------------------------------------------------------

def _failure(tags, *, clashes=(), kind=mesh_probe.OVERLAPPING_FACETS):
    named = {t: _props(TABLE[t - 1]) for t in {*tags, *(x for c in clashes for x in c)}}
    return ProbeResult(False, 0, MeshFailure(kind, tuple(tags), None, "x", stage="3d"), {},
                       tuple(Clash(surfaces=c, hits=1, point=(0, 0, 0), kind="crossing")
                             for c in clashes), named_props=named)


@pytest.fixture()
def step(tmp_path, monkeypatch):
    path = tmp_path / "part.step"
    path.write_bytes(b"ISO-10303-21;")
    monkeypatch.setattr(mesh_hints, "face_table",
                        lambda p, budget_s: {"diagonal_mm": DIAG, "faces": TABLE})
    return path


def _script(monkeypatch, answers):
    seen: list = []

    def fake(path, *, diagonal_mm, hints=None, quality=False, budget_s=0.0, **_):
        seen.append([dict(h) for h in hints or []])
        answer = answers[min(len(seen), len(answers)) - 1]
        return answer

    monkeypatch.setattr(mesh_probe, "probe_file", fake)
    return seen


def test_loop_refines_until_it_meshes_and_records_every_attempt(step, monkeypatch):
    seen = _script(monkeypatch, [_failure([1]), _failure([1], clashes=[(1, 3)]),
                                 ProbeResult(True, 99, None, {})])
    path = mesh_hints.hints_for(step, ())
    assert path == step.with_name("part.step.mesh-hints.json")
    doc = json.loads(path.read_text())
    assert doc["schema"] == "crs-mesh-hints/1" and doc["units"] == "mm"
    assert doc["face_id"] == mesh_hints.FACE_ID
    assert doc["status"] == mesh_hints.STATUS_VERIFIED
    assert doc["attempts"] == 3 == len(doc["attempt_log"])
    assert doc["face_count"] == len(TABLE)
    assert doc["global_max_mm"] is None
    # attempt 0: no hints; 1: face 1 seeded; 2: face 1 halved, its clash partner 3 seeded
    assert [len(s) for s in seen] == [0, 1, 2]
    sizes = {f["face"]: f["size_mm"] for f in doc["faces"]}
    assert sizes == {1: 0.5, 3: 2.0}
    # What is written is exactly what the meshing attempt probed.
    assert {h["face"]: h["size_mm"] for h in seen[-1]} == sizes


def test_a_face_named_again_refines_its_known_partners(step, monkeypatch):
    seen = _script(monkeypatch, [_failure([], clashes=[(1, 3)]), _failure([1]),
                                 ProbeResult(True, 9, None, {})])
    mesh_hints.hints_for(step, ())
    assert {h["face"]: h["size_mm"] for h in seen[-1]} == {1: 0.5, 3: 1.0}


def test_loop_is_bounded_by_attempts_and_never_refines_after_the_last(step, monkeypatch):
    seen = _script(monkeypatch, [_failure([1])])
    doc = json.loads(mesh_hints.hints_for(step, (), max_attempts=3).read_text())
    assert doc["status"] == mesh_hints.STATUS_UNVERIFIED
    assert doc["stopped"] == "max_attempts"
    assert doc["attempts"] == 3
    assert {f["face"]: f["size_mm"] for f in doc["faces"]} == \
        {h["face"]: h["size_mm"] for h in seen[-1]}


def test_loop_stops_at_the_floor(step, monkeypatch):
    _script(monkeypatch, [_failure([2])])     # face 2 has no edge above the floor: half default
    doc = json.loads(mesh_hints.hints_for(step, (), max_attempts=20).read_text())
    assert doc["stopped"] == "nothing_to_refine"
    assert doc["faces"][0]["size_mm"] == pytest.approx(DIAG * mesh_hints.FLOOR_FRACTION)


def test_loop_stops_at_the_wall(step, monkeypatch):
    _script(monkeypatch, [_failure([1])])
    assert mesh_hints.hints_for(step, (), budget_s=0.0) is None   # no attempt, no hint


def test_a_part_that_meshes_at_default_gets_no_sidecar(step, monkeypatch):
    stale = mesh_hints.sidecar_path(step)
    stale.write_text("{}")
    _script(monkeypatch, [ProbeResult(True, 5, None, {})])
    assert mesh_hints.hints_for(step, ()) is None
    assert not stale.exists()


def test_an_unreadable_step_gets_no_sidecar(step, monkeypatch):
    monkeypatch.setattr(mesh_hints, "face_table", lambda p, budget_s: None)
    assert mesh_hints.hints_for(step, ()) is None


def test_a_timeout_stops_the_loop(step, monkeypatch):
    _script(monkeypatch, [_failure([1]), ProbeResult(False, 0, MeshFailure(
        mesh_probe.TIMEOUT, (), None, "t"), {})])
    doc = json.loads(mesh_hints.hints_for(step, ()).read_text())
    assert doc["stopped"] == "no_verdict"
    assert doc["attempts"] == 2


# --- the sidecar and the two numbers -----------------------------------------------------------

def test_read_hints_refuses_another_schema(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"schema": "crs-mesh-hints/2"}))
    with pytest.raises(ValueError):
        mesh_hints.read_hints(path)


def _sidecar(step, faces, *, sha=None):
    doc = {"schema": mesh_hints.SCHEMA, "units": "mm", "diagonal_mm": DIAG, "faces": faces,
           "step": {"name": step.name, "sha256": sha or mesh_hints._sha256(step)}}
    mesh_hints.sidecar_path(step).write_text(json.dumps(doc))


def test_probe_with_hints_reports_both_numbers(step, monkeypatch):
    _sidecar(step, [_hint(TABLE[0])])
    calls = []

    def fake(path, *, diagonal_mm, hints=None, quality=False, budget_s=0.0, **_):
        calls.append(hints)
        return ProbeResult(hints is not None, 1, None if hints else MeshFailure("other", (), None,
                                                                               "x"), {})

    monkeypatch.setattr(mesh_probe, "probe_file", fake)
    verdict = mesh_hints.probe_with_hints(step)
    assert (verdict.meshes_default, verdict.meshes_with_hints) == (False, True)
    assert calls[0] is None and calls[1][0]["face"] == 1


def test_probe_with_hints_ignores_a_sidecar_for_other_bytes(step, monkeypatch):
    _sidecar(step, [_hint(TABLE[0])], sha="0" * 64)
    monkeypatch.setattr(mesh_probe, "probe_file",
                        lambda path, **kw: ProbeResult(False, 0, MeshFailure("o", (), None, ""), {}))
    verdict = mesh_hints.probe_with_hints(step)
    assert verdict.meshes_with_hints is None
    assert "different" in verdict.note


def test_probe_without_hints_passes_no_options(monkeypatch, tmp_path):
    seen = {}

    def fake(path, diagonal, surfaces, volume, budget, **kw):
        seen.update(kw)
        return {"volume_elements": 1, "failure": None, "tag_boxes": {}}

    monkeypatch.setattr(mesh_probe, "_run_child", fake)
    assert mesh_probe.probe_file(tmp_path / "p.step", diagonal_mm=1.0).meshed
    assert seen == {}
    mesh_probe.probe_file(tmp_path / "p.step", diagonal_mm=1.0, hints=[])
    assert "options_path" in seen


def test_face_table_is_an_allow_listed_bounded_kernel():
    assert bounded_child.key_for(mesh_hints._face_table) == "mesh_hints.face_table"


def test_importing_the_module_imports_no_kernel():
    import subprocess
    import sys

    code = ("import sys, meshpipeline.cad.repair.mesh_hints; "
            "print(any(m.startswith(('OCP', 'gmsh')) for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
