# The record mismatch: five explanations measured and eliminated, and what is left

Wave 202611242110 excluded `0051_00518960` (sha 8c6a3f2186e7) in BOTH sequential arms with

```
ValueError: history row for E583 has fingerprint 'edge:8e9a735e55645d4a' but the workspace holds
'edge:e181c14497845870': the record is for a different shape
```

Containment shipped first (`d9acdd6f0`): a repair that cannot be applied is a skipped DEFECT with its own
reason (`record_mismatch`), never a lost file. Everything below is about the CAUSE.

## Reproduced

On that part's own input BREP, repairing its overlap cluster pair by pair as the arm does
(`lane-b24/` probes): pair 1 applies, pair 2 applies, **pair 3 raises**. It needs two kept pair repairs to
fire, which is why only the sequential arm reaches it, and only on a multi-pair cluster.

## Five explanations, each measured and dead

| # | explanation | measurement | verdict |
|---|---|---|---|
| 1 | a relocation aiming at a replaced entity (lane C's seam) | the workspace was CONSISTENT with its BREP at every step, and the repro never calls `relocate` | dead |
| 2 | stale workspace ids | 0 of 613 stored fingerprints differ from a fresh read | dead |
| 3 | the quantum moved | 6.4e-05 at every step; a fresh index of each file computes the same | dead |
| 4 | the two kernel fingerprint paths diverge (`entities_of` vs `_maps`+`fingerprint`) | both paths compared for all 613 live ids: **0 disagree**; 0 carry a non-identity Location | dead |
| 5 | the record's rows are computed over an INTERMEDIATE stage shape | instrumented the failing call: its `old` IS the file the previous op wrote, and the row's fingerprint is that file's own value at that index | dead |

So `history_record` is innocent: it was handed the right shape, read from the right path, and measured it the
same way the workspace does.

## What the instrumentation showed, and the open question

Index 130's fingerprint through the three calls of the failing run (`CAD_REPAIR_V4_DEBUG_RECORD=edge:130`):

```
call 1   old 1ff973edb63ed633  ->  new 93328bf40d47cc3f     (422 -> 424 edges)
call 2   old 93328bf40d47cc3f  ->  new e181c14497845870     (424 -> 421)   the workspace stored e181c1
call 3   old 8e9a735e55645d4a                               (421)          the record's value
```

Call 2's **in-memory** result carries `e181c1` at index 130, and that is what the workspace stored. Call 3,
reading **the file call 2 wrote**, sees `8e9a73` at the same index, with the same edge count and the same
quantum. `ws_kernel._verify_round_trip` exists to catch exactly that divergence - it compares
`entities_of(new)` against `entities_of(read_brep(new_path))` and raises `IndexInstabilityError` on any
difference - and **call 2 passed it**.

**And a repeat of the same pipeline did not reproduce it**: on a second run the equivalent state's file read
back as `e181c1` from both fingerprint paths, twice, with 0 of 613 ids stale. So the divergence between what
the writing op verified and what the next op reads is **INTERMITTENT**, which also explains why one file in a
15-file wave hit it and why both seq arms hit the same one (they run the same pipeline).

**The open question, stated for whoever owns `ws_kernel`:** how can a BREP that verified equal to its
in-memory shape at write time read back differently to the next op in the same run? Candidates not yet
separated: a write that is not byte-deterministic for a twice-Booleaned compound, and a cross-process read of
a file another worker wrote. The cheap defence does not need the answer - verify at READ time as well as
write time, by checking the record's old-shape rows against `ws.ids` BEFORE the op runs rather than after -
which would turn this from a refused record into a re-indexed workspace.

## Two lessons worth more than the bug

**The sequential arm is an instrument.** It has now found three bugs the frontier arms carry and never trip,
because repairing a cluster pair by pair is the only path that chains two Booleans and then a third.

**I named a `enum.py` in its own directory** while probing this, and it shadowed the stdlib `enum` for the
whole interpreter - the environment warns about a planted `json.py` and I walked into the same hazard with my
own file name. Cost one run. Name probe scripts after what they probe, never after a module.
