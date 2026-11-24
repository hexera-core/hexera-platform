# The det `remove_small` ladder cannot reach the oracle's 11.89 mm: a parameter-range ceiling, measured

LEAD-3's FYI on lane D's oracle FINAL: `532f9baa4a` is a ONE-step `remove_small {max_width_mm: 11.89}` at
`vol x1.000`, oracle-only. Asked whether det's ladder reaches 11.89 mm. It does not, and the arithmetic is exact.

**The part.** Measured from the v4 run's own `input.brep` for this file
(`out/snap-v4-dev_input-202611240851/shard-8-of-8/work/532f9baa4a9d/input_only/input.brep`): **diagonal 252.1 mm**.

**The sized ladder.** `ops.shells.det_sliver_widths` is `(1e-4, 1e-3, 1e-2) x diagonal`, so on this part the three
rungs are **0.025 / 0.252 / 2.521 mm**. The oracle's width is **11.89 mm = 4.7x the top rung**, i.e. `4.7e-2` of
the diagonal against a top ratio of `1e-2`. The sized sweep cannot propose it at any site, on any state.

**The one det path that could.** The AIMED row (`search.py:809`) proposes `1.05 x (length + width)` of a narrow
face at the site, which is not bounded by the diagonal - but only for the `MAX_AIMED_SMALL = 2` most elongated
faces of aspect >= `NARROW_ASPECT`, and only where a site located them. Whether such a face exists on this part I
cannot say from this run: it synced work BREPs but no trace, so the proposed `max_width_mm` values are not
recoverable (the one `remove_small` state's params are hashed into its filename).

## What I would change, and why it is safer than it was this morning

The ladder's three rungs are deliberate (a fixed candidate count per expansion), so the fix is a ratio change,
not a fourth rung:

- **(b), cheap:** top ratio `1e-2 -> 5e-2`. Three rungs, same candidate count, reaches 12.6 mm here.
- **(c), principled:** derive the top rung from the measured face widths AT THE SITE rather than from the part
  diagonal - the same move the aimed row already makes, applied to the sweep.

Either is far safer than it would have been before **B22**. The ladder's own docstring says "what bounds the edit
is the verdict, not the ladder", which was true for validity and meshing but had no floor under how much of the
part a wide removal could delete. B22 now refuses a delivery that keeps under 10% of the part or loses a whole
input body, and this oracle win is `vol x1.000`, so for files of this shape a wider ladder removes slivers and
is caught if it ever does more.

I have NOT changed the ladder: a ratio change moves every det proposal on every file, and it needs a ruling and
a fast-loop measurement, not a patch from a single file's arithmetic.
