# Responsibility: Lane C13 - every op payload carries the lineage's ORIGINAL diagonal (`part_diag_mm`), the workspace
#                 keeps it across forks, and the kernels' `part_diagonal` prefers it to the shape in hand, except
#                 when it is the unbounded diagonal of an untrimmed input.
# Boundaries: hermetic; a fake pool records payloads. The real kernels are tests/native/test_native_repair_v4_c13_*.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair_v4 import (
    kernel_calls,  # noqa: F401 - imports ws_kernel in its working order
    ws_kernel,
)
from meshpipeline.cad.repair_v4.worker_pool import CallResult
from meshpipeline.cad.repair_v4.workspace import Workspace


class _Pool:
    def __init__(self, value: dict) -> None:
        self.value, self.payloads = value, []

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        self.payloads.append((fn, payload))
        return CallResult(True, self.value, None, "ok", 0.0)


def _indexed(diag: float | None = 441.0) -> Workspace:
    value: dict = {"quantum": 0.01, "entities": [{"type": "solid", "fp": "s", "index": 1},
                                                 {"type": "solid", "fp": "t", "index": 2},
                                                 {"type": "face", "fp": "f", "index": 1}], "signature": []}
    if diag is not None:
        value["diag_mm"] = diag
    return Workspace.from_brep(_Pool(value), "/fake/in.brep")


def test_the_index_diagonal_is_the_lineages_and_survives_a_fork() -> None:
    ws = _indexed()
    assert ws.part_diag_mm == 441.0 and ws.fork().part_diag_mm == 441.0


def test_an_index_without_a_diagonal_leaves_it_unknown() -> None:
    assert _indexed(None).part_diag_mm is None


def test_part_diagonal_prefers_the_payload_to_the_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_kernel, "diagonal_of", lambda shape: 1237.0)  # the state's pole box
    assert ws_kernel.part_diagonal({"part_diag_mm": 441.0}, object()) == 441.0
    assert ws_kernel.part_diagonal({}, object()) == 1237.0
    assert ws_kernel.part_diagonal({"part_diag_mm": None}, object()) == 1237.0


def test_an_unbounded_lineage_is_sized_from_the_trimmed_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """An untrimmed input's diagonal (~3.5e100 mm) would make the zero-volume eps infinite: every solid dropped."""
    monkeypatch.setattr(ws_kernel, "diagonal_of", lambda shape: 1055.0)
    assert ws_kernel.part_diagonal({"part_diag_mm": 3.46e100}, object()) == 1055.0


@pytest.mark.parametrize("op, targets, params", [
    ("conformalise", ["B1", "B2"], {"mode": "fragment"}),   # ops/bodies.py
    ("sew", [], {"tol_ladder": [0.01]}),                     # ops/shells.py
    ("tighten_tolerances", [], {"target_rel": 1e-5}),       # ops/edges.py
    ("split_angle", ["F1"], {"max_deg": 90}),                    # ops/__init__.py's shared runner
])
def test_every_op_payload_carries_the_lineages_diagonal(op: str, targets: list[str], params: dict) -> None:
    from meshpipeline.cad.repair_v4.ops import REGISTRY

    ws = _indexed()
    pool = _Pool({"declined": "no_change", "detail": {}})
    try:
        REGISTRY[op].apply(pool, ws, targets, params, timeout_s=5)
    except Exception:  # noqa: BLE001 - the fake answer may not satisfy the op; the payload is what is checked
        pass
    sent = [p for fn, p in pool.payloads if fn != "ws_index"]
    assert sent, f"{op} made no kernel call"
    assert all(p.get("part_diag_mm") == 441.0 for p in sent), [sorted(p) for p in sent]
