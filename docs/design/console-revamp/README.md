# Console revamp: design note (Phase 1)

Open `index.html` in a browser. Pictures of every screen are in `shots/`.
All data in the mockups is made up: a demo pump housing, no customer files.

## The goal

The console should be clean, calm and easy to read. While Hexera works, the screen should always move.

Who uses it: an engineer at a desk, often on a second screen, checking in on a run between other jobs.
They glance at it for a few seconds. In that glance they need three answers: Is it working? What is it doing? Is it done?

## The layout

There are three columns. Each column has one job.

1. **Left pane: Runs or Files.** One slot with a two-way switch at the top.
   - *Runs* is the slim nav: New run, Search, All runs, Usage, then recent runs. Each run has a small status dot, and a live run's dot pulses.
   - *Files* shows only what the user takes away: their upload, the case folder, the solver files and the report. Workspace files never show.
   - Rows appear as files are written, with a brief accent wash. Files still to come are shown in grey ("after checks", "at the end").
   - The panel icon hides the whole pane. The pane replaces today's icon rail.
2. **Conversation.** Short sentences from Hexera and a narration list, one row per thing it is doing. The composer sits at the bottom with status chips.
3. **Viewer.** The part, with one compact toolbar floating at the top: View, Section, Parts, Measure, Snapshot. Each button opens one small overlay. There is no button column on the right.

## Narration (screens 1 and 2)

- One row per piece of work. Each row has its own small icon, plain words, and a live number on the right ("239 of 418 faces", "1.23M of about 1.9M cells").
- The row that is running now is brighter and has a thin progress line under it. Done rows get a tick and their time.
- The steps still to come are listed in grey, so the user can see how far there is to go.
- Finished phases fold into one quiet row, like "Check and brief · 7 steps · 1 min 12 s ▸".
- Composer chips show the clock, the machine ("Mesh machine · 64 GB") and the usual time.
- If a stage has no news, the row still moves. The clock ticks, and a heartbeat line says what is still running ("Still filling the volume on the large machine · 34 min"). The screen never freezes.
- **No internals, ever.** The server sends a *kind* of work (read, measure, plan, refine, layer, fill, check, export, machine) plus counts. The words come from one phrase table.
  - Engine names, tool names ("snappyHexMesh", "castellating"), rule ids, prompts, model names and paths never reach the page.
  - Today's mesher stage labels do leak some of these (`engines/mesher_stage.py`: "starting snappyHexMesh", "(castellating)"). The new events fix that.

## Status icons (screen 5)

There are eight icons, all drawn new for Hexera on an 18 px grid:

- Reading: a scan line walks down a page.
- Measuring: a caliper jaw.
- Planning: a route draws itself.
- Refining: a hexagon splits finer.
- Layers: thin layers grow off a wall.
- Filling: hex cells light up in turn.
- Checking: a gauge needle settles.
- Writing files: a sheet rises out of a tray.

The icons are steel at rest, and only the moving part takes the accent. Each loop is slow (about 2 s) and small. All motion stops under "reduce motion".

## Result (screen 3)

- **Verdict in one line:** "Passed with notes. Your mesh is ready."
- **Four numbers:** elements, worst quality (with its floor), walls OK, time.
- **Downloads:** Everything (one zip with the case folder, solver files and report) first, then each file. The **Mesh report** is its own row.
- **Notes** and **Hot spots** are each one collapsed dropdown. Hot spots are grouped by kind ("Sharp inside corners 318 · Kept sharp. Cells are 4× finer there."). There is a "Show them on the part" link. The mockup shows the dropdown open so you can see the grouping. It starts closed.
- **Everything else moves into the Mesh report:**
  - limit tables;
  - cusps and micro-details with their coordinates;
  - the plan in words;
  - the full hot-spot list;
  - the per-solver checks.
- The report is a clean, self-contained HTML file that prints well to PDF, and it goes in the Everything zip. A server-made PDF can follow if we want one; it needs a PDF library in the image.

## Viewer (screens 4 and 5)

