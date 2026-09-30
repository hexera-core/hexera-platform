# Responsibility: Verify an idle fleet worker removes its own VM only when that is safe - never while it holds a job,
#                 never below the group's floor, and only after it has stopped taking work and found itself still empty.
# Boundaries: the decision, the drain-then-remove order against fakes, the Compute API calls against a fake opener, and
#             the start-up gate; nothing reaches a cloud or a broker.
from __future__ import annotations

import io
import json
import threading
import urllib.error

import pytest

from meshpipeline.runtime import idle_retire as ir
from meshpipeline.runtime.idle_retire import Action, GroupState

# THE DEFECT THIS PINS. Shared dev, 2026-09-30, job bc5ddb08: the autoscaler scaled in 3 -> 2 at
# 08:29:52 and 2 -> 1 at 09:00:13 on a correct demand figure, and both times the group deleted the
# VM running the job rather than the idle one - a Compute Engine group cannot be told which VM is
# idle. Removal is now the idle worker's own act, and these are its rules.

MIN = 60.0


def _group(**over) -> GroupState:
    base: dict = {"target_size": 3, "min_size": 1, "recommended_size": 1, "workers_scale_in": True,
                  "opportunistic": False, "target_template": "tpl-new"}
    base.update(over)
    return GroupState(**base)


# ---------------------------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------------------------

def test_a_worker_holding_a_job_never_leaves():
    action, why = ir.decide(holding=1, idle_seconds=10_000, idle_limit_seconds=600,
                            group=_group(opportunistic=True), own_template="tpl-old")
    assert action is Action.STAY and "holding" in why


def test_an_idle_worker_above_the_floor_leaves_after_the_limit():
    assert ir.decide(holding=0, idle_seconds=599, idle_limit_seconds=600, group=_group(),
                     own_template="tpl-new")[0] is Action.STAY
    action, why = ir.decide(holding=0, idle_seconds=600, idle_limit_seconds=600, group=_group(),
                            own_template="tpl-new")
    assert action is Action.RETIRE and "wants 1 of the group's 3" in why


@pytest.mark.parametrize("target,floor", [(1, 1), (0, 0), (2, 2), (1, 2)])
def test_never_below_the_floor(target, floor):
    # even when the autoscaler's recommendation is lower still - it never recommends below its floor
    action, why = ir.decide(holding=0, idle_seconds=10_000, idle_limit_seconds=600,
                            group=_group(target_size=target, min_size=floor, recommended_size=0),
                            own_template="tpl-new")
    assert action is Action.STAY and "floor" in why


@pytest.mark.parametrize("recommended,expected", [(3, Action.STAY), (4, Action.STAY), (None, Action.STAY),
                                                  (2, Action.RETIRE)])
def test_the_autoscaler_decides_how_many_the_idle_worker_decides_which(recommended, expected):
    # The autoscaler holds a peak for about ten minutes before its recommendation falls. A worker
    # that left while it still wanted 3 would only make it add a VM back - churn, not savings.
    action, _ = ir.decide(holding=0, idle_seconds=10_000, idle_limit_seconds=600,
                          group=_group(target_size=3, recommended_size=recommended),
                          own_template="tpl-new")
    assert action is expected


def test_a_group_whose_autoscaler_still_scales_in_is_left_to_it():
    # two removers of one group would fight: the autoscaler would put back what a worker removed
    action, why = ir.decide(holding=0, idle_seconds=10_000, idle_limit_seconds=600,
                            group=_group(workers_scale_in=False), own_template="tpl-new")
    assert action is Action.STAY and "scales in by itself" in why


def test_a_group_without_an_autoscaler_keeps_its_hand_set_size():
    action, _ = ir.decide(holding=0, idle_seconds=10_000, idle_limit_seconds=600,
                          group=_group(min_size=None), own_template="tpl-new")
    assert action is Action.STAY


def test_an_outdated_idle_worker_recycles_itself_even_at_the_floor():
    # recreating keeps the size, so the floor is never crossed
    group = _group(target_size=1, min_size=1, opportunistic=True)
    assert ir.decide(holding=0, idle_seconds=ir.RECYCLE_IDLE_SECONDS - 1, idle_limit_seconds=600,
                     group=group, own_template="tpl-old")[0] is Action.STAY
    action, why = ir.decide(holding=0, idle_seconds=ir.RECYCLE_IDLE_SECONDS,
                            idle_limit_seconds=600, group=group, own_template="tpl-old")
    assert action is Action.RECYCLE and "tpl-new" in why


