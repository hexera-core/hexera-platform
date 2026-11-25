# SDD ledger — plan: docs/superpowers/plans/2026-11-22-crs-search-repair-plan.md

Spec: docs/superpowers/specs/2026-11-22-crs-search-repair-design.md (approved, §7 defaults binding)
Branch: pranavkannepalli/crs-v4-search-repair; base commit 19577310
Worktrees root: /Users/kitts/archductor/sdd-worktrees/

## Owner constraints (binding, from the dispatch brief)
- Never merge to main, never deploy. One draft PR for v4; a separate small PR for Task 10.
- CAD_REPAIR_ENGINE defaults to v3; prod posture untouched.
- Cost: T0/T1 free always; T2 smoke < $5; waves need --model-usd-cap ($50 normal, $300 checkpoint ×≤3, $250 held-out once); ≤ ~600 c2d VM-hours. Verdict cache + ledger/cap + launcher preflight land before the first paid wave.
- Native repair tier run locally before every merge into this branch.
- Held-out never used for tuning or memory.
- If the repair model route cannot reach a Claude model: stop that lane, report to owner. No silent fallback to gpt-5.6-luna.
- Owner-level decisions (design §7, or what "clean" means) escalate to owner.
- Account at ~89% of 7-day usage limit: lanes run sequentially in priority order, max 2 subagents at once until reset.

## Preflight (2026-11-22)

File-level conflict scan across day-1 tasks (1, 2, 6, 7, 9, 10; 8 after 6/7):

| Task | Writes | Overlap |
|---|---|---|
| 1 | repair_v4/worker_pool.py, kernel_calls.py | none |
| 2 | repair_v4/xcaf_io.py, verdict/valid.py; adds entries to kernel_calls.REGISTRY | kernel_calls.py with Task 1 — sequenced after Task 1 |
| 9 | repair_v4/workspace.py, types.py; adds kernel fns | kernel_calls.py — sequenced after Task 2 |
| 10 | cad/repair/model_author.py, investigator.py (v3) | none with v4 |
| 6 | v4_eval/pool.py, corpora/v4/pool.json | none |
| 7 | v4_eval/inject.py | none |
| 8 | v4_eval/reprobe.py, run_arm.py, collect.py; repair_corpus.py | none with 6/7 |

Ruling: Task 1 creates a minimal `repair_v4/__init__.py` (docstring only, no engine entry) — the plan assigns the real entry point to Task 20, but Tasks 1/2/9 need an importable package first. Cost if wrong: Task 20 overwrites one file.

Ruling: Tasks 1 and 10 run as the first concurrent pair. Task 10 is independent of all v4 code and produces the v3 gate before/after number the owner wants early; it must not import anything from repair_v4. Cost if wrong: none — they touch disjoint trees.

Ruling: Tasks 1 and 10 are T0-only (pure Python, no OCCT/gmsh). The native tier (T1) gate applies from Task 2 onward, and is run locally before each merge into the feature branch. Cost if wrong: a native failure surfaces one task later.

## OWNER BLOCKER (raised 2026-11-22, day 1) — Task 19 lane, not day-1 blocking

Hard rule 6 check, run on day 1 so it does not surprise the lane on day 3.

Findings in the tree at 19577310:
- `settings/inventory.py` ROUTE_MATRIX has five roles — intake, builder, visual_reviewer,
  summarizer, planner. There is no `repair` route. v3's repair AI rides an existing route.
- `adapters/model_inference/providers.py` dispatches three providers: deepinfra, deepseek,
  openai. There is no Anthropic adapter.
- `settings/providers.py` declares DEEPSEEK/DEEPINFRA/OPENAI api keys. There is no
  ANTHROPIC_API_KEY, and `PROVIDER_KEY` maps only those three.

So design §7.4's "Claude (strongest available) on the repair route, with images" is NOT
reachable today. Three ways forward, all owner decisions:
  (a) point OPENAI_BASE_URL at an OpenAI-compatible gateway that fronts Claude (smallest code
      change; needs a gateway and a key; image and tool-call fidelity must be proven);
  (b) add a native `anthropic` provider adapter + ANTHROPIC_API_KEY + a `repair` route row
      (most correct; a PR of its own, roughly a day);
  (c) change §7.4 to a non-Claude model (an owner-level spec change).
Per hard rule 6 the Task 19 lane stops rather than silently falling back to gpt-5.6-luna.
Tasks 1-18, 20, 21 are unaffected and proceed.

## OWNER ACTION — host toolchain missing, blocks the merge gate

Checked while handling Task 1's report. On this machine:
- no `.venv` in any checkout (`make setup` has not been run here), and no cp311 interpreter —
  only system python3.9 and homebrew python3.15, while pyproject requires >=3.11;
- `ruff` and `mypy` are not on PATH;
- Docker is INSTALLED (/usr/local/bin/docker, Docker.app present) but the daemon is NOT running.

Consequences: `make lint`, `make typecheck`, `make test-fast` and `make check-fast` cannot run
here, and neither can the native repair tier (hard rule 4) nor the T2 smoke slice, both of which
need the mesh image.

Simplest fix, owner-side: start Docker Desktop. Then `make validate TIER=unit` runs the hermetic
tier in the pinned validation image, and `make test-native-all` runs the native tier — no cp311
venv needed. `make setup` would also work but is heavier.

Ruling until then: implementers run T0 in their own throwaway venvs (adequate for pure-Python
tasks like 1 and 10), and NO task merges into the feature branch until the native tier and the
ratchet have been run in the pinned image. Cost if wrong: a batch of merges would need re-gating.

Ruling: Task 1's reported "9 new mypy ratchet errors in unrelated files" is treated as a
throwaway-venv artefact pending the pinned-image run, not as a Task 1 defect — none of the nine
is in repair_v4. Cost if wrong: a ratchet regression surfaces at the first pinned-image run.

## OWNER UNBLOCK (02:15 UTC) — both blockers cleared, parallelism lifted

Docker Desktop is up (10 CPUs, 7.7 GB). Mesh image built; `make test-native-smoke` exited 0, so
the native tier and the ratchet can now gate merges. The 2-subagent cap is lifted. Merging
reviewed tasks into the feature branch is authorised (still no merge to main, no deploy).
Task 19a added by the owner: an Anthropic Messages adapter behind the existing provider/router
interfaces, tools + images + prompt caching, fakes only, no key (the key is a pending owner
decision). Task 19 proceeds against that interface with a scripted fake model.

Task 6: re-review round 1 — all five findings ADDRESSED, no new Critical/Important.
  NIST/F360 now raise below a minimum (20/100), `nist_group` raises on an unmatched filename,
  dedupe logs its count and names both files when it collapses two members of one NIST group,
  and the manifest records `sources_included` + `deduped`.
Task 6: complete (commits a413607..cd369c6, review clean).
Task 6: minor (deferred): the minimums are not reachable from the CLI (no --nist-min/--f360-min);
  a genuinely smaller dataset needs a one-line constant edit.
Task 6: minor (deferred): `build_pool` used directly (not via `main`) logs the dedupe report only
  if logging is configured.
Task 6: OPEN — pool.json still covers ABC+OCCT only. NIST and Fusion 360 need network. Owner asked
  the controller to run that fetch itself after the fail-loud fix merged. Until then the candidate
  pool is an Onshape-derived monoculture, which is exactly the diversity gap NIST and F360 exist
  to close.

Ruling: Tasks 1, 6 and 10 were merged together and gated once, rather than gate-per-merge. They
touch disjoint trees (repair_v4/, devtools/validation/, cad/repair/) and the unit tier plus ratchet
in the pinned image takes minutes per run. If the gate fails, bisect the three. Cost if wrong: one
bisect. Merges: 41980ab8 (task 1), f9724f6e (task 6), 152727d1 (task 10).

Ruling: plan Task 8 was split. Task 8a (the shared harness's per-file wall-clock kill in
repair_corpus.py) is independent and unit-testable, and was dispatched while the checker was
blocked. The rest of Task 8 — reprobe.py, run_arm.py, collect.py and the split builder — calls
`cheap_verdict`, which does not exist until Task 5, so it waits for it. This also exposes a plan
defect: the plan's day-1 schedule has Task 8 committed and the re-probe LAUNCHED on day 1, but the
re-probe cannot run before Task 5 lands on day 2. The re-probe launch moves to day 2.

Dispatched in parallel after the unblock: Task 2 (branched from Task 1's head, so it has the pool),
Task 9 (same), Task 7, Task 19a, Task 8a.

Task 8a: implemented bda04f2b, DONE_WITH_CONCERNS (8/8 in the isolation file, no leaked processes).
Task 8a: review 1 — Needs fixes. No Critical; 3 Important.
  The SIGKILL mechanism is sound and the `crashed` path is provably unchanged (the timeout branch
  runs only on timed_out + SIGKILL and returns before the crashed code; with file_timeout_s=0 no
  timer is created). The test forks a real child that ignores SIGTERM, so only SIGKILL ends it.
  REAL BUG: a `timeout` row is not excluded where `CRASHED` is. `_scorable` (:1146-1148) treats it
  as scorable, but the killed child never scored it, so it has neither `returned_step` nor
  `meshability_score_error` — and :1163-1171 then makes collect REFUSE the wave. One timeout would
  fail a `--meshability` run, which is the fleet's mode. Same gap at :1021, :1195, :1257 and
  collect.py:210/:222. Sent into the fix loop with the full site list and a demand for coverage in
  meshability mode, since the current test does not use it.
  Ruling on the grandchild hole: NOT reachable today, so no process-group kill. The reviewer
  checked the four grandchild sites (bounded_child.py:279, mesh_probe.py:323, tolerant_probe.py:205,
  bounded_steps.py:92) — all use start_new_session=True, Popen defaults to close_fds=True and
  os.pipe() fds are non-inheritable, so no grandchild holds the pipe's write end and the read still
  sees EOF at SIGKILL. What remains is a bounded orphan burning CPU after a timeout. Fix is a
  comment recording the limit. Cost if wrong: a hang survives in a path nobody has built yet.
  Ruling: `--file-timeout-s` will NOT be added to v3_eval's HARNESS_FLAGS. The reviewer is right
  that nothing passes the flag today, so the protection is off in real runs — but that tuple is
  v3-shared and changing v3's eval behaviour is outside this slice. Booked instead as a
  REQUIREMENT ON THE v4 ARM LAUNCHER: Task 8's `run_arm`/`evalspec` must pass `--file-timeout-s`,
  and Task 21's wave launcher must too. Carry this into both dispatches. Cost if wrong: the first
  v4 wave runs without hang protection and one hung file burns a shard.
  Ruling: two Minors folded into the same round rather than deferred — the watchdog not cancelled
  in a `try/finally` (an unexpected `pickle.load` exception skips the cancel, and a reaped-then-
  reused pid could be killed), and the test's 0.5s window being CI-flaky. Both are cheap, both bite
  in a file the v3 fleet depends on today.
Task 8a: minor (deferred): a blocking waitpid when file_timeout_s <= 0 would keep v3 byte-identical
  (the poll adds 10 ms per file)
Task 8a: minor (deferred): no coverage of "child closed its pipe and then hung"
Task 8a: fix round 1/5 dispatched (Important 2 + the grandchild comment + 2 folded Minors)

Task 8a: re-review round 1 — all findings ADDRESSED, no new Critical/Important. The re-reviewer
  walked every site in the list and confirmed the `crashed` counts and lists are byte-identical to
  base (:1196, :1259, :1266, collect.py:222, :229), and that for a run with zero timeout rows
  collect.py is identical to base. The new test asserts `unscored_rows(rows) == []`, which IS the
  predicate collect.py:621 uses to refuse a wave — so it covers the refusal path, not just the row.
Task 8a: complete (commits bda04f2..7888090, review clean) on branch sdd/task-8a-wall-kill.
Task 8a: minor (deferred): `meshability_summary` now always emits `"timeouts": 0`, so v3's
  meshability.json gains a key (values unchanged; no consumer validates it strictly, and the
  neighbouring `crashed` key is also unconditional). Making it conditional would restore
  byte-identical v3 output.
Task 8a: minor (deferred): no test of `clean_good`/`clean_report` for a timeout row.
Task 8a: minor (deferred): the `crashed` count surfaces in collect.py:460 and
  meshability_report.py:146 notes/tables, but `timeouts` does not — visible only in the JSON.

## CONTROLLER ERROR, corrected (2026-11-22)

I reported "mesh image built, native smoke green — the gate is live". That was WRONG and no image
existed. Cause: I ran `make test-native-smoke 2>&1 | tail -30`, and in a pipeline `$?` is TAIL's
status, not the command's. The script had actually printed
"FATAL: mesh image 'meshpipeline-mesh:native' does not exist - build it with: make mesh-image"
and I read exit 0 as success. The same masking recurred on a second check before I caught it.

Impact: Tasks 2, 7 and 9 were dispatched being told the native tier was available. All three were
messaged with the correction, told NOT to start their own `make mesh-image` (one shared tag;
parallel builds thrash the cache), and told to report NEEDS_CONTEXT rather than fake a GREEN or
weaken an assertion to pass on the host.

Resolution: `make mesh-image` run to completion — `built meshpipeline-mesh:native`, exit 0,
verified by `docker images`. Then `bash tests/native/run_tier.sh smoke` redirected to a file with
the true exit code read separately: **17 passed, 370 deselected, 77s, exit 0.** The native tier is
genuinely live now. `meshpipeline-validation:local` also exists.
Note for every later run: the image is linux/amd64 emulated on this arm64 host, so native runs are
several times slower than native speed. Never read a piped exit status again — redirect, then echo $?.

Task 2: implemented 8213438c, DONE_WITH_CONCERNS (12 native in the mesh image + 9 unit).
Task 2: review 1 — Needs fixes. No Critical; 4 Important, every one a FALSE-FAILURE path (a good
  file marked invalid, which pollutes the failing set and corrupts the search reward):
  body_overlap failing closed when Common doesn't complete (tangent curved contact is where BOP
  Common most often fails) with the touching-compound, sub-tolerance and curved cases untested;
  worker timeout/crash reported as `brep_invalid`, indistinguishable from a genuinely broken file;
  face names/colours — the stated reason for the XCAF move — untested, with sub-shape handling
  resting on an untested assumption that TopExp face index survives write→STEP→re-read; and
  `brep_invalid` itself untested.
  Ruling: the face-sharing departure STANDS. The reviewer agreed on the merits — Common volume is
  the right mechanism, and a face-sharing exemption would be a heuristic that lets identical
  duplicate bodies through while doing nothing for sub-tolerance interpenetration. What the fix
  round demands is that the implementer COMMIT the curved-contact evidence it argued from in prose.
  Controller check, resolving the reviewer's ⚠️: ran `dir(BRepAlgoAPI_Common)` in the rebuilt image.
  Only IsDone / FuzzyValue / SetFuzzyValue — no HasErrors. The implementer's claim was true, and
  the fuzzy-retry fix is available.
  Ruling: two Minors raised into the round because both are false-failure bugs of the same class —
  the valid.py:142-146 label-diff setting `labels_changed = []` on a good file (giving
  round_trip_invalid with an empty detail), and the five-unit vocabulary making a caller's "in" or
  "inches" read as `units_changed`.
