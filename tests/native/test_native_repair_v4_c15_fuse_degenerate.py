# Responsibility: Lane C15 - a Boolean whose result cannot be the UNION of its targets is DECLINED, never accepted.
#                 Dev 0051_00510567 on v4 (search_ai): the AI fused all eight overlapping bodies of the staged root
#                 (after split_closed); OCCT returned 4 solids of +0.14, +19.6, +0.02 and -13.5 mm3 from 47,870 mm3,
#                 BRepCheck-valid, and the part was delivered with 0.07% of its volume. The fixture IS that root state.
# Boundaries: the native tier; real OCCT through the ops' own `apply`.
from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "geometry" / "abc_00510567_staged_root.brep.gz"
TIMEOUT = 600


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def test_the_degenerate_eight_body_fuse_of_00510567_is_declined(pool: WorkerPool, tmp_path: Path) -> None:
    brep = tmp_path / "root.brep"
    brep.write_bytes(gzip.decompress(ROOT.read_bytes()))
    ws = Workspace.from_brep(pool, str(brep))
    bodies = sorted((s for s in ws.ids if s[0] == "B"), key=lambda s: int(s[1:]))
    assert len(bodies) == 8
    new, diff = REGISTRY["fuse"].apply(pool, ws, bodies, {"fuzzy_mm": 1e-6}, timeout_s=TIMEOUT)  # the AI's call
    assert diff.params.get("declined") == "fuse_degenerate", diff.params
    assert new is ws
    detail = diff.params.get("detail") or {}
    assert "negative" in str(detail.get("why")) and min(detail["negative_mm3"]) < -10.0


# The other side - a REAL union still applies - is the C7 fuse tests (test_native_repair_v4_c7_body_ops.py), which run
# with this guard in place. This root cannot host it: the staged orient_and_close left the compound BRepCheck-invalid,
# so any op that keeps those solids declines invalid_result.


# ------------------------------------------------------------------ layer 1 sees an inside-out solid on the BREP

DELIVERED = Path(__file__).resolve().parents[1] / "fixtures" / "geometry" / "abc_00510567_delivered.brep.gz"


def test_an_inside_out_solid_makes_an_op_made_state_invalid_before_the_step_export_hides_it(pool: WorkerPool,
                                                                                         tmp_path: Path) -> None:
    """v4 DELIVERED this BREP: 4 solids, one of -13.46 mm3. The STEP round trip re-orients it, so layer 1 alone called
    it valid. On an op-made state (history) the BREP's signed volumes are now checked: INVALID, not clean."""
    import dataclasses

    from meshpipeline.cad.repair_v4.search import _judged
    from meshpipeline.cad.repair_v4.verdict.assemble import cheap_verdict
    from meshpipeline.cad.repair_v4.workspace import Diff

    brep = tmp_path / "delivered.brep"
    brep.write_bytes(gzip.decompress(DELIVERED.read_bytes()))
    ws = Workspace.from_brep(pool, str(brep))
    blind = _judged(cheap_verdict, pool, ws, str(brep))                  # no history: the input path, unchanged
    assert blind.valid.ok, "precondition: layer 1 alone calls the delivered part valid"
    made = dataclasses.replace(ws, history=[Diff("fuse", {}, {}, {}, [], [], [], 0.0)])
    v = _judged(cheap_verdict, pool, made, str(brep))
    assert not v.valid.ok and not v.clean
    (f,) = [f for f in v.valid.hard if f.code == "non_positive_volume"]
    assert f.detail["volume"] < -13.0 and f.detail["seen_on"].startswith("brep")
