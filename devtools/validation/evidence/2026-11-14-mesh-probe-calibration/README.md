# Mesh probe calibration on the mesh-failure-real track (2026-11-14)

**Verdict: binary agreement on 76 of 79 verdicts, plus 1 unreadable file; the stop rule (more than 8
disagreements) is not tripped.** The in-loop probe (`loop`) and the same shape re-exported through
STEP (`reexport`) agree on whether the part meshes for 76 of the 79 files that have both verdicts:
3 disagree (meshes vs fails). The 80th file, `occt/bug30790.step`, has no `reexport` or `loop`
verdict because OCP cannot measure it. A further 6 files fail under both but with different kinds (kind-swap table).

Why binary is the gate (controller ruling R9): the track's score is always the file probe on the
delivered STEP, so the in-loop probe only steers where repair looks. Whether the part meshes is
what it must get right; which of gmsh's failure kinds it reports first is secondary and is reported
as its own column, not gated. (The first pass of this evidence gated on kind and counted 9
disagreements; the same `run.json` rows, re-summarised, give the figures below.)

Run: `run.json` (rows and summary). Command, host darwin-arm64, gmsh 4.15.2, 120 s per probe,
6 workers, 997 s wall:

```
python devtools/validation/mesh_probe.py --manifest devtools/validation/corpora/mesh-failure-real.json \
  --roots ~/archductor/mesh-fail-corpus/occt ~/archductor/mesh-fail-corpus/abc --json run.json
```

Three probes per file: `file` = probe_file on the upload; `reexport` = OCP read, OCP
STEPControl_Writer, probe_file; `loop` = OCP read, `mesh_defects` (BREP). Agreement compares `loop`
with `reexport`. `file` vs `reexport` is the separate reader effect.

| | count |
|---|---|
| binary agreement, loop vs reexport | 76 / 79 verdicts (+1 unreadable) |
| meshes-vs-fails disagreements | 3 |
| both fail, different kind (not gating) | 6 |
| file OCP cannot read | 1 (`occt/bug30790.step`) |
| reader effect (file differs from reexport) | 17 files |
| rows where a probe hit its 120 s budget (over 200 s) | 11 |

## Meshes-vs-fails (3, gating)

| file | reexport | loop | note |
|---|---|---|---|
| abc/00500875_a97e4a09c926c436c043e3c3_step_028 | edge_through_face | meshes | `file` also meshes; only the re-export fails |
| abc/00503200_c8a4697bcc912578475337ea_step_000 | timeout | meshes | budget-bound: the file probe also timed out |
| occt/trj3_s1-tc-214 | meshes | edge_through_face | `file` fails edge_through_face, as loop does |

## Kind swaps, both fail (6, not gating)

| file | reexport | loop |
|---|---|---|
| abc/00502475_5895e91efca3da0f6b68639a_step_003 | edge_through_face | other (unlocated) |
| abc/00507550_ed3bf58da6711cb579e24def_step_024 | overlapping_facets | edge_through_face |
| abc/00507750_15086f55e7ef8d6b99de417a_step_000 | periodic_surface | edge_through_face |
| occt/bug33100_window | no_volume | timeout (gmsh child crash, 1.2 s) |
| occt/trj4_s1-ai-214 | edge_through_face | overlapping_facets |
| occt/trj5_s1-in-214 | overlapping_facets | edge_through_face |

Mostly swaps between volume-stage PLC kinds: gmsh's 3D step reports whichever intersection it hits
first, which can depend on input ordering a STEP round trip changes. Causes are otherwise
unexplained.

## OCP read failure

`occt/bug30790.step`: OCP reads it but raises `Standard_Failure: NCollection_Array1::Value` the
moment its bounding box is measured, so there is no OCP diagonal, no `reexport` and no `loop`
verdict. The verdicts that exist for it: `file` = `other` (import stage: gmsh's own read of the
upload fails; probed independently of OCP, with an assumed diagonal of 1.0 recorded as
`file_diag_assumed` because an import-stage failure does not depend on it). `reexport`, `loop`: none.
Its manifest `diag_mm` stays null (`diag_source` `unavailable`).

