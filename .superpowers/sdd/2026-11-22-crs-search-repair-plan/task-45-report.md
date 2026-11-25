# Task 45 - the search's dedupe can throw away a correct repair

Verdict: Task 44 read it correctly. The fingerprint was orientation-blind, and fixed here.

## What it hashed before
`search._fingerprint(ws) = tuple(sorted(ws.ids.values()))`. Each value is `ws_kernel.fingerprint(kind, sub, quantum, idx)`
= `identity.fingerprint_of(kind, _centroid_and_box(sub))` for every face, edge and solid: kind + quantised centroid + box.
Nothing about orientation, shells, compounds, or closedness. It is a sorted multiset, so entity COUNTS by kind were
already covered.

## What it hashes now
`(tuple(sorted(ws.ids.values())), ws.signature)`. `signature` is new (`ws_kernel.shape_signature`), computed in the
worker for the shape at `ws_index` (from_brep) and at every `history_record`, carried through `Workspace.signature`
(copied by `fork`, set by `apply_history`). Record version unchanged (extra optional key; old/fake workspaces get `()`).
Contents: top TopAbs type; counts of compounds, compsolids, solids, shells; count of CLOSED shells (BRep_Tool.IsClosed);
sorted signed volume (`%.6g`) of every shell (shells inside solids and bare shells).
Why signed volume: reversing a solid/shell negates it; flipping one face changes it by twice that face's contribution.
Same geometry by two routes agrees to ~1e-12, far inside 6 sig figs.

## Collision classes checked
| class | old key | verdict |
|---|---|---|
| solid vs reversed solid | collides (proved: ids identical) | FIXED (volume sign) |
| orient_and_close output vs input | collides; op keeps every TShape so `after.ids == before.ids` | FIXED |
| bare shell, one face flipped vs fixed | collides | FIXED; first version (solid volumes only) MISSED this, test caught it, moved to per-shell volume |
| solid vs same solid in a compound | collides | FIXED (top type / compound count) |
| face / edge / solid counts | already distinguished (multiset) | fine |
| solid vs bare shell | differs (solid id present) | fine |
| open vs closed | usually differs via face/edge set; closed-shell count added for the rest | covered, no dedicated test |
| body count | solid ids | fine |
| one flipped face with zero volume contribution about the origin | volume unchanged | KNOWN GAP, exotic (coplanar with origin); not closed to stay cheap |

## Cost
Before: no kernel work (sort of strings). After: one extra in-worker pass per op result. Measured (native image, 3 reps
of 50): 67-entity box with 12 drilled holes: entities_of 1.11 ms, shape_signature 1.98 ms; plain box 0.32 vs 0.14 ms.
`history_record` already runs entities_of twice plus per-old-entity rows and each op takes seconds, so this is noise.
Parent side: one extra tuple compare. Two extra kernel calls were avoided: no new IPC, it rides on existing records.

## Two directions (tests/native/test_native_repair_v4_fingerprint.py, real OCCT + WorkerPool + real orient_and_close)
Orientation differs -> no dedupe: reversed solid vs forward; orient_and_close output vs its inside-out input (and equal
to a never-broken box); flipped-face shell vs fixed; solid vs compounded solid. Each asserts the OLD key collides first,
so none can pass vacuously.
Genuine duplicate -> dedupes: same box written to two files and after a no-op orient_and_close; the same inside-out
solid reached twice.
Result: 6 passed.

## Mutation
Restored `return tuple(sorted(ws.ids.values()))` in a throwaway clone: 4 failed (all orientation/layout tests),
2 passed (the two dedupe-still-works tests, as they should). Reverted.

## Task 42 end-to-end (`test_a_fixable_injected_case_ends_clean`, search_det and search_ai)
Passes, 2 passed. Probed on both base 7b44a1d9 and this change (search_det): both stop `clean`, `spent.ops` = 8,
`verdict_candidates` = 5, 8 candidate events. NOT identical on the happy path: base has 3 `duplicate` outcomes (including
an `orient_and_close` duplicate), after has 2 and the `orient_and_close` is now `evaluated`, i.e. one correct repair
that was previously thrown away is admitted as a state. Best path is S0,S1,S3 instead of S0,S1,S2 (an extra state exists),
same length, same score `[1,4,0.161,-0.0,-1]`. Op and verdict counts do not change because `n_verdicts` counts before dedupe.
This is the defect visible in a passing run: the old run silently dropped an orient_and_close result.

## Other tiers
`tests/unit/cad/repair_v4` in meshpipeline-validation:local: 595 passed. ruff on the 3 src files + new test: clean.
mypy on ws_kernel/workspace/search: no issues (validation image, not native).
Not run: the full unit suite and the known-failing three; no failure-id diff made (nothing outside the above was run, so
a diff would prove little).

## Commands (exit codes)
Container commands pipe the pytest output inside `bash -lc ... | tail/grep`, so `exit=0` printed by the host is the shell's,
not pytest's; results were read from the summary lines.
- git worktree add -b sdd/task-45 ... 7b44a1d9: ok
- native new tests (first run): 1 failed 5 passed (bare shell); after per-shell volumes: 6 passed
- mutation: 4 failed, 2 passed
- unit repair_v4 (validation image): 595 passed
- e2e fixable_injected (both arms): 2 passed; base-vs-change probe as above (probe lines lived in an uncommitted edit
  of the clone's e2e test, not in this branch)
- ruff=0, mypy=0 (validation image)

## Not done
Did not touch step_for, patience, k_propose, ops, inject.py, valid_kernel.py, assemble.py, limits.py. No model, GCP, or pkill.
