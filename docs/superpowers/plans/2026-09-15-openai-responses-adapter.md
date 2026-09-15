# OpenAI Responses adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `openai` a callable provider by turning "which wire protocol does this provider speak" into a declared seam, then implementing the Responses protocol behind it.

**Architecture:** A `protocols/` package holds one adapter per wire format. Each owns the whole span from `Conversation` to `ModelRoundResult`: request building, the SDK call, streaming consumption, and normalisation. `router.py` keeps only the five role entry points. `routing.execute()` — retry, backoff, circuit breaking, failover, telemetry — is untouched and protocol-independent.

**Tech Stack:** Python 3.11, `openai` SDK (`client.responses.*`), pytest.

**Spec:** [`docs/superpowers/specs/2026-09-15-openai-responses-adapter-design.md`](../specs/2026-09-15-openai-responses-adapter-design.md) — every wire fact in §1 was probed live against the production key; do not re-derive them from documentation.

## Global Constraints

- **No default may move.** `ROUTE_MATRIX` (`settings/inventory.py:128`) and role settings are not edited by this plan. Pointing a role at `openai` is a separate commit, out of scope here.
- **DeepInfra and DeepSeek behaviour must not change, byte for byte.** `tests/unit/infra/test_invocation_equivalence.py` and `tests/unit/infra/test_call_kwargs_golden_routes.py` pin the current wire shape for all five roles. They must pass unedited at every commit. Any diff in what those providers receive is a bug, never an improvement.
- **`contracts/model_inference.py` is not renegotiated.** `Conversation` in, `ModelRoundResult` out. Its line 48 promise holds: no SDK object, provider response or stream machinery crosses that boundary.
- **The protocol is chosen per TARGET, not per route.** Failover can land an attempt on a different provider than the primary. A protocol resolved once per route would send Responses-shaped requests to DeepInfra on the second attempt.
- **`zero means not reported`** for `ModelRoundResult.reasoning_tokens`. Never substitute `output_tokens`.
- **Run tests with:** `/tmp/hexvenv/bin/python -m pytest <paths> -q -p no:cacheprovider`. No `.venv` in this worktree; the package is installed editable. `vtk`, `gmsh`, `python-multipart` and `langgraph` are absent — some suites have pre-existing failures that are not yours.
- **Never `git stash`** — the stash stack is shared across worktrees on this machine. Use `git worktree add /tmp/<name> <sha>` to compare against another revision, then remove it.

---

### Task 1: The protocol seam

**Files:**
- Create: `src/meshpipeline/adapters/model_inference/protocols/__init__.py`
- Create: `src/meshpipeline/adapters/model_inference/protocols/chat_completions.py`
- Modify: `src/meshpipeline/adapters/model_inference/router.py:74-86` (`_streamed`, `_chat`), `:88-116` (`_normalize`), `:45-64` (`_usage_from`)
- Test: `tests/unit/infra/test_protocol_seam.py`

This task introduces the interface and moves today's code behind it **with no behaviour change**. The Responses adapter is Task 3 onward.

**Interfaces:**
- Produces:
  - `protocols.WireProtocol` — a `typing.Protocol` with `async invoke(...) -> tuple[Any, Usage]` and `normalize(response, target, attempts) -> ModelRoundResult`
  - `protocols.protocol_for(provider: str) -> WireProtocol` — raises `KeyError` naming the registered protocols for an unknown provider
  - `protocols.chat_completions.ChatCompletions` — registered for `deepinfra` and `deepseek`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/infra/test_protocol_seam.py
# Responsibility: Verify each provider resolves to a wire protocol, and an unknown one is refused.
# Boundaries: the registry and its contract; the protocols' own behaviour is tested by their suites.
from __future__ import annotations

import inspect

import pytest

from meshpipeline.adapters.model_inference import protocols


@pytest.mark.parametrize("provider", ["deepinfra", "deepseek"])
def test_a_wire_provider_resolves_to_the_chat_completions_protocol(provider):
    proto = protocols.protocol_for(provider)
    assert proto.__class__.__name__ == "ChatCompletions"


def test_an_unregistered_provider_is_refused_and_names_what_exists():
    with pytest.raises(KeyError, match="cohere"):
        protocols.protocol_for("cohere")


