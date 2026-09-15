# OpenAI on the Responses API: a protocol seam, not a provider branch — design

Date: 2026-09-15
Status: designed, not implemented
Scope: one sub-project — make `openai` actually callable, by turning "which wire protocol
does this provider speak" into a declared seam instead of an assumption baked through the
adapter.

This design **supersedes §5.1** of
[`2026-09-14-runtime-model-routing-design.md`](2026-09-14-runtime-model-routing-design.md),
which said of OpenAI:

> Wire-compatible with what exists. `client_for()` gains a branch, a client module joins
> `deepinfra.py`/`deepseek.py`, and `LLM_PROVIDER_KEY_ENV` gains `"openai"`. **No translation.**

That was true of Chat Completions and is false for Responses. Stage 1 was built on it, which is
why `openai` currently ships as a provider that cannot serve a single route.

## 1. Why this exists

Production has been down since 2026-09-15T04:49Z on an unfunded DeepSeek account. The decision
is to leave DeepSeek and move onto OpenAI, on the Responses API — Chat Completions is not where
these models are going.

Stage 1 made `openai` selectable: a client, a `client_for` branch, prices, a credential in Secret
Manager, and `_openai_native` in `call_kwargs.py` to drop the sampler extensions OpenAI rejects.
A live probe against the real key then showed that is not close to sufficient.

### Verified starting facts

Probed against `api.openai.com` with the production key on 2026-09-15, not read from docs.

| Fact | Evidence |
|---|---|
| All four priced models are available to the account | `GET /v1/models` → `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` present among 130 |
| `max_tokens` is refused | 400 — *"Use `max_completion_tokens` instead"* |
| `temperature` is refused at any non-default value | 400 — *"'temperature' does not support 0.3 … Only the default (1) value is supported"* |
| `top_p` is refused outright | 400 — *"'top_p' is not supported with this model"* |
| `presence_penalty` is refused | 400 — not supported |
| `min_p` is refused | 400 — *"Unknown parameter: 'min_p'"* |
| All of the above hold on **all four** models | probed luna / terra / sol / astra individually |
| Function tools are refused on Chat Completions | 400 — *"Function tools with reasoning_effort are not supported for gpt-5.6-terra in /v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to 'none'."* |
| `reasoning_effort:"none"` makes tools work on Chat Completions | 200, `tool_call -> get_x`, 7 tool-call deltas streamed, usage present |
| `reasoning_effort` low/high and `verbosity` are accepted | 200 |
| Vision works | `image_url` with a base64 data URL → 200 on luna and terra |
| Streaming with `include_usage` works | 200, usage present in the final chunk |

**The forcing fact:** on Chat Completions, function tools and reasoning are mutually exclusive.
Intake, builder, planner and reviewer all call tools. Choosing Chat Completions means choosing
no reasoning for four of five roles; choosing Responses means a translation layer.

### Not yet verified — these block implementation

`gcloud` reauthentication expired mid-probe, so four facts remain unread. **Task 1 resolves them
before any code is written** (see §8). They are listed here so nobody mistakes them for settled:

| Unknown | Why it matters |
|---|---|
| `usage` field names on Responses | `_usage_from` maps them into `ModelRoundResult`; wrong names meter every OpenAI call at zero, exactly like the reviewer's `$0.00` bug this branch just fixed |
| The image-input item shape | `visual_reviewer` sends a rendered view; the reviewer role cannot move without it |
| Streaming event type names | a second stream consumer is the single largest piece of work here |
| Whether `reasoning_effort` and tools coexist **on Responses** | if they do not, the whole reason for choosing Responses over `reasoning_effort:"none"` evaporates and this design should be reconsidered |

The last one is load-bearing. Stage 1 shipped a guessed parameter name because its verification
step was optional; it cost a Critical at final review. This design makes the equivalent step
blocking.

### The shape difference

From OpenAI's own migration guide, corroborated against the reference:

