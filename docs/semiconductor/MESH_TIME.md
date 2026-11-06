# Mesh time: why one run took 38.7 hours and the next one takes about two

Internal. Every number here is read off a run that finished, out of the logs in
`~/corintis_src` and the entries in [DEVLOG.md](DEVLOG.md). Nothing is modelled.

## The headline

| run | cells | mesher | everything after it | total |
|---|---|---|---|---|
| `ws_par336` | 336,666,582 | 2,399.6 s on 8 ranks | 15,389.5 s, 12,831.9 of it in **one process** | 4 h 56 |
| `ws_par182` | 182,269,914 | 727 s on 8 ranks | 2,909 s | about 1 h 0 |
| `ws_shell` | 270,531,894 | **139,230.5 s = 38.7 h** | 4,686 s | 39 h 58 |
| **`cbox2`** | **413,273,323** | **652 s** | 2,452 s to a checked mesh, 4,179 s to reconstruct, 2,728 s serial checkMesh | **2 h 36** |

`ws_shell`'s mesher took 139,230 seconds. `cbox2`'s took 652, on **1.53x as many cells**. That is
213 times faster, and it is one setting.

## What the 38.7 hours actually was

`ws_shell` ran with `anisotropicSources` **scaleY 6** over the microchannel band. `cbox2` ran the
same geometry at **scaleY 3**.

The stretch is not free and the cost does not appear where you would look for it. cfMesh applies an
anisotropic source by scaling the geometry, meshing it, then scaling back. That back-scale sits at
the end of `optimiseFinalMesh` pass 1, and what it hands the optimiser is a mesh whose cells have
just been squashed by the inverse of the stretch:

| stretch | bad faces the back-scale handed the optimiser |
|---|---|
| scaleY 6 | 346,057 |
| scaleY 3 | 440 |

The optimiser then has to fix all of them, and it runs **twice**, because
`projectSurfaceAfterBackScaling` calls `optimiseFinalMesh` again. `ws_shell`'s final untangle ran
152 iterations to take bad faces from 2,219 down to 331.

So scaleY 6 does not buy a cheaper mesh. It buys a cell count, and pays for it in the optimiser,
which is where nearly all of the wall clock lives.

### And the smoother runs on one thread

`ws_shell`'s own output says it: `ExecutionTime = 137983 s  ClockTime = 138476 s`, a ratio of
1.004. That ratio is threads. The same part in one process reads 2530.64 / 640 = 3.95.

One OpenMP thread per rank, on 8 ranks over 8 cores, through the stage that is most of the run. The
cause was `conjugate.py:1008`, `max(1, int(c) // n)`, dividing the core count by the rank count.
Eight ranks on eight cores once spawned 84 threads and never left the smoother.

### Two things that are NOT the cause, and both were checked

| suspected | measured | verdict |
|---|---|---|
| the far field is too fine | a 4x coarser far field changes total cells by 0.9% | not the lever |
| the domain box is too big (3x the part) | a cuboid domain was built and run | refuted on the real part |
| scaleY 4 as a compromise | 7.5x slower for 7% fewer cells | refuted on the real part |

The reproducer lied about scaleY 4. It had to be run on the real geometry to see it.

## The other half: the tail was serial

`ws_par336` is the clearest case. The mesher is 40 minutes of a 4 h 56 run, and the three stages
behind it are 5.35x the mesher, all in one process. (4.4x, which stood here, is `splitMeshRegions`
plus `reconstructParMesh` only, 10,591.7 s; all three are 12,831.9 s.)

| stage | processes | seconds |
|---|---|---|
| preparePar | 1 | 0.6 |
| cartesianMesh | 8 ranks | 2,399.6 |
| reconstructParMesh | 1 | 4,518.4 |
| topoSet | 1 | 2,240.2 |
| splitMeshRegions | 1 | 6,073.3 |
| checkMesh fluid | 1 | 654.3 |
| checkMesh solid | 1 | 1,903.3 |

They were serial deliberately. The ceiling that forces the mesher onto ranks is cfMesh's octree
addressing, nothing downstream shares it, so the block was merged the moment it existed. Right
about correctness, wrong about time.

The fix is to stay decomposed to the end and merge the two **regions** rather than the block:

```
preparePar
mpirun cartesianMesh    -parallel
mpirun topoSet          -parallel
mpirun splitMeshRegions -parallel
reconstructParMesh -constant -region <name>      # once per region
```

Measured on `ws_par182`, same part, same 8 ranks:

| stage | processes | wall |
|---|---|---|
| preparePar | 1 | 1 s |
| cartesianMesh | 8 ranks | 727 s |
| topoSet | 8 ranks | 348 s |
| splitMeshRegions | 8 ranks | 568 s |
| reconstructParMesh, fluid | 1 | 824 s |
| reconstructParMesh, solid | 1 | 1,169 s |

Scaled per cell, the serial tail predicted 1,211 s of topoSet and 3,284 s of split for this size.
On ranks they were 348 s and 568 s. Different builds, so that is a rate and not a race, but the
rate is what the chain was changed for.

One trap worth keeping: `reconstructParMesh` was spending 25 minutes rebuilding the block before
doing the regions, and `-allRegions` cannot finish at the largest sizes while `-regions` is
silently ignored. Call it once per region, by name.

## Where the time goes today

`cbox2`, the coupled four-zone mesh, `couple_box.sh cbox2 0.000352056 1 4 2 1 8 12` on
n2-highmem-96:

