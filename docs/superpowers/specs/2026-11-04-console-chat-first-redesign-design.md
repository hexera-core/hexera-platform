# Console redesign: chat first, open at every phase

Written 2026-11-04. Supersedes nothing; it continues
[console revamp Phase 1](../../design/console-revamp/README.md), whose three-column shell, phrase
table and status icons stand. This document covers what changes on top of that shell.

## Intent

The console is the product surface for the foreseeable future. A CAD extension (ANSYS, Fusion) is
the eventual destination but is far enough out that it must not shape this work. The chat is how a
customer interacts with Hexera, and it stays the spine.

What the console must do better:

1. **Say less, and show more.** The run narration is prose where it should be facts. The same
   information takes about a third fewer words when a decision is a row rather than a sentence.
2. **Be open at every phase.** A user should be able to see exactly what CAD repair did and
   exactly what the mesher chose, from durable records, in deterministic text. Not more narration:
   more receipts.
3. **Show the repair.** Repair runs on every job today and is invisible. Its most reassuring
   behaviour — refusing to seal a through-hole — has never been seen by anybody.
4. **Keep a 3D view on screen at all times.** The part before it is meshed, the mesh once it
   returns, in one panel that never unmounts and never resets the camera.
5. **Offer repair-only as an outcome.** Taking input geometry and returning repaired CAD, with no
   mesh, is a real product. The database already models it; nothing in the request path reaches it.

## Goals

- The run feed reads as instrument output, not as a narrator.
- Every number a user is shown can state where it came from.
- Repair is a visible phase with a receipt that includes refusals.
- One mounted viewer; the camera survives every content swap.
- A customer can ask for repaired geometry without a mesh, and get it.
- No model-written prose in any report artifact.

## Non-goals

- No numeric mesh controls. Configuration stays qualitative; the openness is in the derivation
  shown, not in knobs added.
- No part-centric workspace, no variant management, no convergence families, no batch. A CAD host
  does those better and will.
- No change to the three-column shell, the Runs/Files pane, the composer, or the voice.
- No change to the delivery invariant. A mesh still ships only on a passed review.

## Anti-goals

Things that would make this a failure even if it shipped:

- A run screen that reads like a log file. The register is a competent colleague, not a stack trace.
- Openness implemented as `PUBLIC_TRACE_MODE=raw`. Reasoning text and tool names are the opposite
  of what is wanted.
- A report that contains a sentence a model wrote.
- More total words on screen than today.

## Constraints

- `ui/` and `apps/console/public/static/` must stay byte-identical
  (`tests/unit/deploy/test_console_ui_copy_parity.py`).
- The two most-changed screens are a vanilla ES-module app under `ui/js/`, not React. The React
  layer at `/runs/new` and `/runs/[jobId]` is shell markup plus a bootstrap script.
- Every colour must already exist in `ui/css/tokens.css`. New names may only be roles pointing at
  existing values.
- The narration leak guard (`contracts/narration.py::_LEAKS`) drops any line naming a path, a rule
  id, a mesher, a model or a snake_case key. See the carve-out below.
- Terminal truth is built by application code from durable facts
  (`application/final_result.py`). Nothing here may route a terminal claim through a model.

## The decisions, as taken

### 1. Prose becomes sourced rows, behind disclosure

Narration paragraphs that carry three or more measured facts are replaced by a step row plus an
inline disclosure the user opens: a **Receipt** on the repair step, a **How it was sized** on the
plan step. A disclosure is a two-column list — the thing, its value — with an optional one-line
derivation beneath.

The rule for the derivation line: it states the measured input and the policy that consumed it.
"Part diagonal 243 mm ÷ 24, at Standard detail." The numbers already exist in
`cad/analysis.py::recommend_refinement`'s return value and in each engine's `_FIDELITY_*` table;
they are computed and discarded.

Prose that remains is the conversational connective tissue, one or two sentences, and it keeps the
first person sparingly. Prose that merely restates the user's own last message is cut.

### 2. The leak guard gets a narrow carve-out

Engine identity becomes showable in two places only: the run header chip, and the fields of a
disclosure. It stays forbidden in every free-text narration line. The guard is therefore applied
per-field rather than per-event, and the carve-out is a declared allowlist of engine display names
resolved from the registry — not a loosening of the regex.

