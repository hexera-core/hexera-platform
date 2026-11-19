# CAD simplification - devlog

Branch `raymond/cad-simplification`. Design: `docs/superpowers/specs/2026-11-19-cad-simplification-design.md`.
A working log, not a document: chronological, dead ends included, and later entries correct earlier
ones. For the design, read the spec.

## Where it stands

| | |
|---|---|
| Phase | P1, ground-truth harness (one feature removed at a time, mesh and solve both, compare) |
| Environment | `dev-raymond` (two-run first deploy, 2026-11-19), bench VM `dev-raymond-bench` |
| Corpus | NIST MBE PMI test parts, AP203 geometry-only (public domain, "can be used without any restrictions"), 10 of 11 usable |
| Solver | CalculiX 2.21 (Ubuntu `calculix-ccx`), C3D10 from gmsh 4.15.2, linear static |

## Compute

Nothing heavy runs on the laptop. Fast gates (`make check-fast`) run there; meshing and solving run on
`dev-raymond-bench`:

- `n2-standard-16` (16 vCPU, 64 GB), `us-central1-a`, 200 GB pd-balanced, Ubuntu 24.04. About
  $0.78/h while running. c2d-standard-16 was the first choice and was stocked out in all four
  `us-central1` zones on 2026-11-19.
- Labels `app=hexera, deployment-id=dev-raymond, component=simplify-bench, managed-by=hand`, so the
  `destroy-env` sweep reports it.
- `devtools/simplify/bench_startup.sh` is its startup script: installs CalculiX, a Python 3.12 venv at
  `/opt/bench/venv` with the repo's pinned gmsh/OCP/meshio/trimesh, and an **idle watchdog** that
  powers it off after 45 min with no ssh session and no ccx/gmsh/bench-python process. A stopped VM
  costs only its disk.
- Code and parts reach it by `gcloud compute scp` into `~/bench/{code,corpus}`; results come back the
  same way. `devtools/simplify/run_corpus.sh` runs the harness over every part, skipping finished ones.

```bash
gcloud compute instances start dev-raymond-bench --zone us-central1-a --project hexera-dev
gcloud compute ssh dev-raymond-bench --zone us-central1-a --project hexera-dev
gcloud compute instances stop dev-raymond-bench --zone us-central1-a --project hexera-dev
```

`make seed-secrets SLUG=raymond` (handover step 0) was **not** run: it derives the project
`hexera-dev-<slug>` from the one-project-per-developer era. In `hexera-dev`, a slug's deploy reuses
the shared secret containers and mints its own `minio-secret-key-<slug>`
(`.github/workflows/deploy.yml`, target job).

## 2026-11-19 - harness, first part

`devtools/simplify/harness.py`. Per part: recognise holes (concave full cylinders, with coaxial cones)
and blends (tori and part-cylinders of radius < 3% of the diagonal); invent a load case (fixed at the
low end of the longest axis, 1000 N bending at the high end); remove each feature alone with
`BRepAlgoAPI_Defeaturing`; mesh with gmsh (10-node tets, size diag/30, 12 elements per 2π of
curvature, so small features cost cells); solve in CalculiX; compare compliance, tip deflection, max
displacement and max von Mises away from the ends and away from the removed feature's own zone.

**nist_ftc_09** (158 faces, diagonal 338 mm): 59 features, 7 skipped (touch an end), 52 solved, 203 s
wall on 16 cores. Original: 64,467 tets, 6.6 s mesh, 48 s solve.

| | |
|---|---|
| noise floor (same part, size x0.93) | compliance -0.02%, tip -0.02%, max disp -0.06%, far stress **+1.64%** |
| best single removal | `hole17` (r 1.98 mm): -8.2% cells, compliance -0.05%, far stress -0.7% |
| worst global error | `blend44` (concave, r 6.35): compliance -1.1% |
| a removal that COSTS cells | `hole13` (r 9.52): **+18.8% cells** - filling a large hole adds volume to mesh |

Signs are physical: filling a hole stiffens (compliance down), removing a concave fillet removes
material (compliance up).

What this says:

- Far-field stress below about 2% is inside the meshing noise on this load case. Global QoIs resolve
  to about 0.1%. Any accept/reject threshold must sit above the floor of the QoI it judges.
- Cost is not monotone in "simpler". A feature must be cost-attributed before it is proposed.
- **Solve wall time is not a cost metric here**: 16 concurrent jobs on one box moved it by ±40% on
  identical meshes. Elements and nodes are the cost; timing needs isolated runs.

**Defect found and fixed**: `BRepAlgoAPI_Defeaturing.IsDone()` is true when OCCT could not remove the
faces - it hands the input back and records a warning. 12 "removed" blends had dV ~ 1e-15 and an
identical mesh, which read as free, harmless simplifications. `remove()` now refuses a result with
the same face count and volume. Most of the 12 are 180° concave part-cylinders - slot ends, not
fillets - so the blend recogniser also needs to tell a full round from a fillet.

Open:

| | |
|---|---|
| full NIST corpus | running on the bench |
| slot ends vs fillets | recogniser treats every concave part-cylinder < 3% diag as a blend |
| stress probes | far stress is a weak QoI on a bending load (noise 1.6%); add a local QoI at named points |
| invented load case | one bending case per part; a second (torsion or axial) to see whether rankings hold |
