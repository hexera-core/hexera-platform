# Responsibility: Prove on the REAL kernel, pool and judge the two outcomes the injected `face_delete` kinds exist for:
#                 a small deleted face is REPAIRED to a clean verdict, and a large one is FILLED too (B12: no size cap; the
#                 span is measured and reported). Neither is read off a label: the loop span is measured on the file.
# Boundaries: the native tier (`meshpipeline-mesh:native`). No model, no search: the ops are applied by hand so that
#             what is proved is the op, the faithful layer and the panel, not the search's choice of op (the
#             end-to-end file asserts that separately). Nothing in the engine is faked.
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE  # noqa: E402
from OCP.TopExp import TopExp  # noqa: E402
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape  # noqa: E402
from tests.native._v4_flush_patch_part import write_flush_patch_box  # noqa: E402

import meshpipeline.cad.repair_v4.ops  # noqa: E402,F401  (the ops register their kernel functions on import)
from meshpipeline.cad.repair_v4 import limits, ws_kernel  # noqa: E402
from meshpipeline.cad.repair_v4 import search as search_mod  # noqa: E402
from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.assemble import default_members  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402

_HERE = Path(__file__).resolve()


def _devtools(name: str) -> Any:
    root = Path("/srv/devtools/validation/v4_eval")
    if not root.exists():
        root = _HERE.parents[2] / "devtools" / "validation" / "v4_eval"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location(f"t39_{name}", root / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def clean_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_flush_patch_box(tmp_path_factory.mktemp("t39_src") / "flush_patch_box.step")


@pytest.fixture(scope="module")
def pool() -> Any:
    p = WorkerPool(2)
    yield p
    p.close()


def _free_edges(ws: Any) -> list[str]:
    """Stable ids of the edges that border exactly ONE face: the hole's loop. Read from the workspace's own BREP."""
    shape = ws_kernel.read_brep(ws.brep_path)
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    index = ws_kernel._maps(shape)["edge"]
    ids = [f"E{index.FindIndex(ancestors.FindKey(i))}" for i in range(1, ancestors.Extent() + 1)
           if ancestors.FindFromIndex(i).Size() == 1]
    assert ids and all(i in ws.ids for i in ids), f"free edges {ids} are not workspace ids"
    return sorted(ids)


def _open(pool: Any, path: Path, tmp: Path) -> Any:
    return _devtools("arms").RealEngine().open_workspace(pool, str(path), str(tmp))


def test_a_small_deleted_face_is_repaired_to_a_clean_verdict(clean_source: Path, pool: Any, tmp_path: Path) -> None:
    inj = _devtools("inject")
    case = inj.inject(clean_source, "face_delete", 3, tmp_path)
    assert case.label["status"] == "structural"
    ws = _open(pool, case.path, tmp_path / "ws")
    loop = _free_edges(ws)
    assert len(loop) == 4, f"the hole's loop is {loop}"

    # the span, measured by the OP on the damaged part (not the label): the fraction of the diagonal it will judge
    filled, diff = REGISTRY["fill_loop"].apply(pool, ws, loop, {"continuity": "G0"}, timeout_s=120)
    measured = diff.params["measured"]
    assert measured["span_ratio"] <= limits.MAX_FILL_SPAN_RATIO
    assert measured["span_ratio"] == pytest.approx(case.label["loop_span_ratio"], abs=1e-4)
    assert measured["fill_to_surroundings"] <= measured["bound_mm"] and measured["fill_surface"] == "Plane"
    # the fill closes the shell; it is a shell, not a solid, so the volume is built from the now-closed faces
    faces = sorted(i for i in filled.ids if i[0] == "F")
    solid, _ = REGISTRY["make_volume"].apply(pool, filled, faces, {}, timeout_s=120)

    verdict = search_mod.default_judge_for(pool, solid).full(pool, solid, str(case.path))
    members = [m.name for m in default_members("full")]
    assert not verdict.inconclusive, "the panel could not measure the repaired part"
    assert verdict.valid.ok and verdict.faithful is not None and verdict.faithful.ok
    assert {m.name: m.status for m in verdict.panel} == dict.fromkeys(members, "pass"), verdict.panel
    assert verdict.clean is True
    print(f"\nFIXABLE: loop {measured['span_ratio']:.4%} of the diagonal ({measured['span_mm']:.3f} mm), "
          f"fill_loop({loop}, G0) then make_volume({len(faces)} faces) -> clean={verdict.clean}; fill: {measured}")


def test_a_large_deleted_face_is_now_filled_and_its_span_reported_not_refused(
    clean_source: Path, pool: Any, tmp_path: Path,
) -> None:
    """B12 (owner decision 2026-11-24): the 5%-of-diagonal fill cap is REMOVED from the op. A loop well over the old
    cap is filled; its span is still MEASURED and reported beside MAX_FILL_SPAN_RATIO, which is now a reporting
    threshold only."""
    inj = _devtools("inject")
    case = inj.inject(clean_source, "face_delete_large", 1, tmp_path)
    ws = _open(pool, case.path, tmp_path / "ws")
    loop = _free_edges(ws)
    filled, diff = REGISTRY["fill_loop"].apply(pool, ws, loop, {"continuity": "G0"}, timeout_s=120)
    measured = diff.params["measured"]
    assert "declined" not in diff.params
    assert measured["span_ratio"] > limits.MAX_FILL_SPAN_RATIO
    assert measured["span_ratio"] == pytest.approx(case.label["loop_span_ratio"], abs=1e-4)
    assert {"fill_to_surroundings", "bound_mm", "span_mm"} <= set(measured)
    assert any(i[0] == "F" for i in filled.ids)
    print(f"\nFILLED (no cap): loop {measured['span_ratio']:.2%} of the diagonal, old cap "
          f"{limits.MAX_FILL_SPAN_RATIO:.0%}; measured {measured}")


def test_no_fill_span_refuses_in_the_op(clean_source: Path, pool: Any, tmp_path: Path) -> None:
    """The op never raises OpDeclined on size: whatever span the loop has, a fill is attempted and either applies
    or fails on the kernel, never on a fidelity rule."""
    inj = _devtools("inject")
    case = inj.inject(clean_source, "face_delete_large", 1, tmp_path)
    ws = _open(pool, case.path, tmp_path / "ws")
    try:
        REGISTRY["fill_loop"].apply(pool, ws, _free_edges(ws), {"continuity": "G0"}, timeout_s=120)
    except OpDeclined as why:  # pragma: no cover - the failure being guarded against
        pytest.fail(f"fill_loop declined on size: {why.reason}")
    assert math.isclose(limits.MAX_FILL_SPAN_RATIO, 0.05)
