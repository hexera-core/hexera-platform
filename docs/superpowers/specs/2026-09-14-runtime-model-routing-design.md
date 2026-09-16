# Runtime model routing: three providers, swappable per role — design

Date: 2026-09-14
Status: designed, not implemented
Scope: one sub-project — add OpenAI and Anthropic beside DeepInfra, and move route
resolution from import time to a portal-controlled runtime override with per-role primary
and standby.

## 1. Why this exists

On 2026-09-15T04:49Z every mesh job in production failed at the first gate. DeepSeek answered
`402 Payment Required` — the account balance was `-0.18`, `is_available: false`. `intake` is the
only role that must run before anything else, it is served by DeepSeek alone, and no route in
this product has a standby. One unpaid invoice with one vendor stopped the entire pipeline, and
the user-facing message said *"Please try again in a few minutes"* for a condition that would
never resolve on retry.

The provider swap is the obvious reaction. The durable fix is that **which provider serves a role
must be changeable without a deploy**, and that **a role must have somewhere to fail over to**.

### Verified starting facts

Read from this repository and from the live providers on 2026-09-14/15, not from documentation.

| Fact | Evidence |
|---|---|
| DeepSeek balance is `-0.18`, `is_available: false` | `GET api.deepseek.com/user/balance` with the `hexera-prod` secret |
| DeepInfra is healthy | `POST api.deepinfra.com/v1/openai/chat/completions` → `200` |
| Production has never gotten past intake | `prod-api` logs, 72h: two `chat/message` requests, both `503` |
| **No route has a standby** | `routes.summary()` on the live catalogue — five roles, `standby=None` on all |
| The "neutral" call contract is OpenAI's wire format | `contracts/model_inference.py:48` — *"One OpenAI-style function-tool declaration"* |
| …including tool calls, tool results and images | ibid. — `tool_calls[].function.arguments`, `role:"tool"`, `{"type":"image_url"}` |
| Streaming is already confined to the adapter | ibid. — *"SDK chunks … never cross this boundary"* |
| Routes are frozen at import | `adapters/model_inference/routes.py:32` — `_ROUTES` module global; each agent settings module calls `route_from_catalogue()` at import |
| …but the loader itself reads `os.environ` per call | `settings/env.py:49` — `declared_value()` is live |
| Route defaults live in one table | `settings/inventory.py:128` — `ROUTE_MATRIX`, ten columns per role |
| A half-configured standby is already refused | `settings/routes.py:61` — `STANDBY_PROVIDER`/`STANDBY_MODEL` must be set together |
| A standby sharing the primary's quota domain is already refused | ibid. — *"it would share the provider's pool and fail with it"* |
| `INSUFFICIENT_BALANCE` is deliberately non-failover | `contracts/model_routing.py:43` — `FAILOVER_ELIGIBLE` excludes it |
| The vendor confinement rule already names `anthropic` | `tests/unit/hygiene/test_vendor_sdk_confinement.py:104` |
| Credential validation is derived, not hardcoded | `adapters/model_inference/routes.py:90` — `missing_provider_credentials()` reads the enabled profile |
| The reviewer's model meters at **$0.00 today** | `adapters/inference_telemetry/pricing.py:24` — price unconfirmed, *"deliberately ABSENT"* |
| Capabilities are declared but never enforced | `Capability.MULTIMODAL` is set on the reviewer route and read by nothing |
| `intake` collapses every failure reason to `unavailable` | `adapters/model_inference/failure_markers.py:37` — `_MINIMAL` vocabulary |

### Verified provider surface

Anthropic rates from the bundled `claude-api` reference; OpenAI rates fetched from
`developers.openai.com/api/docs/pricing` on 2026-09-14. USD per 1M tokens.

| Provider | Model | Input | Output | Cached in |
|---|---|---|---|---|
| anthropic | `claude-opus-5` | 5.00 | 25.00 | 0.50 † |
| anthropic | `claude-sonnet-5` | 2.00 | 10.00 | 0.20 † |
| anthropic | `claude-haiku-4-5` | 1.00 | 5.00 | 0.10 † |
| openai | `gpt-6-astra` | 10.00 | 50.00 | 1.00 |
| openai | `gpt-5.6-sol` | 4.00 | 20.00 | 0.40 |
| openai | `gpt-5.6-terra` | 2.00 | 12.00 | 0.20 |
| openai | `gpt-5.6-luna` | 0.20 | 1.20 | 0.02 |

† **Not quoted.** Derived as 0.1× input, Anthropic's standard cache-read ratio. Must be confirmed
against the live pricing page before it reaches a billing path — see §10.

The fetched OpenAI page carried its own caveat: its vision and function-calling columns were
*inferred from model naming conventions*, not read from the docs. No capability claim from that
source is load-bearing here; §7 verifies capability at swap time instead.

