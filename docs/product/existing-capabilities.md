# Capabilities the pipeline already has

Written 2026-09-06. A survey of what the backend can already do, much of which is not
exposed in the UI. Purpose: stop re-inventing features that exist, and give the frontend
a shopping list. Each entry says where it lives so it can be verified rather than trusted.

Status key: **LIVE** = wired end to end. **BACKEND ONLY** = implemented and reachable by
API, but no UI surfaces it. **PARTIAL** = exists with a documented gap.

---

## 1. Dispute / flag regions on a delivered mesh — BACKEND ONLY

The "draw a circle on the mesh where I want it better" feature already exists as an API.

`POST /simulation/{job_id}/dispute` (`api/v1/simulation.py`) accepts:

```
mode:    "rebuild" | "accept"
comment: free text, up to 2000 chars
flags:   [ { x, y, z, span, patch, note } ]   # span = radius of the flagged sphere
```

- **`x, y, z` + `span`** is literally a sphere drawn on the mesh — the circle-selection UI.
- **`patch`** names which boundary the flag sits on; **`note`** is per-flag free text.
- **`mode: rebuild`** = "this approach is wrong, build it differently."
- **`mode: accept`** = "I inspected it and my acceptance criteria were wrong — re-judge
  the SAME mesh against my corrected bar." (Does not rebuild; see
  `route_after_reviewer` in `pipeline/graph.py`.)
- Works on **failed** jobs too, as long as a mesh exists — the retry ladder giving up does
  not remove the user's right to judge what was produced.
- Bounded by `DISPUTE_MAX_FLAGS`.

**Frontend gap:** there is no picker. The whole round trip is typed and durable
(`user_dispute` in `contracts/pipeline_state.py`), it just needs a viewer that can emit
coordinates.

---

## 2. Volume refinement regions (boxes / spheres / cones / lines) — PARTIAL

The mesher can refine arbitrary volumes, not just surfaces.

- **cfMesh**: `objectRefinements` — `{name, type: box|sphere|cone|line, cellSize, geometry}`
  (`engines/cfmesh/cfmesh_runner.py::_render_object_refinements`, schema in
  `engines/cfmesh/authoring.py`). Box needs centre + lengthX/Y/Z; sphere centre + radius;
  cone p0/p1/radius0/radius1; line p0/p1. Intended for wakes, leading/trailing edges,
  junctions.
