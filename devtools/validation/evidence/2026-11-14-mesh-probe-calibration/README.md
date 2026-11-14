# Mesh probe calibration on the mesh-failure-real track (2026-11-14)

**Verdict: the stop rule tripped.** The in-loop probe (`loop`) disagrees with the same shape
re-exported through STEP (`reexport`) on 9 of 80 files; the limit is 8. Plan Task 3 says stop and
report before Task 5. Nothing was tuned. The run is a single pass, so some disagreements may be
gmsh nondeterminism or budget effects (see causes); that was not re-measured.

Run: `run.json` (rows and summary). Command, host darwin-arm64, gmsh 4.15.2, 120 s per probe,
6 workers, 997 s wall:

```
python devtools/validation/mesh_probe.py --manifest devtools/validation/corpora/mesh-failure-real.json \
  --roots ~/archductor/mesh-fail-corpus/occt ~/archductor/mesh-fail-corpus/abc --json run.json
```

Three probes per file: `file` = probe_file on the upload; `reexport` = OCP read, OCP
STEPControl_Writer, probe_file; `loop` = OCP read, `mesh_defects` (BREP). Agreement compares `loop`
with `reexport` (same kind, or both mesh). `file` vs `reexport` is the separate reader effect.

## Result

| | count |
|---|---|
| loop agrees with reexport | 70 / 80 |
| loop disagrees with reexport | 9 |
| file could not be probed (OCP error) | 1 (`occt/bug30790.step`: `Standard_Failure: NCollection_Array1::Value` in OCP's read) |
| reader effect (file differs from reexport) | 17 files |

## loop vs reexport disagreements (9)

| file | reexport | loop | cause |
|---|---|---|---|
| abc/00500875..._028 | edge_through_face | meshes | unexplained; `file` also meshes, so this is the re-export that fails, BREP meshes |
| abc/00502475..._003 | edge_through_face | other | unexplained; loop defect is unlocated |
| abc/00503200..._000 | timeout | meshes | budget: reexport probe ran 120 s; the file probe also timed out (344 s row total) |
| abc/00507550..._024 | overlapping_facets | edge_through_face | unexplained; both are volume-stage PLC failures on the same part |
| abc/00507750..._000 | periodic_surface | edge_through_face | unexplained; different failure kind on the same part |
| occt/bug33100_window | no_volume | timeout | loop failed in 1.2 s with a child crash (the file probe also crashed, rc=139 in the corpus build) |
| occt/trj3_s1-tc-214 | meshes | edge_through_face | unexplained; `file` fails edge_through_face, as loop does |
| occt/trj4_s1-ai-214 | edge_through_face | overlapping_facets | unexplained; same class of volume-stage PLC failure |
| occt/trj5_s1-in-214 | overlapping_facets | edge_through_face | unexplained; same class |

Pattern: 6 of 9 swap between two volume-stage PLC kinds (overlapping_facets / edge_through_face /
periodic_surface) on parts that fail either way. gmsh's 3D tetrahedralisation reports whichever
intersection it hits first, which can depend on the input ordering a STEP round trip changes, so
"same kind" is a strict test for these. Whether the in-loop probe still serves the track (it must
say meshes / does not mesh, and where) is the decision for the controller; by kind it fails the
plan's threshold. If "both fail" counted as agreement, the disagreements would be 4: the three
meshes-vs-fails cases (abc/00500875, abc/00503200, occt/trj3_s1-tc-214) plus occt/bug33100_window
(no_volume vs timeout, a crash).

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
already "fix" them. Eleven rows spent over 200 s, i.e. a probe hit its 120 s budget; those are
budget-dependent verdicts.

Where the corpus build recorded no diagonal (14 files whose child died), the CLI takes OCP's
bounding-box diagonal and records it as `diag_from_ocp` in `run.json`.

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
