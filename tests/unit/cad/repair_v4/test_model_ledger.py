# Responsibility: Verify the per-wave model ledger: one shared total, reserved before a call, settled after it.
# Boundaries: pure stdlib; the "shards" are separate WaveLedger objects over one file, the shared state they
# would have as separate processes.
from __future__ import annotations

import threading

import pytest

from meshpipeline.cad.repair_v4.model_ledger import (
    WAVE_CAPS_USD,
    FileMarkerStore,
    LostShardLog,
    ObjectMarkerStore,
    ShardArithmeticError,
    WaveLedger,
)


def _settle(ledger: WaveLedger, res: str, file_id: str, usd: float) -> None:
    ledger.settle(res, file_id=file_id, job_id="job-1", provider="openai", model="gpt-6-astra",
                  input_tokens=100, cached_input_tokens=0, output_tokens=10, cost_usd=usd)


def test_a_settled_call_counts_toward_the_wave_and_its_file():
    ledger = WaveLedger.for_test(cap_usd=1.0)
    a = ledger.admit("f1", 0.1, file_ceiling_usd=1.0)
    assert a.ok and a.reservation
    _settle(ledger, a.reservation, "f1", 0.07)
    assert ledger.spent_usd() == pytest.approx(0.07)
    assert ledger.file_spent_usd("f1") == pytest.approx(0.07)
    assert ledger.file_spent_usd("f2") == 0.0
    [entry] = ledger.entries()
    assert entry["job_id"] == "job-1" and entry["model"] == "gpt-6-astra" and entry["usd"] == 0.07


def test_an_open_reservation_is_held_against_the_cap_until_settled_or_released():
    ledger = WaveLedger.for_test(cap_usd=0.25)
    first = ledger.admit("f1", 0.2, file_ceiling_usd=1.0)
    assert first.ok
    # the first call is still in flight; its estimate is already spoken for
    second = ledger.admit("f2", 0.2, file_ceiling_usd=1.0)
    assert not second.ok and second.reason == "wave_cap"
    ledger.release(first.reservation)
    assert ledger.admit("f2", 0.2, file_ceiling_usd=1.0).ok


def test_the_file_ceiling_refuses_independently_of_the_wave():
    ledger = WaveLedger.for_test(cap_usd=100.0)
    a = ledger.admit("f1", 0.1, file_ceiling_usd=0.5)
    _settle(ledger, a.reservation, "f1", 0.45)
    denied = ledger.admit("f1", 0.1, file_ceiling_usd=0.5)
    assert not denied.ok and denied.reason == "file_ceiling"
    assert ledger.admit("f2", 0.1, file_ceiling_usd=0.5).ok


def test_shards_over_one_file_share_one_total(tmp_path):
    path = tmp_path / "wave.jsonl"
    shard_a, shard_b = WaveLedger.for_test(cap_usd=0.5, path=path), WaveLedger.for_test(cap_usd=0.5, path=path)
    a = shard_a.admit("fa", 0.1, file_ceiling_usd=1.0)
    _settle(shard_a, a.reservation, "fa", 0.4)
    assert shard_b.spent_usd() == pytest.approx(0.4)
    denied = shard_b.admit("fb", 0.2, file_ceiling_usd=1.0)
    assert not denied.ok and denied.reason == "wave_cap"


def test_concurrent_admissions_never_overbook_the_cap(tmp_path):
    ledger = WaveLedger.for_test(cap_usd=1.0, path=tmp_path / "wave.jsonl")
    admitted: list[str] = []
    lock = threading.Lock()

    def shard() -> None:
        mine = WaveLedger.for_test(cap_usd=1.0, path=tmp_path / "wave.jsonl")
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
    ledger = WaveLedger.for_test(cap_usd=0.3)
    a = ledger.admit("f1", 0.1, file_ceiling_usd=1.0)
    _settle(ledger, a.reservation, "f1", 0.25)        # costlier than estimated
    assert ledger.spent_usd() == pytest.approx(0.25)
    assert not ledger.admit("f1", 0.1, file_ceiling_usd=1.0).ok


def test_settling_an_unknown_reservation_is_refused():
    with pytest.raises(KeyError):
        _settle(WaveLedger.for_test(cap_usd=1.0), "nope", "f1", 0.1)