| Concern | Chat Completions (what stage 1 built) | Responses |
|---|---|---|
| System prompt | `messages[0] {role:"system"}` | top-level `instructions` |
| Conversation | `messages[]` | `input[]` items |
| Tool definition | externally tagged `{type, function:{name, description, parameters}}` | internally tagged `{type, name, description, parameters}` |
| Tool call | `message.tool_calls[].{id, function:{name, arguments}}` | output item `{type:"function_call", call_id, name, arguments}` |
| Tool result | `{role:"tool", tool_call_id, content}` | input item `{type:"function_call_output", call_id, output}` |
| Output cap | `max_tokens` | `max_output_tokens` |
| SDK entry point | `client.chat.completions.create` | `client.responses.create` |

## 2. The real finding: we have protocols, not providers

Stage 1's model is `provider → client + parameters`. That was right when both providers spoke one
wire format. It is wrong now, and `call_kwargs.py` — the module stage 1 was built around — solves
only the *parameter* half. The *protocol* half is assumed everywhere:

| Site | What it assumes |
|---|---|
| `router.py:77,83` | `client.chat.completions.create(...)` |
| `router.py:88-93` | `response.choices[0].message.tool_calls` |
| `router.py:55-63` | `prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens` |
| `router.py:99-107` | `completion_tokens_details.reasoning_tokens` |
| `router.py:140-145` | chat-shaped `tools` / `tool_choice` / `parallel_tool_calls` |
| `streaming.py` (all 168 lines) | `chunk.choices[0].delta`, rebuilding a ChatCompletion-like object |
| `messages.py` | a conversation *is* a `messages[]` array |

Adding Responses as branches inside those seven sites would double every one of them, and
Anthropic (spec §5.2) would triple them. So the change is a seam, not a branch.

## 3. Decisions

1. **A protocol adapter owns request-building, the call, streaming, and normalisation** — the
   whole span from `Conversation` to `ModelRoundResult`. `router.py` keeps only role entry points.
2. **`execute()` stays shared.** Retry, backoff, circuit breaking, failover and telemetry are
   protocol-independent and are not touched.
3. **`ModelRoundResult` and `Conversation` do not change.** `contracts/model_inference.py:48`
   already promises no SDK object crosses that line; this design keeps that promise rather than
   renegotiating it. The product layer sees nothing.
4. **Responses is the OpenAI protocol**, not an option alongside Chat Completions. One code path
   per provider; no runtime toggle to test twice and reason about at 3am.
5. **Sampling intent that a protocol cannot express is dropped, and the drop is declared** —
   see §6.

## 4. The seam

```
contracts/model_inference.py     (unchanged — Conversation in, ModelRoundResult out)
        │
router.py                         role entry points only; picks an adapter, hands it to execute()
        │
routing.py execute()              (unchanged — retry / circuit / failover / telemetry)
        │
protocols/__init__.py             provider -> adapter registry
   ├── chat_completions.py        deepinfra, deepseek     (today's code, moved not rewritten)
   ├── responses.py               openai                  (new)
   └── (stage 2) anthropic.py     anthropic
```

Each adapter implements one method:

```python
async def invoke(target: RouteTarget, conversation: Conversation, *,
                 tools: list[ToolDefinition] | None, tool_choice: ToolChoice,
                 spec: SamplingSpec, parallel_tool_calls: ParallelToolCalls,
                 on_reasoning: ReasoningSink, label: str, trace: dict) -> tuple[Any, Usage]
```

returning what `execute()` already expects, plus a `normalize(response, target, attempts)`.

**The chat-completions adapter is a move, not a rewrite.** `streaming.py`, `messages.py`,
`_normalize`, `_usage_from` and `_openai_wire` relocate behind the interface with their behaviour
byte-identical — pinned by the golden tests and `test_invocation_equivalence.py` that stage 1
left in place. That is the safety property: DeepInfra and DeepSeek must not be able to tell this
happened.

## 5. What the Responses adapter owns

- **Request**: `instructions` split from `input`; conversation turns → input items; tool
  definitions flattened; `max_output_tokens`; `reasoning_effort` (pending the §1 unknown).
- **Tool round-trip**: `function_call` items out; `function_call_output` items back in, correlated
  by `call_id`. Stateless — full history resent each turn, no `previous_response_id`. The product
  already owns the transcript and a server-side conversation handle would be a second source of
  truth for something `agents/loop/` is already authoritative about.
- **Streaming**: a second consumer over typed `response.*` events, producing the same assembled
  result shape the chat consumer produces.