def test_nothing_is_recycled_under_a_proactive_roll_or_a_canary():
    # proactive: the group replaces it itself; canary: two versions, no single "current"
    for group in (_group(opportunistic=False), _group(opportunistic=True, target_template=None)):
        assert ir.decide(holding=0, idle_seconds=10_000, idle_limit_seconds=10**6, group=group,
                         own_template="tpl-old")[0] is Action.STAY


# ---------------------------------------------------------------------------------------------
# the drain-then-remove order, against fakes
# ---------------------------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Tap:
    def __init__(self, events, holding=0):
        self.events, self.held, self.consuming = events, holding, True
        self.on_stop = None     # a job that lands while the drain is under way
        self.neighbour = None   # the geometry worker on the same VM: None = nobody answered

    def holding(self):
        return self.held

    def neighbour_holding(self):
        self.events.append("ask_neighbour")
        return self.neighbour

    def taking(self):
        return self.consuming

    def stop_taking(self):
        self.events.append("stop_taking")
        self.consuming = False
        if self.on_stop:
            self.on_stop(self)

    def resume_taking(self):
        self.events.append("resume_taking")
        self.consuming = True


class _Group:
    def __init__(self, events, states, refuse=False):
        self.events, self.states, self.refuse = events, list(states), refuse

    def state(self):
        self.events.append("state")
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def remove_self(self):
        self.events.append("remove_self")
        if self.refuse:
            raise ir.Refused("deleteInstances refused (403)")

    def recycle_self(self):
        self.events.append("recycle_self")


class _Lock:
    def __init__(self, events, free=True):
        self.events, self.free = events, free

    def acquire(self, ttl):
        self.events.append(f"lock:{ttl}")
        return self.free

    def release(self):
        self.events.append("unlock")


def _retirer(events, *, group_states, holding=0, lock_free=True, refuse=False, own="tpl-new",
             limit=10 * MIN):
    clock = _Clock()
    tap, group, lock = _Tap(events, holding), _Group(events, group_states, refuse), _Lock(events, lock_free)
    r = ir.IdleRetirer(tap=tap, group=group, lock=lock, own_template=own, idle_limit_seconds=limit,
                       grant_hint="gcloud ... once", clock=clock, sleep=clock.sleep)
    return r, clock, tap


def test_a_clean_drain_comes_before_the_removal():
    events: list[str] = []
    r, clock, _tap = _retirer(events, group_states=[_group()])
    clock.t += 10 * MIN
    assert r.tick() is Action.RETIRE
    # lock, stop taking, (confirm, settle), ask the geometry worker, look again under the lock,
    # THEN remove - and the lock is held past the removal so the next worker reads a group that
    # already counts it
    assert events == ["state", f"lock:{ir.RETIRE_LOCK_SECONDS}", "stop_taking", "ask_neighbour",
                      "state", "remove_self"]


def test_a_geometry_check_running_on_the_same_vm_keeps_it():
    # the check was already taken; it would go down with the VM
    events: list[str] = []
    r, clock, tap = _retirer(events, group_states=[_group()])
    tap.neighbour = 1
    clock.t += 10 * MIN
    assert r.tick() is Action.STAY
    assert "remove_self" not in events and events[-2:] == ["resume_taking", "unlock"]


def test_a_geometry_worker_that_does_not_answer_does_not_hold_the_vm():
    events: list[str] = []
    r, clock, tap = _retirer(events, group_states=[_group()])
    tap.neighbour = None
    clock.t += 10 * MIN
    assert r.tick() is Action.RETIRE


def test_a_worker_holding_a_job_makes_no_call_at_all():
    events: list[str] = []
    r, clock, _tap = _retirer(events, group_states=[_group()], holding=1)
    for _ in range(100):
        clock.t += 10 * MIN
        assert r.tick() is Action.STAY
    assert events == []


def test_the_idle_clock_restarts_when_a_job_finishes():
    events: list[str] = []
    r, clock, tap = _retirer(events, group_states=[_group()], holding=1)
    clock.t += 60 * MIN
    r.tick()                          # busy: the clock is pinned here
    tap.held = 0
    clock.t += 9 * MIN
    assert r.tick() is Action.STAY and "remove_self" not in events
    clock.t += 1 * MIN
    assert r.tick() is Action.RETIRE


