# Repair review: the unsure residue becomes a conversation with the engineer

**Date:** 2026-10-17 · **Status:** draft, awaiting approval
**Builds on:** `2026-10-14-cad-repair-targeted-loop-design.md` (slice 6, the deterministic loop — shipped on this branch, PR #201)

## Intent

More parts through the system. Slice 6 proved the autonomous loop repairs real defects —
430 of them across the OCCT corpus — but the same measurements showed where the ceiling is:

- **21 of 33 NIST parts never reached the loop.** Triage routes any `small_face` or
  `invalid_brep` to `manual_cleanup`, and today that is a dead end: nobody is told, nothing
  happens, the part meshes on its defective original or fails.
- **8 more reached the loop, stalled, and were declined.** Correct behaviour — and also a dead end.
- **202 `paired_rim` refusals** on 331 parts: the bolt-hole guard fires on any parallel plates,
  so planar filling is off and every genuinely fillable hole is refused with it.

Every dead end above becomes a **review**: the meshing engineer, in the console they already use,
is shown each unsure defect on the actual geometry and chooses the fix direction. An LLM proposes
and interprets; the slice-6 machinery (caps, rollback, progress rule) executes and remains the
safety authority. Every decision is captured as a training label.

We operate as a service company: the customer's time is the scarcest resource in the system.
Everything we are confident of is fixed silently, exactly as today. The engineer is only ever
asked about the residue, and only when it matters.

## Goals

1. A part that today dies at `manual_cleanup`, a stalled loop, or a planner refusal instead
   becomes a reviewable run the engineer can push through to a mesh.
2. The engineer sees each unsure defect **on the geometry** — camera driven to the entity,
   entity highlighted — and answers in one of three ways: accept the proposal, leave it, or
   redirect in their own words.
3. Scale is handled by **grouping and sampling**: defects of one kind with one proposed fix are a
   group; the engineer reviews a small sample individually, then one decision covers the rest.
4. Every review decision (and its eventual outcome) lands in the training log — the human-label
   stream the future planner model learns from.
5. A PR and a recorded proof video on the deployed `pranav` environment, showing:
   park → review → approve → re-run → repaired (and meshed, where the part allows).

## Non-goals

- **No autonomous LLM repair.** The LLM proposes and interprets; it never executes anything the
  slice-6 session would not accept, and nothing bypasses caps, rollback, or the progress rule.
- **No new repair operations.** The catalogue stays the five shipped operations. A redirect the
  catalogue cannot express is answered honestly ("I can't do that yet") and recorded.
- **No admin/operator workflow changes.** The existing operator queue stays as is.
- **The public CAD-healing benchmark is a separate, later project.** This slice only keeps
  accumulating the data (corpus logs, review labels) a benchmark would be built from.
- **UX polish.** The user said: settle the UI, address UX later. Layouts below are agreed;
  typography, motion and wording polish are explicitly later.

## Anti-goals

- A review that quietly mutates the part beyond what the engineer approved. Approval of a sample
  extends only to group members that match it (same defect code, same proposed operation); every
  application is still individually subject to caps and rollback, and anything rolled back
  returns to review marked "could not be done safely" rather than being silently dropped.
- Blocking a deliverable mesh on an unanswered question. A run with open questions that meshes
  anyway is delivered, with review offered — never withheld.
- Training-label pollution: a decision must be recorded as what it was (accepted / redirected /
  declined / apply-all) with the proposal it answered, never conflated with the outcome of
  executing it.

## What the user decided (verbatim decisions from the design conversation)

1. The reviewer is **the meshing engineer interacting with Hexera** — the customer, in the
   customer console. Not an internal operator.
2. **One defect at a time, with a CAD viewport open and the defect highlighted.** Viewport
   manipulation (camera driven to the entity) is expected.
3. **Full-screen review step with a progress rail** (option C of the mockups), not chat-embedded
   — plus the open text reply from option B as the third answer on every defect.
4. **Sample then approve-all:** show a few defects from a group individually; after the sample,
   offer "fix all N the same way" alongside "keep reviewing one by one". Granular control stays
   available throughout.
5. **Fix the confident ones silently.** Autonomous behaviour is unchanged; review receives only
   the unsure residue. ("We hope to operate more as a service company than anything else.")
6. **Mesh first, ask second.** The run repairs what it is sure of and attempts the mesh. If the
   mesh lands, it is delivered with an optional "N things I wasn't sure about" review. Only when
   the mesh fails, or cannot be attempted, does the run park at review and notify the engineer.

## Architecture

### What counts as "unsure" (the review feed)

A `ReviewItem` is minted from each of these, at the moment the pipeline today gives up:

| source | today | becomes |
|---|---|---|
| triage routes `manual_cleanup` / `ask_customer` | silent dead end | one item per located defect, with the triage reason |
| loop stalls / every operation rolled back | declined, original stands | one item per surviving defect, carrying what was tried and why it was rolled back |
| planner refusals (`paired_rim`, `target_absent`, policy bans) | recorded, invisible | one item per refusal, with the refusal reason |
| defects remaining after a kept run | listed in the report | one item each, marked "survived the autonomous pass" |

Each item carries: defect code, entity type + index + fingerprint, measurements, the proposed
operation (if any), the proposal's provenance (`rules` or `llm`), and a camera/highlight payload
(below).

**Triage becomes per-defect, not per-job.** Today any `manual_cleanup`/`ask_customer` route
declines the whole repair (`pipeline/repair_attempt.py:121`), so a part with one small face gets
*none* of its repairable defects fixed — which contradicts "fix the confident ones silently", the
user's stated behaviour. Under this slice the job-level gate is replaced: defects triage
individually, the loop runs on the confidently-repairable ones exactly as slice 6 built it, and
only the judgement defects (`small_face`, `invalid_brep`, and anything triage abstains on) become
review items. This is the change that makes the 21 NIST parts reachable at all.

**Judgement items repair nothing.** No catalogue operation answers `small_face` or
`invalid_brep`, and that is fine: the item's question is "is this intentional geometry or a
defect?" — the answers are *leave it* (= this is fine, mesh it) and a redirect. "Leave it" on a
judgement item satisfies the gate that `manual_cleanup` used to be: the human judgement the route
was waiting for has been given.

**Paired-rim items are individual-review only** (my call, for the sealed-bolt-hole reason). A
review approval MAY override the planner's `paired_rim` refusal — a human looking at the hole is
precisely the authority the guard lacks — but apply-all never extends across a rim pairing, the
two rims of one pairing are never both fillable from one decision, and the review screen shows
the paired partner when either rim is on screen. The 202 refusals become reviewable one at a
time; they are exempt from sampling.

### Grouping and sampling

Items group by `(defect_code, proposed_operation)`. Group ordering: blocking defects first
(whatever stopped the mesh), then by count descending. Sample size: **3** (or the whole group if
smaller) — my call, builder may surface it as a setting. "Show me the largest first" reorders a
group by the defect's measured size. An approve-all decision applies the *sample's settled
direction* to every remaining member — including a redirect the engineer gave during the sample —
and each application still runs individually through the slice-6 session.

### The run lifecycle

```
upload → geometry check → intake → dispatch
  → autonomous repair (unchanged) → mesh attempt
      ├─ mesh delivered → terminal, review OFFERED on the run page (non-blocking)
      └─ mesh failed / staging refused / nothing meshable
            └─ residue exists → run parks: awaiting_repair_review (notified)
                 → engineer reviews → approved plan executes in a NEW pipeline run
                 → (repeat: that run may itself park, with what was learnt attached)
```

- The parked state is the existing `JobStatus.pending_review` — reserved in the enum and marked
  unused today (`persistence/job_state.py:30-50`); this slice legalises its transitions rather
  than inventing a new status. The geometry check's pause-for-the-user machinery
  (`geometry_hold.py`, stored markers, resumed by a POST plus a conversation message) is the
  precedent for how parking and resuming feel.
- **Items are minted against the post-autonomous-pass shape** — the shape the engineer is shown —
  and the parked state persists the kept-operation sequence from the autonomous pass. The re-run
  replays that sequence deterministically first (slice 6's resume behaviour already re-derives
  repairs this way), THEN executes the engineer's directives, then lets the rules planner at
  whatever remains. Directives executed against the raw upload would miss their fingerprints and
  bounce every approval as `target_absent`; the replay-first order is what makes an approval land
  on the entity the engineer actually looked at. The upload remains immutable throughout; every
  re-run derives from it.
- **A review ends explicitly.** The screen carries a persistent "Run it" action; finishing the
  queue offers it; answering only some items and running is legal (unanswered items are recorded
  `unreviewed` and survive to the next park). All-left is a legal end: the judgement defects are
  declared fine, the gate is satisfied, and the re-run attempts the mesh on what stands. If that
  re-run fails with nothing new to ask, it terminates as an ordinary failed run whose outcome
  message says what was left and why. No timeout on `pending_review` in this slice; a parked run
  waits like an unconfirmed geometry check does. (My calls.)
- **On a DELIVERED run**, review rides the existing "Run it again" semantics: approving leftovers
  produces a new run in the same conversation, its result beside the old one — nothing ever
  replaces a delivered mesh. (My call; billing posture below is the user's.)
- **LLM proposal cost is bounded at park time** (my call): one batched call per defect-code group,
  at most four calls per park, with a timeout — on failure or overrun the group falls back to the
  rules proposal or to no-proposal (accept/leave-it on the kernel's description). Parking must
  stay cheap; a bad part has hundreds of located defects and per-item calls would put minutes of
  model latency in the worker's hot path.
- The re-run is a fresh dispatch of the same job with an `approved_plan` input: the loop consumes
  the engineer's decisions as planner directives (after the replay, above) before the rules
  planner takes over for anything newly discovered.
- A parked run holds no workspace. Everything the review needs (items, measurements, camera
  payloads, proposals) is persisted when the run parks; the re-run re-derives geometry from the
  original, exactly as resume does today.
- Notification: the run page shows the parked state and the review entry point; the conversation
  that owns the run gets a message ("Your part needs about ten minutes of your eyes"). Email and
  other channels are later work.

### The LLM's seat

Two duties, both bounded by the catalogue:

1. **Propose.** For items where the rules planner had nothing (manual_cleanup routes above all),
   the LLM is given the defect's targeted context — code, measurements, neighbours, what the
   autonomous pass tried, the part's overall story — and proposes one catalogue operation with
   targets, plus a one-sentence rationale in the product's voice. This proposal is what the
   review screen shows. Provenance is recorded as `llm`.
2. **Interpret redirects.** The engineer's free-text answer is translated into either (a) a
   catalogue operation + targets + "apply to group" scope, played back for confirmation, or
   (b) an honest refusal naming what the catalogue cannot do, which is itself recorded — those
   refusals are the roadmap for new operations.

The LLM uses the same model-routing plumbing the intake agents use. It never calls kernel code.
Its proposals and interpretations execute only through the slice-6 session, under every cap.
With no reachable model, review still works: rules proposals where they exist, and items without
a proposal fall back to accept/leave-it on the kernel's own description. (My call.)

### The viewport

Reuse the geometry-check viewer, which already has the needed primitives: openings are keyed by
numeric id, placed as pins at `centroid` + `normal`, with `select(id)` for highlight and
`look(id)` driving the camera down the entity's normal (`ui/js/viewer/geometry_stage.js:423-459`).
Review items follow the same pattern: each item's payload carries the entity's centroid, normal,
and extent, computed server-side when the item is minted — so the review screen is a pure
consumer of the pin/look machinery. Framing shows the defect at screen scale with the part
visible for context ("zoomed to edge 0.0021 mm · part is 212 mm"). Rendering the entity's own
tessellation as a painted highlight (rather than a pin at its centroid) is an enhancement the
builder may defer; the pin + camera is the slice's bar.

### Feedback capture (the point of all this)

Every decision writes a training row: the item (defect + context + proposal + provenance), the
decision (`accepted` / `redirected(text, interpretation)` / `left` / `apply_all(scope)`), the
decision latency, and — joined later — the execution outcome (kept / rolled back + reason) and
the run's end state. Decline-to-propose rows from the LLM are captured too. This stream is the
labelled dataset future planners train on, and the measure of whether review earns its
interruptions.

## Constraints

- Everything behind the existing `CAD_REPAIR_ENABLED`, plus a new `CAD_REPAIR_REVIEW_ENABLED`
  (default false) so the branch deploys dormant.
- The slice-6 safety machinery is not weakened: caps, rollback, progress rule, bore-rim refusal
  all apply to review-approved operations exactly as to autonomous ones. A review approval is a
  planner directive, not a cap waiver.
- Upload immutability and fall-back-to-original behaviour unchanged.
- The deliverables and PR follow this repo's rules: host gates green, native tier where the
  kernel is needed, dev-deploy proof on `pranav`, honest claims only.

## Deliverables

1. The PR, subagent-driven with per-task review, same as slice 6.
2. A proof video on the deployed `pranav` console: a real part (chosen from today's corpus
   candidates) that currently dies unsure → parks → is reviewed with sampling and apply-all →
   re-runs → delivers repaired CAD, and a mesh where the part allows. Progressive screenshots
   and an explanation doc beside it, as for the slice-6 demo.

## Left to the builder

- Exact persistence shape. My call on the boundary: NEW tables for review items and decisions
  (per-defect granularity, customer-actor), leaving `cad_repair_jobs`/`cad_repair_decisions`
  untouched — the operator queue is per-job, operator-actored, standalone admin CRUD, and this
  slice's non-goal is changing it. The builder shapes the new tables, API routes, and the
  console/UI protocol for pin + camera.
- The LLM entry point: a new role call beside `call_intake_model`/`call_planner_model` on the
  `contracts/model_inference.py` seam, or reuse of an existing one — builder's choice, same
  routing either way.
- The approved-plan directive format consumed by the loop.
- Choice of demo part (today's corpus logs list the candidates: the NIST stalled eight, the
  manual_cleanup 21).
- Sample size setting, group ordering ties, review-screen copy.
- Model choice + prompt for the two LLM duties, within existing routing.

## Open for the user

- **Billing posture.** Proposed default: the re-run of a PARKED run is free — the customer paid
  for a run we paused with questions, and finishing it is the same purchase. Review offered on a
  DELIVERED run bills as an ordinary "Run it again". Awaiting the user's word.

## My calls (made during design, flagged for the user)

- Triage goes per-defect (derived from "fix the confident ones silently" — see Architecture).
- Paired-rim items: approval may override the refusal, but individual review only — apply-all
  never crosses a rim pairing, and the partner rim is shown.
- Items minted against the post-pass shape; re-run replays kept ops, then directives.
- Review ends via explicit "Run it"; partial answers legal; all-left = mesh it as it stands;
  no `pending_review` timeout this slice.
- Delivered-run review rides "Run it again"; nothing replaces a delivered mesh.
- LLM proposals batched per defect-code group at park time, capped and fail-open.
- Sample size 3; approve-all extends a sample redirect to the group.
- Review works degraded with no LLM reachable.
- Parked runs hold no workspace; re-run re-derives from the immutable upload.
- Notification channels beyond console + conversation message are out of scope.
- `CAD_REPAIR_REVIEW_ENABLED` dormant-by-default flag.