Rationale: the intake engine card already says "Use cfMesh" out loud, so the current state is
inconsistent rather than private. Mesher *internals* ("castellating", `snappyHexMesh`) stay
forbidden everywhere.

### 3. Repair becomes a visible phase

A step row, a short sentence, and a receipt covering: what was closed, what was merged, what was
**refused and why**, and the measured shape change against its cap. All of it exists already —
`RepairReport.defects`/`entities`/`operations`, `CadRepairAttempt.caps`, the paired-rim guard's
refusal reason — and none of it is published.

`repair_report` and `repaired_cad` are already durable artifacts on every terminal outcome and
already in `_CUSTOMER_VISIBLE`. The repaired STEP appears in the Files pane as it is written.

### 4. One viewer, always mounted

A single panel, mounted from first paint, whose content swaps: empty → CAD part with numbered
opening stickers → repaired part → part during meshing → delivered mesh. Two toggles, both in one
top-right slot, each appearing only when a second thing genuinely exists: **Before / After** once a
repair has run, **Part / Mesh** once a mesh exists.

The promise is **the viewing direction is kept and the part is re-framed**, not that the camera
object is preserved. Those are different, and only the weaker one is honest: a CAD part and its
delivered mesh are not the same geometry. The mesh carries a far-field box and a ground plane the
part does not (`viewer.js:329-344` exists only to cope with this), may be in a different unit, and
installs a zoom envelope derived from cell statistics. Persisting a raw camera position across that
boundary puts the user inside the part. Store direction, up, and the framing fraction
(distance ÷ bounds diagonal); reconstruct against the new bounds.

The two toggles are **not** content swaps. Before/After and Part/Mesh are visibility flips between
two skins already loaded in one content. They must be instant and must not re-fit.

Consequence: the geometry check stops taking over the screen and stops locking the chat. Its form
becomes a card in the conversation — a path the code already carries as its fallback.

Scope correction, from the extraction survey. There are **five** vtk.js scene constructors, not
four: the flow stage, the thermal stage (a near-duplicate scene in the same file), the delivered
mesh viewer, the review view, and the delivered-thermal branch inside the mesh viewer. The FEA
"variant" is not a scene at all — it is a panel swap over the flow scene, which is today's only
working precedent for content swapping and cost about 70 lines.

The measured duplication is **450-550 lines out of ~3,900**, not the larger figure stated in
earlier discussion. Line count is therefore *not* the case for this work, and should not be used to
justify it. The case is: one WebGL context instead of N — `viewer.js` never disposes anything and a
session with three runs holds three live contexts against a browser cap of 8-16; one answer instead
of four to fit, resize, lighting, material and dispose, each of which currently differs visibly;
and camera continuity, which is impossible today.

The largest hidden cost is not JavaScript. `window._vdbg` is the UI test interface: 39 files under
`tests/ui/` drive the viewers through it, along with DOM ids (`gs-canvas-<sid>`, `v-canvas-<job>`)
that a single canvas cannot both carry. Budget for alias ids or a test rewrite explicitly.

