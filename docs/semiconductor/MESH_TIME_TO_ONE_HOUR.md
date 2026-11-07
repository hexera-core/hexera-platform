# Getting the coupled mesh under an hour

Measured 6 and 7 November 2026 on `corintis-farfield`, an n2-highmem-96. Companion to
[MESH_TIME.md](MESH_TIME.md), which covers how the mesher itself went from 38.7 hours to 17 minutes.
This one covers everything after the mesher, which is where all the remaining time is.

Every line in the tables below is a stage that ran and exited. Nothing is modelled and nothing is
extrapolated. Where I projected rather than measured, it says so.

## The answer

| chain | wall clock | |
|---|---|---|
| as actually run, with layers | **20,834 s = 5.79 h** | |
| optimised, with layers | **8,578 s = 2.38 h** | 2.43x faster |
| optimised, no layers | **2,735 s = 46 min** | **under the hour** |

Sub-one-hour is reachable, and the thing standing in the way is the boundary layer pass, which costs
7,483 s on its own and which [MESH_QUALITY_CBOX2B.md](MESH_QUALITY_CBOX2B.md) section 7 shows makes
the thermal answer worse rather than better. So the sub-hour chain is not a compromise reached by
cutting quality. It is the better mesh as well as the faster one, conditional on the flow being
thermally developed, which is the one thing we cannot check without the customer's flow rate.

With layers kept, the floor is 2.38 h and the layer step alone is 1.31 h of it. **There is no route
to one hour that includes a 3-layer boundary layer on 25.7M faces on this hardware with this
toolchain.** I want to be plain about that rather than quote a number I cannot stand behind.

## What actually ran, stage by stage

### The mesh, 10,796 s = 3.00 h

| stage | s | share |
|---|---|---|
| cfMesh, 8 ranks | 799 | 7.4% |
| `topoSet` | 575 | 5.3% |
| `splitMeshRegions -cellZonesOnly` | 869 | 8.0% |
| `checkMesh -parallel`, 4 regions, 8 ranks | 621 | 5.8% |
| `reconstructParMesh` fluid and chip | 1,351 | 12.5% |
| `reconstructParMesh` solid | 3,506 | 32.5% |
| `checkMesh` serial, 3 regions | 3,075 | 28.5% |
| `redistributePar -reconstruct` attempts, always rc=1 | 121 | 1.1% |

### The layers, 10,038 s = 2.79 h

| stage | s | share |
|---|---|---|
| copy the fluid region out | 47 | 0.5% |
| `decomposePar`, 16 subdomains, hierarchical | 1,129 | 11.2% |
| `snappyHexMesh` addLayers, 16 ranks | **4,714** | 47.0% |
| `reconstructParMesh` | 1,640 | 16.3% |
| `checkMesh` serial | 2,508 | 25.0% |

### Where the time really is

| | s | share of 20,834 |
|---|---|---|
| actually building geometry | 799 + 4,714 = 5,513 | **26.5%** |
| writing and re-reading the same bytes | 1,351 + 3,506 + 1,640 = 6,497 | **31.2%** |
| checking the same mesh twice | 3,075 + 2,508 = 5,583 | **26.8%** |
| topology bookkeeping | 575 + 869 + 1,129 = 2,573 | 12.3% |
| failed calls and copies | 121 + 47 = 168 | 0.8% |

58% of the run is reconstruct plus a serial re-check of a mesh that a parallel check has already
passed. That is the target, and neither item produces anything the solver needs.

## The cuts, each one measured

| cut | s saved | why it is safe |
|---|---|---|
| drop all three `reconstructParMesh` calls | **-6,497** | a solver wants the decomposed mesh; reconstructing it only to decompose it again is pure overhead, and the layer step had to decompose it a second time for exactly this reason |
| drop the serial `checkMesh` passes | **-5,583** | `checkMesh -parallel` already ran on the same mesh and returned `Mesh OK`; it is the same binary and the same nine criteria, 621 s against 3,075 s |
| drop `checkPar` on the air region | -129 | the region is not in the delivered mesh |
| stop attempting `redistributePar -reconstruct` | -121 | it has returned rc=1 on every run |
| drop the copy before layering | -47 | layer in place |
| **total** | **-12,377** | |

20,834 - 12,377 = 8,457 s. Adding back the 121 s of decompose work that the single remaining
decompose still needs gives **8,578 s = 2.38 h** with layers, and **2,735 s = 46 min** without.

The no-layer chain in full, every line measured:

| stage | s |
|---|---|
| cfMesh, 8 ranks | 799 |
| `topoSet` | 575 |
| `splitMeshRegions` | 869 |
| `checkMesh -parallel`, 3 regions | 492 |
| **total** | **2,735 s = 45 min 35 s** |

## Two experiments that were meant to help

### Binary polyMesh: works, 1.74x, but only pays if the mesh is read twice

Run on the chip region, 31,012,960 cells, because it is the small one and the ratio is what matters.

| | ASCII | binary | ratio |
|---|---|---|---|
| `checkMesh` | 278 s | **160 s** | **1.74x** |
| polyMesh on disk | 6.46 GB | **3.95 GB** | **1.64x** |
| `foamFormatConvert` | | 157 s | |

