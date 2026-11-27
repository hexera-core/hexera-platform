# ruff: noqa: F811 - fixtures imported from a sibling module are redefined as test arguments, by design
# Responsibility: Prove the `search_ai` arm of the production entry point `repair_v4()` puts requests on the wire of the
#                 REAL model router (fake provider client beneath it), that the route is chosen by the engine, and that
#                 every request is admitted by the ledger exactly once. Task 31's own admit/wire assertion sits behind
#                 `arms.run_file`'s measured-ness assertions and so never ran; this one does not.
# Boundaries: the native tier. Nothing in the engine is faked; no model is called, no money is spent.
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")

from test_native_repair_v4_end_to_end import Wire, defect_step, wire  # noqa: E402,F401  - fixtures, shared not copied

import meshpipeline.cad.repair_v4 as v4  # noqa: E402
from meshpipeline.cad.repair_v4.model_ledger import WaveLedger  # noqa: E402
from meshpipeline.contracts.model_inference import REPAIR_V4_PROPOSE_ROUTES  # noqa: E402


def _run_ai(defect: str, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[v4.V4Result, list[tuple]]:
    import time

    admits: list[tuple] = []
    real = WaveLedger.admit

    def admit(self: WaveLedger, file_id: str, estimate_usd: float, *, file_ceiling_usd: float):  # type: ignore[no-untyped-def]
        a = real(self, file_id, estimate_usd, file_ceiling_usd=file_ceiling_usd)
        admits.append((file_id, estimate_usd, a.ok))
        return a

    monkeypatch.setattr(WaveLedger, "admit", admit)
    monkeypatch.setattr(v4, "ARM", "search_ai")
    result = v4.repair_v4(defect, str(tmp / "out.step"),
                          budget=v4.Budget(wall_s=120.0, ops=12, model_calls=3, model_usd=1.0),
                          deadline_s=time.monotonic() + 300.0)
    return result, admits


def test_search_ai_through_repair_v4_admits_every_request_once(
        defect_step: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: Wire) -> None:
    _result, admits = _run_ai(defect_step, tmp_path, monkeypatch)
    assert wire.requests, "search_ai put no request on the wire: the proposer never reached the router"
    assert len(admits) == len(wire.requests), f"{len(wire.requests)} requests but {len(admits)} admits: {admits}"
    assert all(ok for _f, _e, ok in admits), admits
    assert sum(e for _f, e, _ok in admits) <= 1.0 * len(admits)
    # the route is the engine's choice: the strongest of the proposer chain, not a caller's argument
    assert all(r["model"] == REPAIR_V4_PROPOSE_ROUTES[0].partition(":")[2] for r in wire.requests), \
        [r.get("model") for r in wire.requests]


def test_search_ai_route_can_be_pinned_to_the_second_route(
        defect_step: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: Wire) -> None:
    monkeypatch.setattr(v4, "ROUTE", REPAIR_V4_PROPOSE_ROUTES[1])
    _run_ai(defect_step, tmp_path, monkeypatch)
    assert wire.requests
    assert all(r["model"] == REPAIR_V4_PROPOSE_ROUTES[1].partition(":")[2] for r in wire.requests)


def test_a_file_never_spends_past_its_ceiling_even_when_the_budget_asks_for_more(
        defect_step: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wire: Wire) -> None:
    import time

    from meshpipeline.cad.repair_v4.proposer import FILE_USD_CEILING

    monkeypatch.setattr(v4, "ARM", "search_ai")
    # ASK FOR MORE THAN THE CEILING, whatever the ceiling is: it was $1 when this test said `5.0`, and the CRS v5 beta
    # (#540) raised it to $5 on purpose, so a literal $5 now asks for exactly the ceiling and is (rightly) allowed.
    with pytest.raises(ValueError, match="usd_ceiling"):  # AIProposer's own (0, FILE_USD_CEILING] guard
        v4.repair_v4(defect_step, str(tmp_path / "o.step"),
                     budget=v4.Budget(wall_s=60.0, ops=4, model_calls=2, model_usd=FILE_USD_CEILING * 2),
                     deadline_s=time.monotonic() + 200.0)
    assert not wire.requests