Task 2: fix round 1/5 dispatched, plus a process requirement: produce GREEN through `run_tier.sh`
  on the rebuilt image, not a bind-mounted src, since a bind-mount can mask a kernel-fn
  registration failure (kernel_calls.py:31-34 bottom imports).
Task 2: minor (deferred): valid.py:187 hides the round_trip_invalid CODE when another hard finding
  exists (round_trip_ok=False is still reported)
Task 2: minor (deferred): kernel_calls.py:31-34 bottom-import cycle with xcaf_io — works only
  because `register` is defined first; fragile to reordering
Task 2: minor (deferred): findings carry positional ids (S1, E4) with no mapping to XCAF stable
  ids, so downstream cannot name the offending body or face to a customer
Task 2: minor (deferred): check_shape has no per-check unit seam; imports inside loops; O(N²)
  label scan in xcaf_io._apply

Controller note: a stray `make mesh-image` was run from Task 2's worktree, briefly overwriting the
shared tag with a pre-change tree. Rebuilt from the main checkout (exit 0, verified).

## Task 6 — real source shapes, probed by the controller with network access

Both of Task 6's guessed fetchers were WRONG, and its fail-loud guards would have caught both.
- **NIST has no direct .stp/.step links at all.** The page links to Drupal `/document/<slug>`
  paths; `/document/nist-cad-model-ctc-01` resolves to a drawing PDF, not CAD. The real CAD is in
  three zips, all verified by HEAD: `/document/nist-pmi-step-files` → NIST-PMI-STEP-Files.zip
  (13,976,599 B), `/document/nist-ftc-ctc-pmi-cad-models` → NIST-FTC-CTC-PMI-CAD-models.zip
  (60,294,624 B), `/document/nist-stc-pmi-v4` → NIST-STC-PMI-v4.zip (51,125,067 B).
- **Fusion 360's real URL** is `.../segmentation/s2.0.1/s2.0.1_extended_step.zip` (HTTP 200,
  application/zip, 506,333,119 B; 42,912 STEP files), listed in the repo README under "Additional
  Downloads" — NOT in docs/segmentation.md, whose only URL is the legacy s1.0.0 set that contains
  no STEP files at all. A fetcher aimed there would have been wrong in a way no download error
  would reveal. Layout is nested (`breps/step`), not flat.
- **Fusion 360 STEP files are scaled ×10** by the data-exchange process (bbox longest side in
  [-10, 10]). Not to be rescaled, but it MUST be recorded: a fidelity bound of
  min(0.1 mm, 0.001 × diagonal) means something different on a part inflated tenfold.
- Licence: Fusion 360 is governed by an Autodesk-specific LICENSE.md in the dataset repo, not a
  standard open licence. The benchmark will be published, so the best-effort wording must be
  replaced with cited terms.
Dispatched to Task 6 as round 2, with the ×10 scaling, the licence requirement, streaming instead
of in-memory for a 483 MB archive, and a demand for the real per-group variant counts — the number
that says whether NIST actually delivers the cross-kernel diversity it is being added for.

## OWNER ESCALATION — Fusion 360 licence blocks 80% of the pool

Task 6's round-2 build produced a real pool: 53,799 entries (abc_fail 9,923, abc_pass 887, occt 33,
nist 44, f360 42,912), no sha256 duplicates. Reading the REAL licence terms, as instructed, turned
up a problem the best-effort wording had hidden:

**The Fusion 360 Gallery Dataset licence is non-commercial research only and bars redistributing
the dataset.** That covers 42,912 of 53,799 entries — 80% of the candidate pool. Hexera is a
commercial product and the benchmark is intended for publication. Two separate questions for the
owner: (a) may we use it for benchmarking at all, and (b) may the eval bucket and the published
report include or redistribute any of it. The manifest itself stores only names, hashes and URLs,
which is likely fine, but the downloaded corpus is a different matter.
Also relevant: ABC is governed by Onshape Terms of Use 1.g.ii, NIST by its page disclaimer, and
OCCT's dataset terms remain UNVERIFIED and are marked as such.

## NIST is weaker than the design assumed

Design §5.2 added NIST for "the same part exported through several translators: real cross-kernel
variants". The real archives give 44 STEP files in 16 groups — ctc-01..05 and ftc-08 with 4 each,
ftc-06/07/09/10/11 with 3, stc-06..10 with 1 each — and those are AP203/AP242 and PMI-level
exports from NIST's own reference pipeline, NOT different vendors' translations of one native part.
The cross-kernel-diversity premise is therefore not met by NIST. Sent back to Task 6 to state this
in the module docstring and in the manifest's source_notes, since the benchmark will be published
and 44 "translator variants" would be a false claim. Whether real cross-kernel diversity can be
obtained at all is an open question for the owner — defect injection (Task 7) was the other
mitigation, and four of its ten kinds are currently quarantined (below).

Task 6: round 2 implemented 57e51103. Re-review: all verified-shape findings ADDRESSED (streaming
  real, F360 minimum pinned at the documented 42,912, ×10 note in the manifest, slugs resolved via
  /document/, User-Agent carries no identifying info). Two left open → round 3 dispatched.
Task 6: Ruling — take the 33 MB manifest compaction NOW rather than defer it again. The licence
  string repeats on all 53,799 entries (~20 MB of the 33). Deferred at 10,843 entries, but every
  regeneration now writes another 33 MB blob into git history, so the cost grows each round.
  Intern licences into a top-level table with licence_id per entry; authorised the PoolEntry shape
  change that implies. Licence stays required and non-empty.

Task 7: implemented 81228495, DONE_WITH_CONCERNS (28 native tests).
Task 7: review 1 — Needs fixes. No Critical; 4 Important.
  Ruling: `pcurve_corrupt` STAYS infeasible. The OCCT STEP reader rebuilds pcurves on import, so
  every corruption read back valid at 1e-7; raising is honest and better than shipping a file that
  reads back clean. But the claim lives only in a docstring, so a canary test is required (corrupt,
  write, read back, assert valid) to pin the negative result against an OCCT upgrade. Plan
  deviation recorded: 9 of 10 kinds are implemented. Cost if wrong: one injector kind missing.
  Ruling: the four tolerance-only kinds (loose_reapprox, tolerance_inflate, seam_variant,
  precision_truncate) are QUARANTINED until their damage is proven. They assert `valid is True` —
  the same property layer 1 requires of a CLEAN file — and only that a reader tolerance exceeded a
  bare 1e-3 with no spec basis. Marked in KINDS so Task 8 excludes them by default; inclusion
  gated on layer-2 evidence. This is the one failure mode that would silently destroy every number
  the project produces: a failing set padded with files clean by our own definition.
  Also sent: `inject()` has no post-condition, so a no-op injector writes a byte-identical copy
  labelled `entities: []` (precision_truncate's regex only matches single-line entities — it WILL
  fire on real non-OCCT-written sources); and `calibrate`'s test is circular, building `induced`
  from the same prior it checks against.
Task 7: fix round 1/5 dispatched.

Task 9: implemented 8e5b5bf7, DONE_WITH_CONCERNS (19 unit, 1 native).
Task 9: review 1 — Needs fixes, one **CRITICAL**. Both concerns the implementer raised itself were
  confirmed:
  (1) CRITICAL — a 1:1 `Modified` entity gets a NEW id. A face trimmed, moved or ShapeFix'd in
  place, with its bbox changed by one 1e-6 cell, is re-minted. Most healing ops are 1:1 in place,
  so the ID churns on the commonest case — which is precisely the v3 failure this task exists to
  prevent and contradicts §5.3's "the proposer can keep referring to face 41". Fix: keep the parent
  ID when `modified` yields one child of the same kind.
  (2) Important — `fingerprint_of` hashes only entity type plus quantised bbox, so duplicate or
  coincident faces, duplicated solids and a coplanar disk/annulus pair all collide. `row_for` pops
  first-come, so one duplicate's row can bind to the other's ID with nothing raised; the wrong ID
  is retired and its label goes to the wrong children. Highest risk on defective parts — exactly
  what repair targets. `identity.refs_for` already marks such cases `ambiguous`.
  (3) Important — the unmeasurable-entity fallback names every such child `kind:#0`.
Task 9: fix round 1/5 dispatched. Op tasks 11-15 are HELD until this lands: they build directly on
  Workspace, and fanning them out over a broken identity scheme would spread the defect.