## Budget-bound rows

Eleven rows ran over 200 s (a probe reached its 120 s budget), so their verdicts depend on the
budget: abc/00502300_..._002, 00502775_..._071, 00502950_..._000, 00503200_..._000,
00503600_..._000, 00507075_..._000, 00507750_..._000, occt/bug32596_abc_0_13, trj3_s1-al-214,
trj6_b1-ec-214, trj7_b1-ec-214.

## Not measured

- Nondeterminism: one pass only. Whether a repeat run gives the same verdicts is unknown.
- Budget dependence: every verdict is at a 120 s per-probe budget; 11 rows reached it.
- Outcome validity: agreement was never checked against a real repair outcome.
- One platform (darwin-arm64) and one gmsh version (4.15.2).

## Diagonals

`diag_mm` in the manifest is what the probe meshes at (diagonal / 40). `diag_source` is `gmsh`
(66 files, from the corpus build), `ocp` (13 files whose corpus-build child died before measuring;
OCP's bounding-box diagonal from this run) or `unavailable` (1, above).

## Reader effect (file vs reexport, 17 files)

OCP's STEP reader and gmsh's disagree on these (file / reexport / loop):

| file | file | reexport | loop |
|---|---|---|---|
| abc/00500875..._028 | meshes | edge_through_face | meshes |
| abc/00505375..._006 | timeout | edge_through_face | edge_through_face |
| abc/00506325..._012 | edge_through_face | overlapping_facets | overlapping_facets |
| abc/00507550..._024 | edge_through_face | overlapping_facets | edge_through_face |
| abc/00507750..._000 | edge_through_face | periodic_surface | edge_through_face |
| abc/00509500..._062 | periodic_surface | meshes | meshes |
| occt/bug25013_25013face | timeout | no_volume | no_volume |
| occt/bug27453_165-174_piattello | other | edge_through_face | edge_through_face |
| occt/bug32596_abc_0_13 | periodic_surface | timeout | timeout |
| occt/bug33100_window | timeout | no_volume | timeout |
| occt/trj10_ch1-id-chs | open_loop | meshes | meshes |
| occt/trj3_s1-id-203 | open_loop | edge_through_face | edge_through_face |
| occt/trj3_s1-tc-214 | edge_through_face | meshes | edge_through_face |
| occt/trj4_s1-ai-214 | overlapping_facets | edge_through_face | overlapping_facets |
| occt/trj5_s1-in-214 | edge_through_face | overlapping_facets | edge_through_face |
| occt/trj6_b1-ec-214 | overlapping_facets | timeout | timeout |
| occt/trj7_k1-id-214 | other | no_volume | no_volume |

Two files mesh once OCP has read them (trj10, abc/00509500): a repair that changes nothing would
already "fix" them.

## Fetching the inputs (no CAD is committed)

ABC models are their creators' copyright and the OCCT dataset carries no redistribution licence,
so the manifest (`devtools/validation/corpora/mesh-failure-real.json`) holds names, sha256 and
source URLs only.

1. Download `opencascade-dataset-7.8.0.tar.xz` (GitHub release V7_8_0 of Open-Cascade-SAS/OCCT)
   and `abc_0050_step_v00.7z` (https://archive.nyu.edu/retrieve/88830/abc_0050_step_v00.7z).
2. Verify sha256: OCCT `ee7eea667dda1ed8615e6101fa4bb2c65915a4678a0d1ad0ab76728bde88ed33`, ABC
   `1855b6fa5f2c074558d61127bebc2f359a6b6380288c1b2d1ab72f4120515536`.
3. Extract each into its own empty directory (`tar xf` / macOS `bsdtar -xf` reads 7z).
4. Pass those directories as `--roots`; files are matched by sha256, wherever they sit. A file not
   found is listed as MISSING and the exit code is non-zero.
