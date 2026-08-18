# Responsibility: Carry typed job events to browsers, with a replayable backlog.
# Owns: publication, subscription, backlog trimming, and the fencing that stops a superseded generation from publishing.
# Boundaries: transport for a vocabulary it does not define; events/ owns the vocabulary and the API owns the WebSocket.
# Collaborates with: events/channels.py and application/execution_fence.py.
from __future__ import annotations

import json
import logging
import time
from typing import Any

import meshpipeline.events as E
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.adapters._shared.redis_client import async_client, sync_client
from meshpipeline.contracts.event_stream import StaleExecutionPublish, expected_fence
from meshpipeline.events.channels import (
    channel_for,
    fence_key_for,
    log_key_for,
    opkey_set_for,
    seq_key_for,
)

logger = logging.getLogger(__name__)

_WARN_INTERVAL = 60.0

# seq, append, expire, publish - atomically. A non-atomic version can interleave two
# writers and put the log out of order with respect to seq, which silently corrupts
# every replay for that job from that point on.
# NOTE the seq is APPENDED TO THE BYTES Python already produced - we do NOT decode and
# re-encode through cjson. Lua's cjson cannot tell an empty array from an empty object,
# so a round trip turned `"actions":[]` into `"actions":{}` - a corrupt event that the
# renderer then dropped on the floor. The payload is a JSON object, so it ends in `}`;
# we splice the seq in before that brace and the rest of the bytes are untouched.
# ARGV[4] is an OPTIONAL logical operation key. When present, the first emission claims it and
# any later emission carrying the same key is suppressed and returns -1 - so a node that
# published, crashed before its checkpoint, and replayed does not publish the same logical event
# twice. The claim, the sequence increment and the append are one atomic script: a caller can
# never claim the key and then fail to append.
# Callers that do NOT pass a key keep at-least-once progress semantics deliberately. Two
# identical progress notes in one run are legitimate, and blanket-hashing every note would
# silently swallow the second.
_EMIT_LUA = """
if ARGV[5] ~= '' and redis.call('GET', KEYS[5]) ~= ARGV[5] then
  return -2
end
if ARGV[4] ~= '' then
  if redis.call('SADD', KEYS[4], ARGV[4]) == 0 then
    return -1
  end
  redis.call('EXPIRE', KEYS[4], tonumber(ARGV[2]))
end
local seq  = redis.call('INCR', KEYS[1])
local body = string.sub(ARGV[1], 1, -2)
local out
if body == '{' then
  out = '{"seq":' .. seq .. '}'
else
  out = body .. ',"seq":' .. seq .. '}'
end
redis.call('RPUSH', KEYS[2], out)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
if ARGV[3] == '1' then
  redis.call('PUBLISH', KEYS[3], out)
end
return seq
"""








def _durable_wire(event: E.UiEvent) -> dict:
    w = event.wire()
    if w.get("type") == E.SCREENSHOT:
        w.pop("image", None)
    return w


def _current_generation() -> int:
    try:
        from meshpipeline.application.execution_fence import current_ownership
        own = current_ownership()
        return int(getattr(own, "execution_generation", 0) or 0)
    except Exception:  # noqa: BLE001 - identity scoping must never break publication
        return 0


