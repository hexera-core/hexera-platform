# Lane C11/C12 evidence: what the body ops leave behind, and v3b's overlap files

All scripts here run in `meshpipeline-mesh:native` (or the crsv4-mesh image) with `PYTHONPATH=<tree>/src:<tree>`.

## netgen reads only the first root (handed to Lane D)

`ngread.py <step>`: what `netgen.occ.OCCGeometry` sees in a STEP, and the netgen_occ member's verdict, on the
original STEP and on its engine form (`search.engine_form`).

On 0053_00530659_a7f966eb7895d0814869e759_step_000.step (sha 9c6e77a7...), a 3-body overlap file:
- The STEP's top compound is `[SHELL, SOLID, SOLID, SOLID]`. read_step_xcaf and plain STEPControl both see all four.
- OCCGeometry returns `type SHELL, solids 0, shells 1, faces 100`: only the loose shell.
- The member reports `fail step_unreadable: STEP file holds no solids`, the same on the original and on the engine form.

This is the same class as Lane D's D17 (5 valid dev files with that message). It is a reading bug: netgen reads only
the loose shell and never sees the solids. It is not geometry and not an op output, and it caps netgen on such a file
whatever any repair does.

## Empty compound after a body op (fixed, 46468893)

`body_kernel._rebuild` re-added a sub-compound that the op had emptied. netgen then failed on the result with
'Bnd_Box is void'. On two boxes overlapping by 0.5 mm, cut_by and fuse passed 3/4 members as written, and 4/4 and
CLEAN once stripped. Native test: `tests/native/test_native_repair_v4_c11_empty_compound.py`.

Scope: of v3b's 42 overlap files only 3 are 2-body parts (bodies per file range from 2 to 331). The fix's reach there
is those 3 files, plus any cut that consumes a whole sub-compound. None of the four C11 table files was in that class.

## C11 per-file diagnosis

- `diag.py <step> <out.json>` gives the full verdict on the input, then the full verdict after every body op the det
  sweep proposes (fragment, cut_by each body, fuse). Each op output is judged as written and with empty compounds
  stripped. Failing faces are tagged NEW (minted by the op) or OLD.
- `chain.py <step> <out.json> <key>` runs op sequences from `plans.json` (cut, then fragment or orient_and_close).
  After each step it records per-solid volume, orientation and validity.
- `chain2.py <step> <out.json> <key>` targets the tip, which carries A11.2's contact sites. After the plan it records
  every site with its measured clashes, the det sweep's proposals in order, and the first 6 proposals applied as
  branches, each with a full verdict.

## C12 triage of v3b's 42 overlap files

- `fp.py <inputs dir> <fp.json>` writes every input's face fingerprints on the workspace grid.
- `c12_triage.py <rows.jsonl> <fp.json> <ovl.json>` reports, per file and arm: whether a body op applied, valid,
  clean, and the failing members. Each failing face is classed NEW (not among the input's own faces), OLD, or
  unlocated.

The triage always prints an **instrument validation** line. On input_only rows no op ran, so NEW must be 0. When it
is not, the line says FAIL and the NEW/OLD split is NOT trustworthy.