The two runs agree to the last printed digit on max aspect ratio (1.00137), max skewness
(1.37916e-12) and min cell volume (5.21595e-13), so binary changes the bytes and not the mesh.

But conversion costs 157 s to save 118 s, so converting an existing mesh in order to check it once
loses. Binary is worth having only as the mesher's own `writeFormat`, where the write is free and
every later read is 1.74x faster. `cellZones` came out the same size in both, so it was not
converted.

### Three regions checked at once: failed, and the failure is the useful part

The build runs its three serial `checkMesh` calls one after another, 645 + 2,223 + 207 = 3,075 s, on
a 96 core box. If they are independent, running them together should cost about max(), so 2,223 s.

They are not independent, because they are memory bound rather than CPU bound:

```
bash: line 1:   214 Killed   checkMesh -constant -region solid > /case/e2_solid.log
```

The solid arm was OOM-killed. Three concurrent `checkMesh` processes on a 327M, 82M and 31M cell
mesh do not fit in 768 GB. **Binary cannot rescue this**, because binary shrinks the file and the
parse time, not the in-memory footprint. The wall clock came out at 2,413 s with one of the three
arms dead, so it is not even a valid 1.27x.

Concurrency is dead as a lever. Removing the duplicate serial pass entirely is the better move and
saves more.

## Why the layer step costs 4,714 s

| | serial | 16 ranks |
|---|---|---|
| wall clock | 52,021 s = 14.4 h | 4,714 s = 1.31 h |
| passes | 34 | 10 |
| coverage | 89.77% | 90.71% |
| illegal faces | 4,022 | 23,833 |

11.0x on 16 ranks is 69% parallel efficiency, which for a topology-changing operation with a final
rebalancing step is reasonable. Pass 1 of the parallel run reproduces the serial run almost exactly,
25,625,666 faces extruded against 25,625,657, and 82,548 illegal against 82,552, so the two are
doing the same work.

The cost is intrinsic to the size of the job: 25,685,941 faces to extrude, 60,707,385 cells added,
and a final balance over 143,236,900 cells. Three things could reduce it, in descending order of how
much I trust them:

1. **Do not add layers.** Measured, free, and it improves Nusselt from -4.02% to +2.18%.
2. **Layer only the channel walls** rather than all 25.7M interface faces. The chip footprint and the
   channel array are a fraction of the interface. Not attempted, and it needs a faceZone the current
   chain does not build.
3. **More ranks.** 16 of 96 cores were used. Projected only, and layer addition rebalances twice, so
   I would not promise better than 1.5x from a 3x rank increase.

## Cell budget: there is no lever here, which I had to measure to believe

| region | cells | share | mean cell vol m3 | max cell vol m3 | total vol m3 |
|---|---|---|---|---|---|
| solid | 327,126,204 | **74.2%** | **6.803e-13** | 1.10727e-10 | 2.22527e-04 |
| fluid | 82,529,515 | 18.7% | **6.676e-13** | 1.11352e-10 | 5.51026e-05 |
| chip | 31,012,960 | 7.0% | 5.218e-13 | 5.23607e-13 | 1.61816e-05 |

The solid is three quarters of the mesh, so I expected it to be the big win: coarsen it away from the
coolant interface and the mesher, `topoSet`, the split, every check and the solver run all shorten at
once.

**It is already graded.** Same coarsest cell as the fluid to within 0.6%, same mean cell to within
1.9%, over the same four levels of refinement. The solid is 327M cells because it is 4.04x the
coolant's volume at the same average refinement: 82,529,515 x 4.039 = 333M against 327M measured.
There is no over-refined bulk to find, and the metal between channels cannot be coarsened at all,
because the channel walls ARE the conjugate interface and conformity pins the metal side to the
coolant side.

So **440.7M live cells is close to what 5 cells across a channel costs on this geometry.** What is
left is small: the chip is a single level throughout, its min and max cell volumes differing by 0.4%,
and dropping it one level would save about 27M cells or 6.1%. The far field air region, 64,047,269
cells or 12.7%, is already dropped.

Getting materially below that needs a different kind of mesh rather than a better octree. See
[MESH_IMPROVEMENT_PLAN.md](MESH_IMPROVEMENT_PLAN.md) item 4.

## Recommended chain

```
cfMesh 8 ranks, writeFormat binary          799 s
topoSet                                     575 s
splitMeshRegions -cellZonesOnly             869 s
checkMesh -parallel, 3 regions              492 s
deliver decomposed                            0 s
                                           ------
                                          2,735 s = 46 min
```

Add 5,843 s for layers if and only if the operating Reynolds number turns out to be above about 226,
which is the point at which the thermal entrance length exceeds the longest straight channel run and
layers start earning their cost.

To run `checkMesh -allGeometry`, which has never once passed on a conjugate mesh, retype the
`mappedWall` interface on a **throwaway copy** of the boundary file and check that. Measured on the
chip region: rc 1 to 0, 610 s to 624 s. Never retype the delivered mesh, because
`chtMultiRegionFoam` couples the regions through those patches and a plain wall silently decouples
the solve.