def test_a_job_that_arrives_during_the_drain_is_kept_and_nothing_is_removed():
    events: list[str] = []
    r, clock, tap = _retirer(events, group_states=[_group()])

    def _lands(t):
        t.held = 1
    tap.on_stop = _lands
    clock.t += 10 * MIN
    assert r.tick() is Action.STAY
    assert "remove_self" not in events
    assert events[-2:] == ["resume_taking", "unlock"]
    assert r.idle_seconds < 60       # the idle clock starts again


def test_a_group_that_reached_its_floor_while_draining_is_left_alone():
    # another worker retired between the first read and the one under the lock
    events: list[str] = []
    r, clock, _tap = _retirer(events, group_states=[_group(target_size=2),
                                                    _group(target_size=1)])
    clock.t += 10 * MIN
    assert r.tick() is Action.STAY
    assert "remove_self" not in events and events[-2:] == ["resume_taking", "unlock"]


def test_while_another_worker_is_retiring_nobody_else_drains():
    events: list[str] = []
    r, clock, _tap = _retirer(events, group_states=[_group()], lock_free=False)
    clock.t += 10 * MIN
    assert r.tick() is Action.STAY
    assert events == ["state", f"lock:{ir.RETIRE_LOCK_SECONDS}"]


def test_a_refused_removal_resumes_work_and_names_the_grant(caplog):
    events: list[str] = []
    r, clock, _tap = _retirer(events, group_states=[_group()], refuse=True)
    clock.t += 10 * MIN
    with caplog.at_level("ERROR"):
        assert r.tick() is Action.STAY
    assert events[-2:] == ["resume_taking", "unlock"]
    assert "gcloud ... once" in caplog.text
    # and it does not hammer the API: nothing again until the back-off has passed
    n = len(events)
    clock.t += ir.REFUSED_BACKOFF_SECONDS - 1
    r.tick()
    assert len(events) == n


def test_a_consumer_that_will_not_stop_means_no_removal():
    events: list[str] = []
    r, clock, tap = _retirer(events, group_states=[_group()])
    tap.stop_taking = lambda: events.append("stop_taking")   # never confirms
    clock.t += 10 * MIN
    assert r.tick() is Action.STAY
    assert "remove_self" not in events and events[-2:] == ["resume_taking", "unlock"]


def test_an_outdated_worker_recycles_under_the_longer_lock():
    events: list[str] = []
    r, clock, _tap = _retirer(events, group_states=[_group(target_size=1, opportunistic=True)],
                              own="tpl-old")
    clock.t += ir.RECYCLE_IDLE_SECONDS
    assert r.tick() is Action.RECYCLE
    assert f"lock:{ir.RECYCLE_LOCK_SECONDS}" in events and events[-1] == "recycle_self"


def test_a_worker_whose_removal_did_not_happen_takes_work_again():
    events: list[str] = []
    r, clock, tap = _retirer(events, group_states=[_group()])
    clock.t += 10 * MIN
    r.announce = lambda: None
    ticks = iter([Action.RETIRE])
    r.tick = lambda: next(ticks)

    class _Stop:
        def __init__(self):
            self.calls = 0

        def wait(self, s):
            self.calls += 1
            return self.calls > 2     # poll -> tick (RETIRE), still-here wait, then stop
    tap.consuming = False
    r.run(_Stop())
    assert tap.consuming is True and events[-1] == "resume_taking"


# ---------------------------------------------------------------------------------------------
# the Compute API, against a fake opener
# ---------------------------------------------------------------------------------------------

_IDENT = ir.Identity(project="hexera-dev", zone="us-central1-a", instance="dev-workers-6p3w",
                     group="dev-workers", template="dev-workers-tpl-old",
                     service_account="224734058693-compute@developer.gserviceaccount.com")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(routes, sent):
    def _open(req, timeout=None):
        sent.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
        for fragment, body in routes.items():
            if req.full_url.endswith(fragment):
                if isinstance(body, int):
                    raise urllib.error.HTTPError(req.full_url, body, "x", {}, None)
                return _Resp(json.dumps(body).encode())
        raise AssertionError(req.full_url)
    return _open


_IGM = {"targetSize": 2, "status": {"autoscaler":
        "https://www.googleapis.com/compute/v1/projects/hexera-dev/zones/us-central1-a/autoscalers/dev-workers-eyms"},
        "versions": [{"instanceTemplate": "https://www.googleapis.com/compute/v1/projects/hexera-dev/global/instanceTemplates/dev-workers-tpl-new"}],
        "updatePolicy": {"type": "OPPORTUNISTIC"}}
_AS = {"autoscalingPolicy": {"minNumReplicas": 1, "mode": "ONLY_SCALE_OUT"}, "recommendedSize": 1}


