# Responsibility: Task 45 - the search's dedupe key must tell apart two states that differ only in what an op can
#                 change and a centroid-and-box fingerprint cannot see (orientation, shell/compound layout), and
#                 must still treat the same state reached twice as one state.
# Boundaries: the native tier, REAL OCCT through a real WorkerPool: `Workspace.from_brep` and the real
#             `orient_and_close` op. `search._fingerprint` is the real function. No fake pool.
"""Why this exists: `search._fingerprint` was `tuple(sorted(ws.ids.values()))`, and each id value is the
centroid-and-box fingerprint of one face, edge or solid. `orient_and_close` keeps every TShape and flips orientation,
so its output had IDENTICAL entity fingerprints to its input and the search discarded the repair as a duplicate."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4 import search  # noqa: E402
from meshpipeline.cad.repair_v4.ops import shells  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 180
ORIENT = {op.name: op for op in shells.OPS}["orient_and_close"]


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _box() -> Any:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()


def _ws(pool: WorkerPool, shape: Any, path: Path) -> Workspace:
    from OCP.BRepTools import BRepTools

    assert BRepTools.Write_s(shape, str(path))
    return Workspace.from_brep(pool, str(path))


def _old_key(ws: Workspace) -> tuple:
    """The dedupe key before Task 45, kept here so each test can show the collision it fixes is REAL."""
    return tuple(sorted(ws.ids.values()))


def test_a_reversed_solid_does_not_dedupe_against_its_forward_twin(pool: WorkerPool, tmp_path: Path) -> None:
    forward = _ws(pool, _box(), tmp_path / "fwd.brep")
    reversed_ = _ws(pool, shells._entities(_box(), "solid")[0].Reversed(), tmp_path / "rev.brep")
    assert _old_key(forward) == _old_key(reversed_), "precondition: the OLD key cannot tell them apart"
    assert search._fingerprint(forward) != search._fingerprint(reversed_)


def test_orient_and_close_output_is_not_a_duplicate_of_its_input(pool: WorkerPool, tmp_path: Path) -> None:
    """The real op, the real key: the repaired state must be new to the search."""
    inside_out = _ws(pool, shells._entities(_box(), "solid")[0].Reversed(), tmp_path / "inside_out.brep")
    repaired, _diff = ORIENT.apply(pool, inside_out, [], {}, timeout_s=TIMEOUT)
    assert repaired.ids == inside_out.ids, "precondition: the op keeps every entity, so only orientation moved"
    assert _old_key(repaired) == _old_key(inside_out)
    assert search._fingerprint(repaired) != search._fingerprint(inside_out)
    # and the repaired state is the SAME state as a box that was never broken
    assert search._fingerprint(repaired) == search._fingerprint(_ws(pool, _box(), tmp_path / "fresh.brep"))


def test_a_shell_with_one_flipped_face_does_not_dedupe_against_the_fixed_shell(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Shell

    shell = TopoDS_Shell()
    builder = BRep_Builder()
    builder.MakeShell(shell)
    for i, face in enumerate(shells._entities(_box(), "face")):
        builder.Add(shell, face.Reversed() if i == 2 else face)
    broken = _ws(pool, shell, tmp_path / "flipped.brep")
    fixed, _diff = ORIENT.apply(pool, broken, [], {}, timeout_s=TIMEOUT)
    assert _old_key(fixed) == _old_key(broken), "precondition: the OLD key cannot tell them apart"
    assert search._fingerprint(fixed) != search._fingerprint(broken)


def test_a_solid_does_not_dedupe_against_the_same_solid_wrapped_in_a_compound(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, _box())
    bare, wrapped = _ws(pool, _box(), tmp_path / "bare.brep"), _ws(pool, compound, tmp_path / "wrapped.brep")
    assert _old_key(bare) == _old_key(wrapped), "precondition: the OLD key cannot tell them apart"
    assert search._fingerprint(bare) != search._fingerprint(wrapped)


def test_the_same_state_reached_twice_still_dedupes(pool: WorkerPool, tmp_path: Path) -> None:
    """A genuine duplicate: two files written separately, and the box after an op that changes nothing."""
    first = _ws(pool, _box(), tmp_path / "a.brep")
    second = _ws(pool, _box(), tmp_path / "b.brep")
    untouched, _diff = ORIENT.apply(pool, first, [], {}, timeout_s=TIMEOUT)  # already positive: a no-op
    assert untouched.brep_path != first.brep_path, "precondition: a different file, reached by running an op"
    assert search._fingerprint(first) == search._fingerprint(second) == search._fingerprint(untouched)


def test_an_inside_out_solid_reached_twice_still_dedupes(pool: WorkerPool, tmp_path: Path) -> None:
    """The signature must not make every broken state unique: the same broken state is still one state."""
    one = _ws(pool, shells._entities(_box(), "solid")[0].Reversed(), tmp_path / "x.brep")
    two = _ws(pool, shells._entities(_box(), "solid")[0].Reversed(), tmp_path / "y.brep")
    assert search._fingerprint(one) == search._fingerprint(two)
