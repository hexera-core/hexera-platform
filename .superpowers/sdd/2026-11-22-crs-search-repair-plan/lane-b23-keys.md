# Lane B23: which verdict key survives what, and the two savings that were already there

Measured on this host with real OCCT, on **6 real dev part BREPs** from `snap-runs/v3-202611240544/states`
(6 to 225 faces). The probe is `lane-b23/probe_keys.py`; one row per part.

| part | two STEP exports of one BREP | STEP sha survives a STEP round trip | BinTools digest survives it | faces |
|---|---|---|---|---|
| 0df08227f52a | SAME | DIFFERENT | DIFFERENT | 62 |
| 2f3034493d1d | SAME | DIFFERENT | DIFFERENT | 6 |
| 52cd2fdb5dc3 | SAME | DIFFERENT | DIFFERENT | 225 |
| 615e4cde0518 | SAME | DIFFERENT | DIFFERENT | 216 |
| a3cbae214aa3 | SAME | DIFFERENT | DIFFERENT | 223 |
| f3819472cf82 | SAME | DIFFERENT | DIFFERENT | 223 |

Three conclusions.

1. **Lane C is right, and it is worse than the BinTools digest alone: the STEP sha does not survive a round
   trip either.** 6 of 6. So neither key in the tree today is geometry-stable across an export and re-read, and
   B23's cache has to be built on lane C's one geometry-stable key function (C16 lever (a)). Nothing to build
   here until it lands.

2. **I withdraw a claim I made before measuring at breadth.** On a synthetic two-box part the STEP export DID
   normalise the round trip (`step_for(brep)` and `step_for(round_trip(brep))` were byte-identical), and I
   reported that as a reason to think the existing key already had the property lane C is building. On real
   parts it is false, 6 of 6. The two-box case was the artifact: exact planar numbers survive the writer where
   real surfaces do not.

3. **The existing key is stable for what it is actually used for.** Two separate exports of ONE BREP are
   byte-identical on 6 of 6 (`stages._deterministic` zeroes the header stamp for exactly this reason), which is
   why the panel cache hits at all. The round-trip instability is not a bug in that key; it is the reason the
   key cannot be read as a statement about GEOMETRY.

## The two verdict-level savings I went looking for, and what was already there

- **Layer 1 memoised across the cheap and full tiers.** Already done, one layer up: `valid.check_valid`
  memoises per (pool, path, bytes, original unit), with this exact rationale in its docstring. I built a second
  memo in `assemble`, found the duplicate when a cache-wiring test caught `check_valid` running once instead of
  twice, and reverted mine. Nothing to win here.
- **Layer 3 on cheap-tier invalid states.** This one was real and is landed (3becc3ba3): `score` zeroes keys 3
  and 4 while layer 1 fails, `clean_of` is already False, and no row carries a cheap verdict's fidelity
  numbers, so the measurement was computed and discarded on the search's inner loop.

One small thing I did NOT change because it is not mine: `verdict/memo.content_key` keys layer 1 on
**(absolute path, sha256)**. The path adds nothing but misses - two states with identical STEP bytes at
different paths each pay layer 1, and `step_for` exports per BREP PATH, so that pair really occurs. Dropping
the path and keeping the sha is a one-line change for whoever owns `verdict/memo.py`.

## What is still unmeasured

Where a verdict's seconds actually go, per layer. `3becc3ba3` puts that on every search row
(`triage.search.verdict_phases`: layer 1, panel, panel cache hit, key, layer 3, with counts), but it needs one
fast-loop run to read. Until then the layer-3 skip's saving is unquantified and I am not building on a guess.