def test_the_group_is_read_from_its_own_settings():
    sent: list = []
    g = ir.GceGroup(_IDENT, opener=_opener({"instanceGroupManagers/dev-workers": _IGM,
                                            "autoscalers/dev-workers-eyms": _AS}, sent),
                    token=lambda: "t")
    assert g.state() == GroupState(target_size=2, min_size=1, recommended_size=1,
                                   workers_scale_in=True, opportunistic=True,
                                   target_template="dev-workers-tpl-new")
    # the autoscaler link is followed on the same endpoint as everything else
    assert sent[1][1] == ("https://compute.googleapis.com/compute/v1/projects/hexera-dev/zones/"
                          "us-central1-a/autoscalers/dev-workers-eyms")


def test_an_autoscaler_that_still_scales_in_is_read_as_such():
    g = ir.GceGroup(_IDENT, opener=_opener({"instanceGroupManagers/dev-workers": _IGM,
                                            "autoscalers/dev-workers-eyms":
                                            {"autoscalingPolicy": {"minNumReplicas": 1, "mode": "ON"}}}, []),
                    token=lambda: "t")
    assert g.state().workers_scale_in is False


def test_removal_names_this_vm_and_nothing_else():
    sent: list = []
    g = ir.GceGroup(_IDENT, opener=_opener({"/deleteInstances": {}, "/recreateInstances": {}}, sent),
                    token=lambda: "t")
    g.remove_self()
    g.recycle_self()
    (m1, u1, b1), (m2, u2, b2) = sent
    assert (m1, m2) == ("POST", "POST")
    assert u1.endswith("/zones/us-central1-a/instanceGroupManagers/dev-workers/deleteInstances")
    assert u2.endswith("/zones/us-central1-a/instanceGroupManagers/dev-workers/recreateInstances")
    assert b1["instances"] == b2["instances"] == ["zones/us-central1-a/instances/dev-workers-6p3w"]


def test_a_403_is_a_refusal_not_a_crash():
    g = ir.GceGroup(_IDENT, opener=_opener({"instanceGroupManagers/dev-workers": 403}, []),
                    token=lambda: "t")
    with pytest.raises(ir.Refused):
        g.state()


def test_the_grant_command_is_the_one_the_deploy_prints():
    cmd = _IDENT.grant_command()
    assert "gcloud iam roles create hexeraWorkerSelfRetire --project hexera-dev" in cmd
    assert ("compute.instanceGroupManagers.get,compute.instanceGroupManagers.update,"
            "compute.autoscalers.get") in cmd
    assert ("--member serviceAccount:224734058693-compute@developer.gserviceaccount.com "
            "--role projects/hexera-dev/roles/hexeraWorkerSelfRetire") in cmd
    # `;` so a role left by a half-finished grant does not skip the binding (and PowerShell runs it)
    assert " ; gcloud projects add-iam-policy-binding" in cmd and "&&" not in cmd


def test_a_metadata_hiccup_at_start_is_asked_again_not_given_up(monkeypatch):
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("metadata server slow")
        return _IDENT
    monkeypatch.setattr(ir, "IDENTITY_RETRY_SECONDS", 0.01)
    assert ir._resolve_identity(_flaky, threading.Event()) == _IDENT
    assert calls["n"] == 3
    # ...and a stop while waiting ends it cleanly
    stop = threading.Event()
    stop.set()
    assert ir._resolve_identity(lambda: (_ for _ in ()).throw(OSError("down")), stop) is None


# ---------------------------------------------------------------------------------------------
# who runs it
# ---------------------------------------------------------------------------------------------

def _meta(values):
    def fetch(path):
        if path not in values:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return values[path]
    return fetch


def test_the_identity_comes_from_the_vm_the_group_created():
    ident = ir.Identity.from_metadata(_meta({
        "instance/attributes/created-by": "projects/224734058693/zones/us-central1-a/instanceGroupManagers/dev-workers",
        "instance/attributes/instance-template": "projects/224734058693/global/instanceTemplates/dev-workers-tpl-b2",
        "instance/service-accounts/default/email": "sa@x",
        "project/project-id": "hexera-dev", "instance/name": "dev-workers-w95h"}))
    assert ident == ir.Identity(project="hexera-dev", zone="us-central1-a",
                                instance="dev-workers-w95h", group="dev-workers",
                                template="dev-workers-tpl-b2", service_account="sa@x")


