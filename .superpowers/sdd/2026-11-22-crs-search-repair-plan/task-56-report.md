# Task 56 - the hung-provider round, and the attempt that never reached a provider

Branch `sdd/task-56`, based on `5dc953ea`. Worktree `/Users/kitts/archductor/sdd-worktrees/task-56`.

## Verdict: option 3 - both, and the two are unrelated

The red test and the real defect are two different things. Neither one caused the other.

1. **The red test was a cold-start artifact in the test** (not a production defect, and not the
   `Hang` fake's counting). The fix is in the test.
2. **There is a genuine production fabrication in the attempt counter**, at
   `src/meshpipeline/adapters/model_inference/routing.py:325` as it stood: a round whose budget
   died before the first attempt reported `attempts=1` for **zero** invocations. It is a real
   defect of exactly the class the brief named, it is *not* the cause of the red test, and it
   could not have distorted a wave ledger row (see §3). Fixed anyway.

The brief's option 1 is wrong on its stated mechanism: the `Hang` fake increments `requests`
**before** awaiting (`test_proposer_wave_resilience.py:361-362`), so a cancelled request *is*
counted. `t.requests == 0` therefore meant `Hang.handle` was never entered at all — no request
was ever put on the wire.

## 1. Why the test was red

`asyncio.wait_for(invoke(target), timeout=attempt_timeout)` (`routing.py:327`) cannot fire while
`invoke` is doing synchronous work, and the **first** propose call in a process does a lot of it:
imports (`pricing`, `tracing`/langfuse, the protocol module), `require_price`, and building a fresh
`openai.AsyncOpenAI` + `httpx.AsyncClient`. Measured in a throwaway diagnostic (deleted; it spied on
`routing._attempt_target` and timestamped `Hang.handle`):

| run order | `PROPOSE_ATTEMPT_TIMEOUT_S` / `ROUND_DEADLINE_S` | first `handle` entered at | `t.requests` | reported `attempts` |
|---|---|---|---|---|
| first in process | 0.05 / 0.2 | never | **0** | 1 |
| first in process | 5.0 / 20.0 | **0.98 s** | 2 | 2 |
| warm (2nd, 3rd, 4th) | 0.05 / 0.2 | 0.004-0.008 s | **2** | 2 |

So ~0.6-1.0 s is spent inside `invoke` before the wire on a cold process, versus ~5 ms warm. With
the attempt timeout shrunk to 50 ms, the cold first attempt is cut before the transport is reached;
the round then ends via `if budget.expired(): break` (`routing.py:342-345`) with `attempts=1` and
`detail="timeout after 0.1s"` — exactly the log line in the brief (`0.05` formatted `%.1f` is `0.1`).

That is why it failed 3/3 **in isolation** and passes inside the full file: the earlier tests in
`test_proposer_wave_resilience.py` warm the same import graph. It was never a flake and never
related to `5b11250e`.

**This is not a production defect.** Production is `timeout_s=120.0` against the same ~1 s of
cold-start (`router.py:270-271`), and the cold start happens once per process. The test was
measuring its own shrunken timeout, not the hang.

**Fix** (`tests/unit/cad/repair_v4/test_proposer_wave_resilience.py:349-382`): warm the call path
with one answered round *before* shrinking the timeouts, then assert the real requirement — the
timeout marker, the wall-clock bound, `1 <= t.requests <= 2`, **and** the new
`reply.reply.provider.attempts == t.requests`, i.e. every attempt the round reports is one that
actually reached the transport. That last assertion is the anti-fiction guard the brief asked for
and the test did not previously have.

## 2. The production defect: `attempt or 1`

`routing.py` `_attempt_target`, before the fix:

```python
attempt_timeout = budget.cap(route.timeout_s)
if budget.expired():
    return (None, FailureCategory.TIMEOUT,
            f"route deadline {route.total_deadline_s}s exhausted after {attempt} attempt(s)",
            attempt or 1, usage, ...)      # <- attempt is 0 here; 1 is reported
```

`attempt or 1` turns zero attempts into one. The exception it produces contradicts itself in a
single line, which is how it is proven red:

```
RouteExhausted('summarizer: no route available - category=timeout phase=timeout
provider=openai model=gpt-5.6-luna attempts=1 route deadline 0.0s exhausted after 0 attempt(s)')
```

It also disagreed with its own neighbour: the admission path three screens up reports the *same*
condition ("the route deadline died before any call") as `attempts=0` (`routing.py:244-246`).

Reachability: this branch needs the whole `total_deadline_s` gone before the first `invoke`, i.e.
consumed by admission. Today every configured route has `queue_deadline_s < total_deadline_s`
(propose 60 < 300 at `router.py:282-284`; summarizer 10 < 60 at
`settings/inventory.py:161` + `agent_tools/shared/settings.py:28`), and `queue_wait` is itself
capped by the budget (`routing.py:222`), so a lease always returns with budget left. It is
**latent**, not live — but `QUEUE_DEADLINE` is an env-tunable per role
(`settings/inventory.py:181`), so `SEARCH_SUMMARIZER_QUEUE_DEADLINE=120` against a 60 s total makes
it live with no code change. Fixed to report `attempt`, with the reasoning in a comment.

## 3. Could the ledger or the report have attributed an attempt that never reached a provider?

**Not in the wave ledger: zero rows.** `ProviderAttemptInfo.attempts` is never read in
`src/meshpipeline/cad/repair_v4/` — `grep -rn attempts src/meshpipeline/cad/repair_v4/` returns only
unrelated prose and the gmsh panel's own timeout counters. The wave ledger records usage and spend,
not attempt counts, so no ledger row and no per-file status could carry a fabricated attempt.

**In the operator-facing surfaces: yes, two of them, one row per affected round.**

- the router's exhaustion log line (`router.py:317-319` for propose, `router.py:105-107` for every
  other role) prints `attempts=%d` straight from `exc.attempts`;
