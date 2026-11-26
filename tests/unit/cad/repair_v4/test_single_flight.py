"""D23: a wave judges each input ONCE. Concurrent units take a lease on the verdict's key; one meshes, the others
wait for its panel; a released-without-panel or expired lease hands the work to a waiter; nobody blocks forever."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from meshpipeline.cad.repair_v4.verdict.cache import DirStore, MemoryStore, VerdictCache
from meshpipeline.cad.repair_v4.verdict.panel import MemberResult


def _panel(status: str = "pass") -> list:
    return [MemberResult("gmsh_prod", status, None if status == "pass" else "timeout", "")]


def test_two_concurrent_units_mesh_once_and_both_get_the_panel(tmp_path: Path) -> None:
    cache = VerdictCache(DirStore(tmp_path))
    meshed: list[int] = []

    def compute() -> list:
        meshed.append(1)
        time.sleep(0.3)
        return _panel()

    got: list = []
    threads = [threading.Thread(target=lambda: got.append(cache.single_flight("s", "c", compute, lease_s=30,
                                                                              poll_s=0.05)))
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(meshed) == 1 and len(got) == 3 and all(g[0].status == "pass" for g in got)
    assert not (tmp_path / cache.lease_key("s", "c")).exists()          # released


def test_an_uncacheable_result_is_not_served_and_a_waiter_computes_its_own() -> None:
    """The claimer's member timed out (never cached): waiters must not get it; one of them meshes."""
    store = MemoryStore()
    cache = VerdictCache(store)
    store.create_if_absent(cache.lease_key("s", "c"), json.dumps({"expires_at": time.time() + 60}))
    released = threading.Timer(0.2, lambda: store.delete(cache.lease_key("s", "c")))   # claimer ends, nothing stored
    released.start()
    calls: list[int] = []
    panel = cache.single_flight("s", "c", lambda: calls.append(1) or _panel(), lease_s=30, poll_s=0.05)
    assert calls == [1] and panel[0].status == "pass"


def test_an_expired_lease_is_taken_over() -> None:
    store = MemoryStore()
    cache = VerdictCache(store)
    store.create_if_absent(cache.lease_key("s", "c"), json.dumps({"owner": "dead", "expires_at": time.time() - 1}))
    panel = cache.single_flight("s", "c", _panel, lease_s=30, poll_s=0.01)
    assert panel[0].status == "pass" and cache.get("s", "c") is not None


def test_no_wait_outlives_its_own_bound() -> None:
    store = MemoryStore()
    cache = VerdictCache(store)
    store.create_if_absent(cache.lease_key("s", "c"), json.dumps({"expires_at": time.time() + 3600}))   # never ends
    t0 = time.monotonic()
    panel = cache.single_flight("s", "c", _panel, lease_s=0.3, poll_s=0.05)
    assert panel[0].status == "pass" and time.monotonic() - t0 < 5


def test_a_store_without_leases_just_computes() -> None:
    class Plain:
        def __init__(self) -> None:
            self.d: dict = {}

        def get(self, k: str):
            return self.d.get(k)

        def put(self, k: str, t: str) -> None:
            self.d[k] = t

    assert VerdictCache(Plain()).single_flight("s", "c", _panel, lease_s=1)[0].status == "pass"


def test_a_claimer_renews_its_lease_while_it_meshes(monkeypatch) -> None:
    """The lease is short and HEARTBEATS, so a killed claimer frees the input in minutes, not the panel's worst case."""
    from meshpipeline.cad.repair_v4.verdict import cache as C

    monkeypatch.setattr(C, "LEASE_HEARTBEAT_EVERY_S", 0.05)
    monkeypatch.setattr(C, "LEASE_HEARTBEAT_TTL_S", 0.2)
    store = MemoryStore()
    vc = VerdictCache(store)
    seen: list[float] = []

    def compute() -> list:
        for _ in range(6):
            time.sleep(0.05)
            seen.append(json.loads(store.get(vc.lease_key("s", "c")))["expires_at"])
        return _panel()

    vc.single_flight("s", "c", compute, lease_s=30, poll_s=0.01)
    assert seen[-1] > seen[0]                                 # renewed while meshing
    assert store.get(vc.lease_key("s", "c")) is None          # released after