def test_every_protocol_satisfies_the_interface():
    # A protocol missing normalize() would fail at the end of a real call, after the
    # provider has already been paid for the tokens.
    for provider in protocols.registered():
        proto = protocols.protocol_for(provider)
        assert inspect.iscoroutinefunction(proto.invoke), provider
        assert callable(proto.normalize), provider
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `/tmp/hexvenv/bin/python -m pytest tests/unit/infra/test_protocol_seam.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'meshpipeline.adapters.model_inference.protocols'`.

- [ ] **Step 3: Write the registry**

```python
# src/meshpipeline/adapters/model_inference/protocols/__init__.py
# Responsibility: Say which WIRE PROTOCOL a provider speaks, and hand back the adapter that speaks it.
# Boundaries: a registry and its contract; no request is built and no call is made here.
# Collaborates with: router.py, which resolves a protocol per ATTEMPT TARGET.

# WHY THIS EXISTS. Until now a provider was a client plus a parameter set, because both configured
# providers spoke the OpenAI chat-completions wire format - so the format itself could be assumed
# in router.py, streaming.py and messages.py without anyone noticing it was an assumption. OpenAI's
# Responses API is a different protocol on every axis the product uses (see the design doc's §1),
# and Anthropic is a third. Branching on provider inside each of those seven sites would multiply
# them; declaring the protocol once, here, does not.
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from meshpipeline.adapters.model_inference.call_kwargs import SamplingSpec
from meshpipeline.adapters.model_inference.routing import Usage
from meshpipeline.contracts.model_inference import (
    Conversation,
    ModelRoundResult,
    ParallelToolCalls,
    ReasoningSink,
    ToolChoice,
    ToolDefinition,
)
from meshpipeline.contracts.model_routing import RouteTarget


@runtime_checkable
class WireProtocol(Protocol):

    async def invoke(
        self, target: RouteTarget, conversation: Conversation, *,
        tools: list[ToolDefinition] | None, tool_choice: ToolChoice,
        spec: SamplingSpec, parallel_tool_calls: ParallelToolCalls,
        on_reasoning: ReasoningSink, label: str, trace: dict,
    ) -> tuple[Any, Usage]:
        """Make ONE attempt against one target and return what routing.execute expects.

        The returned object is this protocol's own; it is opaque to everything above and is
        handed straight back to this protocol's normalize()."""

    def normalize(self, response: Any, target: RouteTarget, attempts: int) -> ModelRoundResult:
        """Turn this protocol's response into the one result type every role shares."""


def _registry() -> dict[str, WireProtocol]:
    # Imported lazily: the protocol modules import the SDK-facing helpers, and this module is
    # imported by router.py at call time.
    from meshpipeline.adapters.model_inference.protocols.chat_completions import ChatCompletions
    chat = ChatCompletions()
    return {"deepinfra": chat, "deepseek": chat}


_CACHE: dict[str, WireProtocol] | None = None


def registered() -> list[str]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _registry()
    return sorted(_CACHE)


def protocol_for(provider: str) -> WireProtocol:
    global _CACHE
    if _CACHE is None:
        _CACHE = _registry()
    try:
        return _CACHE[provider]
    except KeyError:
        raise KeyError(
            f"no wire protocol registered for provider {provider!r}; providers that have one: "
            f"{sorted(_CACHE)}") from None
```

- [ ] **Step 4: Move the chat-completions code behind the interface**

Create `protocols/chat_completions.py`. Move `_streamed`, `_chat`, `_normalize` and `_usage_from` out of `router.py` **verbatim** — same bodies, same comments — as methods/helpers of `ChatCompletions`. `invoke` chooses streaming or not from `spec.stream`, and does what `_run_glm_stream` / `_run_chat` did around the call: build kwargs via `kwargs_for(target, spec)`, apply tool kwargs, call, and raise `_EmptyResponse` when `choices` is empty.

Keep `_EmptyResponse` importable from where `router._classify` already reads it, or move it beside the protocol and update that import — but do not change what it classifies to (`FailureCategory.EMPTY_RESPONSE`).

- [ ] **Step 5: Point `router.py` at the registry**

Each role helper resolves the protocol **inside** `_invoke`, from the attempt's target:

```python
    async def _invoke(target: RouteTarget):
        proto = protocol_for(target.provider)
        return await proto.invoke(
            target, messages, tools=tools, tool_choice=tool_choice,
            spec=spec_for(route.role), parallel_tool_calls=parallel_tool_calls,
            on_reasoning=on_reasoning, label=label,
            trace=langfuse_kwargs(session_id=job_id, user_id=user_id, name=label))
```