Task 7: fix round 1 implemented 081bd359 (34 native tests). Re-review: all four findings ADDRESSED,
  no new Critical/Important. The quarantine is real — DEFAULT_KINDS gives exactly the five
  structural kinds, and `calibrate` genuinely REFUSES a quarantined kind unless the case carries an
  `observed_family` from the gmsh re-probe. The pcurve canary corrupts, asserts BRepCheck invalid
  in memory, writes, reads back and asserts valid at <1e-6, pinning the negative result.
Task 7: complete (commits 8122849..081bd35, review clean) on branch sdd/task-7-inject.
Task 7: FINDING for the benchmark, not a defect: `loose_reapprox` measures 0.031 mm deviation on a
  70 mm diagonal — BELOW the fidelity bound of 0.001 × diagonal = 0.07. The re-reviewer's judgement:
  deviation size cannot make this kind meaningful at all, because the file reads back valid
  whatever the deviation and layer 3 measures against the DAMAGED input, where the deviation is
  invisible. Only a gmsh failure (layer-2 evidence) can justify it. Unproven, not sub-threshold.
Task 7: REQUIREMENT carried into Task 8's dispatch — Task 8 must consume `DEFAULT_KINDS`, never
  `KINDS`. Ruling: `inject()` itself does not enforce the quarantine (it accepts all ten kinds, and
  the unknown-kind error still points at `KINDS`), so a consumer iterating `KINDS` silently gets the
  four quarantined kinds and hits `InjectionInfeasible` on pcurve_corrupt. Enforcing it inside
  `inject()` was judged not worth another round when Task 8 is the only consumer. Cost if wrong:
  the benchmark includes unproven kinds, visible as a `status` field on every label.
Task 7: minor (deferred): `tolerance_inflate` and `seam_variant` still have no geometric
  measurement, only a tolerance proxy
Task 7: minor (deferred): the byte-identical post-condition is vacuous for the 9 OCCT-rewritten
  kinds, since write_step stamps a timestamp and counter; it only bites for precision_truncate
Task 7: minor (deferred): the loose_reapprox 0.01 threshold and precision_truncate drift bar were
  set after measurement, not derived from the spec (both disclosed in the code)
Task 7: minor (deferred): the five remaining family priors are still unmeasured guesses until the
  re-probe supplies observed_family

Task 6: rounds 3 and 4 implemented (7cfc00d4 honest NIST note + interned licences + compact
  manifest; 6e7a512f interned archive url and hash). pool.json 33.2 MB → 20.9 MB → 12.2 MB,
  manifest version 3, archives table of 15 rows, rebuilt with no network, 53,799 entries round
  tripping equal. Re-review dispatched over both commits.
Task 9: fix round 1 implemented a0105f3b. Took the STRONGER route: identity is now keyed by kernel
  index rather than fingerprint, so a fingerprint collision cannot mis-bind; a 1:1 Modified entity
  keeps its ID with Diff.modified[id] == [id]; children are no longer fingerprinted, removing the
  `kind:#0` name. Re-review dispatched, weighted toward new breakage — this is a re-keying made
  under fix-round pressure, kernel indices are positional and stable only within one shape, and
  unit tests DROPPED from 19 to 15, so the re-reviewer must account for the four deleted tests.

Task 6: re-review of rounds 3+4 — all findings ADDRESSED, no new Critical/Important. Verified the
  interning is lossless (read_pool rehydrates licence, url and archive_sha256; a dangling id raises
  KeyError rather than yielding empty; write_pool still refuses an empty licence or url), the v3
  rejection is right and nothing else in the repo reads pool.json, and the NIST note reaches the
  manifest with "NOT distinct vendor translations" and the STC single-STEP fact asserted by test.
Task 6: complete (commits a413607..6e7a512, review clean) on branch sdd/task-6-pool-manifest.
Task 6: minor (deferred): read_pool does not re-check emptiness on rehydrate (only matters for a
  hand-edited manifest)
Task 6: minor (deferred): _licence_id/_archive_id truncate to 12 hex and the write loop overwrites
  a table slot on collision without checking (4 and 15 distinct values today)

Task 9: re-review of fix round 1 — all three findings and the minor note ADDRESSED. The re-keying
  is sound: old indices are never used to look up into the new shape, because `now` and the child
  indices are computed inside the kernel by FindIndex of the old sub-shape in the new map.
  CONTROLLER ERROR corrected by the re-reviewer: I reported unit tests dropping 19 → 15 and asked
  it to account for four deleted tests. Nothing was deleted — test_workspace.py went 10 → 15, the
  19 was the whole repair_v4 directory, and the two changed assertions both got STRONGER (one had
  encoded the Critical bug). My misread, not a regression.
Task 9: TWO NEW Important items from the re-keying, dispatched as round 2 — both must close before
  tasks 11-15 build on this, and both are the same silent-mis-binding shape as the bug just fixed:
  (1) index stability across the BREP round trip is ASSUMED and spot-checked on one face only.
  history_record indexes `new` in memory, write_brep writes it, and the next op re-reads that file
  using the in-memory indices as its "old" indices — with nothing asserting the read-back map order
  matches. Asked for the kernel-side guard (re-read, compare per-index fingerprints, raise) over
  the test-side one: the test proves one shape on one day, the guard proves every shape the search
  ever forks. The informational fingerprints are exactly what this needs.
  (2) the record contract is documented but half-enforced: an old-format record dies on a bare
  KeyError, and nothing ties the record's old side to ws.brep_path, so a stale workspace yields a
  valid-looking record whose mismatched rows silently retire IDs. Asked for `old_path` + a
  `version` field. Five op tasks are about to emit these records in parallel from five different
  implementers — "documented" is not protection against five independent chances to get it wrong.
  Plus two contract Minors: `now` relies on IsSame, so any op that copies or relocates untouched
  geometry retires every ID (loud but harsh — must be in the contract); and the workspace.py header
  still says IDs are minted from the fingerprint, stale after the re-keying.

## OWNER DECISION (2026-11-23) — AI proposer runs on OpenAI, not Claude

Owner settled design §7 decision 4: the proposer uses the **strongest OpenAI model on the existing
`openai-api-key` route**. Rule 6 of the controller brief is amended — OpenAI is the INTENDED route
for v4, not a fallback. Task 19a (the Anthropic Messages adapter) is STOPPED and parked unmerged on
`sdd/task-19a-anthropic`; its worktree stays for a possible revisit. Docs updated: design §5.8 and
§7.4 each carry a one-line amendment, and the plan's Task 19 Step 3 now names OpenAI and records
19a as deferred. The $1.00/file and per-wave caps are unchanged, and the ledger must price the
chosen model correctly.

Model choice, from the repo's own evidence:
- Priced in `adapters/inference_telemetry/pricing.py`: `gpt-6-astra` (10.00/50.00/1.00),
  `gpt-5.6-sol` (4.00/20.00/0.40), `gpt-5.6-terra` (2.00/12.00/0.20), `gpt-5.6-luna`
  (0.20/1.20/0.02) — all per Mtok in/out/cached.
- In service in ROUTE_MATRIX: `gpt-5.6-terra` on builder, planner and visual_reviewer;
  `gpt-5.6-luna` on intake and summarizer. `astra` and `sol` are priced but used NOWHERE.
- Capabilities are declared per ROUTE, not per model (`Capability.TOOLS`, `MULTIMODAL` in
  contracts/model_routing.py), and the Responses adapter converts image parts generically
  (protocols/responses_request.py:33-41) — so the adapter does not gate by model.
RECOMMENDATION: `gpt-6-astra` — the strongest priced tier and a generation ahead of terra; being
priced, the ledger meters it correctly. FALLBACK: `gpt-5.6-terra`, the strongest model this repo
actually exercises, with vision proven in production through visual_reviewer.
CAVEAT the owner should know: astra has no in-repo usage at all, so its tool-use and image support
are unproven HERE. Before the first paid wave, Task 19 must run a single minimal probe (tools +
one image) on astra and fall back to terra if it fails. At 5× terra's input price and ~4× its
output price, the $1.00/file cap also buys proportionally less search per file.

## COST POLICY — where v4 stands after the OpenAI decision (from Task 19a's exit analysis)

Good news first: **all four openai models in `_PRICES` are priced today** (astra, sol, terra, luna),
so the ledger meters v4's actual route correctly and the $1.00/file cap IS enforceable. And the
OpenAI Responses path reports `cached_tokens` with no write premium, which retires the
cache-accounting problem entirely for the chosen vendor — that was an Anthropic-only defect.

The real gap that remains: **`unpriced_route_models()` is called only by TESTS**
(`test_no_configured_route_meters_at_zero`, `test_inference_telemetry_sink`); nothing in `src`
calls it. So a deployment whose env points a role at an unpriced model — a `*_MODEL` override, or
a newly released model id — passes CI and then meters $0.00 at runtime. `pricing.price_for` returns
(0,0,0) with one warning per process for ANY unpriced provider:model key, openai included.

REQUIREMENT carried into Task 19 and Task 21 verbatim: a fail-closed unpriced check must sit in
BOTH places —
  (a) per-call, at `router._cost_of` / `pricing.estimate_cost`, or before dialling in
      `routing.execute`, since that is the figure the per-file ledger and cap read; and
  (b) at startup, beside `missing_provider_credentials`.
Both, because startup alone misses overrides applied after boot, and call-time alone fails the
first file of a wave instead of failing the deploy.

Task 19a: STOPPED and parked. Its uncommitted unpriced-guard/cache-uplift work is being committed
on the branch as an explicitly UNREVIEWED, gate-not-passed WIP commit, so parked work survives a
worktree cleanup. Do not merge it without a fresh gate and review.

## POSSIBLE OWNER ESCALATION (being verified first) — shared faces may not survive STEP

Task 12 reports that **a shared face does NOT survive a STEP round trip, plain or via XCAF**: a
shared-face fragment written to STEP came back with 12 faces, none shared, because the writer emits
each solid's shell separately. If true, that contradicts a Global Constraint AND owner decision
§7.1 — "Touching bodies must share faces after the STEP round trip, so the output is written as one
compound with shared faces" — and it would force a real design change: either the checker
re-derives sharing after every read, or the clean definition stops requiring share-after-round-trip.

NOT escalated yet, deliberately. This codebase has now produced the claim "OCCT cannot do X through
STEP" twice and been wrong about it once, for an invisible reason: `Interface_Static` switches are
only registered once a reader or writer is CONSTRUCTED, so a `SetIVal_s` before construction
silently returns False. That produced both the bogus face-name conclusion and a real
`write.step.unit` bug. So Task 12 was sent back to probe, in a fresh process and checking every
`SetIVal_s` return value: `write.step.assembly` (0/1/2 — "emits each solid's shell separately" is
precisely what assembly mode does), `write.step.schema` (AP214 vs AP242), `write.surfacecurve.mode`,
and writing the fragment as ONE compound in ONE product rather than separate XCAF labels. Sharing
must be counted by IsSame/TShape identity, not by face count.
If it still fails with all statics verifiably set, it is a real kernel limit and goes to the owner
with the artifact. Also asked: does re-applying `conformalise` after the read restore sharing, and
at what cost.

Task 12: implemented b59333f7, DONE_WITH_CONCERNS (11 new native + 5 existing, exit 0). Found and
  fixed a real bug on the way: a Cut's history reports its tool solid as REMOVED, which retired the
  whole keeper body; Cut stages are now scoped to their own solid. Used BRepAlgoAPI_BuilderAlgo/Cut
  rather than BOPAlgo_CellsBuilder (histories compose) — accepted. Decline is represented as the
  same workspace plus `params["declined"]`, since the protocol defines no decline type — accepted.
  Admitted the TDD order was implementation-first with a collection-error RED — recorded, not
  punished; the GREEN run is what caught the Cut bug.