def test_a_negative_or_nonfinite_cost_is_refused_rather_than_recorded():
    ledger = WaveLedger.for_test(cap_usd=1.0)
    a = ledger.admit("f1", 0.1, file_ceiling_usd=1.0)
    for bad in (-0.01, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            _settle(ledger, a.reservation, "f1", bad)


@pytest.mark.parametrize("cap", [float("inf"), float("nan"), 0.0, -1.0])
def test_a_ledger_refuses_a_cap_that_is_not_a_cap(cap):
    with pytest.raises(ValueError):
        WaveLedger.for_test(cap_usd=cap)


def shard(tmp_path, i, *, n=12, wave="normal", wave_id="w1", markers=None, path=None):
    return WaveLedger.for_shard(wave, wave_id=wave_id, n_shards=n, shard_index=i,
                                path=path or tmp_path / f"shard{i}.jsonl",
                                markers=markers or FileMarkerStore(tmp_path / "bucket"))


def test_shard_slices_bound_the_wave_with_no_shared_state(tmp_path):
    # 12 shards, each on its own "host" (its own log file, no common lock), the $50 normal wave
    n = 12
    shards = [shard(tmp_path, i, n=n) for i in range(n)]
    assert all(s.cap_usd == pytest.approx(50.0 / 12) for s in shards)
    total = 0.0
    for ledger in shards:
        for _ in range(1000):
            a = ledger.admit("f", 0.37, file_ceiling_usd=1e9)
            if not a.ok:
                assert a.reason == "wave_cap"
                break
            _settle(ledger, a.reservation, "f", 0.37)
            total += 0.37
    assert total <= 50.0                                   # the wave bound, whatever the placement
    assert total > 50.0 - n * 0.37                         # and the slices are actually used


def test_the_wave_caps_are_the_owners_named_limits():
    assert WAVE_CAPS_USD == {"normal": 50.0, "checkpoint": 300.0, "held_out": 250.0}


def test_a_typed_number_above_the_largest_wave_is_refused():
    with pytest.raises(ValueError, match="largest wave"):
        WaveLedger.for_test(cap_usd=1e6)
    WaveLedger.for_test(cap_usd=300.0)


def test_an_unknown_wave_type_is_refused(tmp_path):
    with pytest.raises(ValueError, match="wave type"):
        shard(tmp_path, 0, wave="huge")


def test_a_restarted_shard_resumes_its_spend_rather_than_resetting_it(tmp_path):
    first = shard(tmp_path, 0, n=2)
    a = first.admit("f", 1.0, file_ceiling_usd=100.0)
    _settle(first, a.reservation, "f", 24.5)
    again = shard(tmp_path, 0, n=2)                        # the process restarted; marker and log both exist
    assert again.spent_usd() == pytest.approx(24.5)
    assert not again.admit("g", 1.0, file_ceiling_usd=100.0).ok


def test_a_shard_whose_log_was_lost_refuses_to_start_at_zero(tmp_path):
    first = shard(tmp_path, 3, n=4)
    a = first.admit("f", 1.0, file_ceiling_usd=100.0)
    _settle(first, a.reservation, "f", 5.0)
    (tmp_path / "shard3.jsonl").unlink()                   # the VM was replaced; the bucket marker remains
    with pytest.raises(LostShardLog, match="shard 3"):
        shard(tmp_path, 3, n=4)


def test_a_fresh_shard_and_a_retried_shard_with_its_log_both_start(tmp_path):
    shard(tmp_path, 0, n=4)
    shard(tmp_path, 0, n=4)                                # same index, log present: a resume
    shard(tmp_path, 1, n=4)                                # a different index: fresh


def test_a_shard_with_a_different_n_than_the_wave_is_refused(tmp_path):
    shard(tmp_path, 0, n=4)
    with pytest.raises(ShardArithmeticError, match="launcher's N"):
        shard(tmp_path, 1, n=5)                            # a real count above the configured N
    with pytest.raises(ShardArithmeticError):
        shard(tmp_path, 1, n=4, wave="checkpoint")


def test_two_shards_cannot_share_a_log_under_different_indexes(tmp_path):
    shard(tmp_path, 0, n=4, path=tmp_path / "same.jsonl")
    with pytest.raises(ShardArithmeticError, match="belongs to"):
        shard(tmp_path, 1, n=4, path=tmp_path / "same.jsonl")


def test_the_marker_store_creates_once(tmp_path):
    m = FileMarkerStore(tmp_path / "b")
    assert m.get("a/b") is None and m.create("a/b", "1") and not m.create("a/b", "2") and m.get("a/b") == "1"


@pytest.mark.parametrize("n, i", [(0, 0), (3, 3), (3, -1)])
def test_an_invalid_shard_is_refused(tmp_path, n, i):
    with pytest.raises(ValueError):
        shard(tmp_path, i, n=n)


def test_the_stop_reason_is_recorded_in_the_ledger(tmp_path):
    ledger = WaveLedger.for_test(cap_usd=1.0, path=tmp_path / "w.jsonl")
    ledger.mark("f1", stop_reason="wave_cap", ai_capped=True)
    ledger.mark("f2", stop_reason="max_turns", ai_capped=False)
    assert ledger.file_statuses() == {"f1": {"stop_reason": "wave_cap", "ai_capped": True},
                                      "f2": {"stop_reason": "max_turns", "ai_capped": False}}


def test_a_custom_store_plugs_in_behind_the_same_interface():
    from meshpipeline.cad.repair_v4.model_ledger import MemoryStore
    ledger = WaveLedger.for_test(cap_usd=1.0, store=MemoryStore())
    assert ledger.admit("f", 0.5, file_ceiling_usd=1.0).ok


# --- the marker race, the closed constructor, truncation and rollback ----------------------------------------------


def test_a_shard_that_loses_the_marker_race_is_refused(tmp_path):
    # the same index handed to two shards that start at once: the marker create is atomic and only one wins
    class RacingMarkers(FileMarkerStore):
        """The first get() sees no marker for both starters, as two simultaneous starts would."""
        def get(self, key):
            return None if key.endswith("shard-2") and not getattr(self, "raced", False) and self._lose() else super().get(key)

        def _lose(self):
            self.raced = True
            FileMarkerStore.create(self, "w1/shard-2", "{\"someone else\": 1}")     # the other shard wins
            return True

    markers = RacingMarkers(tmp_path / "bucket")
    with pytest.raises(ShardArithmeticError, match="unique indexes"):
        shard(tmp_path, 2, n=4, markers=markers)


def test_the_object_marker_store_needs_an_atomic_create(tmp_path):
    class NoPrecondition:
        def exists(self, *, object_key): return False
    with pytest.raises(TypeError, match="atomic"):
        ObjectMarkerStore(NoPrecondition())


def test_the_object_marker_store_uses_the_precondition_write_and_never_exists_then_write(tmp_path):
    blobs, calls = {}, []

    class Store:
        def exists(self, *, object_key): calls.append("exists"); return object_key in blobs
        def get_bytes(self, *, object_key): return blobs[object_key]
        def create_object_if_absent(self, *, local_path, object_key, content_type=None):
            calls.append("create_if_absent")
            if object_key in blobs:
                return False
            blobs[object_key] = local_path.read_bytes()
            return True
        def upload_file(self, *, local_path, object_key, content_type=None, metadata=None):
            calls.append("upload"); blobs[object_key] = local_path.read_bytes()

    m = ObjectMarkerStore(Store(), "ledger")
    assert m.create("w/x", "v") and not m.create("w/x", "v2")
    assert "exists" not in calls                           # create never probes first
    m.put("w/x", "v3")
    assert m.get("w/x") == "v3"


def test_the_raw_constructor_is_closed():
    with pytest.raises(TypeError, match="for_shard"):
        WaveLedger(299.0)
    with pytest.raises(TypeError):
        WaveLedger(cap_usd=10.0, path=None)


def test_for_test_refuses_to_run_outside_pytest(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    with pytest.raises(RuntimeError, match="for_shard"):
        WaveLedger.for_test(10.0)


def test_for_test_also_refuses_a_blank_pytest_marker(monkeypatch):
    # fail closed: a blank marker is "not under pytest", so the test-only door stays shut
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "  ")
    with pytest.raises(RuntimeError, match="for_shard"):
        WaveLedger.for_test(10.0)


def test_for_test_is_open_under_pytest_and_still_refuses_a_non_finite_cap():
    assert WaveLedger.for_test(10.0).provenance == "test"          # the marker pytest set is honoured
    with pytest.raises(ValueError, match="finite"):
        WaveLedger.for_test(float("inf"))


def test_a_truncated_log_with_a_surviving_marker_is_refused(tmp_path):
    first = shard(tmp_path, 1, n=4)
    a = first.admit("f", 1.0, file_ceiling_usd=100.0)
    _settle(first, a.reservation, "f", 3.0)
    (tmp_path / "shard1.jsonl").write_text("")             # emptied, not deleted
    with pytest.raises(LostShardLog, match="no init row"):
        shard(tmp_path, 1, n=4)


def test_a_rolled_back_log_that_shows_less_than_the_marker_claims_is_refused(tmp_path):
    first = shard(tmp_path, 1, n=4)
    a = first.admit("f", 1.0, file_ceiling_usd=100.0)
    b = first.admit("f", 1.0, file_ceiling_usd=100.0)
    _settle(first, a.reservation, "f", 3.0)
    snapshot = (tmp_path / "shard1.jsonl").read_text()     # a backup taken here ...
    _settle(first, b.reservation, "f", 4.0)                # ... then more is spent
    (tmp_path / "shard1.jsonl").write_text(snapshot)       # and the VM comes back from the backup
    with pytest.raises(LostShardLog, match="rolled back"):
        shard(tmp_path, 1, n=4)


def test_the_spend_marker_tracks_every_settle(tmp_path):
    markers = FileMarkerStore(tmp_path / "bucket")
    ledger = shard(tmp_path, 0, n=4, markers=markers)
    a = ledger.admit("f", 1.0, file_ceiling_usd=100.0)
    _settle(ledger, a.reservation, "f", 2.5)
    assert markers.get("w1/shard-0-spent-micro") == "2500000"


def test_a_settled_row_records_the_cache_write_tokens_it_was_priced_on():
    ledger = WaveLedger.for_test(cap_usd=1.0)
    a = ledger.admit("f1", 0.1, file_ceiling_usd=1.0)
    ledger.settle(a.reservation, file_id="f1", job_id="j", provider="openai", model="gpt-6-astra",
                  input_tokens=100, cached_input_tokens=0, output_tokens=10, cost_usd=0.01,
                  cache_write_tokens=60, cache_write_30m_tokens=50)
    [row] = ledger.entries()
    assert (row["cache_write_tokens"], row["cache_write_30m_tokens"]) == (60, 50)
def test_cap_to_lowers_a_shard_slice_and_the_lowered_cap_binds(tmp_path):
    # Task 49: a $5 smoke inside the $50 normal wave. 12 admissions of $0.40 fit $5.00 exactly (12 x 0.40 = 4.80),
    # the 13th would make $5.20 and is refused by the WAVE cap, not by luck.
    ledger = shard(tmp_path, 0, n=1)
    assert ledger.cap_usd == 50.0
    ledger.cap_to(5.0)
    assert ledger.cap_usd == 5.0
    spent = 0.0
    for i in range(12):
        a = ledger.admit(f"file-{i}", 0.40, file_ceiling_usd=0.50)
        assert a.ok, i
        _settle(ledger, a.reservation, f"file-{i}", 0.40)
        spent += 0.40
    refused = ledger.admit("file-12", 0.40, file_ceiling_usd=0.50)
    assert (refused.ok, refused.reason) == (False, "wave_cap")
    assert ledger.spent_usd() == pytest.approx(4.80) and ledger.spent_usd() <= 5.0


def test_cap_to_never_raises_the_cap_and_refuses_nonsense(tmp_path):
    ledger = shard(tmp_path, 0, n=12)           # slice = 50/12
    with pytest.raises(ValueError, match="only lower"):
        ledger.cap_to(10.0)
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            ledger.cap_to(bad)
    assert ledger.cap_usd == pytest.approx(50.0 / 12)


def test_the_file_ceiling_binds_under_a_lowered_cap(tmp_path):
    ledger = shard(tmp_path, 0, n=1)
    ledger.cap_to(5.0)
    a = ledger.admit("f", 0.30, file_ceiling_usd=0.50)
    _settle(ledger, a.reservation, "f", 0.30)
    refused = ledger.admit("f", 0.30, file_ceiling_usd=0.50)          # 0.60 > 0.50 on ONE file; the wave has room
    assert (refused.ok, refused.reason) == (False, "file_ceiling")
    assert ledger.admit("g", 0.30, file_ceiling_usd=0.50).ok          # another file is unaffected