and `_run` normalises through the target `execute()` actually used:

```python
    response, target, attempts = await execute(route, _invoke, _classify, job_id=job_id,
                                               cost_of=_cost_of)
    return protocol_for(target.provider).normalize(response, target, attempts)
```

Resolving inside `_invoke` rather than once per route is load-bearing: a standby on a different provider would otherwise be sent the primary protocol's request.

- [ ] **Step 6: Prove the move changed nothing**

Run:
```
/tmp/hexvenv/bin/python -m pytest tests/unit/infra/test_protocol_seam.py \
  tests/unit/infra/test_invocation_equivalence.py \
  tests/unit/infra/test_call_kwargs_golden_routes.py \
  tests/unit/infra/test_call_kwargs_per_provider.py \
  tests/unit/infra/test_router_uses_per_target_kwargs.py \
  tests/unit/infra/test_provider_error_hardening.py \
  tests/unit/platform/test_model_routing.py \
  tests/unit/infra/test_route_timing_equivalence.py \
  tests/unit/infra/test_failure_marker_equivalence.py \
  tests/unit/engines/test_planner_contract.py -q -p no:cacheprovider
```
Expected: PASS. `test_invocation_equivalence.py` is the one that matters — it pins the exact wire shape for all five roles through the real `router.call_*` path. If it fails, the move was not a move.

`test_router_uses_per_target_kwargs.py` asserts `router.py`'s source no longer names the old dicts; if it also asserts helpers this task moved, update the assertion to follow them rather than deleting it.

- [ ] **Step 7: Commit**

```bash
git add src/meshpipeline/adapters/model_inference/protocols/ \
        src/meshpipeline/adapters/model_inference/router.py \
        tests/unit/infra/test_protocol_seam.py
git commit -m "Declare the wire protocol a provider speaks"
```

---

### Task 2: Responses request building

**Files:**
- Create: `src/meshpipeline/adapters/model_inference/protocols/responses_request.py`
- Test: `tests/unit/infra/test_responses_request.py`

Pure translation, no network. Splitting it from the call makes it testable against recorded shapes.

**Interfaces:**
- Consumes: `SamplingSpec`, `RouteTarget`.
- Produces:
  - `build_request(target, conversation, *, tools, tool_choice, spec, parallel_tool_calls) -> dict`
  - `to_input_items(conversation: Conversation) -> tuple[str, list[dict]]` returning `(instructions, input_items)`
  - `to_responses_tools(tools: list[ToolDefinition]) -> list[dict]`

- [ ] **Step 1: Write the failing test**

Every expected shape below is from the design doc §1, probed live — do not "correct" them.