Task 2: round 3 — ALL findings addressed; re-review confirms the reversal is sound (a real
  write→STEP→read through production code, names AND colours asserted by id and by centroid, the
  written text asserted directly, and the mechanism pinned by a FRESH-process test). `_set_static`
  now raises on a False return for every parameter. Forcing MM on read is correct: it matches
  OCCT's default and the file's own unit is still reported separately, so units are preserved.
Task 2: complete (commits 8213438..fcdc21d, review clean). Merged at 137f7ead — the merge hit the
  expected `kernel_calls.py` conflict (Tasks 2 and 9 both appended registration imports); resolved
  by keeping ws_kernel, xcaf_io and valid_kernel, which is what the registry pattern intends.
Task 2: minor (deferred): the fresh-process test pins only the WRITER switch; the reader switch is
  covered only by `_set_static` raising and by the passing round-trip tests.
Task 2: minor (deferred): the subshape-name and cascade-unit statics stay set in the worker after a
  call (only `write.step.unit` is restored) — harmless, since every call re-sets them.
Task 2: RECOMMENDATION for the final review: a layer-1 check for a face that VANISHES in the round
  trip (face count, total area, volume, and label KEYS rather than names). Both reviewers agree it
  is a real layer-1 gap — a face lost inside a shell compound or an assembly currently goes
  unnoticed.

Controller error, corrected: the first merge gate mounted the tree READ-ONLY, so ruff and mypy
failed on their caches (exit 2 each) and the ratchet said outright "CANNOT CHECK ... mypy needs a
WRITABLE checkout". Those exit codes said nothing about the code. Re-run against a writable
`git archive` copy: **ruff All checks passed** on the merged tree.

## CORRECTION PENDING — the §7.5 face-name escalation below may be WRONG

The Task 2 re-review found that the face-name evidence never set the subshape-name switches, and
that this repo's own `cad/face_names.py:76-80` reads face names with
`Interface_Static.SetIVal_s("read.stepcaf.subshapes.name", 1)`, set AFTER constructing the reader,
with the comment "constructing it resets the STEP parameters to their defaults". That is the SAME
silently-ignored-static mechanism as the `write.step.unit` bug the implementer itself found this
round. So "OCCT 7.9.3 cannot carry face names" is unsupported: the loss may simply be a missing
switch. Sent back to set both switches after construction, check the SetIVal_s return value, and
re-run. The escalation below stands only if names still vanish with the switches verifiably set.

## OWNER ESCALATION (SUSPECT — see correction above) — design §7.5 and FACE names

Task 2's fix round, while writing the face-name test the review demanded, established that
**STEPCAFControl in OCCT 7.9.3 writes `ADVANCED_FACE('')`** — face names are not carried through
the STEP round trip, and the `*.stepcaf.subshapes.name` switches do not exist in this build.
The implementer did NOT weaken the check: the test is `xfail(strict=True)`, so it flips to XPASS
if a future OCCT ever carries them.

What survives: body/solid names, assembly structure, and face COLOURS — colours verified both by
id and by face centroid.
What does not: face names.

Why it matters: §7.5's stated reason for moving I/O to XCAF at all is "customers set boundary
conditions by name". Body names still work, so the decision is whether face-level naming was
load-bearing. Options for the owner: (a) accept colours as the face-level carrier and amend §7.5;
(b) carry face identity in a sidecar keyed by stable id, outside STEP; (c) treat it as an OCCT
limitation to revisit on upgrade. This is an owner-level change to what the checker promises, so
it is not being decided here.

Also found in that round, and worth noting on its own: `FindAttribute` on a label with no name
**segfaulted the worker**, so any STEP carrying a coloured but unnamed face would have killed it.
Now guarded by `IsAttribute`, with a regression test. That is a real crash the face-name test
flushed out — the demand for evidence paid for itself.

Task 2: fix round 1 implemented e55521d2 (54 passed, 1 xfailed, exit 0, from a branch-built image
  with no src mount). Re-review dispatched, with instructions NOT to read the xfail as a pass.
Controller note: Task 2 built `meshpipeline-mesh:task2` from its own branch rather than touching
  the shared tag — the right call, and `run_tier.sh` legitimately refuses a worktree whose digest
  differs from the main checkout. A clean confirmation still needs a rebuild from the merged tree.

Task 9: re-review of round 2 — all findings ADDRESSED, no new Critical/Important. The index guard
  is real: `_verify_round_trip` re-reads the written BREP, compares EVERY (kind, index, fp) whole
  and order-sensitively, sits on the first line of `history_record` (the path every op takes), and
  is shown able to fail by a deliberate wrong-file test. The record contract is enforced at every
  door through a single `_check_record` entry point, each check with its own unit test and a
  specific message. Re-reviewer's readiness verdict: SAFE for five parallel implementers.
Task 9: complete (commits 8e5b5bf..7fd85f2, review clean) on branch sdd/task-9-workspace.
Task 9: minor (deferred): RECORD_VERSION is hand-duplicated in workspace.py and ws_kernel.py
  (the parent cannot import the kernel module). A host-side unit test reading ws_kernel.py with
  `ast` and asserting equality would catch a lone bump in seconds instead of at the native tier.
Task 9: minor (deferred): `ws_kernel.py`'s header still describes records as keyed by fingerprints
Task 9: residual risk carried into tasks 11-15: an op that copies or relocates geometry produces a
  VALID record whose effect is to retire every id. Documented in the CONTRACT FOR OPS docstring but
  not detectable. Every op dispatch names it.

## Op lane fanned out — Tasks 11-15 dispatched in parallel off Task 9's head (7fd85f25)

Ruling on the registry collision: the plan has Task 11 create `ops/__init__.py` with the protocol
and REGISTRY while 12-15 add their own modules. Five tasks editing one file in five worktrees would
collide on every merge. So 12-15 were told NOT to touch `ops/__init__.py`: each module exposes
`OPS: tuple[Op, ...]` and registry wiring happens at integration. Task 11 was told the protocol
shape is FIXED because four siblings are coding against it, and to escalate rather than deviate
silently. Cost if wrong: one integration pass to wire five modules into the registry.

Every op dispatch carries the Task 9 contract verbatim — kernel-index identity, `history_record`
with untouched `old_path`, the IndexInstabilityError guard, and the IsSame requirement — plus a
named warning at the op most likely to break it: booleans for Task 12, in-place ShapeFix for 13,
face rebuild for 14, and sewing for 15 (BRepBuilderAPI_Sewing rebuilds topology wholesale, and the
staged healer depends on it).
Each was also told the verdict does not exist yet, to assert against concrete OCCT properties
instead, to state which assertions stand in for it, and NOT to build a competing checker.

## Task 19a — the cost cap cannot trip, which is a breach of the binding cost policy

Review: the adapter half is spec compliant and well built — additive dispatch with the three
existing providers unchanged, error mapping tested through real MockTransport responses rather than
inspected, caching asserted on the BUILT REQUEST BODY (markers on the final tool/system/message
element, which is the real mechanism), images checked on the wire shape, httpx isolated behind
`create_message` so an SDK swap stays one module. No credential anywhere in the diff. Leaving the
`repair` route row out was judged correct.
But: `pricing.price_for` returns (0,0,0) with one logged warning for an unpriced model, and the
adapter adds no check — so an `anthropic:<model>` call can spend real money while metering $0.00.
The repo's only guard inspects CONFIGURED routes, so it is dormant until a route exists, which is
exactly when the $1.00/file cap starts to matter. Cache writes also under-count (creation tokens
fold into input_tokens and price at the plain input rate, while writes bill ~1.25-2×).
Ruling: fail closed. Dispatched a fix raising ConfigurationError when no price is declared, naming
MODEL_PRICE_OVERRIDES — inventing no prices. A cap that cannot trip is indistinguishable from a
working one until the bill arrives, and the policy says these controls must be implemented, not
just followed. Also asked whether an existing role's *_PROVIDER could be pointed at anthropic by
env and bypass the startup pricing check.
OWNER: `ANTHROPIC_API_KEY` is already bound as a secret on the separate admin service — a key may
already exist. Whether one key serves both is an owner decision.

## Merge gate — ruling: targeted, not the full unit tier, per merge

The pinned-image full unit tier ran for over an hour without finishing and was killed. Cause: the
validation image is linux/amd64 emulated on this arm64 host, so the whole hermetic suite is
impractical to run on every merge. Ruling: the per-merge gate is ruff + `mypy_ratchet.py` + the
changed-area unit tests (`tests/unit/cad/repair_v4`, `tests/unit/dev`, the clone-parity file) in
the pinned image, plus the native repair subset in the mesh image. The FULL unit tier runs once
before the PR, not per merge. Cost if wrong: a regression outside the changed areas is caught at
the PR gate instead of at its merge.

Merged into the feature branch after review: 569d5eaa (task 6), 1e6cc71e (task 7), 4b247420
(task 8a), 00637245 (task 9) — all four merged clean, no conflicts.
Task 19a: parked at 53e10af1 (unreviewed WIP commit, gate not passed, explicitly marked).

## Gate, now with a real baseline (the number that retires the "~45 pre-existing" folklore)

Subagents asserted "~45 pre-existing failures, identical on base" all session and nobody checked.
Checked: `tests/unit/dev` on base 19577310 → **5 failed / 649 passed**; on the merged tree →
**5 failed / 725 passed**. The SAME five (`test_onboarding_contract` ×2, `test_repair_corpus_bundle`,
`test_v3_eval_staging` ×2), in the real repo with `.git` present, not a harness artifact. Zero
regressions, 76 more tests passing. Worker-pool tests pass 9/9 in isolation — the three failures
seen in the big subset run are interaction/contention under emulation, a CI liability to fix but
not a code defect.
Merged-tree gate: ruff **All checks passed**; ratchet **OK: 27 errors, all baseline, 0 new**;
repair_v4 unit **82 passed**; native smoke 17 passed.

Task 11: re-review — all six findings ADDRESSED, no new Critical/Important. The runtime guard runs
  first in `_finish` before any write, on all five ops, and genuinely compares old to new using the
  LEGACY noise bounds (1e-4 area, 1e-3 volume, adaptive integration) — verified unloosened.
  `extra_errors` is genuinely additive (read via getattr). pkgutil auto-import fails loudly.
Task 11: complete (commits 723c6f0..9a35344, review clean). Merged f9d4e76f.
Task 11: minor (deferred): `validate_call` raises OverflowError on a huge int (math.isfinite);
  `additionalProperties` as a schema object silently unenforced; valid→invalid guard branch untested.
Task 12: re-review — all findings ADDRESSED (4 carried to integration by its own terms).
Task 12: complete (commits b59333f..14af383, review clean). Merged 5af4dde3.
Task 15: review APPROVED; three fixes dispatched (sew destroying a closed solid undeclared; the
  2·area/perimeter width guard letting a face ~2× the threshold be removed AND declared
  fidelity-exempt; remove_small leaving a strip face with no signal).

## STEP-writer claim — what the committed evidence does and does not prove

PROVEN, committed: writing a 2-solid fragment with 1 shared face (11 distinct faces) emits **12
`ADVANCED_FACE` entities in the file**, across both writers × assembly 0/1/2 × AP214CD/AP242DIS ×
surfacecurve 0/1, plus `AddShape(makeAssembly=True)` and the ShellBased transfer, with every static
set after construction and every return asserted True, and statics restored in a `finally`. The
loss is therefore at WRITE time, not a reader split.
NOT proven: that the schema/assembly switches had any EFFECT on the written file (no FILE_SCHEMA or
product-count assertion — only that they were accepted); that the restore leaks nothing (the final
assertion re-reads a file written earlier, so a leak would not fail it); the per-solid-tool
mechanism; and anything beyond planar boxes or other `write.*` statics.
Safe to tell the owner: "OCCT 7.9.3's STEP writer does not preserve a face shared by two solids."
NOT safe: a general kernel or STEP-format claim.

