# The LLM planner: slice 8, the model takes the planner's seat

**Date:** 2026-10-17 · **Status:** approved by the user's instruction to build all remaining slices
**Builds on:** slice 6 (`2026-10-14-cad-repair-targeted-loop-design.md`) and slice 7
(`2026-10-17-repair-review-design.md`), both shipped on this branch.

## Intent

Slices 6 and 7 built the machinery an LLM needs to repair CAD safely: five operations, caps
measured against the upload, keep-or-rollback on evidence, a review loop for the unsure, and a
training capture of every decision and outcome. Slice 8 puts the model in the planner's seat:
choosing which operation to aim at which entity, without waiting for a human — first in shadow,
where its choices are scored but never executed, then live behind a setting.

The safety argument is unchanged and is the whole point of the sequence: **the model proposes,
the kernel executes, the caps judge.** Nothing in this slice weakens a cap, a rollback, the
progress rule, or the bore-rim protections.

## Decisions (the user's, from the running conversation)

1. Build slice 8 now; all slices finished and thoroughly tested tonight; merge-ready.
2. The LLM is the planner — autonomous operation/target choice "when possible", with the review
   loop as the net where it is unsure.

## My calls (stated, since the user skipped the question round)

1. **Plan-ahead, not in-loop.** The repair loop runs synchronously inside the kernel thread; the
   model seam is async. Rather than thread a sync model client into the loop, the LLM planner is
   an async PRE-PASS in the node: given the inspection, it emits an ordered list of directive
   dicts (the slice-7 wire format, `source="llm-planner"`), which execute through the existing
   `DirectedPlanner` with the `RulesPlanner` as fallback. Costs one adaptivity step (no re-plan
   after a rollback mid-run — the rules fallback and the stall machinery cover that); buys the
   entire proven execution path: caps, unplayable accounting, outcome joins, `not_reached`
   honesty.
2. **Three postures, one setting.** `CAD_REPAIR_LLM_PLANNER = off | shadow | live`, default
   `off`. `shadow`: the pre-pass runs, its proposals are LOGGED with what the rules planner did
   and what the run's outcome was — geometry untouched. `live`: the proposals execute (as above),
   rules as fallback, and everything the loop rolls back or declines surfaces exactly as today
   (including parking for review when the run fails with residue).
3. **The rim guard treats the LLM as a machine, not an engineer.** `DirectedPlanner` already
   re-runs the paired-rim check for every non-`engineer` source; `llm-planner` directives keep
   that guard. The model can never fill a paired rim; only an engineer's individual approval can.
4. **Confidence is a gate, not a vibe.** The model must return per-proposal confidence; below
   `CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE` (default 0.5) the proposal is dropped (logged as a
   decline). Malformed replies, unknown ops, fingerprints not in the scan: dropped and logged,
   never guessed.
5. **Bounded like every model call on this branch:** at most `CALLS_MAX` (default 2) calls per
   run, `TIMEOUT_S` (default 20s) each, fail-open to the rules planner alone. The pre-pass adds
   nothing to the hot path when `off`.
6. **Validation is measured, not asserted:** the corpus tool gains `--planner off|shadow|live`.
   Tonight's evidence is the OCCT corpus (331 real parts) run in shadow (agreement and coverage
   vs the rules planner) and live (kept-rate, repaired-defect counts, never-worse check against
   the slice-6 baseline), plus the unit/native tiers.
7. **Deployment:** the deploy workflow's repair plumbing gains `llm_planner` (choice
   off/shadow/live) beside `cad_repair`, riding the same env-pair/metadata/startup path.
   `pranav` gets `shadow` for the deployed proof; `live` is demonstrated via the corpus and a
   scripted native proof, not flipped on the shared customer path tonight.

## Architecture

```
node_repair_attempt
  ├─ off:    exactly today
  ├─ shadow: llm_plan(report, story) → proposals LOGGED (TrainingLogger "planner_shadow":
  │          proposals + rules-run ops + outcomes, joined post-run) — loop runs on rules alone
  └─ live:   directives = engineer/replay directives (slice 7, FIRST, unchanged)
                        + llm_plan(...) proposals as "llm-planner" directives
             → repair_step_file(..., directives=...) → DirectedPlanner → session caps
```

- `llm_plan` lives in `agents/repair_review/planner.py` beside the slice-7 proposer, same seam
  (`call_repair_review_model`), same context discipline (clipped measurements, tried, story).
  Reply schema: an ordered list of `{op, defect_fingerprint, targets, confidence, rationale}`
  or an explicit decline; validated against the catalogue and the scan's fingerprints.
- Shadow scoring joins by the run's outcome machinery: the logged row carries the proposals, the
  rules planner's attempted ops with their verdicts, and `stopped_because`/`resolved`, so offline
  analysis can say "the model agreed with rules on N, diverged on M, and its divergent choices
  would have been playable K times".
- Live directives are stamped `source="llm-planner"` and (new, small) `planner_confidence`, so
  the slice-7 outcome capture distinguishes model-driven keeps/rollbacks from engineer ones in
  the corpus — the signal slice 9+ trains on.

## Goals

1. `off` is byte-for-byte today's behaviour (pinned by test).
2. Shadow on the OCCT corpus completes within the budget caps and produces the
   agreement/coverage report.
3. Live on the OCCT corpus is **never worse than slice 6's baseline** (repaired-defect count ≥
   baseline; no new cap violations — the caps make worse impossible, the test makes it measured).
4. A native end-to-end proof: a part the rules planner stalls on, repaired live by an LLM-chosen
   `fix_shape_global`/sequence, kept by the caps (scripted model stub in native tier; real model
   in the corpus run).
5. Host gates green; deployed shadow proof on `pranav`; PR updated.

## Non-goals

- No new repair operations; the catalogue stays five.
- No LLM-authored geometry or kernel calls, ever, in this slice.
- No change to review semantics: live-mode failures with residue park exactly as slice 7 built.
- `live` on by default anywhere, or flipped on for `pranav`'s customer path tonight.
- Context enrichment beyond what items/reports already carry, and the operator-queue writer from
  the original roadmap: the slice-7 review superseded the latter; the former is this slice's
  prompt context, already built.

## Constraints

- Settings via the inventory + docs regen; config-doc test run directly (not check-fast).
- Everything behind the existing flags: with `CAD_REPAIR_ENABLED=false` nothing runs at all.
- Model failures are never a run's failure: every model path fails open to rules.
- Known pre-existing test failures stand as ledgered; any other failure belongs to this slice.

## Deliverables

PR #201 updated with slice 8; corpus shadow+live reports committed beside the demo evidence;
deployed shadow proof (a pranav run whose capture carries planner_shadow rows); host gates green.

## Left to the builder

Prompt wording; the exact shadow-row schema; where the corpus report tables print; the native
stub-model fixture shape; `llm_planner` input plumbing details (mirror `cad_repair`).