class JobPublisher:

    def __init__(self, job_id: str, agent: str = "") -> None:
        self.job_id = job_id
        self.agent  = agent
        self._channel = channel_for(job_id)
        self._redis = None
        self._last_warn_ts: float = 0.0

    def _get_redis(self):
        if self._redis is None:
            self._redis = sync_client()
        return self._redis

 # the one write
    def _emit_once(self, event: E.UiEvent, op_key: str = "") -> None:
        r = self._get_redis()
        live = event.wire()
        keep = _durable_wire(event)
        same = keep == live
        # THE expectation, set only by the ownership gate. Empty for intake, terminal and
        # maintenance publication, which run with no claim to check against.
        seq = r.eval(_EMIT_LUA, 5,
                     seq_key_for(self.job_id), log_key_for(self.job_id), self._channel,
                     opkey_set_for(self.job_id), fence_key_for(self.job_id),
                     json.dumps(keep), str(rtcfg.EVENT_LOG_TTL_SECONDS),
                     "1" if same else "0", op_key or "", expected_fence())
        if seq == -2:
            # The claim this write was authorized under is no longer the one Redis holds. It is
            # refused BEFORE replay detection, so a stale retry cannot pass as an idempotent one.
            raise StaleExecutionPublish(
                f"the execution claim that authorized this event no longer holds job "
                f"{self.job_id} - refusing to publish")
        if seq == -1:
            # an earlier attempt at this same logical event already reached the log
            return
        if not same:
            # a screenshot: the LIGHT version is logged, the full one goes live. The
            # seq comes from the same atomic INCR, so the client's cursor still lines
            # up with the log.
            live["seq"] = seq
            r.publish(self._channel, json.dumps(live))

    def emit(self, event: E.UiEvent) -> None:
        try:
            self._emit_once(event)
        except StaleExecutionPublish:
            raise
        except Exception as exc:  # noqa: BLE001 - the user stream is best-effort; a
            now = time.monotonic()  # Redis blip must never fail a meshing job
            if now - self._last_warn_ts >= _WARN_INTERVAL:
                self._last_warn_ts = now
                logger.warning("JobPublisher: publish failed job_id=%s stage=%r - %s",
                               self.job_id, self.agent, exc)
            self._redis = None

    def publish_terminal(self, text: str, event_id: str = "") -> None:
        try:
            self._emit_once(E.closing(text, event_id), op_key=event_id)
        except Exception:
            self._redis = None       # drop the poisoned client so the next attempt reconnects
            raise

 # the vocabulary a stage may speak
    # Every one of these reaches the replayable backlog, so every one of them can be replayed at
    # the user after a takeover. `op_id` is the PRODUCER's name for which occurrence this is -
    # the publisher never guesses it from the text, because two notes with the same words can be
    # genuine repeated progress and only the caller knows which.
    def stage(self, op_id: str = "") -> None:
        self._emit_keyed(E.stage(self.agent), op_id)

    def attempt(self, n: int, of: int, op_id: str = "") -> None:
        # the attempt number IS the occurrence, so it is a sensible default identity
        self._emit_keyed(E.attempt(self.agent, n, of), op_id or f"attempt:{n}")

    def note(self, text: str, tone: str = "info", op_id: str = "") -> None:
        self._emit_keyed(E.note(self.agent, text, tone), op_id)

    def warn(self, text: str, op_id: str = "") -> None:
        self._emit_keyed(E.note(self.agent, text, "warn"), op_id)

    def error(self, text: str, op_id: str = "") -> None:
        self._emit_keyed(E.note(self.agent, text, "error"), op_id)

    def _emit_keyed(self, event, op_id: str) -> None:
        if not op_id:
            self.emit(event)
            return
        key = f"{self.agent}:{event.type}:{op_id}:g{_current_generation()}"
        self._emit_guarded(event, op_key=key)

    def check(self, statement: str, *, ok: bool) -> None:
        self.emit(E.check(self.agent, statement, ok=ok))

    def action(self, actions: list[str]) -> None:
        self.emit(E.action(self.agent, actions))

    def search(self, query: str) -> None:
        self.emit(E.search(self.agent, query))

    def reasoning(self, rid: str, phase: str, *, duration_ms: int | None = None,
                  token_count: int | None = None, content: str | None = None,
                  status: str = "active") -> None:
        self.emit(E.reasoning(self.agent, rid, self.agent, phase,
                              duration_ms=duration_ms, token_count=token_count,
                              content=content, status=status))

    def rationale(self, conclusion: str, because: str = "") -> None:
        self.emit(E.rationale(self.agent, self.agent, conclusion, because))

    def tool_call(self, cid: str, tool_name: str, arguments: object = None,
                  status: str = "started", op_id: str = "") -> None:
        self._emit_keyed(E.tool_call(self.agent, cid, self.agent, tool_name, arguments, status),
                         op_id)

    def tool_result(self, rid: str, call_id: str, tool_name: str, result: object = None,
                    status: str = "success", duration_ms: int | None = None,
                    op_id: str = "") -> None:
        self._emit_keyed(E.tool_result(self.agent, rid, call_id, self.agent, tool_name,
                                       result, status, duration_ms), op_id)

    def file(self, display_path: str, byte_count: int, operation: str = "created",
             op_id: str = "") -> None:
        self._emit_keyed(E.file(self.agent, display_path, byte_count, operation), op_id)

    def screenshot(self, image_b64: str, op_id: str = "") -> None:
        self._emit_keyed(E.screenshot(self.agent, image_b64), op_id)

    def meshing(self, engine: str, budget_s: int, history: dict | None = None,
                op_id: str = "") -> None:
        self._emit_keyed(E.meshing(self.agent, engine, budget_s, history), op_id)

    def meshed(self, cells: int | None = None, op_id: str = "") -> None:
        self._emit_keyed(E.meshed(self.agent, cells), op_id)

    def verdict(self, verdict: str, summary: str = "") -> None:
        self.emit(E.verdict(self.agent, verdict, summary))

    def closing(self, text: str, event_id: str = "") -> None:
        if event_id:
            self._emit_guarded(E.closing(text, event_id), op_key=event_id)
        else:
            self.emit(E.closing(text))

    def _emit_guarded(self, event, *, op_key: str) -> None:
        try:
            self._emit_once(event, op_key=op_key)
        except StaleExecutionPublish:
            # Not a stream hiccup: the fence refused this write because the claim that
            # authorized it is gone. The caller must learn that, not read a warning.
            raise
        except Exception as exc:  # noqa: BLE001 - the user stream is never load-bearing
            logger.warning("JobPublisher: keyed publish failed job_id=%s - %s", self.job_id, exc)
            self._redis = None