## CONTROLLER ERROR found by integration — body ops were invisible

My own ruling (12-15 expose `OPS` rather than editing `ops/__init__.py`, wired "at integration")
left a live gap: Task 11's pkgutil auto-discovery registers only `@register` ops, so after merging
both, `ops.REGISTRY` held the five split ops and NOT `conformalise`/`remove_duplicate_faces` — the
modules imported fine and the ops were simply invisible. No test caught it, because each task's
tests import its own module directly. Found by running the registry import in the mesh image after
the merge, which is exactly why that check exists.
Fixed in e30900b5: auto-discovery now also drains a module-level `OPS` tuple into REGISTRY, with a
duplicate-name guard. Verified in the mesh image: 7 ops, 11 kernel fns, 82 unit tests pass.
Lesson for the remaining op merges (13, 14, 15): the same invisibility applies to them, and the
import check is the only thing that catches it.

## Lane fan-out (coordinator, 03:24 UTC) — 7 more lanes started in parallel

Dispatched: Tasks 3 (gmsh panel + FEA config extraction), 4 (Netgen, with the sanctioned
three-member fallback restated), 5 (faithful/Verdict/cheap/score/cache), 17 (sites), 18 (search),
19 (proposer + ledger/cap, gpt-6-astra with a terra fallback and a mandatory pre-wave probe),
20 (engine flag). Each dependent lane got real contracts and explicit instructions to FAKE, not
block. Task 8's re-probe stays gated on Task 5 — it is the critical path to the first measured
number and cannot wire to `verdict` before it exists.
Branch PUSHED to origin.

Task 13: complete (commits 3c1ed01..7f39132, review clean after 2 fix rounds). Merged 9220e1e1.
  Final verdict: trustworthy for smooth edges, NOT a proven bound — uniform sampling with
  local-maximum refinement can miss a narrow spike, and the kernel value is the only guard. The
  module docstring says so. Residuals recorded: `under_reported` is passive (the edge keeps its
  false claim and nothing escalates), face tolerance is capped not measured.
Registry after the merge, verified by import in the mesh image: **14 ops, 18 kernel fns.**

Task 18: review — Needs fixes. Found a bug that would have broken a HEADLINE number: the model
  budget stopped the WHOLE search, and `Budget(model_calls=0, model_usd=0.0)` with only
  DetGenerator tripped `0 >= 0` and stopped before the first expansion — **making a
  deterministic-only run impossible**, i.e. the `search_det` arm that `search_ai − search_det` is
  measured against. Also: `propose`, `on_rejected`, `judge.sites` and the confirm-step
  `judge.full` were unguarded, so one flaky call lost the whole run's best result; and the fake
  returned the SAME value for cheap and full, so no test ever had them disagree — the exact
  "fake that makes the search look like it works" risk. Fix round reports all five closed plus a
  `panel_passes()` accessor replacing the bare `score[1]`.
  Verified by the reviewer, not assumed: `WorkerPool.call` IS thread-safe (worker owned until
  `finally`, I/O only on a checked-out worker, `_all`/`busy` under `_lock`, excess threads queue
  without deadlock).

## Task 5 — my ruling on infrastructure failure vs verdict

The review found panel `timeout`/`crashed` scored as "unclean", inconsistent with layers 1 and 3
which raise `CheckerUnavailable`. Ruling, grounded in the clean-STEP definition ("meshes in every
mesher on a fixed panel, **within the normal time limit**"):
- `timeout` is a GENUINE non-pass — exceeding the limit is failing the definition.
- A mesher that CRASHED on the geometry is also a genuine non-pass; it has not meshed the file.
- The POOL dying underneath the mesher is not a verdict at all → `CheckerUnavailable`.
So `Verdict` gains an `inconclusive` marker for the third case only; the scoreboard excludes
inconclusive verdicts and `score` must not rank them as bad. Carried to Task 3 as a requirement if
its `MemberResult` vocabulary cannot yet separate "gmsh crashed" from "the worker died".
Cost if wrong: flaky infrastructure reads as broken CAD and the search learns to avoid slow states.

Other Task 5 findings sent back: the cache key depends on an OPTIONAL `config` attribute (a changed
gmsh option returns a stale verdict unless someone hand-bumps the version) — made fail-closed, with
mesher library versions added; an in-range but wrong declared id silently exempts the WRONG face,
letting a real deviation pass; `fill_too_large` measures the UNION bbox of all fills, rejecting two
legitimate small fills at opposite ends; and `score` omits `faithful.ok`, so an unfaithful state can
outrank a faithful one — which matters because Task 18 ranks on exactly that tuple.

## Task 14 — CRITICAL: the gated number was ~0 by construction

`rebuild_face` could move the trim **0.41 mm against a 0.0566 mm bound (~7×) and succeed**.
`_over_bound` read only `old_to_new`, `new_to_old`, `to_carrier`; `boundary_shift` was written but
not gated, and the area change was ungated. The one gated number was tautological — `to_carrier`
projects candidate points onto the very carrier the candidate was built on, measuring ~4e-6 by
construction. The test asserted only that number, with a fixture chosen to be detectable, so it
COULD NOT FAIL on the defect. Fix round demands the test fail until gated.
Also sent: per-face `_isolate` on multi-face replacement likely cracks shared edges (last `Replace`
wins, the other face keeps its copy, shell gains free edges); the validity gate is face-local so it
cannot see orientation-against-neighbours, closure or solid validity (reuse Task 11's `guard()`);
`ShapeFix_Wire` precision passed in UV units while named in mm, masked by a fixture that
reparametrises in mm; `fill_loop`'s `old_to_new` measure is ~0 by construction AND expensive, and
never bounds the fill face against its surrounding surface; unchecked OCCT returns; and the same
`try/except ImportError` shim Task 15 had, which can silently define a SECOND OpDeclined/OpFailed.

## THE PATTERN OF THE SPRINT — four checks that could not fail

Found in four separate lanes, each passing its own tests, each caught only because a reviewer was
asked "could this check ever fail?" rather than "does it pass?":
1. Task 13 — `tighten_tolerances` re-measured its own measurement against a bound derived from that
   measurement. Tautology; could not fail.
2. Task 18 — the search's fake returned the SAME value for `cheap` and `full`, so no test ever had
   them disagree, hiding a real heap-ordering problem.
3. Task 14 — `rebuild_face` gated only `to_carrier`, which projects candidate points onto the very
   carrier they were built on: ~4e-6 by construction, while the trim moved 0.41 mm against a
   0.057 mm bound.
4. Task 20 — the prod-posture guard scanned the deploy workflow's TARGET PICKER, while settings
   actually reach a deployment through the step `env:` block. The one automated thing standing
   behind "production posture is untouched" could not see a realistic regression.
Carry this into the whole-branch review: for every gate, assert it REJECTS something, not only that
it accepts the happy path. Task 20's fixed guard is the model — a 4-row injection matrix where
three cases fail and the legitimate dev-keyed case passes.

Task 5: complete (commits 6be2eae..c62b6b7, review clean except finding 1, which is now a
  REQUIREMENT ON TASK 3, not an open defect: nothing sets `error_kind="infrastructure"`, and the
  check fails OPEN, so until Task 3 marks pool deaths every one scores as a file defect). Sent to
  Task 3 verbatim, together with "every panel member must expose a JSON-able `config`" (the cache
  now fails closed without it) and the ruling that a Netgen segfault is a genuine mesher failure,
  not infrastructure. Merged e5953803.
Task 14: complete (commits 13aef23..04f2e6d, review clean). Merged 3d7e079e.
Task 14 FINDING for the eval report, not a defect: `rebuild_face` is "nearly vestigial" as a
  self-intersection fixer. Nothing is detectable below a ~0.069 mm trim shift; a 40 mm patch's
  bound is 0.057 mm; so the usable window is parts with a ~70-100 mm+ diagonal AND loops shifting
  between ~0.069 and 0.1 mm. Its success test passes only because a 300 mm distant plane raises the
  bound. The implementer disclosed this rather than loosening the bound — the right call.
  Ruling: KEEP it (it declines cheaply and its re-fill path has value) but DO NOT count it as
  repair coverage, and say so in the eval write-up rather than implying 15 working ops.
Task 20: complete (commits d1a7359..b1ed4cf1, review clean after one fix round). Merged a0dc3a6a.
  Includes the branch-regression fix: `HEXERA_TEST_KERNEL_FNS` now goes through the settings loader,
  verified to work in spawned workers (read at call time, workers re-import fresh).
Task 20: residual for the final review — the guard's allow-rule is a per-line substring test, so
  `CAD_REPAIR_ENGINE: ${{ inputs.x || 'search' }}` would PASS and resolve to `search` in prod on a
  tag push (where `inputs` is empty). Narrow, but a real escape. Also: the prod-arm slice is bounded
  by an arbitrary 250-line cap (the arm is ~161 lines today) — it fails loudly rather than silently,
  but a structural anchor on the matching `}` would be sounder.
Task 20: NOTE FOR PLANNING — `CAD_REPAIR_ENGINE` is NOT plumbed through `create-api-service.sh` or
  `startup.sh` the way `CAD_REPAIR_MESHABILITY_PASS` is, so **a personal dev slug cannot select
  `search` yet**. Nobody can exercise v4 in a real environment until that lands.

Task 4: APPROVED, conditional on one test. Did NOT take the sanctioned three-member fallback, so
  the panel is genuinely four meshers and the mesher-agnostic claim holds. The vertex-swap risk I
  flagged was analysed and dismissed on sound grounds: a fixed odd permutation maps valid→valid and
  inverted→inverted, a bijection, so it cannot mask inversions. BUT there is no test with a
  genuinely inverted tet, so the quality floor has no negative control — that is the condition.
Task 17: Needs fixes. The observation omits `h_mm`, so the proposer STILL cannot compare feature
  size against element size — the exact gap the v3 diagnosis named. Clash points and per-site mesher
  coordinates are dropped, and site ids are positional so they renumber between rounds, breaking a
  proposer's ability to refer back to a site it discussed.
Task 8 (re-probe + splits) dispatched — the critical path to the first measured number, now that
  Task 5's checker is merged. Carries three booked requirements: pass `--file-timeout-s` (nothing
  in the fleet does today, so the hang protection is off), the launcher preflight must refuse above
  ~600 c2d VM-hours, and it must consume `DEFAULT_KINDS` so the four quarantined injector kinds
  cannot pad the failing set.

## Spend so far
- Model (eval/fleet): $0.00
- VM: 0 c2d VM-hours

## Task log

Task 1: dispatched (worktree sdd-worktrees/task-1, branch sdd/task-1-worker-pool, BASE 19577310)
Task 1: implemented 94060780, DONE_WITH_CONCERNS (6/6 tests; concerns were environmental)
Task 1: review 1 (19577310..94060780) — Needs fixes. 2 Important, both on failure paths:
  (1) a failed respawn returns the dead worker to _idle and then raises ValueError out of every
      later call — the pool never recovers; (2) close() racing an in-flight call spawns a worker
      after _all was cleared, leaking it. Neither is tested.
  Ruling: the reviewer filed "test_timeout_kills_and_replaces only asserts the pid changed" as a
  Minor test gap. Raised to Important and sent into the fix loop — a changed pid does not prove
  SIGKILL, and SIGKILL-ing an unresponsive worker is the one guarantee this pool exists to give
  (design anti-goal: "a hang that a timeout can't stop"). Cost if wrong: one extra test.
  Ruling: mypy ratchet noise is not a Task 1 defect. Reviewer confirmed the config
  (ignore_missing_imports=true, check_untyped_defs=false) cannot produce errors from this diff.
Task 1: minor (deferred): `seconds` on timeout/crash includes kill+respawn time
Task 1: minor (deferred): unpicklable payload escapes `call` as an exception, not a CallResult
Task 1: minor (deferred): a worker found dead between calls is reported crashed with no retry
Task 1: minor (deferred): `send`/`recv` are not covered by timeout_s (tiny payloads, low risk)
Task 1: minor (deferred): `_Worker.kill` ignores a join(5) timeout, silently leaving a zombie
Task 1: minor (deferred): `close()` joins workers serially, up to 2s each
Task 1: minor (deferred): `__init__` leaks earlier workers if the Nth spawn raises
Task 1: minor (deferred): `os.abort()` in the crash test can leave core files / crash reports
Task 1: minor (deferred): no tests for close(), size>1, raising kernel fn, or slow spawn
Task 1: fix round 1/5 dispatched (resumed original implementer; 2 Important + 1 raised test gap)
Task 10: dispatched (worktree sdd-worktrees/task-10, branch sdd/task-10-gate-parity, BASE 19577310)
Task 10: implemented 2beb2ec5, DONE_WITH_CONCERNS (new clone-parity test 2/2; `pytest tests/unit/cad
  -q -k repair` 45 failed / 2486 passed, and the SAME 45 fail on base 19577310 — no regressions)
Task 10: review 1 dispatched (19577310..2beb2ec5). Implementer's three substantive doubts were put
  to the reviewer: stage_precondition still refusing clone-cured edits with no stage metadata;
  whether try_package / targets_for re-decline unanswered codes on the real session; and the cure
  being judged per package rather than per plan entry.
Task 10: review 1 verdict — APPROVED, no Critical, no Important. Verified: the carve-out waives only
  `op_does_not_answer`; `cured_on_clone` defaults to False in both `plan_entry` and
  `validate_package`; the signal is the real `meshes_after_target_remains` recorded by the clone;
  both directions are tested through the real `validate_package`. All three implementer doubts
  were resolved — (b) no downstream gate re-imposes the refusal (`op_does_not_answer` is raised in
  exactly one place and `targets_for` is never called on delivery), (c) the whole-package cure is
  correct because the plan IS the kept-edit log, (a) `stage_precondition` genuinely still refuses
  stage-less clone-cured edits but is out of this task's scope.
Task 10: Ruling — the reviewer's Minor 2 (nothing exercises `_conclude`'s wiring, so a dropped
  argument would silently restore the bug with all tests green) is being fixed NOW rather than
  deferred to the whole-branch review. Reason: Task 10 ships as its own PR and leaves that
  review's scope, and the gap would silently undo the exact fix. Cost if wrong: one extra test.
