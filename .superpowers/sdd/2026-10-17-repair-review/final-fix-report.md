# Final fix wave — slice 7 (repair review)

Base 9bd20de. Commits 949c762..38fc989 (14). Every behaviour change has a test; where a red was
feasible it was shown by reverting the source change (stash or temporary edit) and running the
new test, then restoring.

## CRITICAL 1-3 — the re-run reuses the job row (lease / deadline / billing)
**Where:** the run route's transaction (my call). The park stays a plain status CAS; the reset
lives beside Run-it's own CAS because that is the moment the row starts belonging to a new
execution, and the parked wait is only known there.
**What changed (949c762):** `JobRepository.reset_for_review_rerun` + pure `review_rerun_reset`
(persistence/repositories/job_repository.py), called right after the `pending_review -> pending`
CAS in `api/v1/repair_review.py`. It clears `active_worker_token`, `lease_expires_at` (NULL, so
the stalled-job reaper does not read it as released-but-unqueued), `pipeline_deadline_at` (NULL:
the re-run's claim re-anchors it, lease.py:139-140), `ended_at`, and — found while reading the
claim — `pipeline_execution_id`: a worker process keeps one id for its life, and a matching id is
read by the claim as the SAME execution resuming (old generation, finished checkpoint thread).
Billing: `started_at` moves forward by exactly the parked wait (`now - ended_at`), so
`credits_for_job(started_at, ended_at)` at the re-run's ending equals span 1 + span 2. Metering
is untouched; no migration. Side effect stated: `started_at` on a re-run job is the billed-span
anchor, not the wall-clock first start.
**Tests:** tests/unit/persistence/test_review_rerun_reset.py — real `LeaseRepository.claim_execution`
over a parked row: CAS-only re-run => `active_lease_conflict` (the old bug, pinned); with reset =>
`acquired_new_generation`, generation 2, even from the same process id; re-run 3 h past the
original deadline gets `now + 6h` and heartbeats; charge for park->review->re-run equals
base + 30 min (20 + 10) for a 3 min and a 30 h wait. tests/unit/api/test_api_repair_review.py —
reset runs after the CAS with the job's own started/ended.
**Command:** `.venv/bin/python -m pytest tests/unit/persistence/test_review_rerun_reset.py -q`
**Output:** `6 passed`; with `review_rerun_reset` stubbed to `{}`: `5 failed, 1 passed`.

## CRITICAL 4 — bore sealing via a group redirect
**What changed (b971076):** `_group_scope` in the redirect route: a group-scoped interpretation
targets the item plus OPEN, UNPAIRED members only; paired members are excluded and the playback
says "N paired defects in this group are left out: each one needs its own review". A group
redirect given ON a paired defect is refused (recorded nothing, logged `redirect_refused`).
`DirectedPlanner` (cad/repair/directives.py): the engineer bypass of the rim guard applies only
to a SINGLE-target engineer fill; a multi-target engineer fill is judged by the guard.
Side effect fixed: covered members are recorded `redirected` with `covered_by` (no directive of
their own, so no duplicate), the reply lists them as `covered`, the UI marks them answered and
does not ask them again; a new answer on the item reopens the members it no longer covers
(`RepairReviewRepository.reopen_covered`).
**Tests:** API — group redirect over a paired group: playback names the exclusion, no recorded
directive names a paired member, paired items untouched, covered member recorded with
`covered_by` and no directive; group redirect on a paired defect refused; covered members are not
marked `unreviewed` and add no directive at run; re-answer reopens covered. Planner — engineer
fill on both rims => `fill_refused`; single-rim engineer fill proposed. Repo — reopen SQL scoped
to redirected + covered_by + open park. UI — covered item is skipped, unanswered count drops.
**Command:** `pytest tests/unit/api/test_api_repair_review.py tests/unit/cad/test_repair_directives.py tests/unit/persistence/test_repair_review_repository.py -q` and the ui file.
**Output:** all passed; planner test red without the directives.py change (`1 failed, 9 passed`).

## CRITICAL 5 — leave-it never ends
**What changed (a7571c1):** `repair_review_park.settle_left`, called in pipeline_run right after
`plan_review`: reads the job's decided items; an item whose fingerprint's LATEST decision was
`left` is not put to the engineer again. Nothing else left => the park is withdrawn, the run
stays `failed` (an ordinary failure) and `FinalResult.review_left = {count, codes}` makes the
closing say "You chose to leave N things as they were (...), so the run went on with the part as
it stands. It still failed, and there is nothing new to ask you about, so the run ends here
rather than asking you the same questions again." Mixed answers park again only on what was not
left (unreviewed and could-not-be-done-safely items are re-asked). Delivered runs untouched.
Fail-open: unreadable history parks as before.
**Tests:** tests/unit/application/test_pipeline_run_parks.py — leave-all => plan None, failed
transition, no park, honest message; leave-some/approve-some => park on the two not left; a later
answer replaces an earlier leave; broken history => unchanged plan; delivered run records all.
**Output:** `16 passed` (file).

## CRITICAL 6 — apply-all ignores the sample's settled direction
**What changed (72f1618):** `_settled_direction` reads the sample (the first SAMPLE_SIZE members,
engineer answers only, by decided_at): all leave => `leave` (record_group_decision `left`); else
the latest non-leave answer — `redirect` repeats that directive's operation on each remaining
open member individually (own target, own item_id, source engineer); `accept` applies the
proposal as before. No sample answer => 409. Reply carries `direction`. UI computes the same
direction: an all-leave sample offers "Leave the rest" (never "Fix all N the same way"); a sample
redirect offers Fix all with "the rest get what you asked for in the sample".
**Tests:** API — redirect sample => per-member sew directives, no group accept; all-leave =>
`left`; accept => accepted; unanswered sample => 409. UI — all-leave sample offers only Leave the
rest and posts apply_all; a sample redirect makes Fix all mark the rest `redirected`; the
nothing-proposed group now offers Leave the rest after a leave.
**Output:** api file and ui file passed.

## IMPORTANT
- **Compensation CAS pinned (949c762):** `_compensate` passes `allow={JobStatus.pending}`. Test
  `test_the_compensation_can_only_pull_back_a_job_nobody_claimed` (red before: no `allow`).
- **Replay keeps source/item_id (04c4104):** `_replay_directives` replays a kept op with
  `source == "engineer"` and an item_id as the engineer's with that item_id. Test
  `test_an_approved_fill_survives_the_next_cycles_replay`: replayed fill on a paired rim is
  proposed (not fill_refused) with its item_id, and `directive_outcomes` writes `kept` to it.
  Red without the change (`1 failed`).
- **decision_history surfaced (b50292d):** `historyLine` — "this was tried before and could not
  be done safely (reason)" for rolled_back/unplayable, plus not_reached/kept/answered wording.
  DOM test `test_a_defect_tried_before_says_it_could_not_be_done_safely`.
- **Paired partner shown (b50292d):** a partner in the payload gets a second pin (`rv-pin-partner`,
  its own camera) and a line "paired with SEAM face 6 - it is pinned too"; otherwise a text line
  naming the partner's fingerprint from the pairing key (or "another defect" for a digest key).
  DOM test `test_a_paired_defect_pins_and_names_its_partner` (checks the second pin's position).
- **Rim pairing over all rims (561f42f):** minting computes `paired_rim_fingerprints(shape, pool)`
  and keys every rim it names (plus refusal-named rims) with `_pairing_keys`; survivors carry the
  key too. Unit test `test_rims_a_kept_operation_renamed_are_still_paired_at_mint` (red without
  the change); native minting tier still green.
- **Truncation marker (d9fe5a0):** chosen path: `repair_review_truncated` declared in
  PipelineState + data_contract STATE_FIELDS; carried on `ReviewPlan.truncated` into
  `FinalResult.review_truncated` (written with the park / delivered record — no column); GET
  serves `truncated` and `total`; the review header says "showing the N largest of M". Tests:
  state-merge + final-result record, GET payload, DOM copy.
- **Offered review after park->re-run->success (0526711):** a succeeded job serves its
  park_id=None items as `offered`, and an offered review frames from the dispatch payload's
  session_id. Test `test_a_run_delivered_after_a_park_serves_its_offered_items_not_the_closed_park`.
- **RepairRefused after-check carries operations (c236cf5):** the after-check refusal now carries
  the attempts and `result_discarded`; `directive_outcomes(discarded=True)` turns kept matches into
  `not_reached` / `discarded_with_result`. Tests: unit node test, native test (real kernel, after-
  check forced to refuse). Both red without the change.
- **Charge test fake (f634a8a):** `_Db.begin_nested` is a plain call returning an object that is
  both awaitable and an async context manager (as SQLAlchemy's AsyncSessionTransaction); the
  charge-once test observes `metering_service.charge_for_job` below the real `_charge_fail_open`.
  Proof: removing the park-skip in terminal_finalize => "a parked run was charged" (fails);
  restoring the old `async def begin_nested` fake => `assert 0 == 1` (the old fake was vacuous).
  The six "coroutine never awaited" warnings in the file are gone.

## MINOR (folded)
- Parked copy (b50292d) mirrors the server: "nothing was changed without you - your file is as it
  was except for the repairs it was sure of". DOM test.
- Unit scale (b50292d): `_rescale_skin` records a cumulative `rescaled_by` in the skin; review.js
  reads every kernel-mm camera through it (the stage's scale_to_m equivalent). Unit assertion in
  test_geometry_check; DOM test with a 2x skin lands the camera on the scaled point.
- Early triage decline (1c566c2) returns `_not_reached` rows. Test red without the change.
- offerReview (b50292d): a 409-once memo on "not enabled" — later offers make no GET. DOM test.

## Also found and fixed
- `pipeline_execution_id` reuse (see CRITICAL 1-3).
- Publication authority manifest was stale for the review route's two `logger.error` sites
  (2485ab3, regenerated).
- A native test patching `caps.sanity_refusal` leaked into the session's import-time binding under
  pytest-randomly; the test imports session/loop before patching.

## Gates (at 38fc989)
- `make lint` — All checks passed. `make typecheck` — 0 new (28 baseline).
- `pytest tests/unit/api tests/unit/application tests/unit/pipeline tests/unit/persistence tests/unit/cad -q`
  — 3451 passed, 85 skipped, 1 failed: `test_bore_staging::test_a_flanged_tube_stages_its_bore_alone`
  (pre-existing, also failing at 9bd20de).
- `CHROME_BIN=... pytest tests/ui -q` — 114 passed.
- `pytest tests/unit/hygiene tests/unit/settings tests/unit/agents tests/unit/platform -q` — 4177
  passed, 3 failed: config_parity (compose render) and builder run_python x2, all pre-existing.
- `pytest tests/native -q -k repair` — 70 passed.

## Not done / limits
- No deploy in this wave; nothing here is proven on `pranav` yet.
- The billing fix shifts `started_at`; any reader showing it as "first started" on a re-run job
  will show the shifted time.
- A crash between the Run-it commit and the dispatch still strands the job (ledgered window,
  unchanged); with the deadline now NULL the reaper does not pick it up either.

## Re-review residual — the Run-it crash window escaped every reaper rule
**Cause (introduced by 949c762):** the reset left a pending, unowned job with `started_at` set,
NULL deadline and NULL lease expiry; no stalled-job rule selects that shape, so a crash between
the Run-it commit and the dispatch stranded it behind a closed park.
**What changed (mitigation exactly as recommended):** (1) the Run-it transaction writes the
re-run payload (`set_dispatch_payload`: directives + `review_continuation`) beside the CAS;
(2) `review_rerun_reset` sets `lease_expires_at = now` — the existing "released but not confirmed
queued" mark — instead of NULL; (3) after a successful dispatch the route calls
`LeaseRepository.confirm_requeued(job_id, released_at)` (best effort, logged on failure); a
failed dispatch confirms nothing and compensates as before.
**Tests:** `test_the_reset_leaves_no_release_mark_for_the_reaper` rewritten as
`test_the_reset_leaves_the_release_mark_until_the_dispatch_confirms_it` (mark == Run-it's moment,
`rerun_eligible` accepts the row) + `test_a_confirmed_dispatch_clears_the_release_mark` (the
confirm UPDATE sets the mark NULL, matching on that exact mark); new
`test_a_run_it_that_crashed_before_its_dispatch_is_relaunchable_with_its_directives` (pending,
no token, expiry=now, payload with directives, NULL deadline => `rerun_eligible`). API:
payload -> dispatch -> confirm order with the reset's own timestamp; a failed dispatch confirms
nothing. Claim tests unchanged and green (no token, so the mark is not a live lease).
**Command:** `pytest tests/unit/persistence tests/unit/api tests/unit/application -q`;
`make lint && make typecheck`.
**Output:** `2238 passed`; ruff all checks passed; mypy 0 new (28 baseline).