```python
# tests/unit/infra/test_responses_request.py
# Responsibility: Verify a neutral Conversation becomes the exact request the Responses API accepts.
# Boundaries: shape only; no network and no SDK.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.adapters.model_inference.protocols import responses_request as rr
from meshpipeline.contracts.model_routing import RouteTarget

TARGET = RouteTarget(provider="openai", model="gpt-5.6-luna", account="default",
                     circuit_group="openai")


def test_a_system_turn_becomes_top_level_instructions():
    instructions, items = rr.to_input_items([
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ])
    assert instructions == "be brief"
    assert items == [{"role": "user", "content": "hi"}]


def test_several_system_turns_are_joined_not_dropped():
    instructions, items = rr.to_input_items([
        {"role": "system", "content": "one"},
        {"role": "system", "content": "two"},
        {"role": "user", "content": "hi"},
    ])
    assert "one" in instructions and "two" in instructions
    assert items == [{"role": "user", "content": "hi"}]


def test_an_assistant_tool_call_becomes_a_function_call_item():
    _instr, items = rr.to_input_items([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "get_x", "arguments": '{"a":"1"}'}}]},
    ])
    assert items[-1] == {"type": "function_call", "call_id": "call_1",
                         "name": "get_x", "arguments": '{"a":"1"}'}


def test_a_tool_result_becomes_a_function_call_output_item():
    _instr, items = rr.to_input_items([
        {"role": "tool", "tool_call_id": "call_1", "name": "get_x", "content": "42"},
    ])
    assert items == [{"type": "function_call_output", "call_id": "call_1", "output": "42"}]


def test_a_multimodal_turn_uses_input_text_and_a_FLAT_input_image():
    # Probed 2026-09-15: the chat shape {"type":"image_url","image_url":{"url":...}} is refused
    # by name - "Supported values are: 'input_text', 'input_image'".
    _instr, items = rr.to_input_items([
        {"role": "user", "content": [
            {"type": "text", "text": "colour?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]},
    ])
    assert items[0]["content"] == [
        {"type": "input_text", "text": "colour?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
    ]


def test_tool_definitions_are_flattened_from_the_chat_shape():
    out = rr.to_responses_tools([
        {"type": "function", "function": {"name": "get_x", "description": "d",
                                          "parameters": {"type": "object", "properties": {}}}}
    ])
    assert out == [{"type": "function", "name": "get_x", "description": "d",
                    "parameters": {"type": "object", "properties": {}}}]


def test_the_request_uses_max_output_tokens_and_no_rejected_sampling():
    req = rr.build_request(TARGET, [{"role": "user", "content": "hi"}],
                           tools=None, tool_choice="auto",
                           spec=ck.spec_for("intake"), parallel_tool_calls=None)
    assert req["model"] == "gpt-5.6-luna"
    assert req["max_output_tokens"] == ck.spec_for("intake").max_tokens
    for rejected in ("max_tokens", "temperature", "top_p", "presence_penalty",
                     "min_p", "top_k", "extra_body"):
        assert rejected not in req, f"{rejected} is a 400 on this API"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `/tmp/hexvenv/bin/python -m pytest tests/unit/infra/test_responses_request.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: … protocols.responses_request`.

- [ ] **Step 3: Implement the translation**

Write `responses_request.py` with a `# Responsibility:` / `# Boundaries:` header. Points to get right:

- **System turns** leave `input` entirely and become `instructions`. Join multiples with `"\n\n"` — dropping any is silent prompt loss.
- **Content parts** rename *and* flatten: `text` → `input_text`, and `image_url` → `input_image` whose `image_url` is the URL **string**, not an object.
- **Assistant tool calls** become top-level `function_call` items, not a message with a field.
- **Tool results** become `function_call_output` items correlated by `call_id`, taking the chat turn's `tool_call_id`.
- **Sampling:** emit only `model`, `max_output_tokens`, and `stream` when `spec.stream`. Everything in `SamplingSpec` that OpenAI refuses is dropped here; put the *why* in a comment naming the 400s, and log once per dropped field per role at WARNING (design §6) so an operator can see tuning being discarded.
- **`tool_choice`** passes through; `parallel_tool_calls` passes through when not `None`.
- **No `reasoning` field yet** — Task 5 adds effort once a role needs it.

- [ ] **Step 4: Run the tests**

Run: `/tmp/hexvenv/bin/python -m pytest tests/unit/infra/test_responses_request.py -q -p no:cacheprovider`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/adapters/model_inference/protocols/responses_request.py \
        tests/unit/infra/test_responses_request.py
git commit -m "Translate a conversation into a Responses request"
```

---

### Task 3: Responses normalisation and usage

**Files:**
- Create: `src/meshpipeline/adapters/model_inference/protocols/responses_normalize.py`
- Test: `tests/unit/infra/test_responses_normalize.py`

**Interfaces:**
- Produces: `usage_from(response) -> Usage`, `normalize(response, target, attempts) -> ModelRoundResult`

- [ ] **Step 1: Write the failing test**

The usage block is copied from a live response in design §1.

```python
# tests/unit/infra/test_responses_normalize.py
# Responsibility: Verify a Responses payload becomes the one result type every role shares.
# Boundaries: normalisation only; no network.
from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.adapters.model_inference.protocols import responses_normalize as rn
from meshpipeline.contracts.model_routing import RouteTarget

TARGET = RouteTarget(provider="openai", model="gpt-5.6-luna", account="default",
                     circuit_group="openai")


def _resp(output, usage=None):
    return SimpleNamespace(output=output, usage=usage, status="completed")


USAGE = {"input_tokens": 7,
         "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 0},
         "output_tokens": 11,
         "output_tokens_details": {"reasoning_tokens": 5},
         "total_tokens": 18}


def test_usage_maps_onto_the_neutral_shape():
    u = rn.usage_from(_resp([], USAGE))
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens) == (7, 3, 11)