Task 10: minor (deferred, needs benchmark evidence): `stage_precondition` still refuses a
  clone-cured edit on a defect with no stage metadata. After the v3 wave, count packages still
  refused with `stage_precondition` among the 17 clone-cured files; a separate task if any remain.
Task 10: minor (no action): `_cured_on`'s `getattr(scratch, "edits", ()) or ()` is consistent with
  `_tried_on` beside it.
Task 1: fix round 1/5 implemented 0dca8359 (9 tests, 12 consecutive green runs). Failed respawn now
  leaves an empty slot filled lazily; spawn failure returns `crashed` rather than raising; a new
  per-worker `busy` flag stops `close()` closing a connection a call thread is polling. Scoped
  re-review dispatched (94060780..0dca8359), with the new concurrency flag called out for scrutiny.

Task 1: re-review round 1 — all findings ADDRESSED, no new Critical/Important breakage.
Task 1: complete (commits 9406078..0dca835, review clean) on branch sdd/task-1-worker-pool.
  NOT yet merged into the feature branch — held by the native-tier gate above.
Task 1: minor (deferred): a worker killed by close() can return via the `ok` path with its
  connection never closed — an fd leak only; the process is dead and joined. Fix by clearing
  `busy` and closing the connection under `_lock` in the `finally`.
Task 1: minor (deferred): `test_close_during_inflight_call_does_not_leak` uses a 0.5s sleep; not
  flaky (a late call returns `crashed` either way) but may not exercise the intended race.

Task 10: wiring test added 7ebab71a (test-only; parametrized over cure / no cure, calls the real
  `_conclude` with a spy on `validate_package`; 4/4). Mutation-checked: removing the
  `cured_on_clone=` argument from `_conclude` fails 2 of the 4.
Task 10: Ruling — no scoped re-review dispatched for this round. It was a controller-closed Minor
  outside the fix loop, test-only with no source change, and its effectiveness was demonstrated by
  mutation rather than asserted. Cost if wrong: an unreviewed test file, caught by the PR review.
Task 10: complete (commits 2beb2ec..7ebab71, review clean) on branch sdd/task-10-gate-parity.
  PR NOT yet opened, and the v3 before/after wave NOT yet run — both await the owner (the wave
  spends model budget on the fleet and the cap machinery from Tasks 19/21 does not exist yet).

## CRITICAL PATH: Docker now blocks most of the remaining day-1 work

Tasks 2, 7 and the native half of 9 are native-tier tasks: their TDD cycle runs real OCCT, gmsh
and Netgen fixtures inside the mesh image. With the Docker daemon down and no OCP on the host,
an implementer could write that code but could not run RED or GREEN on it.

Ruling: do NOT dispatch native-tier tasks blind. Writing OCCT code against unrunnable tests
produces exactly the unverified work the merge gate exists to stop, and v0.2.6 broke because the
native tier was skipped. Instead, day 1 continues on the tasks that are fully verifiable without
a kernel — Task 6 (candidate pool manifest: fetchers, dedupe, licence, NIST grouping; pure Python
with unit tests). Tasks 2, 7, 9 are held pending Docker. Cost if wrong: a day-1 lane slips to
day 2; the alternative cost is a lane of unverified kernel code.

Task 6: dispatched (worktree sdd-worktrees/task-6, branch sdd/task-6-pool-manifest, BASE 19577310)
Task 6: implemented a4136074, DONE_WITH_CONCERNS (4 tests, offline with faked fetchers).
  Committed pool.json has 10,843 entries / 5.1 MB, but from the committed ABC and OCCT manifests
  ONLY (abc_fail 9923, abc_pass 887, occt 33). NIST and Fusion 360 need the network and are absent,
  so the pool covers 2 of the 5 real sources. Implementer added an optional `group` field to
  PoolEntry beyond the specified fields.
Task 6: review 1 dispatched (19577310..a4136074). pool.json excluded from the review package (5.1
  MB would flood the reviewer) and replaced by a sampled head/tail. Reviewer asked to judge two
  things in particular: whether the NIST/F360 fetchers — built on an admittedly guessed page
  structure and a remembered URL — fail LOUDLY or quietly yield a short pool, and whether a
  committed 5.1 MB generated artifact covering 2 of 5 sources is right.

Task 6: review 1 — Needs fixes. No Critical; 4 Important, all one risk: a NIST or F360 fetch that
  yields nothing returns [] and `main` still reports success, so a requested source silently
  contributes zero and the pool is quietly short. Plus `nist_group` returning None unsignalled,
  and sha256 dedupe silently dropping a byte-identical NIST "variant".
  Reviewer independently verified the committed pool.json by source (9923/887/33, zero dedupe
  collisions) and confirmed the tests drive the real build_pool with no network path, and that
  F360 extraction is path-traversal safe (members are read into memory, never written to disk).
  Ruling on Important 4: keeping one entry per sha256 is CORRECT — dedupe by sha256 is explicit in
  the plan and two byte-identical exports are one file. The fix is visibility, not behaviour:
  report the dedupe count, and surface when a drop collapsed two members of one NIST group, since
  a "translator variant" that is byte-identical is itself a benchmark finding. Cost if wrong: the
  pool under-counts NIST variants in a way the dedupe log would reveal.
  Also folded in one Minor: write a `sources_included` provenance field, so a 2-source pool.json
  cannot be mistaken for the full one.
Task 6: fix round 1/5 dispatched (resumed original implementer; 4 Important + provenance field)
Task 6: minor (deferred): F360 loads the whole archive into memory; stream to a temp file instead
Task 6: minor (deferred): no retry or size cap on NIST downloads
Task 6: minor (deferred): pool.json uses indent=1; compact JSON would roughly halve 5.1 MB
Task 6: minor (deferred): the abc_pass/abc_fail split is trusted from the harvest manifests
Note: the Task 6 reviewer claimed task-6-brief.md did not exist. It does, intact. A reviewer-side
  read failure, not a missing file — its one ⚠️ about unverifiable interface wording is void; its
  code-cited findings stand.

Note: the 45 baseline failures corroborate the missing host toolchain above — they fail on an
  untouched base, so they are environmental, not caused by either task.

## Task 4 — merged (`edc2bf5f`)

NetgenOCC, the one non-gmsh member. Re-review: all four findings ADDRESSED, no new
Critical/Important breakage.

- The condition is met. `test_floor_passes_a_good_tet_and_fails_a_mirrored_one`
  (`tests/native/test_native_repair_v4_netgen.py:245-260`) writes a real `.msh`, calls the real
  `_measure`, and asserts a good tet `> 0` / `pass` and a mirrored tet `< 0` / `fail` /
  `low_quality`. The quality floor is now proven able to fail.
- Crash vocabulary settled and shared with Task 3: a Netgen segfault is `crashed` /
  `netgen_crash`, a vote against the file (Netgen is a child `subprocess.run`, so a −11 is visible
  while the worker survives). A pool death, a failed kernel call or a missing interpreter is
  `crashed` / `infrastructure`.
- Conflict resolved in `kernel_calls.py` (both import feet kept). `verdict/panel.py` arrives as
  Task 4's stand-in; **Task 3's `panel.py` and `MIN_SICN_PASS` are authoritative** and supersede it
  at that merge.

Three non-blocking notes carried forward:

1. `test_the_orientation_conversion_does_not_hide_a_mirrored_tet` (`:263-277`) is **tautological** —
   it applies its own `(a,b,d,c)` swap inside the test and never calls `_write_msh2`, so it reduces
   to the same two orderings as the first test. The "conversion cannot hide inversions" claim rests
   on the odd-permutation argument plus test 1, which is sufficient. Another entry in the
   checks-that-cannot-fail list, caught by a reviewer, not by the suite.
2. **A gmsh abort inside `_measure` reads as `infrastructure`.** `_measure` runs in the pool worker,
   so a hard abort kills the worker and the file reads `inconclusive` rather than as a vote against
   it. Arguably a file property. Not unsafe — it cannot read as a clean pass — but it is a hole in
   the vote count. Fix would be to run `_measure` in a child process as Netgen already is.
3. **Non-conformity is not detected at runtime on arbitrary files.** `free_faces` genuinely cannot
   gate it: a tet face used once is boundary whether or not the mesh is conformal, and the count
   carries no location, so conformal and non-conformal meshes give the same number. Conformity rests
   on `Glue(solids)` (`netgen_occ_script.py:61-64`) plus the native two-box test, which checks for
   free faces at x=10. If `Glue` fails to identify faces on a real file, the member passes a
   valid-looking mesh. A cheap guard exists (boundary face count or area against the B-rep) and was
   not taken; scoped out deliberately.

## Controller error — a truncated gate log

I ran the post-Task-4 gate as `pytest -q 2>&1 | tail -6` and then grepped the log for `FAILED|ERROR`
lines to diff against base. `tail -6` had thrown away all but five of the 39 entries, so the diff
would have compared a 5-line list against a 39-line one and "found" 34 fixed tests. Caught before
reporting. Re-ran without the pipe.

Same family as the exit-code-through-a-pipe error from day 1: **a pipe is not a neutral observer.**
The rule now reads: never pipe a log you intend to analyse, and never read an exit code through one.

## Zero-regression check on the Task 4 merge — clean

Same command on both sides (`tests/unit/cad/repair_v4 tests/unit/dev tests/unit/settings`), in
`meshpipeline-validation:local`, against frozen `git archive` copies.

