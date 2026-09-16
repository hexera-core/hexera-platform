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

### Verified on the Responses API

The four unknowns that blocked this design are resolved. Probed against `api.openai.com/v1/responses`
with the production key on 2026-09-15.

**Tools and reasoning coexist — this is the finding the design rests on.**

| Request | Result |
|---|---|
| tools, no `reasoning` field | 200 — output items `['reasoning', 'function_call']` |
| tools + `reasoning:{effort:"low"}` | 200 — same |
| tools + `reasoning:{effort:"high"}` | 200 — same |

Chat Completions refuses exactly this. It is the whole reason to pay for a second protocol.

**Usage** — maps cleanly onto `Usage` and `ModelRoundResult`, and `reasoning_tokens` is a real
number rather than an absence to be invented:

```json
"usage": {"input_tokens": 7,
          "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
          "output_tokens": 11,
          "output_tokens_details": {"reasoning_tokens": 0},
          "total_tokens": 18}
```

Confirmed non-zero on a reasoning prompt (`"reasoning_tokens": 50`). Usage arrives on the final
`response.completed` event when streaming.

**Image input** is flat, and the text part is renamed too:

```json
{"type": "input_text",  "text": "colour?"}
{"type": "input_image", "image_url": "data:image/png;base64,…"}
```

The chat shape `{"type":"image_url","image_url":{"url":…}}` is refused: *"Invalid value: 'image_url'.
Supported values are: 'input_text', 'input_image', …"*. So `ContentPart` needs translating in both
name and nesting, not just nesting.

