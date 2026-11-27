# Responsibility: Verify TIME-21 - a big FEA sizing runs on a VM of its own only when its predicted worker time is over the line; the VM's result is stored exactly as the worker's would be (the same size choice, the same plans beside the check); when no VM can be had, or the hand-off fails at any step, the worker sizes it as before; the stage keeps saying "Sizing the mesh..." meanwhile and TIME-11 still ends it; and no VM is left running at any exit (sized, failed, lost, the check moved on, the session gone, the run started first, past the clock, the watch failed).
# Boundaries: no worker, no VM, no bucket: the remote route is stood in for (it counts the VMs it holds), the sizing child is stood in for (its answer is a fixed size choice and plans file); the check is the real record in the unit tier's in-memory object store.
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from meshpipeline.application import big_sizing as BS
from meshpipeline.application import fea_check as FC
from meshpipeline.application import geometry_check as gc
from meshpipeline.contracts import fea_sizing_remote as seam
from meshpipeline.contracts.object_storage import get_object_store
from meshpipeline.engines import fea_plan_options as PO

SID = "s-vm-1"
TOKEN = "tok1"
#: what the sizing child answers (a two-option size choice) and the plans it keeps
OPTIONS = {"version": PO.VERSION, "rule": "PLAN-fea-22", "choice": True, "recommended": "quicker", "why": "w",
           "full": {"elements": 3_000_000, "standard": False}, "quicker": {"elements": 900_000, "standard": True}}
PLANS = {"key": "k1", "full": {"base_size_m": 0.004, "zones": []}, "quicker": {"base_size_m": 0.006, "zones": []}}
#: a part like the customer assembly (1,764 hot spots, 95k triangles) and one like a knuckle
BIG = {"measured": True, "basis": "cad", "bodies": 3, "hot_spots": [{}] * 1764, "surface_triangles": 95_000}
SMALL = {"measured": True, "basis": "cad", "bodies": 1, "hot_spots": [{}] * 40, "surface_triangles": 60_000}