| | HEAD `edc2bf5f` | BASE `33cdaefa` |
|---|---|---|
| counts | 27 failed / 1400 passed / 12 errors | 27 failed / 1400 passed / 12 errors |
| failure ids | 39 | 39 |

`comm -23` and `comm -13` both empty. **Zero regressions, proved on the lists.**

Counts alone would not have been evidence: two runs can agree on 27 and disagree on which 27. That
is the lesson from the Task 9 phantom regression, where I read a directory total as a file total.

Now running the gate on `main` (`a3225988`) and on the current branch tip to find out whether those
39 pre-date the branch or were accumulated by it. **I have never actually checked that**, and
"same as the previous commit" twenty-one times in a row is not the same claim as "same as main".

## Controller error — two agents in one worktree

I dispatched the Task 3 fix round into `sdd-worktrees/task-3` while the original implementer was
still running its `tests/unit/engines` no-regression comparison there. Two writers, one branch, one
report file.

Resolved by role, not by luck: the fix-round agent owns the tree and the report file; the original
is finishing only the engines comparison and reporting its result to me **in a message**, not by
writing the file. Both were told to leave processes and containers they did not start alone.

The generalisation: **a task is not finished when it reports — it is finished when its background
work is finished.** A `DONE_WITH_CONCERNS` whose own note says "still running" is not a free
worktree. Check for live children before re-dispatching into a worktree.

## Controller error — the gate was blind to ~35 tests for the whole sprint

My gate built its checkout with `git archive | tar -x`, which produces a tree with **no `.git`**.
`tests/unit/dev/test_v4_fleet_launcher.py:23` does `git -C REPO rev-parse HEAD`, so HEAD came back
empty, argparse rejected it, and every launcher test failed before reaching any code under test.
The settings certification suite failed the same way.

So my gate could never run, among others:

- `test_a_live_over_ceiling_wave_creates_no_vm[v3]` and `[v4]`
- `test_v4_over_ceiling_wave_is_refused`, `test_v3_over_ceiling_wave_is_refused`
- `test_allow_over_budget_waives_the_ceiling_and_says_so`
- `test_the_override_is_the_only_way_past_the_gate_live`
- `test_v4_default_sources_exclude_fusion_360_and_count_the_cleared_files`

**The budget refusals and the F360 exclusion — the gates in front of the first paid wave.**

Rebuilt on `git clone --no-hardlinks` plus `safe.directory`. The two pictures:

| | archive gate | real clone |
|---|---|---|
| HEAD `e39b3965` | 28 failed / 1484 passed / 12 errors | **4 failed / 1520 passed** |

Thirty-five of the thirty-nine "failures" I had been reporting as a stable pre-existing baseline were
**artifacts of my own gate**. The zero-regression comparisons stayed valid as comparisons — both
sides equally crippled, id sets identical — but they were computed over a set that excluded the
checks that matter most, and I did not know it.

The four real failures, and where they come from:

| id | main? | owner |
|---|---|---|
| `test_run_arm_drops_f360_from_the_manifest_visibly` | no | **Task 23** — real: `run_arm.py:96` raises `AttributeError: 'Namespace' object has no attribute 'sources'` |
| `test_no_production_module_reads_a_developer_setting_directly` | no | **Task 23** — real: `model_ledger.py:255` reads env directly |
| `test_the_settings_tier_passes_with_every_required_setting_hostile` | **yes** | pre-existing on main |
| `test_no_machine_specific_path_reaches_the_template` | **yes** | pre-existing on main |

Checked by cloning `a3225988` and running the two: both fail there as well. **So the branch adds
exactly two failures, both real, both booked.**

How it was found: Task 21's id diff showed one launcher test failing in the main workspace and
passing in its worktree with identical code. Chasing that — rather than accepting "environmental" —
led to the `.git` dependency. Three agents had reported variants of "looks environmental, same on
base"; each was locally reasonable and collectively they masked a broken instrument.

**The generalisation: a gate that cannot fail is worth less than no gate, because it is believed.**
That is the same defect class the reviewers have now caught ten times in the code — and it was
sitting in my own measurement apparatus the whole time.

## Branch health on `3f25cab7`, measured on the rebuilt gate

`tests/unit/cad/repair_v4 tests/unit/dev tests/unit/settings` in a real `git clone`:
**2 failed, 1601 passed, 2 skipped.**

Both failures fail on `main` (`a3225988`) too — verified by cloning main and running them:

- `test_onboarding_contract.py::test_the_settings_tier_passes_with_every_required_setting_hostile`
- `test_configuration_authority.py::test_no_machine_specific_path_reaches_the_template`

**So the branch now adds zero failures over main.** Task 23 closed the two it did add.

For the record, the same tree on the old archive-based gate read 28 failed / 1484 passed / 12 errors.
The difference is entirely the missing `.git`.

## Corpus fact that changes what the re-probe measures

`pool.json` v3, by source: `abc_fail` 9923, `abc_pass` 887, `f360` 42912, `nist` 44, `occt` 33,
**`inject` 0**. `sources_included: ['abc','nist','f360']`.

The cleared 10,887 is `abc_pass + abc_fail + nist + occt`. **The structural-injector stratum is
empty**, and `pool.inject_entry` has no production caller, so the re-probe as it stands measures a
corpus with no injected defects.

That matters because injection is the stated mitigation for the one gap the corpus cannot source:
no ABC/OCCT/NIST file reproduces a customer **translation** defect, ABC and DeepCAD are both
Onshape-derived, and NIST is AP203/AP242 exports from a single pipeline. Cross-kernel diversity is
absent from the corpus and injection was the plan for it.

Second-order consequence of the internal-only ruling: `stage.py:150` fetches with
`urllib.request.urlopen` and **no credential path**, while 7b's generator output was to be served at
`--base-url` for that unmodified stager. Injected derivatives living in a private bucket are
therefore unreachable by the stager as written — either it gains an authenticated fetch, or inject
entries are staged by another route. Small, but new, and created by the ruling rather than by a bug.

Recommendation given: run the re-probe without the injector stratum and state the limitation in the
report, rather than block the first measurement on a producer plus a stager change. Injection can
land as a second stratum without re-running the first.

## Instance twelve, and the first found by running the assumption

Task 3's `test_one_infrastructure_constant` asserted `module.X is panel.INFRASTRUCTURE_KIND` to prove
one shared constant. **CPython interns identifier-like string constants across modules**, so a
retyped `"infrastructure"` literal in `assemble` or `netgen_member` satisfies `is` regardless. The
reviewer compiled two separate modules each containing `X = "infrastructure"`, `exec`'d them, and got
`a.X is b.X` → `True`.

So the guard passes on exactly the duplication it forbids — and unlike the tests that merely skip on
this branch, this one stays broken **after** merge.

What distinguishes this from the previous eleven: it is not a fixture that guarantees its answer or a
threshold nothing can breach. The code is right, the intent is right, and the **language** quietly
makes the comparison vacuous. Reading the test cannot find it; only running the premise can.

Replacement: monkeypatch `panel.INFRASTRUCTURE_KIND` to a fresh non-interned string and assert the
other modules follow it — which proves they *read* the constant, not merely that they do not spell
it. Plus a literal scan of `verdict/*.py`.

The ledger of twelve, by kind:

- **Fixture guarantees the answer** (4): the cheap/full fake returning identical scores; Task 18's
  fake; `KindedEntry`'s invented attribute; Task 21's nested-by-construction `_fixed`.
- **Threshold nothing can breach** (3): Task 13's tolerance gate re-measuring its own measurement;
  Task 14's `to_carrier` ~0 by construction; Task 17's clustering guard falling open past 1500 pairs.
- **Wrong target** (3): Task 20's prod guard scanning the deploy workflow's target picker; 8f's
  `KIND_STATUS` loop with nothing feeding it a kind; `select_sources("")` returning the whole pool.
- **Sentinel silences the signal** (1): Task 5's `_ratio` 1.0.
- **Language semantics** (1): this one.

And one in my own apparatus: a gate built from `git archive`, blind to every test that shells out to
`git`, including every budget refusal and the F360 exclusion.

## Probe prerequisites, measured (ahead of running it)

The MinIO/GCS two-writer probe has to run against the real wave bucket before any paid wave. Checked
each precondition read-only rather than discovering it mid-probe:

| precondition | result |
|---|---|
| wave bucket readable | `gcloud storage ls gs://dev-crsv3-transfer-224734058693/` exit 0; `worker.env`, `eval/` present |
| `google-cloud-storage` on the host | **absent** — `ModuleNotFoundError: No module named 'google'` |
| `google-cloud-storage` in the mesh image | present, 2.18.2 (confirmed earlier under `--network none`) |
| GCS client + ADC from inside the image | **works, but only with the credential mounted as a file** |

So the probe runs inside `meshpipeline-mesh:native`, not on the host.

The ADC detail cost two attempts and is worth recording, because the obvious invocation fails:

- Mounting the whole `~/.config/gcloud` directory → `DefaultCredentialsError`, even with `HOME=/root`.
- Mounting `application_default_credentials.json` to a path and pointing
  `GOOGLE_APPLICATION_CREDENTIALS` at it → works. `bucket.exists()` True, listing returns objects.

Working invocation:

```
docker run --rm \
  -v "$HOME/.config/gcloud/application_default_credentials.json":/adc.json:ro \
  -e GOOGLE_APPLICATION_CREDENTIALS=/adc.json -e GOOGLE_CLOUD_PROJECT=hexera-dev \
  meshpipeline-mesh:native python devtools/validation/v4_eval/marker_probe.py \
  --store gcs:dev-crsv3-transfer-224734058693 --rounds 50
```

Note this proves the **controller's** ADC reaches the bucket. It says nothing about the VM service
account, which is a separate grant — and 8d's preflight prints that caveat on its own passing checks
for the same reason. The probe record the shards read back is what binds them.

## Gate method: bind mount verified sound, one hazard unexplained

Task 26 reported that bind-mounting a clone into `meshpipeline-validation:local`, or cloning from a
mount inside the container, made the launcher's `git cat-file` report commits as missing — 3-4
spurious REFUSE failures in `test_v4_fleet_launcher.py`. It ruled out `safe.directory` and object
file permissions and never found the cause, and worked around it with `cp -r`.

That matters because **bind-mounting a clone is my gate method**, so I checked it rather than assume:

```
git clone --no-hardlinks --quiet --branch <feature> . /tmp/bmtest
docker run --rm -v /tmp/bmtest:/w -w /w meshpipeline-validation:local \
  bash -lc 'git config --global --add safe.directory /w; PYTHONPATH=src python -m pytest \
  tests/unit/dev/test_v4_fleet_launcher.py -q'
-> 22 passed, exit 0
```