- **Normalisation**: `output[]` → `ModelRoundResult`, including `reasoning_tokens` **only when
  genuinely reported** — `ModelRoundResult.reasoning_tokens`'s own docstring says zero means *not
  reported*, and an invented count is worse than none.

## 6. Sampling that cannot survive

OpenAI rejects `temperature` (non-default), `top_p`, `presence_penalty` and `min_p`. So the
builder's tuned `0.3 / 0.95 / 0.05`, the planner's identical trio, and the reviewer's
`0.7 / 0.8 / top_k 20 / presence 1.5` are **not expressible**. `reasoning_effort` and `verbosity`
are what exist instead.

This is a real behaviour change on the roles that do the hardest work, and it is not a bug to be
fixed — it is a property of the models. The adapter drops the fields; `SamplingSpec` keeps
carrying them because DeepInfra and DeepSeek still honour them.

**Open decision for §11:** whether dropping a deliberately-tuned value should be silent (with a
comment) or should raise at request construction. Silent is simpler and matches what
`_openai_native` does today; raising refuses to quietly discard tuning somebody chose on purpose.
Recommend: log once per (role, field) at WARNING on first drop — visible to an operator, not fatal
to a run.

## 7. What must not change

- No default moves. `ROUTE_MATRIX` is not edited by this design; pointing roles at `openai` is a
  separate, deliberate commit once the adapter works.
- DeepInfra and DeepSeek behaviour stays byte-identical, proven by the existing golden tests.
- `contracts/model_inference.py` is not renegotiated.
- `FAILOVER_ELIGIBLE` is not widened.

## 8. Delivery

1. **Resolve the four unknowns** (§1) against the live API and write them into this document.
   **Blocking** — no code until they are facts. Needs `gcloud auth login`.
2. **Extract the protocol seam.** Move today's code behind `protocols/chat_completions.py` with
   no behaviour change; golden tests and `test_invocation_equivalence.py` are the proof.
3. **Responses adapter: request + non-streaming + normalisation.** Intake and summarizer are the
   non-streaming roles, so this makes them callable first.
4. **Responses adapter: streaming + tools.** Builder, planner, reviewer.
5. **Point roles at `openai`** — the `ROUTE_MATRIX` change, its own commit, with the cost table.

Stages 2 and 3 are independently useful: 2 removes a latent duplication whatever happens next, and
3 alone unblocks intake, which is the gate production is currently failing at.

## 9. Testing

- Recorded-shape tests per conversation form: system+user, assistant-with-tool-calls, tool result,
  multimodal reviewer turn. No network.
- A round-trip test that a `function_call` out and a `function_call_output` back in correlate by
  `call_id` across two turns.
- Streaming assembled from a recorded event sequence, asserting the same `ModelRoundResult` the
  non-streaming path produces for equivalent content.
- Usage mapping asserted against a recorded response, so a renamed field fails loudly rather than
  metering at zero.
- The stage-1 golden tests must pass unchanged throughout — they are what proves step 2 was a move.

## 10. Risks

| Risk | Mitigation |
|---|---|
| The four unknowns turn out worse than assumed (esp. tools+reasoning on Responses) | Step 1 is blocking; if tools and reasoning are exclusive there too, this design is reconsidered rather than built |
| The protocol move silently changes DeepInfra/DeepSeek | Golden tests + `test_invocation_equivalence` pin the wire shape; step 2 lands alone so a bisect is one commit |
| Two stream consumers drift | Both normalise to `ModelRoundResult`; the equivalence test above is the shared contract |
| Losing reasoning-token and cached-token fidelity | Assert against recorded responses; zero must keep meaning *not reported* |
| Prod stays down while this is built | Step 3 unblocks intake on its own; DeepInfra still serves builder/planner/reviewer meanwhile |

## 11. Open questions

- The four unknowns in §1.
- Silent drop vs. loud refusal for inexpressible sampling (§6) — recommendation stated.
- Which model each role gets. `gpt-5.6-luna` is chosen for intake and summarizer; builder, planner
  and visual_reviewer are unspecified, and moving them is a 4–17× increase in output cost.
- With DeepSeek abandoned and DeepInfra unused, OpenAI becomes a single point of failure with no
  funded standby — structurally the position that caused this outage. Stage 3 of the parent spec
  (per-role standby) is the answer and is not yet built.
