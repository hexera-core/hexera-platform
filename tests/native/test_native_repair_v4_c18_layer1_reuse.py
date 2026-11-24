# Responsibility: lane C18 P1 - the staged healer reuses the standing state's layer-1 report ONLY where layer 1 would
#                 answer the same, proven on the FOUR stored v4 transactions where a per-solid BRepCheck flipped under
#                 orient_and_close with an UNCHANGED geometry key (the C18 measurement's 4 of 5,142 carried solids).
# Boundaries: the native tier; real OCCT in a spawned worker. Fixtures: each transaction's parent and child BREP
#             (`tests/fixtures/geometry/c18_layer1_reuse`, named by the file's sha12).
from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.stages import layer1_gate, step_for  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.valid import check_valid  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402

PAIRS = Path(__file__).resolve().parents[1] / "fixtures" / "geometry" / "c18_layer1_reuse"

#: sha12 -> whether A17's key (and so P1's gate) is unchanged by the orient_and_close on that transaction
CASES = {"7ab61ea7d641": True, "3e7c2a1197c3": True, "6aa493da5bbf": True, "6109dea12cc5": False}
#: where the gate says REUSE, the real layer-1 check on parent and child is compared here only on the transaction
#: that takes seconds. The other two take 130 s and 370 s (the owner's under-a-minute rule for targeted tests): their
#: equality was proven once with this same test un-trimmed (4/4 passed, 512 s, 2026-11-24) and by the C18 record of
#: the STEP-read full check on every stored state (identical findings, entities included, on all 570 no-op pairs).
FULL_CHECK = {"7ab61ea7d641"}


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _brep(tmp_path: Path, name: str) -> str:
    out = tmp_path / name.removesuffix(".gz")
    out.write_bytes(gzip.decompress((PAIRS / name).read_bytes()))
    return str(out)


def _findings(report) -> tuple:
    return (report.ok, report.solids, sorted((f.code, tuple(f.entities)) for f in report.hard),
            sorted(f.code for f in report.soft))


@pytest.mark.parametrize("sha12", sorted(CASES))
def test_p1_reuses_layer_1_only_where_layer_1_answers_the_same(pool: WorkerPool, tmp_path: Path, sha12: str) -> None:
    parent, child = _brep(tmp_path, f"{sha12}_parent.brep.gz"), _brep(tmp_path, f"{sha12}_child.brep.gz")
    gate_parent, gate_child = layer1_gate(pool, parent), layer1_gate(pool, child)
    assert gate_parent is not None and gate_child is not None
    assert (gate_parent == gate_child) is CASES[sha12]
    if gate_parent == gate_child and sha12 in FULL_CHECK:
        # the gate would REUSE the parent's report: it must be exactly what layer 1 says about the child
        judged = [check_valid(pool, step_for(pool, b), original_units=None) for b in (parent, child)]
        assert _findings(judged[0]) == _findings(judged[1])
