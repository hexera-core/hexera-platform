# Responsibility: Prove the mesh-hints face ID survives our STEP write and a re-read, on a
#                 re-exported STEP and on untouched upload bytes, and that gmsh applies the hints.
# Boundaries: the native tier (OCC + gmsh). The loop's policy is unit-tested with fakes.
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

pytest.importorskip("gmsh", reason="gmsh is a native-tier dependency")
pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut  # noqa: E402
from OCP.BRepPrimAPI import (  # noqa: E402
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt  # noqa: E402
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402

from meshpipeline.cad.repair import mesh_hints  # noqa: E402
from meshpipeline.cad.repair.mesh_probe import probe_file  # noqa: E402


def _part():
    """Two solids, three surface kinds: a block with a through hole, and a separate sphere."""
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(10, 10, 0), gp_Dir(0, 0, 1)), 4.0, 10.0).Shape()
    block = BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(20.0, 20.0, 10.0).Shape(), hole).Shape()
    ball = BRepPrimAPI_MakeSphere(gp_Pnt(40, 10, 5), 5.0).Shape()
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, block)
    builder.Add(compound, ball)
    return compound


def _write_step(shape, path: Path) -> Path:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    assert int(writer.Write(str(path))) == 1   # IFSelect_RetDone
    return path


def _table(path: Path) -> dict:
    table = mesh_hints.face_table(path, budget_s=60.0)
    assert table is not None and table["faces"]
    return table


def _fp(row: dict) -> tuple[float, float, float, float]:
    return mesh_hints._key(row)


def _hints(table: dict, *, size: float) -> list[dict]:
    return [{"face": r["face"], "size_mm": size, "why": "test",
             "fingerprint": {"surface": r["surface"], "area_mm2": r["area_mm2"],
                             "centroid": r["centroid"], "bbox": r["bbox"]}}
            for r in table["faces"]]


@pytest.fixture()
def written(tmp_path):
    return _write_step(_part(), tmp_path / "repaired.step")


def test_face_index_survives_a_reexport(written, tmp_path):
    """Write, re-read, write again from the re-read shape, re-read: every face index names the
    same face (area and centre of mass) in both files."""
    from meshpipeline.cad.repair.mesh_hints import _read_shape

    first = _table(written)
    again = _write_step(_read_shape(str(written)), tmp_path / "reexported.step")
    second = _table(again)
    # 6 block faces + the hole + the sphere
    assert len(first["faces"]) == len(second["faces"]) == 8
    for a, b in zip(first["faces"], second["faces"], strict=True):
        assert a["face"] == b["face"]
        assert a["surface"] == b["surface"]
        assert mesh_hints.same_face(_fp(a), _fp(b), diagonal_mm=first["diagonal_mm"])


def test_advanced_face_entity_ids_are_a_numbering_not_an_identity(written, tmp_path):
    """The writer's ADVANCED_FACE ids are not the face index and gmsh never sees them; the index
    is defined by the re-read, so it is the key."""
    ids = [int(m) for m in re.findall(r"#(\d+)\s*=\s*ADVANCED_FACE", written.read_text())]
    table = _table(written)
    assert len(ids) == len(table["faces"])
    assert ids != [r["face"] for r in table["faces"]]


@pytest.mark.parametrize("source", ["reexported", "passthrough"])
def test_gmsh_applies_every_hint_on_both_delivery_kinds(written, tmp_path, source):
    """A repaired re-export and the untouched upload bytes (lane H's passthrough) both map
    every hint to a gmsh surface by fingerprint."""
    from meshpipeline.cad.repair.mesh_hints import _read_shape

    if source == "passthrough":
        delivered = tmp_path / "upload.step"
        shutil.copyfile(written, delivered)    # the bytes, unchanged
    else:
        delivered = _write_step(_read_shape(str(written)), tmp_path / "reexported.step")
    table = _table(delivered)
    hints = _hints(table, size=table["diagonal_mm"] / 80)
    result = probe_file(delivered, diagonal_mm=table["diagonal_mm"], hints=hints, budget_s=120)
    assert result.meshed
    assert result.hints_applied == len(hints)
    assert result.hints_unmapped == ()


def test_a_hint_is_applied_by_fingerprint_when_the_number_is_wrong(written):
    """gmsh's surface tags are not trusted to equal the face index (0054_00544974 imports 4
    fewer surfaces): a hint whose number is off still lands on the face its fingerprint names,
    and one whose fingerprint matches nothing is reported, never guessed."""
    table = _table(written)
    hints = _hints(table, size=1.0)
    n = len(hints)
    shifted = [dict(h, face=(h["face"] % n) + 1) for h in hints]
    bogus = dict(hints[0], face=1, fingerprint=dict(hints[0]["fingerprint"], area_mm2=1e6))
    result = probe_file(written, diagonal_mm=table["diagonal_mm"], hints=[*shifted, bogus],
                        budget_s=120)
    assert result.hints_applied == n
    assert result.hints_unmapped == (1,)


def test_no_hints_is_the_plain_probe(written):
    table = _table(written)
    plain = probe_file(written, diagonal_mm=table["diagonal_mm"], budget_s=120)
    empty = probe_file(written, diagonal_mm=table["diagonal_mm"], hints=[], budget_s=120)
    assert plain.meshed and empty.meshed
    assert plain.hints_applied == empty.hints_applied == 0
    assert plain.volume_elements == empty.volume_elements


def test_hints_for_a_part_that_meshes_writes_nothing(written):
    assert mesh_hints.hints_for(written, ()) is None
    assert not mesh_hints.sidecar_path(written).exists()
    verdict = mesh_hints.probe_with_hints(written)
    assert verdict.meshes_default is True
    assert verdict.meshes_with_hints is None    # no sidecar: no hinted verdict


def test_quality_is_reported_when_asked(written):
    table = _table(written)
    result = probe_file(written, diagonal_mm=table["diagonal_mm"], quality=True, budget_s=120)
    assert result.meshed
    assert result.quality["elements"] == result.volume_elements
    assert result.quality["min_sicn"] > 0
    assert result.quality["min_edge_mm"] > 0


_CORPUS = Path("~/archductor/mesh-fail-corpus").expanduser()


def test_corpus_part_meshes_only_with_its_hints(tmp_path, monkeypatch):
    """A real part that fails at default (overlapping facets) meshes with the producer's hints,
    and the two numbers are reported apart. Third-party CAD: read locally, never committed."""
    import os

    root = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS", str(_CORPUS))).expanduser()
    source = root / "abc/00500400_bce216c87d2d17025db6ab36_step_000.step"
    if not source.is_file():
        pytest.skip(f"mesh-failure corpus file {source} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    step = tmp_path / source.name
    shutil.copyfile(source, step)
    sidecar = mesh_hints.hints_for(step, ())
    assert sidecar == mesh_hints.sidecar_path(step)
    doc = json.loads(sidecar.read_text())
    assert doc["schema"] == mesh_hints.SCHEMA
    assert doc["status"] == mesh_hints.STATUS_VERIFIED
    assert doc["attempts"] == len(doc["attempt_log"]) <= mesh_hints.MAX_ATTEMPTS
    assert doc["face_count"] == 8
    verdict = mesh_hints.probe_with_hints(step, quality=True)
    assert verdict.meshes_default is False
    assert verdict.meshes_with_hints is True
    assert verdict.hinted is not None and verdict.hinted.quality["min_sicn"] > 0
