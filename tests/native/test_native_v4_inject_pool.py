# Responsibility: Prove on the REAL OCCT kernel that the injected-pool generator's files are byte-reproducible
#                 (the STEP header clock is pinned), that `verify` regenerates them, that its guards refuse
#                 quarantined kinds against the real inject.py constants, and that the files still read as STEP.
# Boundaries: the native tier; the unit tier (tests/unit/dev/test_v4_inject_pool.py) covers the rest with a fake.
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import sys
import time
from pathlib import Path

import pytest
from tests.native._v4_flush_patch_part import imprint_squares

pytest.importorskip("OCP.BRepCheck", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Tool  # noqa: E402
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut  # noqa: E402
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform  # noqa: E402
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder  # noqa: E402
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf  # noqa: E402
from OCP.TopAbs import TopAbs_EDGE  # noqa: E402
from OCP.TopExp import TopExp  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402
from OCP.TopTools import TopTools_IndexedMapOfShape  # noqa: E402

EVAL = Path(__file__).resolve().parents[2] / "devtools/validation/v4_eval"
if not EVAL.exists():
    EVAL = Path("/srv/devtools/validation/v4_eval")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gen = _load("v4_inject_pool", EVAL / "inject_pool.py")
inj = gen.load_backend()
pool = sys.modules["v4_pool"]


def _clean_part():
    box = BRepPrimAPI_MakeBox(40.0, 30.0, 20.0).Shape()
    fillet = BRepFilletAPI_MakeFillet(box)
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(box, TopAbs_EDGE, edges)
    for i in range(1, edges.Extent() + 1):
        edge = TopoDS.Edge_s(edges.FindKey(i))
        a, b = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge)), BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge))
        if abs(a.X() - b.X()) < 1e-9 and abs(a.Y() - b.Y()) < 1e-9:
            fillet.Add(3.0, edge)
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20.0, 15.0, -1.0), gp_Dir(0, 0, 1)), 4.0, 22.0).Shape()
    part = BRepAlgoAPI_Cut(fillet.Shape(), hole).Shape()
    # a 1.4 mm square imprinted flush in the top face: the one small face `face_delete` may take (loop ~3.7% of the
    # diagonal, within the fill cap); every other face of this part spans over 5% and is refused by design
    part = imprint_squares(part, [(10.0, 7.5, 20.0, "z", 0.7)])
    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(3.3, 1.7, 0.9), gp_Dir(1, 2, 3)), 0.37)
    return BRepBuilderAPI_Transform(part, turn, True).Shape()


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    d = tmp_path_factory.mktemp("clean")
    tmp = d / "p.step"
    inj.write_step(_clean_part(), tmp)
    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    path = d / sha / "clean_part.step"
    path.parent.mkdir()
    tmp.rename(path)
    entry = pool.PoolEntry("occt/clean_part.step", "occt", sha, "https://x/o.tar.xz", "1" * 64, pool.LICENCE["occt"])
    return entry, path


@pytest.fixture(scope="module")
def made(clean, tmp_path_factory):
    """Every structural kind, generated twice, a second apart: each is a real OCCT job, slow under emulation."""
    out = tmp_path_factory.mktemp("a")
    first = gen.generate([clean], kinds=None, seeds=[1], out_dir=out,
                         base_url="https://b.example/inj", backend=inj)
    time.sleep(1.2)   # the header clock ticks in whole seconds
    second = gen.generate([clean], kinds=None, seeds=[1], out_dir=tmp_path_factory.mktemp("b"),
                          base_url="https://b.example/inj", backend=inj)
    return first, second, out


def test_the_real_constants_refuse_every_quarantined_kind(clean, tmp_path):
    assert set(inj.QUARANTINED_KINDS) == {"loose_reapprox", "tolerance_inflate", "seam_variant", "precision_truncate"}
    for kind in (*inj.QUARANTINED_KINDS, "pcurve_corrupt"):
        with pytest.raises(gen.RefusedKind):
            gen.generate([clean], kinds=[kind], seeds=[0], out_dir=tmp_path, base_url="https://b", backend=inj)
    assert not any(tmp_path.iterdir())


def test_raw_injector_output_is_not_reproducible_so_normalising_is_load_bearing(clean, tmp_path):
    entry, path = clean
    a = inj.inject(path, "face_delete", 1, tmp_path / "a").path.read_bytes()
    time.sleep(1.2)
    b = inj.inject(path, "face_delete", 1, tmp_path / "b").path.read_bytes()
    assert a != b and gen.normalize_step(a) == gen.normalize_step(b)


def test_regenerating_gives_the_same_bytes_for_every_structural_kind(made):
    first, second, _ = made
    assert {r.kind for r in first.recipes} == set(inj.DEFAULT_KINDS)
    assert [e.sha256 for e in first.entries] == [e.sha256 for e in second.entries]


def test_verify_regenerates_every_entry_and_rejects_a_forged_hash(made, clean):
    first, _, _ = made
    entry, path = clean
    lookup = {entry.sha256: path}.get
    assert gen.verify(first.recipes, first.entries, lookup, inj) == []
    forged = [dataclasses.replace(first.recipes[0], sha256="0" * 64), *first.recipes[1:]]
    assert any("differs" in p for p in gen.verify(forged, first.entries, lookup, inj))


def test_files_are_damaged_labelled_and_still_read_as_step(made, clean):
    first, _, out = made
    entry, _path = clean
    for e, r in zip(first.entries, first.recipes, strict=True):
        assert r.label["entities"], f"{r.kind}: the label names nothing"
        assert r.label["status"] == "structural" and r.label["fidelity_reference"] == "damaged_input"
        assert e.sha256 != entry.sha256
        written = out / e.sha256 / Path(e.name).name
        assert hashlib.sha256(written.read_bytes()).hexdigest() == e.sha256
        inj.read_step(written)        # a damaged file that cannot be read is not a benchmark input


def test_a_fresh_process_regenerates_the_same_bytes(made, clean, tmp_path):
    """OCCT's counters are per process: the process that verifies is never the one that generated."""
    import json
    import subprocess
    first, _, _ = made
    entry, path = clean
    recipes = tmp_path / "injected.json"
    gen.write_records(recipes, first.recipes, skipped=[])
    manifest = tmp_path / "pool.json"
    pool.write_pool([entry, *first.entries], manifest, sources_included=["occt", "inject"])
    code = (f"import sys; sys.path.insert(0, {str(EVAL)!r}); import inject_pool as g; "
            f"raise SystemExit(g.main(['verify','--pool',{str(manifest)!r},'--inputs',{str(path.parents[1])!r},"
            f"'--records',{str(recipes)!r}]))")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(recipes.read_text())["records"]