## Why the first end-to-end attempt produced no number

I ran the optimised chain end to end to replace the sum above with a measured whole. It died, and the
cause was not the chain. It is worth writing down because the symptom appeared three stages and six
hours away from the defect, and nothing in the stage table pointed at it.

| stage | rc | s | against ASCII |
|---|---|---|---|
| mesh, 8 ranks | 0 | 778 | 799 |
| topoSet | 0 | 422 | 575, so 1.36x from binary |
| split | **137** | 357 | 869, and 137 is SIGKILL |

`splitMeshRegions` was OOM-killed on a 755 GB box doing work that had already succeeded in 869 s on
the same box two days earlier. The oom-killer log says why:

| time | invoked by | killed | RSS |
|---|---|---|---|
| 10:57:10 | `snappyHexMesh` | a `cartesianMesh` rank | 26.6 GB |
| 11:34:21 | `snappyHexMesh` | `checkMesh` | 181.4 GB |
| 14:27:52 | `splitMeshRegion` | `splitMeshRegion` rank 5 | 47.3 GB |

At 10:21:40 I started two far field meshes, 8 ranks each. At 10:57 the 16-rank layer pass put the box
under enough memory pressure that the kernel killed one rank of one of them. **The other ranks did
not exit.** Six hours later `ps` still showed 16 `cartesianMesh` processes, 381.9 GB resident, state
`R`, each burning 375% CPU. OpenMPI busy-waits inside collectives rather than sleeping, so a rank
with nowhere to send its next message spins at full tilt indefinitely. The box reported 387 of 755 GB
used and a load average of 43 with nothing of mine supposed to be running on it.

So the chain at 14:02 started with 352 GB available and 16 of 96 cores already gone. The mesh and
`topoSet` fit anyway. The split wanted 8 ranks at 47 GB, which is 378 GB, and 378 + 382 does not fit
in 755.

**The defect is that a partially killed mpirun leaves the survivors running forever.** There is no
reaper. One rank dying converts the box into a box with half the memory and a permanent load of 16,
and the next job to need real memory dies instead, in a stage that has nothing to do with it. Killing
the two containers took memory from 387 GB used to 5 GB and the load average to near zero within
twelve seconds.

Three things follow for anyone running this chain:

1. **Check for stray ranks before trusting any timing.** One `ps` filtered to `cartesianMesh` is
   enough. A clean box is part of the measurement.
2. **Never run a mesh concurrently with the layer pass.** The layer job is the memory hog that
   triggered both earlier kills, and what it killed was an innocent bystander both times.
3. The 778 s and 422 s above are **pessimistic**, not optimistic, because they were measured on the
   degraded box. They are not reported as the answer. A clean full re-run is.

I also deleted that run's log files while trying to archive them, because the archive `mkdir` failed
on `/mnt/work` ownership and I did not check its exit status before the `rm -rf`. The numbers survive
in this document and the oom evidence is in `dmesg`, which never lived in those files.

## The far field, and why killing those two runs cost nothing

Correcting myself before this gets quoted: these three cell counts were **already in the record**
before I went looking. I framed them as salvaged from doomed runs, which made them sound new. They
are not. What is new is only the reading in the last paragraph, and the operational point that both
surviving runs had already produced everything they were ever going to produce, hours before I killed
them, because cfMesh fixes the cell count during refinement and only then spends hours in the
optimiser.

| run | `maxCellSize` m | vs ff1 | cells | vs ff1 |
|---|---|---|---|---|
| ff1 | 0.000352056 | 1.00x | 269,516,428 | |
| ff2 | 0.000704112 | 2.00x | 267,282,301 | **-0.83%** |
| ff4 | 0.001408224 | 4.00x | 267,133,252 | **-0.88%** |

**A 4x coarser far field buys 0.88% of the cells, and the first doubling buys 0.83 of that 0.88.** It
saturates immediately. The reason is that refinement here is banded around the channels rather than
graded outward from the domain boundary, so the far field holds almost no cells to begin with and
doubling its cell size removes almost nothing.

All three hold the channel resolution fixed by adding levels as the background coarsens, +1, +2 and
+3 against ff1. All three are `scaleY 6` on `block.stl` without the chip band, so they compare to
each other and not to cbox2b's 504,715,948. The question was always a relative one and that
comparison is clean.

The sharper reading, which is the part that was not already written down: **the first doubling takes
0.83 of the 0.88 points and the second takes 0.06.** It saturates on the first step rather than
declining steadily, which is a stronger statement than a single -0.88% and is what makes the far
field dead as a lever rather than merely weak.

Neither surviving run could ever have reported anything further. ff4 lost its rank 1 to the 10:57 oom
kill above, and ff1 at `scaleY 6` was on the 38.5 hour path, which starting at 10:21 lands at 00:51
on 9 November against a box that expires at 10:21 on the 8th. So the two of them were spending 382 GB
and 16 cores re-deriving a recorded result they could not reach, which is what made them worth
killing on sight.
