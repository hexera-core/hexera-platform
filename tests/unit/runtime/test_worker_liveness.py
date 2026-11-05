# Responsibility: Verify a worker's liveness is judged by its consumer's own event loop - a beat that only a turning loop
#                 writes, a check docker can run on it, presence keys the queue publisher counts - and that the broker
#                 connection itself can never block for ever.
# Boundaries: the liveness module against a temp dir and a fake Redis, the consumer step against a fake consumer, and the
#             celery configuration as data; nothing reaches a broker.
from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

from meshpipeline.runtime import worker_liveness as wl

REPO = Path(__file__).parents[3]
PUBLISHER = REPO / "deploy" / "gcp" / "worker" / "queue_depth_publisher.py"

# THE DEFECT THIS PINS. Shared dev, 2026-10-16 06:58 UTC: Redis reset the geometry worker's broker
# connection, the consumer logged "Connection to broker lost. Trying to re-establish the
# connection..." and then nothing for three days. The process was up, so docker's restart policy
# never fired, and no geometry check ran. A live process is not a working consumer.


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(wl.tempfile, "gettempdir", lambda: str(tmp_path))
    with wl._STATE.lock:
        wl._STATE.at, wl._STATE.node, wl._STATE.queues = None, "", ()
    yield


# ---------------------------------------------------------------------------------------------
# the beat and the verdict
# ---------------------------------------------------------------------------------------------

def test_a_consumer_that_never_beat_is_not_healthy():
    healthy, why = wl.verdict(wl.heartbeat_age(pid=4242))
    assert not healthy and "not beaten yet" in why


def test_a_fresh_beat_is_healthy_and_a_stale_one_is_not(tmp_path):
    wl.beat(node="geometry@vm", queues=["geometry_checks"], pid=7)
    age = wl.heartbeat_age(pid=7)
    assert age is not None and age < 5
    assert wl.verdict(age)[0]
    # the clock moves on and the loop never beats again - the 2026-10-16 worker
    later = wl.heartbeat_age(pid=7, clock=lambda: wl.heartbeat_path(7).stat().st_mtime + 121)
    healthy, why = wl.verdict(later)
    assert not healthy and "event loop is not running" in why


def test_the_beat_names_the_queues_actually_consumed(tmp_path):
    wl.beat(node="celery@vm", queues=["simulation_jobs"], pid=8)
    body = json.loads(wl.heartbeat_path(8).read_text(encoding="utf-8"))
    assert body["node"] == "celery@vm" and body["queues"] == ["simulation_jobs"]


def test_a_restarted_container_does_not_inherit_the_old_runs_beat():
    wl.beat(node="n", queues=[], pid=1)
    wl.clear_heartbeat(pid=1)
    assert wl.heartbeat_age(pid=1) is None
    wl.clear_heartbeat(pid=1)        # nothing to clear is fine


def test_a_beat_that_cannot_write_never_raises_into_the_event_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(wl.tempfile, "gettempdir", lambda: str(tmp_path / "missing" / "dir"))
    wl.beat(node="n", queues=["q"], pid=9)          # must not raise


@pytest.mark.parametrize("beat,code", [(True, 0), (False, 1)])
def test_the_check_command_is_what_docker_runs(beat, code, capsys):
    if beat:
        wl.beat(node="n", queues=["q"], pid=1)
    assert wl.main(["check"]) == code           # PID 1 by default: the container's main process
    assert capsys.readouterr().out.strip()     # the reason lands in docker's health log


def test_the_staleness_limit_is_far_past_a_beat_and_inside_minutes():
    assert wl.STALE_AFTER_SECONDS >= 6 * wl.BEAT_SECONDS
    assert wl.STALE_AFTER_SECONDS <= 300


# ---------------------------------------------------------------------------------------------
# presence
# ---------------------------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self):
        self.sets: dict[str, tuple[str, int]] = {}

    def set(self, key, value, ex=None):
        self.sets[key] = (value, ex)


def test_presence_is_one_expiring_key_per_queue_while_the_beat_is_fresh():
    r = _FakeRedis()
    wl.beat(node="geometry@vm", queues=["geometry_checks"], pid=3, clock=lambda: 1000.0)
    assert wl.publish_presence(r, prefix="dev-a:", clock=lambda: 1010.0) == 1
    (key,) = r.sets
    assert key == "dev-a:fleet:consumers:geometry_checks:geometry@vm"
    assert r.sets[key][1] == wl.PRESENCE_TTL_SECONDS


def test_a_stale_beat_publishes_no_presence_so_the_keys_lapse_by_themselves():
    r = _FakeRedis()
    wl.beat(node="geometry@vm", queues=["geometry_checks"], pid=3, clock=lambda: 1000.0)
    assert wl.publish_presence(r, prefix="", clock=lambda: 1000.0 + wl.STALE_AFTER_SECONDS + 1) == 0
    assert r.sets == {}