def test_text_output_becomes_assistant_text():
    r = rn.normalize(_resp([{"type": "message", "content": [
        {"type": "output_text", "text": "hello"}]}], USAGE), TARGET, 1)
    assert r.assistant_text == "hello"
    assert r.reasoning_tokens == 5


def test_a_function_call_item_becomes_a_tool_call_request():
    r = rn.normalize(_resp([
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "call_id": "call_9", "name": "get_x",
         "arguments": '{"a":"1"}'},
    ], USAGE), TARGET, 1)
    assert [(c.id, c.name, c.arguments) for c in r.tool_calls] == \
           [("call_9", "get_x", '{"a":"1"}')]


def test_absent_usage_reports_zero_rather_than_inventing():
    r = rn.normalize(_resp([], None), TARGET, 1)
    assert (r.input_tokens, r.output_tokens, r.reasoning_tokens) == (0, 0, 0)


def test_reasoning_tokens_zero_means_not_reported_not_output_tokens():
    # ModelRoundResult's own docstring: zero means NOT REPORTED. Substituting output_tokens
    # would put an invented number in front of an operator.
    u = dict(USAGE, output_tokens_details={})
    r = rn.normalize(_resp([], u), TARGET, 1)
    assert r.reasoning_tokens == 0 and r.output_tokens == 11
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `/tmp/hexvenv/bin/python -m pytest tests/unit/infra/test_responses_normalize.py -q -p no:cacheprovider`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Read both dict and attribute access — the SDK returns objects, the tests use dicts, and a recorded fixture is the cheapest way to pin a shape. A small `_get(obj, key, default)` helper that tries `getattr` then `__getitem__` keeps both working; say why in a comment.

Map: `input_tokens` → `Usage.input_tokens`; `input_tokens_details.cached_tokens` → `cached_input_tokens`; `output_tokens` → `output_tokens`; `output_tokens_details.reasoning_tokens` → `ModelRoundResult.reasoning_tokens`, **absent stays 0**. Walk `output[]`: `message` items' `output_text` parts concatenate into `assistant_text`; `function_call` items become `ToolCallRequest(id=call_id, name=…, arguments=…)`; `reasoning` items' summary text, when present, becomes `reasoning_text`. Set `finish_reason` from the response `status`.

- [ ] **Step 4: Run the tests**

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/adapters/model_inference/protocols/responses_normalize.py \
        tests/unit/infra/test_responses_normalize.py
git commit -m "Normalise a Responses payload into the shared round result"
```

---

### Task 4: The Responses streaming consumer

**Files:**
- Create: `src/meshpipeline/adapters/model_inference/protocols/responses_stream.py`
- Test: `tests/unit/infra/test_responses_stream.py`

**Interfaces:**
- Produces: `async consume_responses_stream(stream, *, label, on_reasoning) -> Any` returning an object `responses_normalize.normalize` accepts — so the streamed and non-streamed paths converge before normalisation, exactly as `consume_chat_stream` does for chat.

- [ ] **Step 1: Write the failing test**

Event names are from design §1, observed live. Build a fake async iterator of `SimpleNamespace` events.

```python
# tests/unit/infra/test_responses_stream.py
# Responsibility: Verify a Responses event stream assembles into the same shape the sync call returns.
# Boundaries: assembly only; no network.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from meshpipeline.adapters.model_inference.protocols import responses_normalize as rn
from meshpipeline.adapters.model_inference.protocols.responses_stream import (
    consume_responses_stream,
)
from meshpipeline.contracts.model_routing import RouteTarget

TARGET = RouteTarget(provider="openai", model="gpt-5.6-luna", account="default",
                     circuit_group="openai")


async def _iter(events):
    for e in events:
        yield e


def _ev(t, **kw):
    return SimpleNamespace(type=t, **kw)


def test_text_deltas_assemble_and_usage_arrives_on_completed():
    events = [
        _ev("response.created"),
        _ev("response.output_text.delta", delta="Hel"),
        _ev("response.output_text.delta", delta="lo"),
        _ev("response.output_text.done"),
        _ev("response.completed", response=SimpleNamespace(
            output=[{"type": "message",
                     "content": [{"type": "output_text", "text": "Hello"}]}],
            usage={"input_tokens": 4, "output_tokens": 2,
                   "input_tokens_details": {"cached_tokens": 0},
                   "output_tokens_details": {"reasoning_tokens": 0}},
            status="completed")),
    ]
    assembled = asyncio.run(consume_responses_stream(_iter(events), label="t", on_reasoning=None))
    result = rn.normalize(assembled, TARGET, 1)
    assert result.assistant_text == "Hello"
    assert (result.input_tokens, result.output_tokens) == (4, 2)