- `routing._emit` puts the same number into `InferenceCall.attempts`
  (`routing.py:435`), which `RedisInferenceTelemetrySink.record` persists to the global feed and the
  per-job feed (`adapters/inference_telemetry/redis.py:41-55`).

So the fabrication's blast radius was one telemetry record and one log line per affected round, in
the per-job inference feed. For a 96-shard propose wave on today's configuration the count is **0
affected rows** (the branch is unreachable for that route), which is the clean negative on the
wave-distortion question. Fixed regardless: the count is now true in every branch, and the
hung-provider test now pins the invariant for the propose route.

## Red-then-green

All with `V=/Users/kitts/archductor/sdd-worktrees/task-23/.venv/bin/python` (this worktree has no
`.venv`), `PYTHONPATH=src`, from the worktree root. No pytest output was piped; every log was
redirected and then grepped.

```
# 1. the reported test, at 5dc953ea, before any change
$V -m pytest '...::test_a_hung_provider_ends_the_round_at_the_round_deadline' -q
  -> EXIT=1   assert ((...) < 2.0 and 1 <= 0)   t.requests == 0     [/tmp/t56_red.log]

# 2. the new attempt-counter test, with the production fix NOT yet applied
$V -m pytest tests/unit/infra/test_route_timing_equivalence.py -q
  -> EXIT=1   "1 attempts reported for 0 invocations"   1 failed, 38 passed   [/tmp/t56_cnt_red.log]

# 3. after the fixes
$V -m pytest tests/unit/infra/test_route_timing_equivalence.py -q
  -> EXIT=0   39 passed                                            [/tmp/t56_cnt_green.log]
$V -m pytest '...::test_a_hung_provider_ends_the_round_at_the_round_deadline' -q   x3
  -> EXIT=0, EXIT=0, EXIT=0   (3/3, matching the 3/3 red)   [/tmp/t56_green_{1,2,3}.log]
```

## Tiers and gates run

| gate | result |
|---|---|
| `pytest tests/unit/cad/repair_v4 -q` | **EXIT=0, 729 passed** in 158 s (`/tmp/t56_tier.log`) |
| `pytest tests/unit/infra -q` (the tier owning `routing.py`) | **EXIT=0, 412 passed** in 30 s (`/tmp/t56_infra.log`) |
| `ruff check` on the 3 changed files | EXIT=0, all checks passed |
| `mypy src/.../routing.py` | EXIT=0, no issues |
| `ruff format --check` on the 3 changed files | EXIT=1 — **and EXIT=1 on the same three files read out of `5dc953ea`** (`git show HEAD:<f>` into /tmp, same 3 "would reformat"). Pre-existing; the tree is not ruff-format formatted and `make lint` is `ruff check` only. Nothing was reformatted. |

Not run: the full `tests/unit` sweep (host is saturated; the lead runs it centrally).

## What I could not settle

- **Whether an attempt cut *after* the request left the process but before the provider answered
  should count.** It still counts, and I left that alone: the router cannot distinguish "the
  provider never saw it" from "the provider is generating and we gave up", so counting the attempt
  is the honest reading. The fix only removes counts where the router *knows* it issued nothing.
- **A near-zero attempt window.** `attempt_timeout = budget.cap(route.timeout_s)` (`routing.py:321`)
  only refuses an attempt when the budget is fully `expired()`; a budget with, say, 10 ms left still
  buys a counted attempt that will almost certainly be cut pre-wire. I did **not** add a minimum
  viable-window guard: it needs a threshold I would be guessing, and I could not construct the state
  for any configured route (the pre-attempt backoff is itself budget-capped and followed by an
  `expired()` break, `routing.py:343-345`). Worth a follow-up only if a route is ever configured with
  `queue_deadline_s` close to `total_deadline_s`.
- **Why cold start costs ~1 s** was measured, not attributed to a specific import. Irrelevant to
  production (120 s attempt timeout), so I stopped at the measurement.
