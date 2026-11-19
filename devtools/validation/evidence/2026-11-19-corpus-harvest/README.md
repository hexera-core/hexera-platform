# Corpus harvest: real mesh failures at scale (2026-11-19)

Every STEP file of each ABC chunk was probed with gmsh 4.15.2 (OCC import, 1 thread, MeshSizeMax = diag/40, generate(2) then generate(3), 120 s budget, one interpreter per file). Failing files live in the local corpus only; this repo carries names, sha256 and metadata. Tool: `devtools/validation/corpus_harvest.py`. Chunk 0058 has no STEP archive on NYU (ofs only) and was replaced by 0061.

## Yield per chunk

| chunk | files | pass | fail | timeout | no solids | fail+timeout % |
|---|---|---|---|---|---|---|
| 0051 | 10000 | 8876 | 939 | 64 | 121 | 10.0 |
| 0052 | 10000 | 8878 | 938 | 56 | 128 | 9.9 |
| 0053 | 10000 | 8899 | 924 | 49 | 128 | 9.7 |
| 0054 | 10000 | 8888 | 925 | 58 | 129 | 9.8 |
| 0055 | 10000 | 8591 | 1192 | 67 | 150 | 12.6 |
| 0056 | 10000 | 8878 | 939 | 68 | 115 | 10.1 |
| 0057 | 10000 | 8934 | 854 | 46 | 166 | 9.0 |
| 0059 | 10000 | 8837 | 926 | 60 | 177 | 9.9 |
| 0060 | 10000 | 8927 | 886 | 44 | 143 | 9.3 |
| 0061 | 10000 | 9015 | 780 | 61 | 144 | 8.4 |
| **total** | 100000 | 88723 | 9303 | 573 | 1401 | 9.9 |

No-solids files (surface-only STEP) are not mesh failures and are not kept.

## Family x stage x solids, all failures (9956 = original 80 + harvested 9876)

Stage is where the gmsh error was raised (2d, 3d); the original 80 pre-date stage recording and count as unknown. Timeouts and crashes take the last stage marker seen.

| family | total | 2D | 3D | unknown | single-solid | multi-solid | solids unknown | median faces |
|---|---|---|---|---|---|---|---|---|
| overlapping_facets | 5871 | 0 | 5840 | 31 | 2230 | 3641 | 0 | 139 |
| edge_through_face | 2376 | 0 | 2349 | 27 | 982 | 1394 | 0 | 173.0 |
| timeout | 587 | 386 | 86 | 115 | 0 | 0 | 587 | None |
| periodic_surface | 394 | 390 | 1 | 3 | 133 | 261 | 0 | 439.0 |
| other | 288 | 9 | 127 | 152 | 51 | 90 | 147 | 150 |
| open_loop | 227 | 223 | 0 | 4 | 94 | 133 | 0 | 138 |
| no_volume | 168 | 0 | 167 | 1 | 162 | 6 | 0 | 21.5 |
| crash | 45 | 33 | 12 | 0 | 0 | 0 | 45 | None |

`aggregate.json` has the same numbers plus the harvested-only distribution and per-chunk summaries; `clean-samples.json` holds a seeded 1% sample (name + sha256) of PASSING files per chunk for rebuilding clean controls.
## Notes

- Ran on a fleet of 10 GCE VMs in hexera-dev (one per chunk, labelled `purpose=corpus-harvest`), each deleted after its failures were pulled back and every file's sha256 checked against the manifest. About 100 minutes per chunk.
- Archive hashes, URLs and per-file sha256 are in `devtools/validation/corpora/mesh-failure-real.json` (version 2). No CAD files are committed.
- CAx-IF public test-round files were not harvested: the CAx-IF library page returned HTTP 403 to unauthenticated clients.
- `timeout` and `crash` rows have no solid or face counts because the probe never answered.
