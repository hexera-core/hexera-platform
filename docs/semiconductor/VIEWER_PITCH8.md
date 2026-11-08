# The pitch-8 deliverable in the viewer

Built 2026-11-08. `http://localhost:8788/cbox8`, job id `corintis-pitch8`.

Until today the viewer's coupled page was `cbox2b`, the pitch-10 mesh at 440,668,679 cells. The
pitch-8 deliverable appeared only as the two section pages. So the viewer was showing the previous
mesh while `MESH_PITCH8.md` described the current one.

## What the page states

| | |
|---|---|
| cells | 270,424,307 (fluid 55,303,441 / solid 199,394,482 / chip 15,726,384) |
| boundary faces | 36,490,208 (fluid 16,622,416 / solid 18,104,116 / chip 1,763,676) |
| conjugate interface | 16,619,777, `fluid_to_solid` = `solid_to_fluid` |
| chip contact | 270,816, `chip_to_solid` = `solid_to_chip` |
| ports | `port_a` 1,331, `port_b` 1,308 |
| drawn | 584,366 triangles, merged about 34 delivered faces to one |
| non-orthogonality | max 48.3939, **0 faces over the bar** |
| skewness | max 1.6456, **0 over the bar** |
| aspect ratio | max 3.7571, **0 over the bar** |

## The route, because the ordinary one was closed

`write_index` and `write_quality` both need a serial, ascii polyMesh. `cbox8` is neither. It was
split, checked and archived **decomposed** and in **binary**, and the reconstructed copies were
deleted after archiving, so there is no serial pitch-8 mesh anywhere - not on the box, not in the
bucket. The ordinary route would have been `reconstructParMesh -region X` three times, which is
the serial tail that `MESH_TIME_TO_ONE_HOUR.md` measures at 77% of the chain's wall clock.

Neither builder needs the volume. `write_index` reads `points`, `faces` and `boundary` and keeps
the boundary; `write_quality` adds `owner` and checkMesh's fields. All of those exist per
processor, and a region's real boundary is exactly the union of its non-`processor` patches across
the 8 processor directories: every boundary face lives on one processor, so the union neither
drops a face nor counts one twice.

**Binary was the easier case, not the harder one.** An ascii `faces` file has to be scanned to
find where the boundary starts, because the records are variable length -
`_stream_boundary_faces` walks the file counting `)` characters. The binary form is a
`faceCompactList`: an int32 offsets list of nFaces+1 entries, then the flat vertex list. Face `n`
is two seeks away. Measured layout on processor0/chip, confirmed before anything was written:

| file | contents | bytes | file size |
|---|---|---|---|
| `points` | 2,548,468 x 3 float64 | 61,163,232 | 61,164,161 |
| `faces` | 7,358,952 int32 offsets | 29,435,808 | |
| | then 29,435,804 int32 vertices | 117,743,216 | 147,179,968 |

`label=32`, `scalar=64`, LSB, from the mesh's own Arch line.

| stage | wall clock |
|---|---|
| boundary index, all three regions | **13 s** |
| `checkMesh -writeFields`, all three regions, 8 ranks | ~3 min |
| quality sidecar, all three regions | **9 s** |
| `reconstructParMesh` x3, which this replaced | not run; the measured serial tail is hours |

## The gates

Nothing was trusted. Each builder refuses rather than writes if a gate fails.

**The index.** Summing every patch across the 8 processors, real and `processor` alike, must equal
checkMesh's own global `faces - internal faces`. Measured before the code was written:

| region | real | processor | sum | checkMesh |
|---|---|---|---|---|
| fluid | 16,622,416 | 793,862 | 17,416,278 | 17,416,278 |
| solid | 18,104,116 | 1,797,772 | 19,901,888 | 19,901,888 |
| chip | 1,763,676 | 247,450 | 2,011,126 | 2,011,126 |

**The quality.** For `nonOrthoAngle` and `aspectRatio` the maximum over all cells of all
processors must equal the maximum checkMesh printed for that region. For `skewness` every patch's
value count must equal that patch's face count in the index, and the boundary maximum must not
exceed checkMesh's all-faces maximum.

| region | metric | parsed | checkMesh said |
|---|---|---|---|
| fluid | non_ortho | 47.92129898071289 | 47.9213 |
| fluid | aspect_ratio | 3.739229917526245 | 3.73923 |
| fluid | skewness (boundary) | 1.645550012588501 | 1.64555 |
| solid | non_ortho | 50.512901306152344 | 50.5129 |
| solid | aspect_ratio | 3.757050037384033 | 3.75705 |
| chip | non_ortho | 0.002414159942418337 | 0.00241416 |
| chip | aspect_ratio | 1.0000799894332886 | 1.00008 |

**The alignment.** The quality arrays are assembled patch-major in the index's exact order and
refuse unless each patch starts where the index puts it, each patch's count matches, and the whole
array is the index's `n_faces` long. Verified again after shipping: index and quality agree face
for face on all three regions and every array is exactly 4 bytes per face.

**The cell counts** are measured twice by tools that share no code path. topoSet's `cellZoneSet`
sizes state fluid 55,303,441 and chip 15,726,384 directly; `plateCells` less air is 270,424,307,
which less the fluid zone leaves the solid's 199,394,482 exactly.

Recorded rather than smoothed over: `RESULT.txt`'s "Total number of cells 301,945,509" is
1,079,966 fewer than the 303,025,475 topoSet reports for `plateCells` before the air is removed,
so those two intermediate figures are not on the same basis. Every delivered figure above is.

## What the page does not have

**Per-face colouring on the drawn surface**, same as `cbox2b` and for the same reason: the surface
is merged about 34 delivered faces to one drawn, and a merged triangle has no single metric value.
The quality is there in the three places it can be - the panel's figures, the `over the bar`
counts, and the **detail window**, which is unreduced and paints real cells. Clicking the plate
opens 144 real cells coloured by non-orthogonality.

**A serial volume mesh.** This produced an index and a `boundary` file, which is what travels to
the viewer anyway. It is not a reconstruct and must not be read as one.

## A mislabel this exposed, which was not new

The ParaView button said `.vtk with quality fields` on every page. `vtk_export` writes a quality
SCALAR only for a metric it finds per-face values for, so on a merged surface the file is
`patch_id` and the geometry. Measured across the roster:

| | pages | vtk carries |
|---|---|---|
| reduced | cbox8, cbox2b, cbox2, bl3, plate, chip, assembly | `patch_id` only |
| unreduced | slab8, slabw | `patch_id`, `non_ortho`, `skewness`, `aspect_ratio` |

Seven of nine pages promised a file the user would not get. The label now reads
`.vtk, patches only` where that is true, with a title saying a section page carries the per-face
numbers. Both branches checked in the live DOM.

## Where it all is

| | |
|---|---|
| sidecar (index + quality) | `gs://dev-rehaan-artifacts-224734058693/corintis/sidecar/cbox8/cbox8_sidecar.tar.gz`, 545 MiB |
| mesh, decomposed + binary | `gs://dev-rehaan-artifacts-224734058693/corintis/mesh/cbox8/` |
| layered coolant | `gs://dev-rehaan-artifacts-224734058693/corintis/mesh/bl8/` |
| builders | `devtools/semiconductor/viewer/par_boundary_index.py`, `par_boundary_quality.py` |
| payload | `corintis_view/cbox8_viewer_data.json`, 17.1 MB |

Both archives were verified by extracting them and reading the metadata back, not by trusting the
upload.
