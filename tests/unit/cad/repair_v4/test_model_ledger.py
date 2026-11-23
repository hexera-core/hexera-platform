# Responsibility: Verify the per-wave model ledger: one shared total, reserved before a call, settled after it.
# Boundaries: pure stdlib; the "shards" are separate WaveLedger objects over one file, the shared state they
# would have as separate processes.
from __future__ import annotations

import threading

import pytest

from meshpipeline.cad.repair_v4.model_ledger import WaveLedger


def _settle(ledger: WaveLedger, res: str, file_id: str, usd: float) -> None:
    ledger.settle(res, file_id=file_id, job_id="job-1", provider="openai", model="gpt-6-astra",
                  input_tokens=100, cached_input_tokens=0, output_tokens=10, cost_usd=usd)


def test_a_settled_call_counts_toward_the_wave_and_its_file():
    ledger = WaveLedger(cap_usd=1.0)
    a = ledger.admit("f1", 0.1, file_ceiling_usd=1.0)
    assert a.ok and a.reservation
    _settle(ledger, a.reservation, "f1", 0.07)
    assert ledger.spent_usd() == pytest.approx(0.07)
    assert ledger.file_spent_usd("f1") == pytest.approx(0.07)
    assert ledger.file_spent_usd("f2") == 0.0
    [entry] = ledger.entries()
    assert entry["job_id"] == "job-1" and entry["model"] == "gpt-6-astra" and entry["usd"] == 0.07


def test_an_open_reservation_is_held_against_the_cap_until_settled_or_released():
    ledger = WaveLedger(cap_usd=0.25)
    first = ledger.admit("f1", 0.2, file_ceiling_usd=1.0)
    assert first.ok
    # the first call is still in flight; its estimate is already spoken for
    second = ledger.admit("f2", 0.2, file_ceiling_usd=1.0)
    assert not second.ok and second.reason == "wave_cap"
    ledger.release(first.reservation)
    assert ledger.admit("f2", 0.2, file_ceiling_usd=1.0).ok


def test_the_file_ceiling_refuses_independently_of_the_wave():
    ledger = WaveLedger(cap_usd=100.0)
    a = ledger.admit("f1", 0.1, file_ceiling_usd=0.5)
    _settle(ledger, a.reservation, "f1", 0.45)
    denied = ledger.admit("f1", 0.1, file_ceiling_usd=0.5)
    assert not denied.ok and denied.reason == "file_ceiling"
    assert ledger.admit("f2", 0.1, file_ceiling_usd=0.5).ok


def test_shards_over_one_file_share_one_total(tmp_path):
    path = tmp_path / "wave.jsonl"
    shard_a, shard_b = WaveLedger(cap_usd=0.5, path=path), WaveLedger(cap_usd=0.5, path=path)
    a = shard_a.admit("fa", 0.1, file_ceiling_usd=1.0)
    _settle(shard_a, a.reservation, "fa", 0.4)
    assert shard_b.spent_usd() == pytest.approx(0.4)
    denied = shard_b.admit("fb", 0.2, file_ceiling_usd=1.0)
    assert not denied.ok and denied.reason == "wave_cap"


def test_concurrent_admissions_never_overbook_the_cap(tmp_path):
    ledger = WaveLedger(cap_usd=1.0, path=tmp_path / "wave.jsonl")
    admitted: list[str] = []
    lock = threading.Lock()

    def shard() -> None:
        mine = WaveLedger(cap_usd=1.0, path=tmp_path / "wave.jsonl")
        for _ in range(10):
            a = mine.admit("f", 0.1, file_ceiling_usd=100.0)
            if a.ok:
                with lock:
                    admitted.append(a.reservation)

    threads = [threading.Thread(target=shard) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 10          # 10 x $0.10 fills a $1.00 cap exactly; the other 70 asked and were refused
    assert ledger.reserved_usd() == pytest.approx(1.0)


def test_a_settled_call_may_overrun_its_estimate_and_the_overrun_is_still_counted():
    ledger = WaveLedger(cap_usd=0.3)
    a = ledger.admit("f1", 0.1, file_ceiling_usd=1.0)
    _settle(ledger, a.reservation, "f1", 0.25)        # costlier than estimated
    assert ledger.spent_usd() == pytest.approx(0.25)
    assert not ledger.admit("f1", 0.1, file_ceiling_usd=1.0).ok


def test_settling_an_unknown_reservation_is_refused():
    with pytest.raises(KeyError):
        _settle(WaveLedger(cap_usd=1.0), "nope", "f1", 0.1)


def test_a_negative_or_nonfinite_cost_is_refused_rather_than_recorded():
    ledger = WaveLedger(cap_usd=1.0)
    a = ledger.admit("f1", 0.1, file_ceiling_usd=1.0)
    for bad in (-0.01, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            _settle(ledger, a.reservation, "f1", bad)


@pytest.mark.parametrize("cap", [float("inf"), float("nan"), 0.0, -1.0])
def test_a_ledger_refuses_a_cap_that_is_not_a_cap(cap):
    with pytest.raises(ValueError):
        WaveLedger(cap_usd=cap)


def test_shard_slices_bound_the_wave_with_no_shared_state(tmp_path):
    # 12 shards, each on its own "host" (its own log file, no common lock), $60 wave
    n, wave = 12, 60.0
    shards = [WaveLedger.for_shard(wave, n, i, tmp_path / f"shard{i}.jsonl") for i in range(n)]
    assert all(s.cap_usd == pytest.approx(5.0) for s in shards)
    total = 0.0
    for ledger in shards:
        for _ in range(1000):
            a = ledger.admit("f", 0.37, file_ceiling_usd=1e9)
            if not a.ok:
                assert a.reason == "wave_cap"
                break
            _settle(ledger, a.reservation, "f", 0.37)
            total += 0.37
    assert total <= wave                                   # the wave bound, whatever the placement
    assert total > wave - n * 0.37                         # and the slices are actually used


def test_a_restarted_shard_resumes_its_spend_rather_than_resetting_it(tmp_path):
    path = tmp_path / "s.jsonl"
    first = WaveLedger.for_shard(10.0, 2, 0, path)
    a = first.admit("f", 1.0, file_ceiling_usd=100.0)
    _settle(first, a.reservation, "f", 4.5)
    again = WaveLedger.for_shard(10.0, 2, 0, path)         # the process restarted
    assert again.spent_usd() == pytest.approx(4.5)
    assert not again.admit("g", 1.0, file_ceiling_usd=100.0).ok


@pytest.mark.parametrize("n, i", [(0, 0), (3, 3), (3, -1)])
def test_an_invalid_shard_is_refused(tmp_path, n, i):
    with pytest.raises(ValueError):
        WaveLedger.for_shard(10.0, n, i, tmp_path / "x.jsonl")


def test_the_stop_reason_is_recorded_in_the_ledger(tmp_path):
    ledger = WaveLedger(cap_usd=1.0, path=tmp_path / "w.jsonl")
    ledger.mark("f1", stop_reason="wave_cap", ai_capped=True)
    ledger.mark("f2", stop_reason="max_turns", ai_capped=False)
    assert ledger.file_statuses() == {"f1": {"stop_reason": "wave_cap", "ai_capped": True},
                                      "f2": {"stop_reason": "max_turns", "ai_capped": False}}


def test_a_custom_store_plugs_in_behind_the_same_interface():
    from meshpipeline.cad.repair_v4.model_ledger import MemoryStore
    ledger = WaveLedger(cap_usd=1.0, store=MemoryStore())
    assert ledger.admit("f", 0.5, file_ceiling_usd=1.0).ok