def test_tool_call_argument_deltas_accumulate_into_one_call():
    events = [
        _ev("response.output_item.added",
            item={"type": "function_call", "call_id": "call_2", "name": "get_x"}),
        _ev("response.function_call_arguments.delta", delta='{"a"'),
        _ev("response.function_call_arguments.delta", delta=':"1"}'),
        _ev("response.function_call_arguments.done", arguments='{"a":"1"}'),
        _ev("response.completed", response=SimpleNamespace(
            output=[{"type": "function_call", "call_id": "call_2", "name": "get_x",
                     "arguments": '{"a":"1"}'}],
            usage=None, status="completed")),
    ]
    assembled = asyncio.run(consume_responses_stream(_iter(events), label="t", on_reasoning=None))
    result = rn.normalize(assembled, TARGET, 1)
    assert [(c.name, c.arguments) for c in result.tool_calls] == [("get_x", '{"a":"1"}')]


def test_reasoning_summary_deltas_reach_the_sink_as_they_arrive():
    seen = []
    events = [
        _ev("response.reasoning_summary_text.delta", delta="think"),
        _ev("response.reasoning_summary_text.delta", delta="ing"),
        _ev("response.completed", response=SimpleNamespace(
            output=[], usage=None, status="completed")),
    ]
    asyncio.run(consume_responses_stream(_iter(events), label="t", on_reasoning=seen.append))
    assert "".join(seen) == "thinking"


def test_a_stream_with_no_completed_event_does_not_invent_a_result():
    # A truncated stream must not look like a successful empty answer - the caller's
    # _EmptyResponse path exists for exactly this and classifies to EMPTY_RESPONSE.
    import pytest
    with pytest.raises(Exception):
        asyncio.run(consume_responses_stream(_iter([_ev("response.created")]),
                                             label="t", on_reasoning=None))
```

- [ ] **Step 2: Run it to make sure it fails**

Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Prefer the terminal `response.completed` event's `response` object as the assembled result — it carries the final `output[]` and `usage`, so the deltas are for *liveness and the reasoning sink*, not for reconstruction. That is simpler and less fragile than rebuilding the object from fragments, and it is what the live probe showed the API provides.

Still consume the deltas: forward `response.reasoning_summary_text.delta` to `on_reasoning` (awaiting it when it is a coroutine function — `ReasoningSink` is deliberately two-coloured), and keep the periodic "stream alive" logging `consume_chat_stream` does, since these are long streams and silence is indistinguishable from a hang.

If the stream ends with no `response.completed`, raise — do not return an empty result. Reuse whatever exception `router._classify` maps to `FailureCategory.EMPTY_RESPONSE`.

- [ ] **Step 4: Run the tests**

Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/adapters/model_inference/protocols/responses_stream.py \
        tests/unit/infra/test_responses_stream.py
git commit -m "Assemble a Responses event stream"
```

---

### Task 5: Register the Responses protocol

**Files:**
- Create: `src/meshpipeline/adapters/model_inference/protocols/responses.py`
- Modify: `src/meshpipeline/adapters/model_inference/protocols/__init__.py` (register `openai`)
- Modify: `src/meshpipeline/adapters/model_inference/call_kwargs.py` (retire `_openai_native`)
- Test: `tests/unit/infra/test_responses_protocol.py`

**Interfaces:**
- Produces: `protocols.responses.Responses` implementing `WireProtocol`, registered for `openai`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/infra/test_responses_protocol.py
# Responsibility: Verify the openai provider resolves to the Responses protocol and calls through it.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from meshpipeline.adapters.model_inference import call_kwargs as ck, protocols
from meshpipeline.contracts.model_routing import RouteTarget

TARGET = RouteTarget(provider="openai", model="gpt-5.6-luna", account="default",
                     circuit_group="openai")


def test_openai_resolves_to_the_responses_protocol():
    assert protocols.protocol_for("openai").__class__.__name__ == "Responses"