class FakeRemote:
    """The VM route, stood in for: every VM it starts is `live` until released."""

    def __init__(self, *, refuse: bool = False, states=("done",), result: dict | None = None,
                 collect_fails: bool = False):
        self.refuse, self.states, self.result = refuse, list(states), result
        self.collect_fails = collect_fails
        self.live: set[str] = set()
        self.released: list[str] = []
        self.sent: list[dict] = []

    def start(self, ws: Path, *, key: str, timeout_s: int) -> dict:
        if self.refuse:
            raise seam.SizingNotStarted("ZONE_RESOURCE_POOL_EXHAUSTED; QUOTA_EXCEEDED")
        spec = json.loads((ws / PO.PACKED_FILE).read_text())
        self.sent.append({"spec": spec, "files": sorted(p.name for p in ws.iterdir()), "timeout_s": timeout_s})
        name = f"mesh-big-{len(self.sent)}"
        self.live.add(name)
        return {"op": "0" * 64, "vm": f"projects/p/zones/z/instances/{name}", "machine": "c2d-standard-32",
                "cores": 16, "started_at": time.time()}

    def look(self, handle: dict) -> dict:
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"state": state, "why": "its VM stopped answering" if state == "lost" else state}

    def collect(self, handle: dict, dest: Path) -> dict:
        if self.collect_fails:
            raise OSError("the archive could not be read")
        dest.mkdir(parents=True, exist_ok=True)
        (dest / PO.PLANS_FILE).write_text(json.dumps(PLANS))
        return self.result if self.result is not None else {
            "rc": 0, "fea_sizing": {"options": OPTIONS, "out_of_time": False, "planning_code": PO.planning_code(),
                                    "workers": 8, "seconds": 80.0}}

    def release(self, handle: dict, *, why: str) -> None:
        self.live.discard(str(handle.get("vm") or "").rsplit("/", 1)[-1])
        self.released.append(why)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A ready FEA check marked "sizing", its file staged as the check stages it, and the sizing child stood in for:
    it answers OPTIONS and keeps PLANS. `world["remote"]` is installed with `use(remote)`; the watch's queue records
    what was queued."""
    from meshpipeline.cad import ingest
    from meshpipeline.cad.fea_measure import FACT_KEY
    from meshpipeline.contracts.geometry_source import GeometrySourceRef
    from meshpipeline.contracts.intake_formats import GeometryKind

    src = tmp_path / "geometry.step"
    src.write_text("ISO-10303-21; a part")

    class _Canon:
        kind = GeometryKind.cad
        path = src

    class _Prepared:
        to_metres = 0.001

    monkeypatch.setattr(GeometrySourceRef, "from_payload", staticmethod(lambda p: p))
    monkeypatch.setattr(gc, "_fetch", lambda ref, work: src)
    monkeypatch.setattr(ingest, "canonicalise", lambda *a, **k: _Canon())
    monkeypatch.setattr(gc, "_prepared_coordinates", lambda *a, **k: (_Prepared(), ""))
    sized_here: list[dict] = []

    def bounded(path, *, to_metres, budget_s, plans_out=None, told=None):
        sized_here.append({"path": Path(path).name, "to_metres": to_metres, "budget_s": budget_s})
        if plans_out:
            Path(plans_out).write_text(json.dumps(PLANS))
        return dict(OPTIONS)

    monkeypatch.setattr(PO, "bounded", bounded)
    queued: list[dict] = []

    def use(remote, *, watch=True, record=BIG):
        seam.set_sizing_remote(remote)
        seam.set_watch_enqueuer((lambda **kw: queued.append(kw)) if watch else None)
        mark = {"sizing": True, "token": TOKEN, "since": round(time.time(), 1),
                "until": round(time.time() + gc.sizing_deadline_s(), 1)}
        gc.write_status(SID, gc.STATUS_READY, named=True, source={"sha256": "x"},
                        facts={FACT_KEY: record, FC.PLAN_FACT_KEY: mark})
        return remote

    yield {"use": use, "queued": queued, "sized_here": sized_here, "record": BIG}
    seam.set_sizing_remote(None)
    seam.set_watch_enqueuer(None)


def _plan_fact() -> dict:
    return (gc.read_check(SID) or {})["facts"][FC.PLAN_FACT_KEY]


def _kept_plans() -> dict:
    return json.loads(get_object_store().get_bytes(object_key=gc.check_object_key(SID, PO.PLANS_FILE)))


def _size(record=BIG, **kw) -> dict:
    stored = gc.read_check(SID)
    return gc.size_fea_plan(SID, stored, None, token=TOKEN, started=time.time(), record=record, **kw)


def _watch_until_done(remote, limit: int = 10) -> list[str]:
    seen = []
    for _ in range(limit):
        got = BS.watch(SID, TOKEN, _plan_fact().get("vm") or {"vm": next(iter(remote.live), "")}, None)
        seen.append(got)
        if got != "waiting":
            break
    return seen


# # the prediction and the line


def test_the_prediction_reads_the_checks_own_measurement():
    assert BS.predicted_worker_s(BIG) == pytest.approx(0.56 * (1764 + 95_000 / 2000), abs=0.1)
    assert BS.predicted_worker_s(BIG) > BS.THRESHOLD_S                         # the customer assembly goes away
    assert BS.predicted_worker_s(SMALL) < BS.THRESHOLD_S                       # a knuckle-like part stays
    assert BS.predicted_worker_s({"measured": False}) is None                  # nothing to go on: as before
    # a record kept from before the triangles were counted is predicted from its hot spots
    assert BS.predicted_worker_s({"measured": True, "hot_spots": [{}] * 1000}) == pytest.approx(560.0)


def test_below_the_line_nothing_changes(world):
    remote = world["use"](FakeRemote(), record=SMALL)
    got = _size(SMALL)
    assert got["sized"] is True and not remote.sent and not world["queued"]
    assert world["sized_here"]                                                 # sized on the worker
    fact = _plan_fact()
    assert {k: fact[k] for k in OPTIONS} == OPTIONS
    assert fact["sized_on"]["where"] == "worker" and fact["sized_on"]["rule"] == BS.RULE


# # over the line: sent away, the slot freed, the result stored as the worker would


def test_a_big_sizing_is_sent_away_and_the_naming_task_returns_at_once(world):
    remote = world["use"](FakeRemote(states=["running"]))
    got = _size()
    assert got == {"sizing": True, "where": BS.ON_VM}
    assert not world["sized_here"]                                             # nothing sized on the worker
    (sent,) = remote.sent
    assert sent["files"] == sorted(["geometry.step", PO.PACKED_FILE])          # the check's file, under its name
    assert sent["spec"]["to_metres"] == 0.001
    assert sent["spec"]["budget_s"] == PO.BUDGET_S - BS.VM_ALLOWANCE_S
    (q,) = world["queued"]
    assert q["countdown"] == BS.WATCH_EVERY_S and q["token"] == TOKEN
    mark = _plan_fact()
    assert mark["sizing"] is True and mark["where"] == BS.ON_VM and mark["vm"]["machine"] == "c2d-standard-32"
    # the stage keeps its line, and the user can still go on
    assert FC.plan_view(gc.read_check(SID)["facts"])["words"].startswith("Sizing the mesh... You can go on")


def test_the_vms_result_is_stored_exactly_as_the_workers(world):
    # the worker's own answer, as it is today
    world["use"](FakeRemote(), record=SMALL)
    _size(SMALL)
    on_worker, plans_worker = _plan_fact(), _kept_plans()
    # the same answer from the VM
    remote = world["use"](FakeRemote(states=["running", "done"]))
    _size()
    assert _watch_until_done(remote) == ["waiting", "sized"]
    on_vm, plans_vm = _plan_fact(), _kept_plans()
    assert {k: v for k, v in on_vm.items() if k != "sized_on"} == \
        {k: v for k, v in on_worker.items() if k != "sized_on"}
    assert plans_vm == plans_worker
    assert on_vm["sized_on"]["where"] == BS.ON_VM and on_vm["sized_on"]["machine"] == "c2d-standard-32"
    assert FC.plan_view(gc.read_check(SID)["facts"])["state"] == "sized"
    assert not remote.live                                                     # released once collected


# # nothing gets worse when no VM can be had or the hand-off fails


def test_no_vm_to_be_had_sizes_it_on_the_worker_as_before(world):
    remote = world["use"](FakeRemote(refuse=True))
    got = _size()
    assert got["sized"] is True and world["sized_here"] and not remote.live and not world["queued"]
    assert _plan_fact()["sized_on"]["where"] == "worker"


def test_no_watch_queue_sizes_it_on_the_worker(world):
    remote = world["use"](FakeRemote(), watch=False)
    assert _size()["sized"] is True and not remote.sent and world["sized_here"]


def test_a_watch_that_cannot_be_queued_releases_the_vm_and_the_worker_sizes_it(world, monkeypatch):
    remote = world["use"](FakeRemote())
    monkeypatch.setattr(seam, "enqueue_watch", lambda **kw: False)
    got = _size()
    assert got["sized"] is True and world["sized_here"]
    assert remote.sent and not remote.live
    assert _plan_fact()["sized"] is True and _plan_fact()["sized_on"]["where"] == "worker"


def test_a_check_that_cannot_record_the_vm_releases_it(world, monkeypatch):
    remote = world["use"](FakeRemote())
    real = gc.store_plan_fact
    monkeypatch.setattr(gc, "store_plan_fact", lambda *a, **k: False)
    got = _size()
    monkeypatch.setattr(gc, "store_plan_fact", real)
    assert remote.sent and not remote.live and world["sized_here"] and got["sized"] is True


@pytest.mark.parametrize("remote_kw, why", [
    ({"states": ["lost"]}, "its VM stopped answering"),
    ({"result": {"rc": -4, "log_tail": "[DISPATCH] unknown engine 'fea_sizing'"}}, "its mesh image could not size it"),
    ({"result": {"rc": 0, "fea_sizing": {"options": OPTIONS, "planning_code": "older0code0000"}}},
     "its mesh image sizes by other code"),
    ({"collect_fails": True}, "its result could not be brought back"),
])
def test_a_vm_that_fails_the_sizing_is_released_and_the_worker_sizes_it(world, remote_kw, why):
    remote = world["use"](FakeRemote(**remote_kw))
    _size()
    assert remote.live
    assert _watch_until_done(remote) == ["sized on the worker"]
    assert not remote.live and why in remote.released[0]
    fact = _plan_fact()
    assert fact["sized"] is True and {k: fact[k] for k in OPTIONS} == OPTIONS
    assert fact["sized_on"]["where"] == "worker" and why in fact["sized_on"]["why"]
    assert world["sized_here"]


def test_while_the_worker_sizes_it_after_the_vm_the_marker_is_held_by_its_own_clock(world, monkeypatch):
    remote = world["use"](FakeRemote(states=["lost"]))
    _size()
    seen: dict = {}
    real = gc.size_fea_plan

    def during(*a, **k):
        seen["mark"] = _plan_fact()
        return real(*a, **k)

    monkeypatch.setattr(gc, "size_fea_plan", during)
    BS.watch(SID, TOKEN, _plan_fact()["vm"], None)
    mark = seen["mark"]
    assert mark["sizing"] is True and mark["where"] == BS.AFTER_VM and "vm" not in mark
    assert mark["until"] >= time.time() + gc.sizing_deadline_s() - 60          # the whole clock again
    # the naming task has ended: that is no reason to call the sizing stopped
    facts = {FC.PLAN_FACT_KEY: mark}
    assert FC.sizing_now(facts, {"ended_at": time.time() - 3600}, time.time()) is facts
    assert not remote.live


# # TIME-11 holds while it is away


def test_a_sizing_on_a_vm_is_not_settled_by_its_ended_naming_task():
    now = 1_000_000.0
    facts = {FC.PLAN_FACT_KEY: {"sizing": True, "token": "t", "until": now + 600, "where": BS.ON_VM}}
    assert FC.sizing_now(facts, {"ended_at": now - 600}, now) is facts
    late = FC.sizing_now(facts, {"ended_at": now - 600}, now + 601)
    assert late[FC.PLAN_FACT_KEY]["stopped"] == "clock"                        # it always ends, and says how


# # no VM is left running at any exit


def test_a_check_that_moved_on_releases_the_vm(world):
    remote = world["use"](FakeRemote(states=["running"]))
    _size()
    handle = _plan_fact()["vm"]
    gc.store_plan_fact(SID, TOKEN, lambda m: {**m, "token": "a-new-naming"})
    assert BS.watch(SID, TOKEN, handle, None) == "moved on"
    assert not remote.live


def test_a_closed_session_releases_the_vm(world):
    remote = world["use"](FakeRemote(states=["running"]))
    _size()
    handle = _plan_fact()["vm"]
    get_object_store().delete_object(object_key=gc.check_object_key(SID, "scout.json"))
    assert BS.watch(SID, TOKEN, handle, None) == "moved on"
    assert not remote.live


def test_the_run_starting_first_releases_the_vm_and_says_so(world):
    remote = world["use"](FakeRemote(states=["running"]))
    _size()
    handle = _plan_fact()["vm"]
    BS.at_run_start(SID, "job-1")
    assert not remote.live
    fact = _plan_fact()
    assert fact["sized"] is False and fact["stopped"] == "run"
    assert FC.plan_view(gc.read_check(SID)["facts"])["state"] == "not_sized"
    assert BS.watch(SID, TOKEN, handle, None) == "moved on"                    # the next look stops too
    from meshpipeline.contracts import decision_record as dr
    (cited,) = [c for c in dr.cited_for("job-1") if c["stage"] == "fea_sizing"]
    assert cited["rule"] == BS.RULE and "when the run started" in cited["words"]


def test_past_the_clock_releases_the_vm(world):
    remote = world["use"](FakeRemote(states=["running"]))
    _size()
    handle = _plan_fact()["vm"]
    assert BS.watch(SID, TOKEN, handle, None, now=time.time() + gc.sizing_deadline_s() + 1) == "out of time"
    assert not remote.live and _plan_fact()["stopped"] == "clock"


def test_a_watch_that_cannot_queue_its_next_look_releases_the_vm(world, monkeypatch):
    remote = world["use"](FakeRemote(states=["running"]))
    _size()
    handle = _plan_fact()["vm"]
    monkeypatch.setattr(seam, "enqueue_watch", lambda **kw: False)
    assert BS.watch(SID, TOKEN, handle, None) == "sized on the worker"
    assert not remote.live


def test_a_look_that_breaks_releases_the_vm(world):
    remote = world["use"](FakeRemote(states=["running"]))
    _size()
    handle = _plan_fact()["vm"]

    def broken(_h):
        raise RuntimeError("compute API down")

    remote.look = broken
    assert BS.watch(SID, TOKEN, handle, None) == "sized on the worker"
    assert not remote.live


def test_a_vm_out_of_time_is_said_as_out_of_time_and_released(world):
    remote = world["use"](FakeRemote(result={"rc": 1, "timed_out": True, "fea_sizing": {
        "options": None, "out_of_time": True, "planning_code": PO.planning_code()}}))
    _size()
    assert _watch_until_done(remote) == ["out of time"]
    assert not remote.live and _plan_fact()["why"] == FC.out_of_time_words()


def test_the_run_cites_where_a_worker_sizing_ran(world):
    world["use"](FakeRemote(), record=SMALL)
    _size(SMALL)
    BS.at_run_start(SID, "job-2")
    from meshpipeline.contracts import decision_record as dr
    (cited,) = [c for c in dr.cited_for("job-2") if c["stage"] == "fea_sizing"]
    assert cited["value"] == "worker" and cited["inputs"]["threshold_s"] == BS.THRESHOLD_S
