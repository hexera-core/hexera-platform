# Defect-patch retrieval lab

Lab-only code for `docs/superpowers/specs/2026-10-29-defect-cad-rag-design.md`. Nothing in `src/`
imports it. It reads the repair engine and leaves it unchanged: every attempt goes through
`RepairSession.try_op`, so the caps, rollback, re-inspection and progress rule all still apply.

| module | iteration | what it does |
|---|---|---|
| `patch.py` | B | `isolate_patch()` builds three nested face sets (L0 own faces, L1 the adjacent ring, L2 a second ring clipped to 4× local scale); `describe()` turns them into a 65-dim intrinsic vector (`hand-v2`) |
| `stability.py` | B | metamorphic test: rigid motion, STEP round trip, and both together; reports localiser count drift, fingerprint survival, descriptor drift and self-retrieval |
| `sweep.py` | A + C | independent action sweep: each applicable operation is tried alone from the same snapshot, at most 24 stratified defects per file, one subprocess per file, and per-defect JSONL flushed as it goes |
| `precap.py` | A | lists files that arrive already over an absolute cap; their verdicts say nothing about the operation and are excluded |
| `matrix.py` | C | action-outcome matrix, refusal reasons, kept spread across families, timing distributions |
| `policy.py` | D | calibrated skip threshold (raw-score cutoff at 95% train keep recall) and budgeted attempt-ordering replay against measured attempt times |
| `export_memory.py` | shadow | bakes sweep records into the engine's fix-memory snapshot (`src/meshpipeline/cad/repair/data/`), standardisation and priors included |
| `knn.py` | D | leave-one-family-out exact kNN versus the `rules` baseline (narrowest applicable op) and the `prior` baseline (best op for the code, no geometry), with family-bootstrap CIs and AUC for "will the rules op be kept" |

```bash
export PYTHONPATH=src:.
.venv/bin/python -m labs.defect_cad_rag.sweep ~/cadcorpus/occt --out /tmp/dcr-sweep --workers 8
.venv/bin/python -m labs.defect_cad_rag.precap /tmp/dcr-sweep/records.jsonl ~/cadcorpus/occt \
    --json /tmp/dcr-sweep/precapped.json
.venv/bin/python -m labs.defect_cad_rag.matrix /tmp/dcr-sweep/records.jsonl \
    --precapped /tmp/dcr-sweep/precapped.json
.venv/bin/python -m labs.defect_cad_rag.knn /tmp/dcr-sweep/records.jsonl \
    --precapped /tmp/dcr-sweep/precapped.json
.venv/bin/python -m pytest labs/defect_cad_rag/tests -q
```

The sweep turns every default-off geometry flag on, in the child's environment only (`LAB_FLAGS`).
That posture belongs to the lab and must never be copied into a deployment.

The engine-side shadow consumer lives in `src/meshpipeline/cad/repair/fixmemory.py`
(`CAD_REPAIR_RAG=shadow`), with the descriptor promoted to
`src/meshpipeline/cad/repair/patchdesc.py`; `patch.py` here is a shim re-exporting it.

Results and their interpretation live in
`docs/research/defect-cad-rag-experiment-1.md` and
`docs/research/defect-cad-rag-experiment-2.md`.