- **Toolbar:** View (fit, front, top, side, camera, axes), Section (axis, where, whole cells or flat), Parts (search, patches with colour, face count and show/hide, surface style, colour by patch or quality), Measure (distance, angle, cell size), Snapshot (background, size, scale, save).
- One overlay is open at a time. Esc closes it.
- When a patch is picked, a small chip at the bottom says what it is.
- **Part colour:** today's CAD body is `[0.74, 0.78, 0.84]`, a near-white blue-grey with ambient 0.32 (`viewer/geometry_stage.js` BODY). The new body is a mid-tone grey, about `#7c828a` = `[0.49, 0.51, 0.54]`, with less ambient (~0.22) and less shine (specular ~0.06). It is softer on the eyes, and the orange selection and the steel edges stand out on it.

## Colours and theme

- Every colour is an existing token from `ui/css/tokens.css`. Nothing new is invented.
- New parts read only a few new *role* tokens (`--pane-bg`, `--work-icon`, `--work-live`, `--overlay-bg`, `--part-body`...). Each one points at an existing token. A later theme change swaps one block.

## Server work the narration needs

These came out of reading the code, plus the cyclone-filter run on shared dev. That run showed "Planning the mesh… 84 min". Planning had finished in about 16 min, and the mesh was building on a big VM the whole time.

1. **FEA meshing does not announce itself.** `engines/fea_driver.drive` never publishes `meshing`, and it never installs `mesh_progress.relaying`. So the VM's beacon is read by `cloud_run_client._relay_progress`, but the report goes nowhere. The UI also drops a `mesh_stage` that arrives without a `meshing` first (`run_progress.meshStage` needs `meshInfo`). The run line sticks on "Planning". The fix is to publish `meshing` and relay stages on the FEA path, the same way `agents/builder/executor.py` does.
2. **The big-VM route (RUN-4, `adapters/mesh_execution/gce_vm.py`).** The worker narrates what it knows:
   - "Starting a large machine (256 GB)", from the mesh machine choice;
   - "Getting the mesher ready" while it boots and pulls the image;
   - after that, the beacon's stages and counts.
   The same goes for the Cloud Run job route, with "Starting a mesh machine".
3. **Real counts.** The beacon (`runtime/mesh_runner._beacon` with `engines/mesher_stage.read_stage`) also reads the element or cell count from the mesher's log, for gmsh, Netgen, snappy and cfMesh. It sends that count with the stage.
4. **Heartbeat.** If the beacon has no new stage for a while, the worker publishes a heartbeat with the elapsed time and the last count. The UI shows "Still filling the volume · 34 min" rather than freezing. The beacon already pulses; today the pulse never reaches the page.
5. **Planning sub-steps.** The planning stage sends counted steps: measuring walls (faces done of total), sizing fillets and holes, estimating cells. FEA planning gets the same.
6. **One typed event,** `progress {kind, phase, n, of, unit}`, made from one phrase table on the server. A test checks that no event text holds a path, an engine or tool name, a rule id or a model name.
7. **Files panel data.** It uses the deliverable list the job read already serves (the same list as the downloads). A new `deliverable` event appears as each file is published. It never uses the workspace `file` events.

## Phase 2: small PRs, in order

Each PR is green in CI, including the browser lane, has screenshots, and is tried once on dev-areen.

1. **Shell + sidebar.** The left slot with the Runs/Files switch, slim nav, recent runs with status dots, and hide/show. Next.js `sidebar.tsx` / `shell.tsx` / `sections.ts`, plus the run page frame. None of the open PRs touch these files.
2. **Narration + events.** Server side: the `progress` event, the phrase table, FEA meshing announce + relay (the cyclone bug), VM and Cloud Run machine steps, counts in the beacon, heartbeat, planning sub-steps. UI side: the narration list, the icons and the composer chips, in `render/run_progress.js` and `check_progress.js`. This touches `check_progress.js` and `composer.js`, which #350 also edits, so I rebase after #350 lands.
3. **Files panel.** Deliverables only, live as they appear.
4. **Viewer toolbar + part colour.** Five overlays in place of the side buttons, and the mid-tone material in `geometry_stage.js` and `viewer.js`. #334 also edits `geometry_stage.js`, so this goes after #334.
5. **Result + Mesh report.** The one-line verdict, four numbers, downloads, the two dropdowns, and the HTML Mesh report in the Everything zip. This goes after #351 (result wording) and #329 (per-pair joint list).

`ui/` and `apps/console/public/static/` stay byte-identical in every PR.