- **snappy**: `refinementRegions` with `searchableBox` / `searchableSphere`, in
  `snappy_runner.render_internal_case`. Now used internally for ports (fix #2), the seed
  bubble, and thin plates (fix #4).

**Today these are authored by the planner agent, never by the user.** A UI that let a
user drop a box or sphere would feed an input path that already exists on both engines.

---

## 3. Mesh fidelity tiers — LIVE (backend), thin in UI

`pipeline/enums.py::MeshFidelity`:

- `draft` — fast, cheap: geometry checks, boundary setup, early iteration
- `standard` — balanced default
- `max` — highest bounded detail the engine supports

`MeshFidelitySource` records whether the tier was the **user's** choice or a **default**,
so "the user asked for draft" is never confused with "nobody said." This is the natural
backbone for a "quick preview vs final mesh" toggle.

---

## 4. Multiple mesh engines — LIVE (selection), UNEVEN (coverage)

`engines/registry.py` discovers a catalogue; `implemented` marks the usable ones.
Present: **snappy**, **cfmesh**, **gmsh**, **vmtk**, **snappy_multiregion**.

- Intake proposes an engine and asks the user to confirm
  (`agents/intake/engine_selection.py`).
- **Caution:** intake can offer engines the corpus has never exercised. Only snappy and
  gmsh have been run at scale. cfMesh and VMTK are wired but unproven — a user choosing
  them is in untested territory.

---

## 5. Conjugate heat transfer / multi-region — BACKEND ONLY

`engines/snappy_multiregion/` exists as a full engine spec with its own gates
(`gates.py::_gate_cht_manifest_valid`, `regions_split`). Meshes a fluid + solid assembly
as coupled regions. No UI, never corpus-tested.

---

## 6. 2D planar meshing — LIVE (gmsh)

`engines/gmsh/driver.py::_mesh_planar`. The case's `dimensionality` is intake-declared and
**authoritative** — a spec that contradicts it is rejected rather than silently reconciled.
Rejects non-planar geometry with a measured reason (thinnest extent vs diagonal).

---

## 7. Extra export formats — LIVE (gmsh), unadvertised

`extra_exports` accepts `bdf` (Nastran) and `unv` (I-DEAS) alongside the default
`.inp` + `.msh` (`engines/gmsh/driver.py`, `_EXPORTS`). Free formats nobody is told about.

---

## 8. Symmetry patches — PARTIAL

snappy resolves declared `symmetry` patches, including multiple
(`engines/snappy/drivers.py` ~line 237-251). **Gap:** nothing DETECTS symmetry or offers
to halve the model — the external ANSYS review flagged a full model where a half model was
correct. Auto-detection is unbuilt; honouring a declared symmetry plane works.

---

## 9. Named boundary groups / patch contract — LIVE

The user names patches at intake; the engines carry those names into the deck, and a gate
(`check_contract`) refuses a mesh whose actual boundaries disagree with what was promised.
gmsh maps them to physical groups with roles; unassigned surfaces land in a named default
group rather than vanishing.

---

## 10. Viewer payload — LIVE, and extensible

`application/viewer_payload.py` builds the artifact the browser renders from, containing
surface triangles, `patches`, `parts`, and a `quality` block. Served via
`GET /simulation/{job_id}/surface`. Artifact type `viewer_data` is durable, so the API can
serve it after the workspace is purged.

**This is the natural home for a quality heatmap** — per-face metric arrays alongside the
triangles it already ships. The scorer already computes per-face non-orthogonality and
skewness (`meshscore/metrics.py`) and discards the arrays after taking max/count.

---

## 11. Quality scoring — LIVE (internal), not user-facing

`quality_audit/scorer/` computes per-face non-orthogonality, skewness, aspect ratio,
volume ratio and openness, cross-validated against OpenFOAM `checkMesh`. It already
identifies the single worst face (`scorecard.py::np.argmax(geom.skew)`).

Users never see any of it. Every number needed for a quality report or a heatmap exists.

---

## 12. Reviewer agent with visual inspection — LIVE

A reviewer agent renders and inspects the mesh before delivery and can FAIL it, sending the
builder back with targeted feedback. This is a genuine differentiator — it is not simply
the builder grading its own homework.

---

## 13. Retry ladder with honest reporting — LIVE

Up to 3 build attempts (`MAX_BUILDER_RETRIES=1` → +2). The pipeline records why each attempt
failed and which gate rejected it. Transient infrastructure failures are replayed separately
from mesh failures (`node_infra_retry`), and a deterministic repeat halts early rather than
burning attempts. A review that ends without a verdict on a mesh that already passed every gate
(it stalled, ran out of time, or its renderer or provider dropped out) is started again on the
same mesh (`node_review_retry`, `REVIEWER_RERUN_MAX`) instead of failing the job. If it still
cannot finish, the mesh is delivered with that stated - the review did not finish, and why - and
never reported as an internal failure (a dispute, whose flags need a judgement, stays a failure
that names the review).

**Nothing shows the user this story.** "Attempt 1 sealed a port; attempt 2 refined it and
passed" is available and unsurfaced.

---

## Shortlist: highest value per unit of work

1. **Mesh quality heatmap** — data exists, needs a viewer.
2. **Dispute/flag picker** — API exists, needs a coordinate picker.
3. **Attempt history in the UI** — data exists, needs a panel.
4. **User-placed refinement regions** — both engines accept them already.
5. **Draft-then-final using the fidelity tiers** — enum exists, needs UI + orchestration.

---

## Cancel a running job — LIVE

Added 2026-09-28. `POST /simulation/{job_id}/cancel` (`api/v1/simulation.py`; the authority is
`application/job_cancel.py`) ends a job its owner no longer wants, from `pending` through
`running`. The console shows **Cancel run** on the process card of a live run, behind a confirm
step with an optional reason.

One cancel, in one transaction: the worker is evicted (its execution fence is revoked and its
token cleared, so its late result is refused at every fence and by the transition table), the job
is marked `cancelled` with the reason, the terminal record is written, and the closing line
("Cancelled by you.") is enqueued on the terminal outbox. After the commit the queued Celery task
is revoked and the closing line is delivered to the stream. The owner's active-job slot and the
tenant's credit reserve are released by that same commit (a cancelled job is terminal, not active),
and nothing is charged - only a succeeded job ever is.

- Idempotent: cancelling a cancelled job answers 200 with `already_cancelled: true`.
- Refused with 409 once the job succeeded or failed - a delivered mesh or a recorded failure is not
  something a cancel may undo.
- A Cloud Run mesh execution already running is stopped by the worker waiting on it: that worker
  checks every 30 seconds whether the job is still its own, and on a cancel it calls the Cloud Run
  executions cancel API and stops waiting (`application/native_submission.py`,
  `adapters/mesh_execution/cloud_run_client.py`). The same happens when the worker is shutting down
  and hands the job back, and when it gives up at its own deadline (the mesh timeout plus 10
  minutes). The cancel request never waits on this, and a Cloud Run cancel that fails is logged and
  leaves the execution to finish on its own, unread and uncharged.
- Known limit: the worker keeps computing until its next fence check (a checkpoint write, the
  native accept, delivery). A worker that died outright (its VM gone without a shutdown) cannot
  cancel anything, so an execution it started runs to its own end.
