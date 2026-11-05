# Responsibility: Verify every event is logged under a monotonic sequence that expires, screenshots excepted.
from __future__ import annotations

import json
from pathlib import Path

import pytest

import meshpipeline.settings.runtime as rtcfg

ROOT = Path(__file__).parent.parent.parent.parent
APP = ROOT / "src" / "meshpipeline"

from meshpipeline.adapters.event_stream.redis import JobPublisher, log_key_for, seq_key_for  # noqa: E402


class FakeRedis:

    def __init__(self):
        self.kv: dict = {}
        self.lists: dict = {}
        self.published: list = []
        self.expires: dict = {}

    def eval(self, script, nkeys, *args):
        # FIVE keys: the fourth holds the operation keys already published, so a replay is
        # suppressed atomically alongside the append; the fifth is the execution fence, checked
        # before anything is touched so a stale claim mutates nothing at all.
        seq_key, log_key, chan, ops_key, fence_key = args[:5]
        payload, ttl, do_publish = args[5], args[6], args[7]
        op_key = args[8] if len(args) > 8 else ""
        expected = args[9] if len(args) > 9 else ""
        if expected and self.kv.get(fence_key) != expected:
            return -2                         # the claim that authorized this write is gone
        if op_key:
            claimed = self.sets.setdefault(ops_key, set())
            if op_key in claimed:
                return -1                     # already published by an earlier attempt
            claimed.add(op_key)
            self.expires[ops_key] = int(ttl)
        self.kv[seq_key] = int(self.kv.get(seq_key, 0)) + 1
        seq = self.kv[seq_key]
        # the SAME byte-splice the Lua does - no JSON round trip (see test below)
        body = payload[:-1]
        out = f'{{"seq":{seq}}}' if body == "{" else f'{body},"seq":{seq}}}'
        self.lists.setdefault(log_key, []).append(out)
        self.expires[log_key] = int(ttl)
        if do_publish == "1":
            self.published.append((chan, out))
        return seq

    def publish(self, chan, payload):
        self.published.append((chan, payload))

    def lrange(self, key, a, b):
        return self.lists.get(key, [])


def _pub(job="j1", stage="builder"):
    p = JobPublisher(job, stage)
    p._redis = FakeRedis()
    return p


# the producer emits a durable, ordered, seq'd stream
def test_every_event_is_persisted_under_a_monotonic_seq():
    p = _pub()
    p.note("Designing the mesh")
    p.check("The mesh is structurally sound", ok=True)
    p.meshed(23938)

    log = [json.loads(x) for x in p._redis.lists[log_key_for("j1")]]
    assert [e["seq"] for e in log] == [1, 2, 3]
    assert [e["type"] for e in log] == ["note", "check", "meshed"]
    # the SAME seq goes out live - a client's cursor must mean the same thing on both
    live = [json.loads(x) for _, x in p._redis.published]
    assert [e["seq"] for e in live] == [1, 2, 3]
    assert int(p._redis.kv[seq_key_for("j1")]) == 3


def test_the_log_expires_so_redis_does_not_grow_without_bound():
    p = _pub()
    p.note("hello")
    assert p._redis.expires[log_key_for("j1")] == rtcfg.EVENT_LOG_TTL_SECONDS
    assert rtcfg.EVENT_LOG_TTL_SECONDS >= 6 * 3600, (
        "the event log must outlive a long mesh run, or a reload loses the timeline")


def test_screenshot_bytes_are_streamed_live_but_not_persisted():
    p = _pub("j1", "reviewer")
    p.screenshot("A" * 5000)

    stored = json.loads(p._redis.lists[log_key_for("j1")][0])
    assert stored["type"] == "screenshot" and "image" not in stored
    live = json.loads(p._redis.published[0][1])
    assert live["image"] == "A" * 5000
    assert live["seq"] == stored["seq"], "live and stored disagree on seq - cursor breaks"


def test_an_empty_list_survives_the_log_as_a_list_not_an_object():
    p = _pub()
    p.action([])                       # the shape that broke
    p.meshed(None)                     # and a null, while we are here

    log = [json.loads(x) for x in p._redis.lists[log_key_for("j1")]]
    assert log[0]["actions"] == [], f"empty list corrupted into {log[0]['actions']!r}"
    assert isinstance(log[0]["actions"], list)
    assert log[1]["cells"] is None
    assert log[0]["seq"] == 1 and log[1]["seq"] == 2


# structural guards on the atomic Lua (behaviourally owned by the integration tier)
def test_one_emit_is_one_atomic_script_that_never_re_encodes_the_event():
    from meshpipeline.adapters.event_stream.redis import _EMIT_LUA as lua
    for op in ("INCR", "RPUSH", "PUBLISH", "EXPIRE"):
        assert op in lua, f"{op} is not inside the atomic script"
    assert "cjson" not in lua, (
        "the Lua decodes the event again - cjson will corrupt empty arrays")
    assert "string.sub" in lua, "the seq is not spliced into the original bytes"
    assert "string.sub" in lua, "the seq is not spliced into the original bytes"


# the server hands over before the platform can sever the request
def test_the_ws_session_cap_sits_below_the_platform_request_cap():
    assert rtcfg.WS_MAX_SESSION_SECONDS < 3600, (
        "the WS session cap must sit BELOW the platform's 60-minute request cap")


@pytest.fixture(autouse=True)
def _execution_fence(monkeypatch):
    # This suite claims delivery with no Redis. The mirror is stood in for at the two seams
    # production resolves; ownership checks, claim transitions and the transaction stay real.
    from tests.execution_fence_double import install
    return install(monkeypatch)