The extraction order is the subject of its own plan: eleven small PRs, nothing user-visible until
the shared canvas unifies the lighting, and the permanent mount last. Two open PRs (#389, #377)
touch `geometry_stage.js` and collide with the middle of that sequence; the early and late PRs do
not.

### 5. Progressive mesh display

The product owner asked for the mesh to stream back so the viewer shows it arriving. The scoping
says a genuinely live, growing mesh is not available at acceptable cost, for reasons that are about
the meshers rather than about us:

- **Three of four engines write nothing usable mid-run.** cfMesh builds in memory and writes
  `constant/polyMesh` once; gmsh's 3D fill is one blocking C call with no re-entry; vmtk emits each
  artifact whole. Only snappy has intermediate state, and only because it writes a complete
  polyMesh per phase — which this repo currently suppresses by invoking it with `-overwrite` and
  `debug 0` (`engines/snappy/native.py:314,327`, `snappy_runner.py:987,1497`).
- **Mid-run `constant/polyMesh` is a trap, not a partial.** It holds blockMesh's background box. A
  naive reader would hand the user a hex box and call it their mesh.
- **Snappy's phases are not growth.** Castellated, snapped and layered are three different meshes
  of the same part, and the castellated frame looks visibly worse than the CAD part the user is
  already looking at. Shown in sequence it reads as the part being replaced twice.
- **Parallel runs need a `reconstructParMesh` per snapshot** — minutes of serial CPU charged
  against the mesh's own timeout, on exactly the largest jobs.

The transport, by contrast, is a solved problem and needs nothing new. The beacon already writes
`jobs/<op>/progress.json` into the GCS exchange every 15 s and the waiting worker already polls and
relays it (`runtime/mesh_runner.py:57-100`, `adapters/mesh_execution/gcs_exchange.py:48-55`,
`cloud_run_client.py:133-168`), identically on the Cloud Run and big-VM routes. A partial would
travel as a sibling object plus a pointer event.

What it must not be is an inline payload. A decimated boundary of a 2M-cell mesh is 2.5-5 MB; the
event log is a replayed Redis list, and the codebase already refuses to put images over 4 MB on the
stream at all (`trace/policy.py:22,122`). Separate artifact, pointer event, served like the
geometry-check skin already is.

**Decision taken: Phase 0 first.** The mesher already reports a live cell count, and `of`/`unit`
are already plumbed through `contracts/mesh_progress.py`. Showing that count as a progress bar
against the plan's estimate is days of work, no engine changes, and addresses the actual complaint
— that the screen feels dead during a long mesh. It ships with this redesign.

**Decision revised: Phase 1 is funded and built.** The product owner has since asked for the
snapshot frame, and it has shipped behind a flag, default off. This section's reasoning above is
unchanged — it is what bounds what was built.

What Phase 1 is, exactly:

- **One frame, snappy, serial only.** `MESH_PROGRESSIVE_SNAPSHOT_ENABLED` (default `false`). With
  it on, a serial `snappyHexMesh` is invoked **without** `-overwrite`, so each phase's mesh lands
  in its own numbered time directory; the castellated one (`1/polyMesh`) is read once, as soon as
  it is complete. With the flag off the invocation, the workspace and every consumer are exactly
  what they were.
- **The parallel path never snapshots**, by the rule above and not by accident: each rank writes
  `processor*/1/polyMesh`, so `1/polyMesh` never appears in the workspace at all, and the runner's
  watcher is self-gating on that. A frame there would cost a `reconstructParMesh` per snapshot.
- **`constant/polyMesh` is never read as a partial.** The reader refuses the path, not merely the
  habit: a partial comes from a numbered time directory or from nowhere.
- **The final mesh still lands in `constant/polyMesh`.** Without `-overwrite` it would not, so the
  last phase's mesh is promoted there and the intermediates removed, as the authoritative step of
  the run. A promotion with nothing to promote removes `constant/polyMesh` outright, so the run
  fails on a missing mesh rather than delivering blockMesh's background box.
- **The intermediate is deleted the moment it is read**, and in any case before the workspace is
  archived (`engines/return_scope`: a second copy of a 42 MB polyMesh is how a big job is lost on
  the way home).
- **Transport is the beacon, unchanged in kind.** The frame is a sibling object,
  `jobs/<op>/partial-castellated.json`, written by its own upload; the beacon carries only the
  key and the phase, and the event stream carries only a pointer (`mesh_partial`). It is served
  through the API, from the key the run itself published, so no signed URL and no bucket CORS.
- **Not built, and still unfunded:** a second (post-snapping) frame, the parallel path, and any
  engine but snappy. The kill criterion stands for them: if the castellated frame does not read as
  progress to a non-expert in review, the parallel path is not built.

**Known limitation, as shipped: the flag alone reaches nothing.** "Serial only" is not a
restriction on an edge case here — it excludes every environment this repo deploys.
`progressive.snapshots_enabled` requires `nproc < 2`, and `determinism.snappy_ranks` answers 8 on
both execution routes: `MESH_SNAPPY_RANKS` is blank, the Cloud Run mesh job is 8 vCPU / 32 GiB
(`deploy/gcp/scripts/bootstrap-env.sh`), and the big-machine VM runs the same image under
`docker run` with no `--cpus` at all. So `MESH_PROGRESSIVE_SNAPSHOT_ENABLED=true` on its own
changes nothing anywhere, and the `mesh_snapshots` tick in `deploy.yml` changes nothing on its own
either.

- **To see a frame today:** set `MESH_SNAPPY_RANKS=1` for the environment as well as ticking
  `mesh_snapshots`. The mesh is then serial, and slower in proportion.
- **Why it is not simply lifted:** a parallel run's castellated mesh exists only as
  `processor*/1/polyMesh`, one partition per rank. A frame of the whole thing needs a
  `reconstructParMesh` per look — minutes of serial CPU, charged against the mesh's own timeout,
  on exactly the largest jobs. That is the cost the serial rule refuses to pay silently.
- **What it would take:** either a reconstruction budgeted out of the run's timeout and skipped
  when the estimate does not fit, or a per-rank boundary read that decimates each partition
  separately and lets the viewer draw the union (no reconstruction, but a new reader and a
  payload that is several patches per rank).
- **Until then the silence is explained, not inferred:** a run asked for frames that is parallel
  logs the reason once, at info, naming `MESH_SNAPPY_RANKS=1`. The setting's own help text and the
  workflow input say the same thing, so nobody has to read `snapshots_enabled` to find out why
  turning the flag on did nothing.
- **A hazard to weigh before forcing serial on Cloud Run:** the mesh job's workspace lives in
  memory there (`adapters/mesh_execution/gce_vm.py:22`), the phase meshes are extra copies of the
  mesh in that same 32 GiB, and that container has already been OOM-killed at 16 GiB
  (`bootstrap-env.sh`). A serial mesh with kept phase meshes is the big-VM route's business, not
  the 32 GiB job's, for any case that is not small.

The UI contract is unchanged either way: the viewer's Mesh content is fed whenever something
exists, and the camera does not move. So the viewer work does not block on this.

### 6. Repair-only as an outcome

A deliverable choice — repaired geometry, or repaired geometry and a mesh — asked as one question
in the conversation, not a screen of its own. It sets a field on the session and is carried into
the approved intent.

The backend already models the outcome: `RepairJobStatus.repair_delivered` is a real terminal
state meaning "repaired geometry handed over with no mesh", distinct from `delivered`, with
`simulation_job_id` NULL. What is missing is a request path that reaches it —
`RepairJobRepository.create` is called only from tests. The mesh graph as it stands always runs
`repair_inspect → repair_attempt → repair_promote` as inner stages of a mesh job and cannot
express repair alone.

`triage.recommend()` already returns `mesh_as_is | conservative_repair | manual_cleanup |
ask_customer | abstain` with reasons and located targets, and its answer is currently discarded
except to decline. It becomes the evidence shown alongside the choice.

### 7. Colour discipline

The palette in `ui/css/tokens.css` is sound and is not replaced. The rules it is not currently
following:

- `--accent` means exactly one thing: what is happening now, and the single primary action. A
  full-width saturated accent bar on a download row is spending it in the wrong place.
- `--steel` carries measured values and technical linework. Numbers in a disclosure are steel.
- Status meaning is carried by a word and a mark, never by colour alone. This is already written
  in the token file and should be enforced in review.

## What is left to the builder

- The module decomposition of the one viewer, and the order of extraction.
- ~~The transport for progressive mesh, and whether the first increment is genuinely live or a
  snapshot at a stage boundary.~~ Settled in §5: a snapshot at the castellated boundary, carried
  as a sibling object plus a pointer event on the existing beacon.
- Whether the deliverable question is asked before or after inspection, or both. The evidence from
  triage argues for after; the billing story argues for before.
- Exact copy for every string. The register: state the measured thing, name what was decided, give
  the number. No line restates the user. No line apologises. No line reads as a log level.

## Verification

Per `AGENTS.md`: host gates first (`make lint typecheck test-fast check-fast`), then the branch to
the personal dev slug, then the changed behaviour exercised there against the real deployed API and
console — not "it deployed". A screenshot of the changed screen is part of the evidence for this
work specifically, because the subject is what the screen says.