## 2. Goals and non-goals

**Goals**

- OpenAI and Anthropic callable beside DeepInfra and DeepSeek, per role.
- Primary **and standby** per role, changeable from the admin portal without a deploy.
- A swap that cannot produce a broken route: credentials and capabilities checked before it is accepted.
- Correct cost telemetry for every model a route can reach, including the reviewer's.

**Non-goals**

- Changing any default. `ROUTE_MATRIX` ships exactly as it is today (§4).
- Widening `FAILOVER_ELIGIBLE`. `INSUFFICIENT_BALANCE` stays terminal; routing around a
  configuration failure buys the same failure from a second vendor.
- Per-tenant or per-job routing. The override is deployment-wide.
- Changing the user-facing failure copy. `intake`'s `_MINIMAL` vocabulary still collapses every
  reason to `unavailable`, so a customer is never told which vendor we failed to pay. That is a
  deliberate product choice, recorded here because this design makes it visible, not because it changes.

## 3. Decisions

1. **Anthropic gets a real adapter on the `anthropic` SDK**, not the OpenAI-compatibility
   endpoint. The shim is weakest at streaming tool use and thinking blocks, which is exactly where
   builder and reviewer live.
2. **Defaults do not move.** New providers are selectable, not selected.
3. **Postgres is the record; a 15s TTL cache is the read path.** No pub/sub, no invalidation protocol.
4. **Long-running roles pin their route at job start.** Intake and summarizer resolve per call.
5. **The swap endpoint validates.** A route that cannot work is refused at write time, not discovered at call time.

## 4. Why no default moves

> **Superseded 2026-09-15.** Every default in this section has since moved: all five roles were
> cut over to `openai` (luna for intake/summarizer, terra for builder/planner/visual_reviewer)
> after production went down on an unfunded DeepSeek account. The reasoning below is why the
> increase was worth deliberating over, not a description of the shipped routes — and the
> per-role control it argues for is exactly what made the cutover a five-row edit. The rates
> still hold; see `2026-09-15-openai-responses-adapter-design.md` §11.

Output rate dominates: builder and planner stream long agentic turns under a 1800s timeout.

| Role | Today | Out $/1M | OpenAI | Out | Anthropic | Out |
|---|---|---|---|---|---|---|
| builder | `deepinfra:zai-org/GLM-5.2` | **3.00** | `gpt-5.6-terra` | 12.00 (4×) | `claude-sonnet-5` | 10.00 (3.3×) |
| planner | `deepinfra:zai-org/GLM-5.2` | **3.00** | `gpt-5.6-terra` | 12.00 (4×) | `claude-sonnet-5` | 10.00 (3.3×) |
| visual_reviewer | `deepinfra:Qwen/…-Thinking` | **unpriced** | `gpt-5.6-terra` | 12.00 | `claude-sonnet-5` | 10.00 |
| intake | `deepseek:deepseek-v4-pro` | **0.87** | `gpt-5.6-luna` | 1.20 (1.4×) | `claude-haiku-4-5` | 5.00 (5.7×) |
| summarizer | `deepseek:deepseek-v4-flash` | **0.28** | `gpt-5.6-luna` | 1.20 (4.3×) | `claude-haiku-4-5` | 5.00 (18×) |

Moving the builder to a frontier US provider is a 4–17× increase in its dominant cost. Per-role
control is what makes that affordable to reason about: Claude on the reviewer, where quality
matters and volume is low, while GLM keeps the builder. Shipping the providers without moving a
default makes the increase a deliberate click rather than a side effect of this change.

## 5. The provider layer

### 5.1 OpenAI

Wire-compatible with what exists. `client_for()` gains a branch
(`providers.py:12`), a client module joins `deepinfra.py`/`deepseek.py`, and
`LLM_PROVIDER_KEY_ENV` gains `"openai": "OPENAI_API_KEY"`. No translation.

### 5.2 Anthropic

Not compatible on any axis the product uses. A translation module owns all of it:

| Concern | OpenAI shape (today) | Anthropic |
|---|---|---|
| System prompt | `messages[0]` | top-level `system` |
| Tool declaration | `{"type":"function","function":{…,"parameters"}}` | `{name, description, input_schema}` |
| Tool call | assistant `tool_calls[]` | `tool_use` content blocks |
| Tool result | `role:"tool"` message | `tool_result` block in a **user** message |
| Image | `{"type":"image_url"}` + data URL | `source:{type:"base64", media_type, data}` |
| Usage | `prompt_tokens`/`completion_tokens` | `input_tokens`/`output_tokens` |
| Stream events | chat-completion deltas | `content_block_delta` / `input_json_delta` |

`contracts/model_inference.py:48` already guarantees no SDK object crosses the adapter boundary,
so this is contained: nothing above `adapters/model_inference/` changes.

