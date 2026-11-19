# CRS v3 - the agentic repair loop

Status: approved by the owner inline, 2026-11-20 ("build and test continuously ... spec it and
build it end to end"), including the owner's two amendments. Supersedes nothing: v2's machinery
(session, caps, rollback, packages, reviewer, benchmark) is the foundation this builds on.

## Thesis

v2 proved the guardrails; v3 spends them. Keep the floor that makes mistakes cheap - snapshot
rollback, declaration accounting, audit, honest endings, the grader out of the author's reach -
and in exchange let the model work the way an engineer does: look, measure, try, look again,
many times. The explicit trade, sanctioned by the owner: less determinism for more fixes, with
safety carried by verification rather than by caps on iteration.

Evidence this rests on: the live product run (34 one-shot proposals, 0 survived; machinery held),
the 9,956-file real-failure corpus and its family aggregate, and the measured per-family
negatives (zero surface-stage overlaps in 5,871; open loops on kernel-valid faces).

## Amendment 1 (owner): the trigger is a goal profile, not the mesher

Repair is a first-class job with three entry points and ONE loop; only the finishing verdict
differs:

| Entry | Goal profile | Final verdict |
|---|---|---|
| Standalone repair (future product) | clean CAD back | kernel-clean + fast meshability score + semantic review; the product mesher is not involved |
| Meshing job | make it meshable, then mesh | the handback adapter; product mesher runs once, at the end, on the winner |
| Benchmark / API | evaluation | the sealed track |

## Amendment 2 (owner): the in-loop oracle is a ladder of cheap checks

Large meshers are banned from the loop. The verification ladder:

- Tier 0 - kernel checks (milliseconds): validity, counts, declarations. Every attempt.
- Tier 1 - scoped surface probe (sub-second to seconds): exists today. Every operation.
- Tier 2 - fast whole-part probe (seconds, hard-capped): the existing coarse gmsh probe - one
  process, coarse sizing, an element cap, 60 s budget, worker CPU only. The loop's meshability
  oracle; calibrated 76/79 against delivered-file truth.
- Tier 2b (new) - a coarse tolerant-mesh pass answering "would the tolerant rung eat this?" in
  seconds. The right question for the volume-stage overlap majority (59% of the corpus); lets
  repair stop early with a routing answer instead of grinding.
- Tier 3 - the product mesher: only when the job's goal is meshing, only once, on the final
  candidate, and never the big-VM escalation from inside repair. The grind predictor exists to
  keep repair's verification away from those machines.

Known proxy gap, accepted: tier 2 diverges from real meshing on a measured minority (reader
effect, 3/79). Standalone repair treats meshability as a quality signal, not the deliverable;
meshing jobs catch the divergence at tier 3.

## The six pillars

1. **Goal profiles and the oracle ladder** (amendments 1-2, plus the grind predictor: failure
   kind -> expected mesh cost tier).
2. **The investigator.** The author becomes a bounded tool loop per failing region: look
   (rendered views via the existing review camera), measure (distances, thickness, face
   properties, body-pair contact), probe (tiers 0-2 on a candidate state), edit (catalogue
   operations on a scratch clone), conclude (a package with its pre-registered claim, a
   mesher-side verdict, or escalate). All replies remain data; edits touch clones until
   conclusion; budgets are per-part compute tiers, not fixed attempt counts.
3. **Tournament repair.** K candidate repair paths per region (model- and rules-proposed),
   executed on clones, scored by the ladder plus measured deviation and cleared/revealed
   accounting; the best survivor goes to the reviewer; losers cost nothing.
4. **Tiered review.** Small, fully-accounted changes (declarations match, deviation under a
   per-part threshold the customer sets at upload) auto-accept with audit; the reviewer's seat is
   spent on large moves. A reviewer rejection returns the measured objection (not the reviewer's
   prose) to the investigator. Two rejections still escalate. Byte-identical replay is retired
   for agent-driven runs; replay means re-applying the recorded packages.
5. **Memory.** In-run working notes across regions; cross-run fingerprint-keyed outcomes (this
   face, this op, worked or failed - content addressing exists); the corpus gym scores every
   prompt and policy change offline on stratified samples of the 9,956 before anything ships.
6. **The ladder the part rides.** Deterministic ops (free) -> investigator (bounded spend) ->
   tolerant-mesh routing for volume-stage overlap parts -> human, with the full investigation
   attached. Each rung records why it passed the part down.

## What stays sacred

Kernel validity; declaration accounting; snapshot rollback at package granularity; honest
endings (unmeshable, escalated, not-proved); the sealed benchmark with clean controls and
newly-broken = 0; the reviewer sealed from the author; no CAD ever committed to the repo;
every new posture flag default-off and fail-closed.

## Measured goals

- Meshes-after on the 9,956-file track >= 4x the deterministic ceiling, newly-broken 0, clean
  control untouched.
- Standalone repair envelope: minutes per part on ordinary workers; no VM escalation reachable
  from inside repair.
- Cost per fixed part reported beside every gain; the gym's score gates every policy change.

## Build order (plan set)

A. Oracle ladder + goal profiles (foundation: tier policy, tier-2b, grind predictor, the
   standalone goal-profile contract through repair's entry points).
B. The investigator tool loop.
C. Tournament repair.
D. Tiered review + budget-driven iteration.
E. Memory (in-run, cross-run, gym wiring).
F. Product adapters: the mesher handback and the standalone repair job surface.

Each plan lands flag-off, gym-scored, small PRs, continuous CI; A unblocks B-D; E-F parallel
after B.