def test_a_consumer_that_paused_its_queue_reports_no_presence_for_it():
    # an idle worker draining before it retires has cancelled its queue on purpose
    r = _FakeRedis()
    wl.beat(node="celery@vm", queues=[], pid=3, clock=lambda: 1000.0)
    assert wl.publish_presence(r, prefix="", clock=lambda: 1001.0) == 0


def test_the_publisher_counts_exactly_the_keys_the_worker_writes(monkeypatch):
    import fnmatch
    spec = importlib.util.spec_from_file_location("qdp_liveness", PUBLISHER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.PRESENCE_KEY_TEMPLATE == wl.PRESENCE_KEY_TEMPLATE
    for prefix in ("", "dev-areen:"):
        key = wl.presence_key(prefix, "geometry_checks", "geometry@dev-workers-tjj1")
        pattern = mod.presence_pattern(prefix, "geometry_checks").replace("\\", "")
        assert fnmatch.fnmatchcase(key, pattern)
        # another queue's consumer is not this queue's
        assert not fnmatch.fnmatchcase(wl.presence_key(prefix, "simulation_jobs", "celery@x"), pattern)


# ---------------------------------------------------------------------------------------------
# the consumer step: the beat comes from the consumer's own loop
# ---------------------------------------------------------------------------------------------

class _Queue:
    def __init__(self, name):
        self.name = name


class _TaskConsumer:
    def __init__(self, active):
        self.queues = [_Queue("simulation_jobs"), _Queue("geometry_checks")]
        self._active = set(active)

    def consuming_from(self, q):
        return q.name in self._active


class _Tref:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Timer:
    def __init__(self):
        self.calls = []

    def call_repeatedly(self, secs, fun, args=()):
        self.calls.append((secs, fun, args))
        return _Tref()


class _Consumer:
    def __init__(self, active):
        self.task_consumer = _TaskConsumer(active)
        self.timer = _Timer()
        self.hostname = "celery@vm"


def test_the_step_beats_on_the_consumers_own_timer_and_unhooks_on_restart(monkeypatch):
    from meshpipeline.runtime import celery_worker as cw
    beats = []
    monkeypatch.setattr(wl, "beat", lambda **kw: beats.append(kw))
    c = _Consumer(active=["simulation_jobs"])
    step = cw.ConsumerHeartbeat(c)
    step.start(c)
    # once at once, then on the event loop's timer - never a thread that would beat for a dead loop
    assert beats == [{"node": "celery@vm", "queues": ["simulation_jobs"]}]
    ((secs, fun, args),) = c.timer.calls
    assert secs == wl.BEAT_SECONDS
    fun(*args)
    assert len(beats) == 2
    tref = step._tref
    step.stop(c)
    assert tref.cancelled and step._tref is None


def test_the_step_is_part_of_every_consumer():
    from meshpipeline.runtime import celery_worker as cw
    assert cw.ConsumerHeartbeat in cw.celery_app.steps["consumer"]


# ---------------------------------------------------------------------------------------------
# the broker connection has a bound on every wait
# ---------------------------------------------------------------------------------------------

def test_every_broker_socket_has_a_timeout_keepalive_and_a_health_check():
    from meshpipeline.adapters.pipeline_execution import celery_app as ca
    opts = ca.celery_app.conf.broker_transport_options
    assert opts["socket_timeout"] == ca.BROKER_SOCKET_TIMEOUT_SECONDS
    assert opts["socket_connect_timeout"] == ca.BROKER_CONNECT_TIMEOUT_SECONDS
    assert opts["socket_keepalive"] is True
    assert opts["health_check_interval"] == ca.BROKER_HEALTH_CHECK_SECONDS
    assert opts["retry_on_timeout"] is True
    # kombu's redis transport waits 1 s on BRPOP; a read timeout must be far past any healthy wait
    assert ca.BROKER_SOCKET_TIMEOUT_SECONDS >= 10
    # the keyspace and the visibility window are still there
    assert "global_keyprefix" in opts and opts["visibility_timeout"] == ca.BROKER_VISIBILITY_TIMEOUT_SECONDS
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert opts["socket_keepalive_options"][socket.TCP_KEEPIDLE] == 60


def test_the_worker_retries_at_startup_and_gives_up_after_a_bounded_run():
    from meshpipeline.adapters.pipeline_execution import celery_app as ca
    conf = ca.celery_app.conf
    assert conf.broker_connection_retry is True
    assert conf.broker_connection_retry_on_startup is True
    assert 0 < conf.broker_connection_max_retries <= 50
    assert conf.redis_socket_connect_timeout == ca.BROKER_CONNECT_TIMEOUT_SECONDS
    assert conf.redis_socket_keepalive is True
    assert conf.redis_backend_health_check_interval == ca.BROKER_HEALTH_CHECK_SECONDS