`providers.classify()` gains an Anthropic branch. The SDK's exception names mirror OpenAI's
(`RateLimitError`, `AuthenticationError`, `APIStatusError`, …), so the existing structure holds,
including the 402 → `INSUFFICIENT_BALANCE` rule added in `66e2e84`.

`tests/unit/hygiene/test_vendor_sdk_confinement.py` gains one allowlist entry. The
product-module ban on `anthropic` (line 104) stays as-is and must keep passing.

### 5.3 Sampling parameters — the breaking constraint

`temperature`, `top_p` and `top_k` are **removed on `claude-opus-5` and `claude-sonnet-5`, and
return 400.** Depth is controlled by `thinking: {type:"adaptive"}` and
`output_config: {effort: …}` instead.

Every route here passes `temperature` and `top_p`; builder and planner add
`extra_body:{min_p}`, the reviewer adds `extra_body:{top_k}`. OpenAI rejects `min_p` and `top_k`
as unknown parameters; Anthropic rejects all four.

So `BUILDER_CALL_KWARGS`, `REVIEWER_CALL_KWARGS`, `INTAKE_CALL_KWARGS` and
`SUMMARIZER_CALL_KWARGS` — module-level dicts built from import-time settings — become
**per-target builder functions**. This is not cleanup; without it no Claude route can make a
single successful call. It is also the largest single piece of work in the project.

## 6. The override store

```sql
CREATE TABLE model_route_override (
    role              text PRIMARY KEY,
    provider          text NOT NULL,
    model             text NOT NULL,
    standby_provider  text,
    standby_model     text,
    updated_at        timestamptz NOT NULL,
    updated_by        text NOT NULL
);
```

An absent row falls through to `ROUTE_MATRIX`. The table is empty on deploy, so behaviour is
byte-identical to today — that is the rollback story: `DELETE FROM model_route_override`.

`standby_provider` and `standby_model` are both-or-neither, enforced by the same rule
`settings/routes.py:61` already applies to the env-configured standby, reused rather than
reimplemented.

**Read path.** `routes.all_routes()` loses its `_ROUTES` module global and gains a 15s TTL cache.
A swap reaches every process within 15s. No pub/sub, no invalidation protocol, no new failure mode.

**Degradation.** If Postgres is unreachable the cache serves stale; past TTL it falls back to
`ROUTE_MATRIX`. Routing must never be the component that takes the pipeline down — an override
store outage degrades to today's behaviour, which is a working deployment.

## 7. Resolution, pinning and validation

**Pinning.** `intake` and `summarizer` resolve per call — single requests, no coherence problem.
`builder`, `planner` and `visual_reviewer` resolve **once at job start**, and the resolved targets
are written into the job snapshot and read from there for the run's duration. A mesh run spans
hours; letting a mid-run swap move the builder would split one job's cost attribution and
reproducibility across two vendors.

**Swap-time validation.** The admin endpoint refuses a write when:

1. The target provider has no credential. `missing_provider_credentials()`
   (`routes.py:90`) already computes this against the enabled profile; it becomes a per-target check.
2. The target model lacks a capability the role declares. `Capability.MULTIMODAL` is declared on
   the reviewer route and enforced nowhere — pointing `visual_reviewer` at a text-only model
   currently succeeds and fails on the next mesh job. Capability is confirmed against the
   provider's models endpoint, not a static table, which also contains the caveat in §1 about
   inferred capability data.
3. The standby resolves to the primary's quota domain. Already refused at construction
   (`settings/routes.py:61`); the endpoint surfaces it before the write rather than at import.

**Quota domains.** Keyed `provider:account:model`, so a swap lands on a fresh circuit breaker by
construction. `domain_budget()` already falls back to `MODEL_DEFAULT_CONCURRENCY_BUDGET` for a
domain absent from the static table.

## 8. Credentials

`OPENAI_API_KEY` and `ANTHROPIC_API_KEY` follow the path the two existing provider keys take.

| Location | Edit |
|---|---|
| `deploy/gcp/scripts/create-secrets.sh:47,90` | Container name variable + a row in `SECRETS=()` |
| `deploy/gcp/scripts/bootstrap-env.sh:457` | `*_API_KEY_SECRET` defaults |
| `deploy/gcp/generated.prod.env:139` | Same, materialised |
| `deploy/gcp/scripts/create-api-service.sh:130,247` | Secret mount + plaintext-refusal list |
| `deploy/gcp/scripts/create-worker-fleet.sh:232`, `worker/startup.sh:75` | Fleet mount |
| `settings/providers.py:22` | Values + `LLM_PROVIDER_KEY_ENV` |
| `settings/inventory.py:246` | Declared, so they reach `.env.example` and startup validation |