def test_the_protocol_calls_responses_create_not_chat_completions(monkeypatch):
    seen = {}

    async def _create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            output=[{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            usage={"input_tokens": 1, "output_tokens": 1,
                   "input_tokens_details": {"cached_tokens": 0},
                   "output_tokens_details": {"reasoning_tokens": 0}},
            status="completed")

    client = SimpleNamespace(responses=SimpleNamespace(create=_create))
    monkeypatch.setattr(
        "meshpipeline.adapters.model_inference.protocols.responses.client_for",
        lambda _t: client)

    proto = protocols.protocol_for("openai")
    response, usage = asyncio.run(proto.invoke(
        TARGET, [{"role": "user", "content": "hi"}], tools=None, tool_choice="auto",
        spec=ck.spec_for("intake"), parallel_tool_calls=None, on_reasoning=None,
        label="t", trace={}))

    assert "max_output_tokens" in seen and "max_tokens" not in seen
    assert usage.input_tokens == 1
    assert proto.normalize(response, TARGET, 1).assistant_text == "ok"


def test_call_kwargs_no_longer_claims_to_serve_openai():
    # The parameter-only arm is superseded: a Responses request is not a kwargs dict.
    import pytest
    with pytest.raises(KeyError, match="openai"):
        ck.kwargs_for(TARGET, ck.spec_for("intake"))
```

- [ ] **Step 2: Run it to make sure it fails**

Expected: FAIL on the first test — `openai` is not registered.

- [ ] **Step 3: Implement and register**

`Responses.invoke` builds via `responses_request.build_request`, calls `client_for(target).responses.create(**request, **trace)`, routes through `consume_responses_stream` when `spec.stream`, and returns `(response, responses_normalize.usage_from(response))`. `Responses.normalize` delegates to `responses_normalize.normalize`.

Register `"openai": Responses()` in `_registry()`.

- [ ] **Step 4: Retire `_openai_native`**

Remove `_openai_native` and its `_BUILDERS["openai"]` entry from `call_kwargs.py`: the OpenAI request is built by the protocol now, and leaving a second half-right builder invites someone to call it. Update `tests/unit/infra/test_call_kwargs_per_provider.py`, whose OpenAI cases assert on that function — rewrite them against `responses_request.build_request`, keeping what they actually check (no `min_p`, no `top_k`, no `thinking`). Do **not** weaken those assertions; they are the reason the rejected parameters cannot creep back.

Leave `_openai_wire`, `SamplingSpec` and `spec_for` untouched — DeepInfra and DeepSeek still depend on them.

- [ ] **Step 5: Run the full affected set**

```
/tmp/hexvenv/bin/python -m pytest tests/unit/infra tests/unit/platform \
  tests/unit/engines/test_planner_contract.py -q -p no:cacheprovider
```
Expected: the stage-1 golden and equivalence tests still pass; the new Responses suites pass.

- [ ] **Step 6: Commit**

```bash
git add src/meshpipeline/adapters/model_inference/protocols/ \
        src/meshpipeline/adapters/model_inference/call_kwargs.py \
        tests/unit/infra/test_responses_protocol.py \
        tests/unit/infra/test_call_kwargs_per_provider.py
git commit -m "Serve the openai provider over the Responses protocol"
```

---

## Done when

- `protocol_for("openai")` returns `Responses`; `protocol_for("deepinfra")` and `("deepseek")` return `ChatCompletions`.
- `tests/unit/infra/test_invocation_equivalence.py` and `test_call_kwargs_golden_routes.py` pass **unedited** — DeepInfra and DeepSeek cannot tell any of this happened.
- A live smoke against `gpt-5.6-luna` through `router.call_intake_model` returns text and reports non-zero `input_tokens`/`output_tokens`. Needs the key; `gcloud auth login` first.
- `ROUTE_MATRIX` is unchanged.

## What this plan does not do

Pointing roles at `openai` is a separate commit, deliberately: it moves defaults, it is a 4–17× output-cost change for builder/planner/reviewer, and the model choice for those three is still open (design §11). It should land with the cost table in its message and a live smoke per role.

Nor does it address the standby question. With DeepSeek abandoned and DeepInfra unused, OpenAI becomes a single point of failure with no funded fallback — structurally the position that caused the outage this all started with. That is stage 3 of the parent spec and is still unbuilt.