# the read side
# One asyncio client for the whole API process: a client per socket would burn a
# connection per viewer on a stream that is mostly idle.
# Cached PER EVENT LOOP, not merely per process. A redis.asyncio pool hands back connections whose
# futures belong to the loop that opened them, so reusing this client under a second loop raises
# "attached to a different loop" the moment a pooled connection survives the first one. The API
# serves everything on a single uvicorn loop and never notices, but a process that calls
# asyncio.run() more than once does - and several do (`run_from_job` validates on one loop then
# executes on another; a worker handles job after job). `pipeline_run` already records this hazard
# for the process-cached SQLAlchemy engine; the same reasoning applies here, and here it is
# enforced rather than left to discipline.
_async_redis = None
_async_redis_loop = None


def _get_async_redis():
    import asyncio

    global _async_redis, _async_redis_loop

    loop = asyncio.get_running_loop()
    if _async_redis is None or _async_redis_loop is not loop:
        # The previous client's sockets died with its loop; awaiting aclose() here is impossible
        # (that loop is gone) and pointless (the fds went with it). Drop the reference instead.
        _async_redis = async_client(decode_responses=True)
        _async_redis_loop = loop
    return _async_redis


class RedisEventSubscription:

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._channel = channel_for(job_id)
        self._pubsub: Any = None

    async def open(self) -> None:
        # SUBSCRIBE FIRST. backlog() runs after this, so an event published between the
        # two is delivered live rather than falling into the gap between them.
        self._pubsub = _get_async_redis().pubsub()
        await self._pubsub.subscribe(self._channel)

    async def backlog(self) -> list[str]:
        raw = await _get_async_redis().lrange(log_key_for(self.job_id), 0, -1)
        return list(raw or [])

    async def next_event(self, timeout: float) -> str | None:
        if self._pubsub is None:
            return None
        message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        if message is None or message.get("type") != "message":
            return None          # timeout, or pub/sub bookkeeping - nothing to deliver
        return message["data"]

    async def close(self) -> None:
        if self._pubsub is None:
            return
        try:
            await self._pubsub.unsubscribe(self._channel)
            await self._pubsub.aclose()
        except Exception:        # noqa: BLE001 - teardown must never raise at the socket
            pass
        finally:
            self._pubsub = None