def test_a_vm_no_group_created_and_a_laptop_are_both_off():
    assert ir.Identity.from_metadata(_meta({})) is None

    def _unreachable(path):
        raise urllib.error.URLError("no route to host")
    assert ir.Identity.from_metadata(_unreachable) is None
    # a REGIONAL group is not handled, and says so rather than guessing a zone
    assert ir.Identity.from_metadata(_meta({
        "instance/attributes/created-by": "projects/1/regions/us-central1/instanceGroupManagers/g"})) is None


class _Consumer:
    """The two things of a celery consumer the tap touches."""

    def __init__(self, queues):
        self.task_consumer = type("TC", (), {"consuming_from": lambda s, q: q in queues})()
        self.scheduled: list = []

    def call_soon(self, fn, *args):
        self.scheduled.append((fn.__name__, args))

    def cancel_task_queue(self, queue):
        pass

    def add_task_queue(self, queue):
        pass


def _tap(queues):
    from meshpipeline.runtime.celery_worker import CeleryTap
    return CeleryTap(_Consumer(queues), ir.SIZING_QUEUE)


def test_only_the_worker_on_the_sizing_queue_starts_it():
    started = threading.Event()

    def _ident():
        started.set()
        return None
    assert ir.start_idle_retire(_tap({"geometry_checks"}), minutes=10, identity=_ident) is None
    t = ir.start_idle_retire(_tap({"simulation_jobs"}), minutes=10, identity=_ident)
    assert t is not None
    t.join(5)
    assert started.is_set()


def test_zero_minutes_turns_it_off():
    assert ir.start_idle_retire(_tap({"simulation_jobs"}), minutes=0,
                                identity=lambda: pytest.fail("must not look")) is None


def test_the_celery_tap_counts_every_received_job_and_drains_through_the_event_loop(monkeypatch):
    # celery keeps a received task in reserved_requests from task_reserved until task_ready - the
    # running one included (celery/worker/state.py). The unit tier stubs celery, so the set is ours.
    import sys
    import types
    reserved: set = set()
    monkeypatch.setitem(sys.modules, "celery.worker",
                        types.SimpleNamespace(state=types.SimpleNamespace(reserved_requests=reserved)))
    tap = _tap({"simulation_jobs"})
    assert tap.holding() == 0
    reserved.add("received-not-started")
    assert tap.holding() == 1
    reserved.clear()
    assert tap.holding() == 0
    tap.stop_taking()
    tap.resume_taking()
    assert tap._consumer.scheduled == [("cancel_task_queue", ("simulation_jobs",)),
                                       ("add_task_queue", ("simulation_jobs",))]


def test_the_celery_tap_asks_the_geometry_worker_on_its_own_vm(monkeypatch):
    import socket
    import types

    from meshpipeline.runtime import celery_worker
    asked: list = []

    def _app(replies=None, boom=False):
        def inspect(destination, timeout):
            asked.append(destination)

            def active():
                if boom:
                    raise ConnectionError("broker away")
                return replies
            return types.SimpleNamespace(active=active)
        return types.SimpleNamespace(control=types.SimpleNamespace(inspect=inspect))

    monkeypatch.setattr(socket, "gethostname", lambda: "dev-workers-w95h")
    tap = _tap({"simulation_jobs"})
    monkeypatch.setattr(celery_worker, "celery_app", _app({"geometry@dev-workers-w95h": [{}, {}]}))
    assert tap.neighbour_holding() == 2
    assert asked[-1] == ["geometry@dev-workers-w95h"]
    monkeypatch.setattr(celery_worker, "celery_app", _app({}))
    assert tap.neighbour_holding() is None
    monkeypatch.setattr(celery_worker, "celery_app", _app(boom=True))
    assert tap.neighbour_holding() is None


def test_both_containers_carry_the_vms_hostname_so_the_geometry_worker_can_be_asked():
    from pathlib import Path
    startup = (Path(ir.__file__).parents[3] / "deploy" / "gcp" / "worker" / "startup.sh").read_text(
        encoding="utf-8")
    for name in ("hexera-worker", "hexera-geometry-worker"):
        line = next(ln for ln in startup.splitlines() if ln.startswith(f"docker run -d --name {name} "))
        assert '--hostname "${WORKER_HOSTNAME}"' in line, name
    assert "--hostname geometry@%h" in startup


def test_the_worker_starts_it_when_ready():
    from meshpipeline.runtime import celery_worker
    src = open(celery_worker.__file__, encoding="utf-8").read()
    ready = src[src.index("@worker_ready.connect"):]
    assert "start_idle_retire(CeleryTap(sender" in ready[: ready.index("\n\n\n")]