**A mixed assistant turn (text alongside a tool call) round-trips in one `input` array.**
`agents/loop/provider_round.py:120-125`'s `assistant_turn()` builds `{"role": "assistant",
"content": result.assistant_text, "tool_calls": [...]}` unconditionally, never `content=None` -
so every tool-calling round for builder, planner and reviewer produces exactly that shape.
Probed 2026-09-15: a bare `{"role":"assistant","content":"…"}` item followed by sibling
`function_call` / `function_call_output` items in one `input` array was sent to `/v1/responses`:

```
ACCEPTED - status: completed
output types: ['message']
text: The result is **42**.
```

Accepted, and the model read the tool output through it correctly. No separate "assistant text
must stand alone" turn is required.

**Streaming events** observed:

| Group | Events |
|---|---|
| lifecycle | `response.created`, `response.in_progress`, `response.completed` |
| items | `response.output_item.added` / `.done` |
| content | `response.content_part.added` / `.done` |
| text | `response.output_text.delta` / `.done` |
| reasoning | `response.reasoning_summary_part.added` / `.done`, `response.reasoning_summary_text.delta` / `.done` |
| tools | `response.function_call_arguments.delta` / `.done` |

A tool call streams as `function_call_arguments.delta` fragments terminated by `.done` — the
arguments arrive as a JSON string to accumulate, the same shape `streaming.py` already handles for
chat, under different event names.

**One honest mismatch.** The reasoning stream is `reasoning_summary_text`, a *summary*.
`ModelRoundResult.reasoning_text` is documented as "the model's chain-of-thought … (DeepInfra's
`reasoning_content`)". A summary is not a chain of thought. The field is transport-only and never
reaches a user, so carrying the summary there is defensible — but the docstring must be amended to
say so rather than letting two different things quietly share a name.

**Sampling is refused on Responses too** — `temperature` 400, `top_p` 400, `presence_penalty` 400.
So §6 stands unchanged; switching protocol does not buy sampling control back.

**`tool_choice` is flattened exactly like a tool declaration.** Probed 2026-09-15 at the pinned
SDK version, on `gpt-5.6-luna`:

| `tool_choice` sent | Result |
|---|---|
| `{"type":"function","function":{"name":"get_x"}}` (chat's forced shape) | **400** — *"Missing required parameter: `tool_choice.name`"* |
| `{"type":"function","name":"get_x"}` | 200 — output `['function_call']` |
| `"auto"` | 200 — output `['reasoning','function_call']` |
| `"required"` | 200 — output `['function_call']` |
| `"none"` | 200 — output `['reasoning','message']` |

`agents/loop/provider_round.py:62-64` builds the chat shape whenever a policy forces a tool, so
the builder's first forced round is a 400 without this translation. The string forms are
identical on both protocols. Handled in `responses_request.to_responses_tool_choice`.

**Reasoning summaries are opt-in; nothing produces one unless `reasoning.summary` is sent.**
Probed 2026-09-15 on `gpt-5.6-luna`, same prompt each time:

| Request | Reasoning item | Summary |
|---|---|---|
| no `reasoning` field | absent on a simple prompt; present with tools | — |
| `reasoning:{"effort":"high"}` | present, `reasoning_tokens=37` | `summary: []`, **0** stream deltas |
| `reasoning:{"effort":"high","summary":"auto"}` | present | `summary_text` parts; **84** `response.reasoning_summary_text.delta` events |
| `reasoning:{"summary":"auto"}` | accepted 200 on luna / terra / sol / astra, and alongside tools with a forced `tool_choice` | `summary_text` parts |

So the whole reasoning chain — the stream sink, the cumulative text,
`_summary_text_from_reasoning` — had no producer. Every summary part came back typed
`summary_text`. `build_request` now sends `reasoning: {"summary": "auto"}`; see §11 for the
effort question and for the roles it is deliberately NOT sent for.

**A truncated response is `incomplete`, not an absence.** Probed 2026-09-15 with
`max_output_tokens=16`:

| Path | What arrives |
|---|---|
| non-streamed | `status="incomplete"`, `incomplete_details.reason="max_output_tokens"`, `output=['reasoning']`, real usage |
| streamed | terminal event `response.incomplete` (never `response.completed`), whose `.response` carries exactly the same |

Both map onto `finish_reason="length"`, the word `agents/builder/loop.py:91` already reads.

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
  definitions AND a forced `tool_choice` flattened; `max_output_tokens`; `reasoning.summary`
  (§1 — without it no reasoning summary is ever produced). Not `reasoning.effort`: see §11.
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

1. ~~**Resolve the four unknowns** against the live API.~~ **DONE 2026-09-15** — all four are
   now verified facts in §1, including the load-bearing one: tools and reasoning coexist on
   Responses, so this design is sound rather than merely plausible.
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

- Silent drop vs. loud refusal for inexpressible sampling (§6) — recommendation stated.
- ~~Which model each role gets.~~ — **CLOSED.** `ROUTE_MATRIX` now reads
  `openai/gpt-5.6-luna` for intake and summarizer and `openai/gpt-5.6-terra` for builder, planner
  and visual_reviewer. The 4–17× output-rate increase for the latter three was accepted, not
  avoided; `unpriced_route_models()` is `[]` on both sides of the move. All five roles were
  verified live through their own entry points on 2026-09-15, vision included.
- With DeepSeek abandoned and DeepInfra unused, OpenAI becomes a single point of failure with no
  funded standby — structurally the position that caused this outage. Stage 3 of the parent spec
  (per-role standby) is the answer and is not yet built.
- The dropped-sampling WARNING in `responses_request._warn_on_dropped_sampling` is keyed by
  `target.label` (`provider/model`), not by role, because `build_request`'s signature carries no
  role. `ROUTE_MATRIX` already has `builder` and `planner` on the identical
  `deepinfra`/`zai-org/GLM-5.2` pair with identical tuned values, so the day two roles share an
  `openai` model, one WARNING will not tell an operator that a *second* role's tuning was also
  discarded — it will look like the field was warned about once and is now fine. Not fixed here;
  fixing it means threading a role name into `build_request`, which task 5 deliberately does not do.
  **That day has arrived:** builder, planner and visual_reviewer all sit on
  `openai/gpt-5.6-terra`, and the live cutover run logged the drop of `temperature`/`top_p`/`min_p`
  once (for the builder) and nothing at all for the planner, whose identical tuning was discarded
  in silence. Still a reporting gap, not a behaviour one — the parameters were always going to be
  dropped — but it is now observed rather than predicted.
- ~~A `max_output_tokens` truncation on a streamed Responses call surfaces as
  `_EmptyResponse`~~ — **CLOSED.** `consume_responses_stream` now returns the `.response` of a
  `response.incomplete` event, and `responses_normalize` maps that status onto
  `finish_reason="length"`, so a truncated round reaches the builder's truncation recovery
  instead of burning two retries. `response.failed` still raises: it reports a server-side
  error rather than a short answer, and returning it would normalise to `ok=True` carrying
  whatever fragment preceded the failure. It raises `_ProviderFailure`, not `_EmptyResponse`:
  `router._classify` gives it `SERVICE_UNAVAILABLE`, which is failover-eligible, so a stated
  provider failure can reach a standby. A dead stream keeps `_EmptyResponse`/`EMPTY_RESPONSE`,
  which is not.

- **`DEEPSEEK_MODEL` is now a misleading name for intake's model, and wants retiring.** It
  defaults to `gpt-5.6-luna` and is read by `application/pipeline_run.py` as intake's capture
  provenance, so it must agree with `INTAKE_MODEL` — an OpenAI identifier behind a DeepSeek name.
  Renaming it is a breaking configuration change, not a routing edit: an operator's existing
  `DEEPSEEK_MODEL=` would have to be refused rather than ignored, which is what
  `inventory.REMOVED` exists for. It was deliberately left alone in the cutover commit so that
  commit changes where calls go and nothing else. The replacement is `INTAKE_MODEL`, which is
  already declared, already in the template, and already the authority the route reads.

- ~~**The two quota domains collapsed, and no budget was retuned to match.**~~ — **CLOSED.**
  intake and summarizer share `openai:default:gpt-5.6-luna`; builder, planner and visual_reviewer
  share `openai:default:gpt-5.6-terra`. `routes.domain_budget` takes the MAX of the roles in a
  domain, so the untouched per-role numbers left the shared ceilings at 16 and 8 — the reviewer,
  which had held 8 in-flight calls of its own, was competing for the builder's. Every role in a
  shared domain now declares that domain's ceiling: terra is **16** (builder+planner's 8 plus the
  reviewer's 8) and luna is **24** (intake's 16 plus the summarizer's 8), restoring the capacity
  the four pre-cutover domains summed to rather than inventing a new number. The per-role
  `*_ACCOUNT` column remains the lever that splits a domain in two again.

- **`reasoning.effort` is not sent, and will not be until a role can express one.** §5 listed it
  as something this adapter owns, but `SamplingSpec` carries no effort field: any value here
  would be this module's invention rather than a role's intent, so the API's default stands.
  What IS sent is `reasoning: {"summary": "auto"}` — the opt-in §1 proves the reasoning chain
  needs — and only when the role has not set `thinking_off`.

  **Consequence, stated rather than left implicit:** `ModelRoundResult.reasoning_text` is
  permanently `""` for **intake and the search summarizer** on `openai`. Those are the two roles
  that set `thinking_off=True` (their chat path sends DeepSeek's `thinking: {"type":"disabled"}`),
  and they are also the two non-streaming roles — the ones §8 moves first. Asking OpenAI to
  narrate reasoning a role has declared it does not want would be incoherent, and summary text
  is billed as output tokens. Read `thinking_off` here for what it is: it does **not** disable
  reasoning on OpenAI — the model still reasons at its own default — it only decides whether a
  summary of that reasoning is requested back. Builder, planner and visual_reviewer do get
  summaries, which is where the Task-4 reasoning sink actually runs.

- **Langfuse does not trace the Responses path at all.** `tracing.langfuse_kwargs()` returns
  `{session_id, user_id, name}`, which are not OpenAI parameters: they survive on the chat path
  only because `langfuse.openai`'s wrapper patches the completions methods and strips them
  before the SDK sees them. The pinned `langfuse==2.36.2` patches `Completions`,
  `AsyncCompletions`, `ChatCompletion` and `Completion` — `responses.create` is untouched, and
  passing them to it raises `TypeError`, which `providers.classify` reads as
  `APPLICATION_DEFECT`: terminal, no retry, no failover. `protocols/responses.py` therefore does
  not forward `trace`. Nothing traceable is lost — an unpatched method emits no langfuse
  generation either way — but an operator who enables `LANGFUSE_SECRET_KEY` (it is already in
  `create-api-service.sh`'s secret list) will see DeepInfra and DeepSeek generations and no
  OpenAI ones. OpenTelemetry instruments httpx, not the SDK, and is unaffected.

- **The SDK floor is now load-bearing.** `client.responses` first shipped in `openai-python`
  1.66.0; the previous pin, 1.59.3, has no `responses` resource at all, so every OpenAI call in
  production would have raised `AttributeError` → `APPLICATION_DEFECT` → one attempt, terminal.
  Pinned at 1.109.1 (the last release before the 2.0.0 major). `tests/unit/infra/
  test_openai_provider.py` asserts the client exposes `responses`, so a downgrade fails in CI.