| stage | seconds | share |
|---|---|---|
| cartesianMesh | 652 | 7.0% |
| topoSet | 467 | 5.0% |
| splitMeshRegions | 817 | 8.7% |
| checkMesh, 4 regions, parallel | 515 | 5.5% |
| **to a checked four-region mesh** | **2,452 = 0.68 h** | |
| reconstructParMesh, fluid + chip + solid | 4,179 | 44.7% |
| checkMesh, 3 regions, serial | 2,728 | 29.2% |
| **total** | **9,359 s = 2.60 h** | |

**The mesher is 7.0% of the run.** The single largest item is `reconstructParMesh` at 44.7%, and
it is serial by nature: it merges per-rank meshes into one. The three serial `checkMesh` runs that
follow it are another 29.2%, so the serial tail is 73.8% of the whole chain. That tail is the thing
to attack next if this ever needs to be faster.

An earlier version of this table omitted the serial `checkMesh` row entirely and so reported a
6,531 s total, 1.81 h, with reconstruct at 62%. Both were wrong, and the 1.81 h propagated into the
forecast two sections below.

Memory, measured, because it is the constraint that actually bites:

| stage | peak | per cell |
|---|---|---|
| cartesianMesh | 23.22 GB for one rank of eight | about 450 B/cell per rank, against the engine's assumed 748.1 |
| topoSet | 125,125 MB | about 303 B/cell, a number nobody had before |
| splitMeshRegions | 215,399 MB | |
| reconstructParMesh, metal | 145,046 MB | |
| checkMesh | serial | about 1,050 B/cell |

checkMesh's 1,050 B/cell is why the kernel killed it at 254 GB on a 242M-cell metal region, and it
is the reason a 485M-cell mesh would want about 330 GB against the 251 GiB the box had.

## What the next run should take

Scaling `cbox2`'s own stage table by cells, with the measured h^-2.038 exponent giving about 496M
cells at `maxCellSize 322.020`:

| | cbox2 (413M) | next run (about 496M) |
|---|---|---|
| to a checked four-region mesh | 2,452 s | about 2,940 s = 0.82 h |
| reconstruct, three regions | 4,179 s | about 5,010 s = 1.39 h |
| **total** | **2.60 h** | **about 2.2 h, and this was 28% low** |

Tightening the chip band should pull the cell count toward 440M, so 2.2 h is an upper bound.

**Corrected 6 November.** I had written that an earlier 3 to 3.5 hour estimate "was wrong twice
over". It was not: the run took 3.00 h, inside that band, and the 2.2 h that replaced it was 28%
low. The 2.2 h was built by scaling only the reconstruct and omitting the three serial `checkMesh`
runs, the same omission as in the stage table above. The dismissed 2.6 h figure is in fact `cbox2`'s
own total.

## The rules this leaves

1. **The stretch is the whole cost.** scaleY 3, not 6. The saving is not in cell count, it is in
   what the back-scale hands the optimiser.
2. **The optimiser is nearly all of the mesher, and it runs twice.** Anything that increases bad
   faces after the back-scale is paid for at that multiplier.
3. **Stay decomposed through topoSet and splitMeshRegions.** Merge per region, by name, once each.
4. **`meshQualitySettings` points the wrong way.** Loosening it to go faster does not work here;
   the time is in the untangle, not the test.
5. **Size a level change off measured zone counts and areas, never off the body volume.** The chip
   +3 estimate was 516M counting the chip body and 941M counting the air shell it also refines.
6. **Do not trust a small reproducer for a stretch or a domain change.** Both scaleY 4 and the
   cuboid domain looked good small and were refuted on the real part.

## What the next run actually took, measured 6 November

`cbox2b` at `maxCellSize 322.020`. The prediction above was 496M cells and about 2.2 h.

| | predicted | actual |
|---|---|---|
| cells | about 496M | 506,362,044 |
| to a checked four-region mesh | 2,940 s = 0.82 h | 2,864 s = 0.80 h |
| reconstruct plus serial checkMesh | 4,890 s = 1.36 h | 7,932 s = 2.20 h |
| **total** | **about 2.2 h** | **10,796 s = 3.00 h** |

Stage by stage against the mesh we shipped:

| stage | `cbox2` 413M | `cbox2b` 506M | ratio | cells ratio |
|---|---|---|---|---|
| cartesianMesh | 652 s | 799 s | 1.23 | 1.225 |
| topoSet | 467 s | 575 s | 1.23 | |
| splitMeshRegions | 817 s | 869 s | 1.06 | |
| checkMesh, parallel, four regions | 515 s | 621 s | 1.21 | |
| reconstruct, three regions | 4,179 s | 4,857 s | 1.16 | |
| checkMesh, serial, three regions | 2,728 s | 3,075 s | 1.13 | |
| **total** | **9,359 s, 2.60 h** | **10,796 s, 3.00 h** | **1.15** | |

**Where the 2.2 h prediction went wrong.** It was built on `cbox2`'s reconstruct alone, and on a
figure of 4,079 s which is itself a transcription error for 4,179 s. In `cbox2`'s own stage table
the reconstruct and the three serial checkMesh runs together are 6,907 s, and I scaled only the
first of them. The front half was right to 2.6%: 0.80 h against 0.82 h
predicted. The error is entirely in the tail and it is an arithmetic error of mine, not a
behaviour change.

**The 1.15x total for 1.225x the cells is the real headline.** Every stage scales sub-linearly or
with the cells, nothing blew up, and the mesher is still only 7.4% of the run. Reconstruct plus
serial checkMesh is 7,932 s of 10,796, which is 73%. That tail is the only thing worth attacking
and it is unchanged from what this document already says about it.

A corrected forecast for anything larger: take the front half as 0.80 h scaled by cells, and the
tail as 2.20 h scaled by cells. Both were measured on these two runs rather than on one.
