# Responsibility: Prove a throttled wave is distinguishable from a free-running one in the report, and that a shard
#                 records its wave back-off (or the fact that it had no guard) in provenance.
# Boundaries: hermetic; no model, no bucket. Uses a directory marker store.
from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
V4 = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"
sys.path.insert(0, str(V4))
import report  # noqa: E402
import run_arm as ra  # noqa: E402

GUARD = {"guard": "wave_backoff", "held_s": 0.0, "holds": 0, "reported_429": 0, "store_errors": 0, "trips_seen": []}
TRIP = {"bucket": 7, "at": 100.0, "until": 400.0, "by_shard": "3/4", "threshold": 10, "window_s": 300.0}


def shard(**wave_backoff: object) -> dict:
    return {"prov": {"wave_backoff": {**GUARD, **wave_backoff}} if wave_backoff else {}}


def test_a_wave_that_held_calls_or_saw_a_trip_is_throttled() -> None:
    s = report.backoff_summary([shard(), shard(held_s=42.0, holds=3, trips_seen=[TRIP]),
                                shard(trips_seen=[TRIP])], has_model_arm=True)
    assert s["status"] == "throttled" and len(s["trips"]) == 1 and s["held_s_total"] == 42.0
    assert s["shards_held"] == 1


def test_a_wave_is_free_only_if_every_shard_ran_the_guard_and_none_tripped() -> None:
    assert report.backoff_summary([shard(guard="wave_backoff")] * 3, has_model_arm=True)["status"] == "free"
    # a shard with no account is NOT evidence of no back-off
    assert report.backoff_summary([shard(guard="wave_backoff"), {"prov": {}}], has_model_arm=True)["status"] == "unrecorded"
    assert report.backoff_summary([shard(store_errors=1)], has_model_arm=True)["status"] == "unrecorded"
    assert report.backoff_summary([{"prov": {}}], has_model_arm=False)["status"] == "not_applicable"


def test_a_shard_with_a_ledger_gets_the_wave_guard_and_records_the_floor(tmp_path: Path) -> None:
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    class L:
        markers = FileMarkerStore(tmp_path)

    prov: dict = {}
    # 96 shards > the 37 in-flight ceiling: the floor needs the launch's explicit override (B11), which is recorded
    args = Namespace(n_shards=96, shard="3/96", wave_id="w1", model_max_in_flight=None, allow_in_flight_floor=True)
    t = ra.build_throttle(args, "openai:gpt-6-astra", prov, ledger=L())
    assert t.guard is not None and prov["model_throttle"]["in_flight_floored"] is True
    assert prov["model_throttle"]["in_flight_floor_override"] is True
    assert prov["model_throttle"]["max_in_flight"] == 1
    prov2: dict = {}
    t2 = ra.build_throttle(args, "openai:gpt-6-astra", prov2, ledger=None)      # no markers: no guard, said so
    assert t2.guard is None and prov2["wave_backoff"]["guard"] == "absent"