`create-secrets.sh` creates empty containers and never reads a payload, by design. Values are
written once per project by an owner:

```
printf '%s' '<key>' | gcloud secrets versions add openai-api-key --project hexera-prod --data-file=-
```

**Startup validation changes meaning.** Today a provider's key is required only when a configured
route references it. With runtime overrides, any provider is *potentially* referenced, so the
naive reading — demand all four keys at boot — would make a single-provider deployment
impossible.

It does not change: boot validation keeps checking exactly the set of providers the routes resolve
to **at that moment** (overrides applied, defaults where absent), which is what
`missing_provider_credentials()` already computes. A provider nobody currently routes to still
needs no key. What closes the gap is the swap endpoint refusing a target whose credential is
absent (§7.1), so an uncredentialled provider can never become the resolved set in the first
place. Net effect: a missing credential is a write-time refusal instead of a runtime 503.

## 9. Testing

- `classify()` table extended for Anthropic exceptions, same shape as the existing OpenAI table.
- Message translation: round-trip each conversation shape the product composes — multimodal
  reviewer turn, assistant-with-tool-calls, tool result, system prompt — against recorded
  Anthropic request bodies. No network.
- Call-kwargs builders: assert no `temperature`/`top_p`/`min_p`/`top_k` reaches an Anthropic
  target, and no `min_p`/`top_k` reaches an OpenAI one. These are the 400s in §5.3.
- Override resolution: an absent row yields exactly the `ROUTE_MATRIX` route; a present row
  overrides it; a stale cache serves the old route for under 15s; an unreachable store falls back.
- Swap validation: missing credential, missing capability, and standby-shares-domain each refused.
- Pinning: a swap mid-run does not change the targets a running job uses.
- `unpriced_route_models()` returns empty for every model any route can reach.
- Existing hygiene tests must keep passing unchanged — particularly
  `test_no_product_module_imports_a_model_provider_sdk`.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Anthropic cache-read rates are derived, not quoted (§1 †) | Confirm against the live pricing page before stage 1 lands; `unpriced_route_models()` makes an omission listable rather than silent |
| A swap to a frontier model multiplies spend 4–17× | Defaults unchanged; the portal shows the per-1M rate beside each option |
| Translation bugs surface only under real tool use | Recorded-body tests per conversation shape; stage 2 ships behind an unchanged default, so nothing routes to Claude until someone opts in |
| `anthropic` 1.x is built on `httpx2`, not `httpx` | A dependency change, not a code change — resolve at stage 2, before it surprises the build |
| Route resolution moving to call-time adds a DB read to a hot path | 15s TTL; intake is the only per-call role with user-facing latency, and it already makes a network call to a model |
| The admin portal gains authority over production routing | It already holds mutate authority on the fleet (`2026-09-10-admin-fleet-and-billing-design.md` §7); this adds no new trust boundary, and every write records `updated_by` |

## 11. Delivery

Four stages, each independently shippable.

1. **OpenAI provider + per-target call kwargs + prices.** Genuinely small for OpenAI; the call-kwargs
   refactor (§5.3) is the bulk and is required by everything after it. Also closes the reviewer's
   `$0.00` metering. Useful alone.
2. **Anthropic adapter.** Translation module, `classify()` branch, SDK confinement entry, `httpx2`.
3. **Override table, TTL resolution, pinning.** Migration, read path, job snapshot.
4. **Admin endpoint + portal UI.** Validation, audit, the dropdowns.

Stages 1–2 deliver the provider choice even if 3–4 slip; stage 1 alone fixes a live billing gap.

## 12. Open questions

- Anthropic cache-read rate — derived, needs confirming (§1 †, §10).
- Which OpenAI and Claude models to expose in the portal dropdown. The full price table is in §1;
  narrowing it to a curated set per role is a product call, not a technical one.
- Whether `visual_reviewer`'s unpriced Qwen model should be re-identified on DeepInfra's catalogue
  or simply replaced during stage 1.
- **Stage 2 must validate a route's provider name at construction.** `settings/routes.py:104`
  (`provider=_v("PROVIDER")`) and `:123` (the standby) accept any string. `BUILDER_PROVIDER=deepsek`,
  or `anthropic` named before its adapter exists, passes hardened startup validation in full and
  first surfaces at the model call as an unroutable-provider `KeyError` — which the error taxonomy
  classifies as an application defect and retries with backoff, once per attempt. That is a
  configuration typo diagnosed as a product bug: the exact misdiagnosis this design exists to
  remove, reintroduced at a seam the design did not close. The registry to validate against is
  `providers.LLM_PROVIDER_KEY_ENV` — the same one §8 credentials from and
  `inference_overrides._supported_providers()` already refuses unknown providers with. It belongs
  with the admin swap-validation in stage 2, where a provider name first becomes operator input.
