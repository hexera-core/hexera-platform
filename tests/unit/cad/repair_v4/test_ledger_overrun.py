"""CRS v5 round 3: a file settled $1.40 under a $1 ceiling because calls are admitted on their ESTIMATE and settle at
their real cost. The next estimate is scaled by the worst overrun the file has shown, so the excess stays near one
call."""

from __future__ import annotations

from meshpipeline.cad.repair_v4.model_ledger import WaveLedger


def _call(ledger: WaveLedger, est: float, cost: float) -> bool:
    adm = ledger.admit("f", est, file_ceiling_usd=1.0)
    if not adm.ok:
        return False
    ledger.settle(adm.reservation, file_id="f", job_id="j", provider="p", model="m", input_tokens=1,
                  cached_input_tokens=0, output_tokens=1, cost_usd=cost)
    return True


def test_overruns_do_not_compound_past_the_file_ceiling() -> None:
    ledger = WaveLedger.for_file(50.0)
    calls = 0
    while _call(ledger, 0.05, 0.15) and calls < 100:   # every call costs 3x its estimate
        calls += 1
    assert ledger.file_spent_usd("f") <= 1.0 + 0.15, ledger.file_spent_usd("f")


def test_without_overrun_the_estimate_is_unchanged() -> None:
    ledger = WaveLedger.for_file(50.0)
    assert _call(ledger, 0.1, 0.1)
    assert WaveLedger._overrun(ledger._transact(lambda rows, _a: list(rows)), "f") == 1.0
