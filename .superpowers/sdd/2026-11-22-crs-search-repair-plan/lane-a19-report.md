# A19: the self_intersection family, and why no face op can fix it

Closed with ZERO files cleaned, all three of the owner's candidates measured, and nothing merged
(`3fcef6de8` carries a DO NOT MERGE prefix and should stay unmerged).

## The finding: the defect is not in the wires

`self_intersection` is TWO defects wearing one code, measured on the family's own 9 solo files:

| class | what it is | files |
|---|---|---|
| (A) | a face the kernel calls self-intersecting | 4 |
| (B) | a solid whose DIFFERENT faces cross, no single face at fault | 5 |

On class (A), OCCT's own wire analyser over the 15 self-intersecting faces (27 wires, 135 edges) found:

- **0 looping edges** - an edge crossing itself, the only thing a trim could take;
- 1 crossing edge pair, on eb3bc130 alone;
- **2 of 27 wires** that `ShapeAnalysis_Wire` calls self-intersecting at all.

So on 117ecb94 (26 self-intersecting faces) and 32ca3e9a, the wire analyser finds nothing wrong with the wires
while `BRepCheck_Analyzer`'s SelfInterMode says those faces self-intersect. **The face's own surface
self-intersects with a sound boundary.**

That retires all three of the owner's candidates on ONE mechanism rather than three separate negatives: (i)
rebuilding the face from its boundary keeps the same carrier surface, (ii) trims an edge, (iii) acts on wires - and
the defect is in none of those places. It also explains the decisive measurement: the op I built APPLIED on
eb3bc130 and left `faces_self_intersecting` at 2 before and 2 after, with an area change of 8.7e-08 and a boundary
shift of 0.0068 mm. There was no wire defect to fix.

What 4 of the files need is a SURFACE-level capability nobody has proposed: re-fit the carrier so it no longer
self-intersects, or remove the face and close the hole. The other 5 are lane B's (every boolean, by its own ruling
and LEAD-3's).

## Class (B): the obvious boolean does not clear it either

Read-only measurement, two straight OCCT call shapes per self-intersecting solid, then layer 1's `check_shape`:

| part | `Fuse(solid, solid)` | `BuilderAlgo` general fuse |
|---|---|---|
| 09f296a1 | BUILT, still self-intersects, 134.1 s | would not complete |
| 448333aa | BUILT, still self-intersects, 43.3 s | would not complete |
| f8cbd019 | BUILT, still self-intersects, 129.4 s | would not complete |
| 532be443 | no self-intersecting solid at all | - |

3 measurable, 0 cleared. SCOPE, because it is what makes the negative usable: this rules out the two most obvious
call shapes AT DEFAULT SETTINGS - no fuzzy value, no glue, no `SetNonDestructive` - and NOT lane B's guarded
version. "Same call shape, one target instead of two" does not survive contact with these parts.

## The harness pattern: an error counted as a zero is indistinguishable from a real negative

THREE harness bugs of mine in one stretch, all in THROWAWAY measurement code I was writing faster and checking
less than code I commit, and all three produced a PLAUSIBLE ZERO rather than an error:

1. `check_shape`'s keyword-only `booleans` passed positionally - every part errored and the summary printed
   `CLEARED: 0`. I had already told lane B a result was coming, and that zero would have been read as "a self-fuse
   has no target" by the lane about to build one.
2. a `TopoDS_Shape` handed to `Tolerance_s` uncast - every part errored and the summary printed
   `LOOPING EDGES: 0`, which is exactly what the real answer turned out to look like.
3. backticks in an inline message to the lead - the shell executed a word out of the middle of it.

`CLEARED: 0` and `LOOPING EDGES: 0` are precisely what a true negative looks like, and negatives are what every
lane is producing. THE FIX IS NOT A RESOLUTION TO BE CAREFUL:

- a summary REFUSES to report a total when any row carries an error, naming the errored rows;
- the total is printed only AFTER that refusal check, because the first version printed the false zero ABOVE the
  refusal, where a reader looks first;
- every harness asserts one row per input and refuses a silent short count (the earlier `*.ste*` glob dropped a
  control file and reported 39 rows for 40 inputs);
- prose with backticks, quotes or dollars goes to a FILE, never an inline shell string.

This is the fourth time in one night that a lane's own TOOLING corrupted a result or a report - with the `*.ste*`
glob, the unbuffered `grep` that held 13 minutes of rows in a pipe, and lane B's `enum.py` shadowing - and every
one was caught by the lane that caused it. The pattern is worth more written down than any of the four.

### Re-check of the numbers reported BEFORE those guards went in

Asked for by LEAD-3, because both were load-bearing for lane B. Re-read from the stored JSON:

- the (A)/(B) classification: 9 rows, **0 with an error field set**, none with a null count; recomputed 4 with
  faulty faces and 5 without - unchanged.
- the op's application table: 9 rows, **0 harness errors**, 8 declines every one of which is a real `OpDeclined` /
  `OpFailed` from the op rather than a harness failure; applied 1, `self_intersection` removed 0 - unchanged.

Both stand. The two runs that DID error were caught, refused, fixed and re-run before their numbers were used.