**No spurious failures on my method.** I suspected `--no-hardlinks` was the difference (a hardlinked
clone's objects being links into the parent repo, which a mount may not resolve), but both clones
are 80 MB with no `objects/info/alternates`, so that theory is unproven. The honest position:

- My gate's exact invocation is verified good, on the one test file most sensitive to git.
- Task 26's failure was real but its cause is unidentified.
- So the rule is to use **this** invocation, and to treat a REFUSE-shaped failure in the launcher
  tests as suspect until reproduced with `cp -r`.

This is the second time a gate's own plumbing has produced failures unrelated to the code. The first
cost ~35 silently-unrun tests. Both were found by chasing an anomaly instead of accepting
"environmental".

## The four-member panel was never four members (controller error, 2026-11-23)

`assemble.default_members("full")` returns `[GmshProd(), GmshDefault(), GmshAlt()]`. Its own
docstring says "Full: every member the panel defines (Task 4 adds Netgen here)". **Task 4 never did.**

I reported, when Task 4 merged, that "the four-member panel stands and the pre-approved panel=3
fallback was not needed." That was true of Task 4's member **existing and passing its tests**. It was
not true of the member being **in the panel**, and I never checked the difference — I verified the
component and reported the system.

What it cost in evidence, not just tidiness:

- The clean-STEP definition requires meshability on a **four-mesher** panel; Netgen-via-OCC is the
  only non-gmsh member, so it is the only thing separating "this file meshes" from "this file meshes
  in gmsh". Mesher-agnostic cleanliness is the premise the rebuilt benchmark rests on.
- Compounding it, Task 3 then found that until its fix **all three gmsh members shared the 2D
  algorithm** and two shared the 3D one. So the panel has been three configurations of one mesher,
  two of them algorithmically identical.
- Every statement I made about mesher-agnostic cleanliness rested on this, including when I argued
  the panel=3 fallback was unnecessary — the system was *already* running something weaker than
  panel=3's intent, with none of the fallback's visibility.

How it surfaced: Task 27's reviewer listed it as a ⚠️ aside ("`default_members("full")` still lists
three members; adding Netgen is not in this diff"). It was not its finding to press. I checked
because the four-member claim was mine.

Booked as Task 28 item 1, ahead of three test gaps, with the guard written to be
regression-proof: **assert the full tier contains a non-gmsh member by module or mesher family, not
by count** — a count test passes if someone adds a fourth gmsh profile and drops Netgen.

The generalisation, and it is the same shape as the gate that could not fail: **a component that
passes its own tests is not a component that is wired in.** Task 18b hit this too (`arm_generators`
with no caller, `_run_search` a stub), and Task 8f hit it (`inject_entry` with no production caller,
`KIND_STATUS` with nothing feeding it a kind). Four instances now of "built, tested, never
connected", and per-task review structurally cannot catch any of them, because each task was
complete against its own brief. The lesson for the controller: **for every delivered capability, ask
what calls it in production, and verify that caller exists.**

## The engine seam is closed (2026-11-23)

Four lanes merged against a frozen interface contract (`b9d333b3`), and the chain the wiring audit
found is now connected end to end:

```
read_step_xcaf -> Workspace.from_brep -> staged_start(run_staged) -> default_judge_for (cached,
four-member panel) -> arm_generators -> search -> deliverable -> write_step_xcaf | source bytes
```

`_run_search` no longer raises. All four audited orphans have exactly one production caller each,
verified by grep at the tip rather than asserted.

| lane | merge | what it closed |
|---|---|---|
| 22a | `5cd6a5fe` | entry/exit; `from_brep`'s first caller; passthrough proved by BYTES |
| 22d | `3eabd457` | renders gated and capped at 4/expansion; `search_det` renders none; a failed render is `view_unavailable`, not `judge_error` |
| 22c | `1da83267` | four arms dispatchable; the ledger actually reaches `admit`; exit 6 before any file without key/route/ledger |
| 22b | `a275bff2` | one shared verdict cache; `h_mm` derived, not invented |

### Freezing the interfaces first was the decision that made this work

Four lanes, each faking its neighbours, and **zero merge conflicts across all four.** The contract
was read off the tree rather than designed, so no lane had to guess, and the one rule that made it
safe — a lane needing a signature changed STOPS and asks — was exercised exactly once: 22a wanted
`defects`/`operations` on the delivered report, which needs a new `V4Result` field, and stopped
rather than editing a frozen signature. That is the rule working, not a lane being blocked.

### Two findings that outrank their lanes' scope

1. **The verdict cache could never hit.** The key is the STEP sha256; OCCT stamps `FILE_NAME` with
   the wall-clock second, and that was the only difference between two exports of the same BREP. So
   every lookup missed, across every process, always. The cache is what makes a 10,887-file
   re-probe affordable on a four-member panel. It was dead and nothing noticed, because a miss is
   indistinguishable from a cold cache. Fixed in `stages.step_for`; hit rate now proved by **mesher
   invocation count**, not wall-clock.
2. **A zero-tet mesh read as infrastructure** (`mesh_child.py:~242`, eager message build →
   `min(qualities)` raises → pool converts to `CheckerUnavailable`). A file the mesher produced NO
   VOLUME for is the most broken kind of file there is, and it was leaving the scoreboard instead of
   counting as a failure — so an arm's clean rate would improve as the hardest files vanished. Same
   error class Task 3 closed one layer up with the gmsh abort. Routed as task 30.

### Pattern worth keeping

Both findings came from a lane doing adjacent work, not from the task that owned the code, and
neither could have been found by reviewing either task against its own brief. The sprint's two
structural lessons now rhyme: **per-task review cannot see wiring, and per-task review cannot see
interactions.** The counters are an end-to-end run (the T2 smoke) and asking, for every capability,
what calls it in production.

## Smoke preconditions, verified rather than assumed (2026-11-23)

Task 22c wired the model route but had to leave two things unverified: whether the VM service
account can read the OpenAI secret, and whether v3's `openai-api-key` is the right one. Both are now
settled by reading IAM rather than by trying it at smoke time:

- `wave_spec.SERVICE_ACCOUNT` is `224734058693-compute@developer.gserviceaccount.com`.
- That exact account holds `roles/secretmanager.secretAccessor` on `openai-api-key` in `hexera-dev`,
  and the secret has version 1 enabled.

So `--model-secret openai-api-key` will work for `search_ai` on the fleet. No new grant, no new
secret.

Incidental, worth someone's attention but not this sprint's: **13 of that policy's 22 bindings are
`deleted:serviceAccount:` tombstones** from destroyed personal dev environments (`dev-pranav-api`,
`dev-crs-api`, `dev-kitty-api`, ...). Harmless, but it means the secret's IAM policy is nobody's
clean source of truth, and it is the accumulated residue of the per-developer environment pattern.

### The gate VM

`dev-crsv4-gate-1`, c2d-standard-16, `us-central1-a`, labels `deployment-id=dev-crsv4`,
`purpose=v4-gate`. **Spot was not available** - `PREEMPTIBLE_CPUS` limit is 0 in this project, which
is consistent with Task 8f's note that the v4 fleet VMs are STANDARD, not spot. So it costs about
$0.75/h rather than ~$0.20/h; a spot fleet would need a quota request with lead time.

Two process lessons, both mine:

1. **I provisioned capacity and then looked for work.** It sat idle ~35 min (~$0.44) while Task 43 -
   the most emulation-bound job in the sprint - ran on the slow host. Move the lane first, then
   create the VM for it.
2. **I cancelled the self-delete backstop before confirming its replacement was armed.** My first
   `pgrep` matched the ssh wrapper rather than a real process, so for ~30 seconds the VM had no
   backstop at all - the exact failure the backstop exists to prevent. Arm the replacement first,
   then cancel the old one. Verified afterwards: PID 66091/66093/66094, `sleep 7200` then
   `instances delete`, so it now dies ~20:58 UTC.

## 2026-11-23, the stretch that merged 52-56

Merged, each verified before the commit rather than after: **53** per-applied-op budget
(`b01ea293`), **54** det baseline completion (`0139d59a`), **56** the attempt counter and the
cold-start explanation (`57038cf6`), **52** the wave 429 brake and 5% share (`b0c6810c`), **55** the
walls, the excluded stratum and the marker probe (`7ae1a689`). Pushed; remote matched at each step.

### The gate had been blind for most of the sprint

`pytest tests/unit` **exited 2 from `4894733a` until `5b11250e`** — the whole unit tier did not run
at all in that window, so any "full tier green" claim between those commits was a subset run or was
wrong. Cause: there is no `__init__.py` under `tests/unit`, so every `conftest.py` is imported as
the bare module name `conftest` and the first wins; once `tests/unit/cad/repair_v4/` gained one it
shadowed `tests/unit/conftest.py` for the five modules that import `FakePublisher` from it.

That was the **fourth** gate-blindness bug of the sprint, after `git archive` stripping `.git`, a
relative `PYTHONPATH` resolving to the image's installed package, and a piped pytest log hiding 34
of 39 failures. All four share one shape: **the gate reported something other than the code's
behaviour**, and each was found by a lane declining to round its result up.

**Three of today's bugs are one bug.** `conftest` shadowing, a bare sibling import
(`from test_engine_entry_exit import`), and `import evalspec` in `project_reprobe.py` binding *v3's*
evalspec because five `v3_eval` modules claim that name — all bare top-level module names colliding
in `sys.modules`, all invisible until something ran a whole tier. This deserves a standing rule
rather than three local fixes; proposed to the coordinator, not imposed.

### The branch's honest tier number, for the first time today

**2070 passed, 18 failed, 2 skipped** across `tests/unit/dev` + `tests/unit/cad/repair_v4`. All 18
are in `test_validation_runner_isolation.py` (15), `test_onboarding_contract.py` (2) and
`test_mypy_ratchet_verdict.py` (1), and they are **inherited from upstream**: the same three files
at `main` (`a3225988`) in a clean worktree give 18 failed with the **identical id set**, `comm`
empty both directions. Two lanes had asserted "pre-existing darwin failures"; nobody had checked it
against a commit predating the branch until now.

### My errors this stretch

1. **I called a fix "safe" before testing it.** I grepped for `from conftest import` — the pattern
   matching my hypothesis — rather than for sibling imports generally, so the one-file fix broke a
   sixth module. The guard that caught it was running the modules *with and without* the change.
   Reasoning about safety is not a substitute for the two-sided run.
2. **I printed `PUSHED` unconditionally** while the push had been rejected non-fast-forward. Third
   instance of claiming success without reading the exit code. All success echoes are now gated on
   `$?`.
3. **I diagnosed a red test confidently and wrongly.** "Deterministic, not a load flake" was right
   about the determinism and wrong about the cause: cold-start *synchronous* work inside an awaited
   coroutine, which `asyncio.wait_for` cannot interrupt. So it is **position-dependent, not
   load-dependent**, and "re-run it in isolation" is the wrong probe for that class because
   isolation *is* the trigger. I also inferred a production defect from it; a real one existed
   nearby (`attempt or 1`, one attempt reported for zero invocations) but did not cause the red, and
   its blast radius was one log line plus one telemetry record — **zero wave-ledger rows**.
4. **I over-parallelised the host.** Four concurrent lanes each running the unit tier put a 10-core
   Mac at load 300-368, so every lane's gate became worthless and lanes correctly stopped. Same
   error as stacking lanes on one VM, which earlier understated Lane A's 2.6x. Lanes now run
   targeted tiers; the full tier runs **once, centrally**.
5. **I repeated a stale cost figure** ($0.20/h spot) in a task brief when this very ledger records
   `PREEMPTIBLE_CPUS = 0` and ~$0.75/h standard. Corrected mid-flight. The lesson from the stale
   audit quoted across merges has not fully taken: re-read the ledger per brief.

### Unexplained, and recorded rather than smoothed over

The reflog contains three operations on this branch I did not perform — a merge of `sdd/task-53` at
`b01ea293`, a detached checkout to `70dc52cd`, and a cherry-pick — which explain a push rejection I
hit. Only one process is resuming this session id (my own), so there is no evidence of the
second-instance hazard, and I am not inventing a cause. What **is** verified: `git diff sdd/task-53
HEAD` on source shows only my own later commits, so task 53's changes are faithfully present.

### Open decisions with the coordinator

- **The det/AI move-set asymmetry.** `run_stage` costs one patience tick for k ops, and stage bodies
  act on the **whole part at stage defaults** while det rows are site-local and capped, so for a
  part needing the whole-part call no det sequence reproduces it at any budget. Op-level coverage is
  necessary but not sufficient. Recommendation: give `DetGenerator` stage candidates.
- **The exclusion limit.** The walls are at the ceiling the 8 h run cap permits (1800 s; 2400 s
  gives an 8.37 h p90 wall), and the residual is 1.33-4.75% with a point estimate **above** the 2%
  limit. Recommendation: raise the limit with the stratum as its justification rather than add
  `--allow-excluded`, whose name invites reuse without the stratum.
