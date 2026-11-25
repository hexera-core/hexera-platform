# D21: the oracle sweep - which dev files our ops can fix at all, and with what

Owner-approved, up to 200 VM-hours. No AI. This is the design for LEAD-2's review; nothing is spent until it is agreed.

## The question, exactly
For every FAILING dev_v2 file (150 in PROVISIONAL_dev_v2.json, re-judged at the sweep's commit): starting from the
state the search starts from, does ANY deterministic op/params, 1 or 2 steps deep, make the file CLEAN or add full-panel
mesher passes; which op and params; and which files nothing touches. One table.

## Per file (one worker process, one vCPU)
0. **Judge the input** (engine form, FULL verdict, at the sweep's VERDICT_CODE_VERSION). Clean now -> row
   `clean_at_input`, stop. Pin the LINEAGE'S ORIGINAL diagonal D0 = `stages.diagonal_mm` of the INPUT workspace, once.
   Every size-relative parameter and the fingerprint grid use D0 (`identity.run_grid(D0)`, and
   `DetGenerator(diagonal=lambda ws: D0)`), never an op output's own diagonal (Lane C: a 441 mm part reads 1237 mm
   after an op that leaves free-form faces).
1. **Root = the staged healer's confirmed output** (what both search arms start from). Its full verdict is recorded;
   if staged alone made it clean, the row says so and the sweep still runs from the input to see what ops do alone.
2. **Candidates at a state** = the deterministic sweep with NO k cap
   (`DetGenerator(whole_part_defeature=True).propose(state, 10**6)`, every site, every rung, including D16 located
   defeature, D18 aimed unify/remove_small, C7 fuse/cut_by/trim_unbounded) + `run_stage(s)` for every stage +
   the grid extensions Lane C supplies (`oracle_grid.extra_rows(state)`; empty until C fills it). Sites capped at the
   top 6 by `sites._PRIORITY`; duplicate calls removed (the sweep already does).
3. **Depth 1**: apply every candidate to the root. Per candidate record: applied / declined (reason) / failed /
   timed out; the CHEAP verdict and its score; **the op's own output**: layer-1 valid before -> after (does it ever
   LOSE layer 1), volume ratio and tight-bbox ratio against the ORIGINAL (`BRepBndLib.AddOptimal`, no tolerance
   padding), face/solid counts, declared removals. A candidate whose calibrated cheap rank beats the root
   (`search._estimate`, the search's own rule) gets a FULL verdict: clean? full panel passes?
4. **Depth 2**: from the 6 best depth-1 states by calibrated rank, INCLUDING neutral ones that changed the shape
   and kept layer 1 (multi-op repairs - fill then sew - are neutral at step 1), regenerate sites and candidates and
   repeat step 3. No depth 3.
5. **Row**: input/staged verdicts; best state reached (clean, panel passes, valid); every candidate that made the
   file clean or added full-panel passes, with op, params, depth, path, and its gaming metrics; the owner's gaming
   rule flagged (clean with >25% volume change or >10% bbox shrink, both vs the ORIGINAL); layer-1 losses counted;
   coverage (candidates planned vs run, and why any did not run).

## The four scars, designed in
1. **Count asserted.** The shard manifest lists N shas; the collector REFUSES (non-zero exit, no table) unless it holds
   exactly N rows with matching shas. A killed or crashed file still produces a row (`killed`, `error`) - it is
   counted, never silently absent. Inside a row, candidates generated must equal outcomes recorded, else the row is
   marked `incomplete` and counted as such.
2. **Per-file kill in the harness.** Each file runs in its own child process under a hard wall (proposed 2 h, SIGKILL
   by the parent), plus a per-op timeout (300 s) and the engine's own member timeouts. Candidates are appended to a
   per-file JSONL and flushed one by one, so a killed file keeps its evidence and its row says where it stopped.
3. **The op's own output measured** (step 3), against the ORIGINAL's diagonal, volume and tight box.
4. **Off C2D us-central1.** Pilot on jig (5 files); the sweep on c2d-standard-16 VMs in us-east1 (84 vCPUs free),
   self-deleting, labelled `dev-crsv4`, inputs copied cross-region.

## Size and cost (from D15/D17/D20 measurements)
D15: ~8-23 rows per expansion with k uncapped, median run 28 s per candidate (op + cheap verdict), long tail to
250 s. Per file ~25 depth-1 + 6 x 25 depth-2 = ~175 candidates -> median ~80 min at one vCPU; the 2 h wall caps the
tail. 150 files x <= 2 h = <= 300 vCPU-h = <= 19 c2d-standard-16 VM-hours (~$15), far under 200. Wall clock with 5
VMs (70 slots, LPT shards by predicted cost): ~3-4 h. The 200 VM-hours buys a second pass if the pilot says depth 2
needs more than 6 parents or the grid needs widening.

## The split across three owners
- **Lane D (me): the harness, the launch, the count.** `oracle.py` (per-file child, kill, JSONL), the launcher
  (us-east1 VMs, LPT shards, manifests), the collector with the count assertion. Pilot on jig first.
- **Lane C: the grid and the ops' own output.** Owns `oracle_grid.extra_rows` - the parameter rungs and op choices
  beyond the det sweep (any op det cannot reach today, wider rungs where the det ladder is narrow) - and checks the
  volume/bbox/diagonal measurements are true-size. Can join at any point before the full launch; the pilot runs
  with the det sweep alone.
- **Lane B: the table.** Owns the analysis over the collected rows: the per-file table, the gaming flags, the
  "nothing touched" list with each file's failing members, and the derived det-generator rows and AI prompt hints.
  Starts on the pilot's rows.
Files are NOT split between owners: one launch, one manifest, one collector, so the count assertion has one owner.

## Open decisions for LEAD-2
- Root = staged output (as the arms) - or also from the raw input? (+1x cost; proposed: staged, plus the input only
  for files staged already cleaned.)
- Depth-2 parents: 6 (proposed) - more costs linearly.
- Per-file wall: 2 h (proposed).
- netgen: the sweep judges with whatever netgen_occ is at its commit; if the every-root fix (D20) is decided first,
  the sweep should run after it, or its clean column judges part of the part.

## Logged, not yet worked (placement)
- 68ef8f5e662d (58 bodies, input_only, no op applied): gmsh_alt periodic_surface names "surface 785"; its located
  fingerprint matches no input face (Lane C v3c triage: OLD 28 / NEW 1 on the classifier). Neither .5 (prod padding)
  nor .6 (grid) covers a mm member on an unmodified input. Possibly the same cause as 1c9153fac8 (0 placed for every
  member under both fixes). Next look: compare gmsh's surface 785 box with every workspace face box (multi-body STEP:
  gmsh may split/merge a periodic face across bodies, or number surfaces of a 58-body compound differently).
- v4 input rows (Lane C validation): OLD 54 / NEW 8 (13% impurity, was 1/29 on v3c). 5 of 8 are netgen_occ "netgen
  face N" locations (v4 is the first wave where netgen locates), 3 gmsh_alt (incl. the periodic case above). Since .6
  Netgen's Located already carries its face box and find_sites names it on ws.quantum, so the remaining miss is in
  the BOX Netgen reports (`geo.shape.faces[n].bounding_box`: likely padded or triangulation-based, not the kernel's
  tight-ish BRepBndLib box), or in face numbering of the .8 all-roots compound. Next job after the oracle table:
  location only, field-by-field on a dozen files, cache bump.
