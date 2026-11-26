# CRS v5 beta: product wiring and UX (owner-approved, 2026-11-26)

## Decisions
- Ship v5 to prod as a beta, available to all users. Agents may merge PR 1 once verified in a personal dev slug.
  Human runs shared-dev deploy and the prod tag (AGENTS.md).
- Model spend cap: $5 per file during the beta.
- Incremental PRs after PR 1 (gap closing, time rebalance, re-surfacing, ...).

## Decision 1: mesh / repair / both (option A)
- A visible 3-way choice in the geometry-check form: **Repair + mesh** (default, today's behaviour),
  **Repair CAD only** (deliver a fixed STEP), **Mesh only** (skip repair).
- The intake agent may set it from chat ("just fix my STEP"); the form shows what it chose.
- New field on the dispatch payload: `repair_goal` = `meshing` | `standalone` | `off` (reuse `RepairGoal`).
- Repair engine for beta: v5 for every job whose choice includes repair; if v5 raises, v3 runs instead and the
  job records it. Mesh-only skips repair.

## Decision 2: repair shown as fully as the mesh
| Mesh view | Repair view |
|---|---|
| Live timeline lanes | Repair progress lane in plain language (below) |
| 3D viewer, quality heatmap, hotspots | CAD in 3D, defects pinned where found (fixed / not fixed / left as is), deviation heatmap (how far faces moved), before/after toggle |
| Key numbers | Problems found / fixed (by plain type), meshers passing x/4, largest change vs allowed, volume and size change, flags, time |
| Reviewer notes | Plain trace: problem -> what we did -> result; "left as is: reason" |
| Report + downloads | Repair report (HTML + JSON), repaired STEP, original STEP, mesh hints |
| Review / approval | Geometry changes beyond the auto-accept threshold (e.g. closing a gap) go to the existing review screen with before/after |

### Bypass permission
- An org setting (admins) and a per-run choice let a user auto-accept repairs and skip the review screen.
  Default: review required for changes beyond the threshold. The choice is recorded on the job.

## User-facing language (hard rule)
The UI never shows internal architecture: no "tournament", "entrant", "heat", "rule seed", "fallback", "conformal
pass", class identifiers, op names, or exception text. Lane lines read like:
- "Checking your CAD" / "Found 12 problems to fix"
- "Repairing a thin gap — problem 3 of 12"
- "Fixed" / "Couldn't fix this one — left as is"
- "Trying a broader repair" (the search fallback; never called fallback)
- "Checking the repaired part" (full mesher check)
- "Repair finished: 10 of 12 fixed"

Plain names for classes:

| Class | User text |
|---|---|
| close_faces | thin gap |
| overlap_self_intersection | overlapping parts |
| surface_crossing | intersecting surfaces |
| facet_crossing_local | folded surface |
| low_quality | tiny or sliver feature |
| open_shell_volume | hole or open surface |
| invalid_brep_tolerance | damaged geometry |
| closed_periodic_face | seam problem |
| unknown | problem we couldn't identify |

Admin views (the "why" page, admin repair queue) keep the full technical trace.

## Contract (PR 1 defines it, PR 2/3 consume it)
- `RepairResultV5` on the job result: `engine` ("v5"|"v3"), `choice`, `outcome` (repaired | partly_repaired |
  unchanged | unrepairable), `problems`: list of {`id`, `type` (plain key from the table), `location`
  {faces, bodies, points}, `status` (fixed | not_fixed | left_as_is), `reason_text` (plain), `change_mm`},
  `meshers_passing`, `meshers_total`, `max_change_mm`, `allowed_change_mm`, `volume_ratio`, `size_ratio`,
  `flags`, `seconds`, `spend_usd`, `needs_review`, `auto_accepted`.
- Progress events: the v5 event log is mapped to plain `repair_progress` events on the existing job WebSocket
  stream: {`stage`: checking | repairing | broader_repair | verifying | done, `problem_index`, `problem_total`,
  `problem_type`, `text`}.
