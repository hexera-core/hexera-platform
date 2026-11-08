# The mesh viewer, and the boundary index that makes it possible

Internal. This is how I look at the Corintis cold-plate meshes: what runs, what it serves, and the
sidecar that lets a detail cut work on a 413 million cell mesh whose workspace is not on the
machine. Written because no document for it existed and the knowledge was in three places at once:
the server's own docstrings, `render/boundary_index.py`, and 140 KB of viewer JavaScript.

Everything below traces to a file I read or a number I measured on 2026-11-06 in WSL. Where I did
not verify something I say so in the line.

## 1. Running it

```
cd /home/rehaan/corintis_view
VIEWER_NO_FETCH=1 /home/rehaan/hexera-platform-semi/.venv/bin/python serve_view3.py 8787
```

The repo's venv python, not the system one. A cut needs numpy and under the system python it fails
at the import rather than at the data (`serve_view3.py` line 51). `VIEWER_NO_FETCH=1` because a
machine that stages its own files gains nothing from a bucket round trip, and gcloud does not
always decline quickly: a credential that needed re-proving once held startup past 85 s with the
port unbound.

`serve_view3.py` is the one to run. `serve_view.py` (8 KB) and `serve_view2.py` (13 KB) beside it
are earlier versions. `devtools/semiconductor/viewer/serve_view.py` in the repo is a 46 KB fourth
copy with its own README; the laptop copy is 47 KB and is what I have been using. I have not
diffed the two.

| variable | default | what it does |
|---|---|---|
| `PORT` | argv[1], else 8787 | read from the environment first, because Cloud Run assigns it and passes no argv |
| `VIEWER_REPO` | `/home/rehaan/hexera-platform-semi` | where `src/` and the static assets are |
| `VIEWER_HOME` | `/home/rehaan/corintis_view` | where the payloads and sidecars live |
| `VIEWER_SIDECARS` | `$VIEWER_HOME/sidecar` | a mounted path holding the sidecars, for Cloud Run, where a volume mounts a whole bucket and cannot mount a prefix |
| `VIEWER_NO_FETCH` | unset | do not reach for the bucket at all |
| `HEXERA_ARTIFACTS_BUCKET` | `dev-rehaan-artifacts-224734058693` | the persistent bucket, not the exchange bucket, which deletes at two days |

### What it serves

| route | body |
|---|---|
| `GET /` | internally rewritten to the first of `assembly`, `plate`, `chip` that loaded |
| `GET /<slug>` | the page, slug one of `assembly`, `cbox2`, `plate`, `chip` |
| `GET /<slug>?bare=1` | the same page with the mesh's own title in place of the brand, for the `/both` iframes |
| `GET /both` | every loaded mesh side by side in iframes, with the reason measured off the payloads |
| `GET /api/v1/client-config` | `{"viewer": ..., "intake": {}, "auth": {}}`, the viewer block read from `meshpipeline.settings.policy` |
| `GET /api/v1/simulation/<job>/surface` | the stored payload's `surface`, plus `job_id` and `up_axis` |
| `GET /api/v1/simulation/<job>/surface.vtk` | `render.vtk_export.surface_to_vtk`, as an attachment |
| `GET /api/v1/simulation/<job>/detail?x&y&z&radius` | the cut, or 409/422 with the reason in `detail` |
| `GET /static/js/...` | `$VIEWER_REPO/ui/js`, with `vendor`, `css` and `assets` off `apps/console/public/static/` |

Anything else is a JSON 404. Mounts are path-checked against `resolve()`, so a traversal gets 403.
The server is a `ThreadingHTTPServer` because `/both` is two viewers at once and a single-threaded
one queued the second iframe behind the first for every module, for vtk.js and for a 14 to 33 MB
payload.

### The four jobs

| job id | slug | payload on disk | drawn patches | parts |
|---|---|---|---|---|
| `corintis-assembly` | `assembly` | 31.8 MB | 12 | 14 |
| `corintis-coupled` | `cbox2` | 16.4 MB | 6 | 11 |
| `corintis-plate` | `plate` | 13.6 MB | 5 | 7 |
| `corintis-chip` | `chip` | 18.2 MB | 7 | 9 |

`cbox2` is the coupled plate and chip, one mesh of four zones with three delivered, and the current
deliverable. It is **not** in the root's fallback list, so `/` never opens it while an assembly,
plate or chip payload is present. Reach it at `/cbox2`. A job whose payload cannot be read is named
and skipped rather than fatal, and `BY_SLUG` is rebuilt after the skip so a dead slug cannot route
to a job whose `surface` was never loaded: a plate still being staged used to take the chip page
down with it.

## 2. Architecture

Two halves, and the split is the whole design.

| | precomputed into the payload JSON | read live per request |
|---|---|---|
| what | the drawn surface, decimated to a triangle budget; the parts roster; every quality figure and limit | the detail cut: one ball of unmerged boundary faces, with their checkMesh values |
| from | `build_coupled_payload.py` / `build_plate_payload.py`, off the sidecar's own arrays | `render.detail_window.detail_at` over the sidecar, memory-mapped |
| size | 13.6 to 31.8 MB of base64 | 89 KB of points and 142 KB of polys for a 1 mm ball on `cbox2` (measured) |
| cost | once, by hand | 0.59 to 1.30 s per cut (measured, section 5) |

The payload exists because the mesh cannot be sent. The live half exists because the payload is
merged, so nothing drawn at that budget can be trusted at cell scale. The stub serves the same
three URLs the API serves with the same bodies, pointed at payloads produced by hand rather than by
a job, and the panel on screen is `ui/js/viewer/viewer.js` off this branch under
`apps/console/public/static/css/workbench.css`: not a mock of the viewer, the viewer.

## 3. The boundary index sidecar

This is the interesting part. `render/boundary_index.py` writes it once, where the mesh is. It
carries the boundary's own points and connectivity, so it answers **without the mesh it was built
from**. Confirmed directly: on all six `cbox2` and `plate` regions, `faces` and `points` are absent
and every index still opens. The layout has to be exactly a workspace's, because
`detail_window.delivered_regions` finds regions by globbing `constant/*/polyMesh/boundary`, so
`sidecar/<slug>/constant/<region>/polyMesh/` holds the real 1.5 KB `boundary` file, `boundaryIndex/`
and `boundaryQuality/`. Flattening it would leave every file present and every cut impossible.

### The files

| file | dtype | shape | meaning |
|---|---|---|---|
| `points.f32` | float32 | (n_points, 3) | the boundary's **own** points, compacted and renumbered off the volume's point list |
| `sizes.u8` | uint8 | (n_faces,) | corner count per face. A face under three corners stays in the numbering and is dropped by the reader |
| `conn.u32` | uint32 | (sum of sizes,) | every face's corners end to end, as local ids into `points`. A patch's faces are contiguous here |
| `centroids.f32` | float32 | (n_faces, 3) | one centroid per face, in the index's own face numbering |
| `patch_of.u16` | uint16 | (n_faces,) | which patch each face belongs to, or `NO_PATCH` = 0xFFFF for a face no patch names |
| `bucket_face.u32` | uint32 | (n_faces,) | face ids sorted by bucket |
| `bucket_start.i64` | int64 | (n_buckets + 1,) | where each bucket's run starts in `bucket_face` |
| `meta.json` | | | the patch table, the grid, the bbox, the byte sizes and the source stamp |

Nothing is read at open time. Every array is a `np.memmap`, so opening an index costs a handful of
stat calls whatever the mesh's size. The one thing that cannot be mapped is the per-face offsets
into `conn`, a running total: `BoundaryIndex.offs` derives it on first use and only `pack` asks for
it. Before that it was in `open_index`, and a request opens the index four times, so 0.55 s of a
0.78 s request was one `cumsum` done four times (measured 2026-10-30, 13,181,685-face coolant).

### The real numbers, `cbox2/constant/fluid`

Read out of
`/home/rehaan/corintis_view/sidecar/cbox2/constant/fluid/polyMesh/boundaryIndex/meta.json`.

| `version` | `region` | `n_faces` | `n_points` | `first_face` |
|---|---|---|---|---|
| 2 | fluid | 21,711,994 | 21,733,689 | 197,788,633 |

| patch | type | first_face | n_faces |
|---|---|---|---|
| `fluid_to_solid` | mappedWall | 0 | 21,708,529 |
| `port_a` | patch | 21,708,529 | 1,759 |
| `port_b` | patch | 21,710,288 | 1,706 |

The grid origin is the centroid bbox's own minimum, so the two tables below share three numbers.
`edge` is 0.000966472313679858 m, that is 966.47 um, `dims` is 177 x 39 x 101, `n_buckets` is
697,203, and the mean occupancy is 31.1 faces per bucket.

| axis | bbox min (m) | bbox max (m) | span |
|---|---|---|---|
| x | -0.05960934981703758 | 0.11061300337314606 | 170.22 mm |
| y | 0.021611500531435013 | 0.05864174664020538 | 37.03 mm |
| z | -0.006676749791949987 | 0.09049595147371292 | 97.17 mm |

| file | bytes | = |
|---|---|---|
| `points.f32` | 260,804,268 | n_points x 12 |
| `sizes.u8` | 21,711,994 | n_faces x 1 |
| `conn.u32` | 347,577,728 | 86,894,432 ids, 4.0022 per face |
| `centroids.f32` | 260,543,928 | n_faces x 12 |
| `patch_of.u16` | 43,423,988 | n_faces x 2 |
| `bucket_face.u32` | 86,847,976 | n_faces x 4 |
| `bucket_start.i64` | 5,577,632 | (n_buckets + 1) x 8 |
| **total** | **1,026,487,514** | 979 MiB |

The source stamp in the same file says what was indexed: `faces` 8,466,782,519 bytes and `points`
2,642,672,807 bytes, neither of which is on this machine.

### All three regions, and what the sidecar costs

| region | n_faces | n_points | buckets | edge | index | quality | source faces + points |
|---|---|---|---|---|---|---|---|
| fluid | 21,711,994 | 21,733,689 | 697,203 | 966.47 um | 979 MiB | 249 MiB | 11.11 GB |
| solid | 23,651,176 | 23,673,617 | 750,834 | 1004.96 um | 1.1 GiB | 271 MiB | 43.89 GB |
| chip | 2,309,716 | 2,309,720 | 78,408 | 650.77 um | 105 MiB | 27 MiB | 3.56 GB |
| **total** | **47,672,886** | | | | **2.254 GB** | **0.572 GB** | **58.56 GB** |

The index is 3.85% of the two files it was built from. `index_cbox.sh` prices the build at 105 s
and 623 MB for 13.2 M faces on the plate; the coupled mesh's `write_index` + `write_quality` took
180 s for all three regions (DEVLOG, the cbox2 port-split table).

`boundaryQuality/` is three more float32 arrays of n_faces each, written by
`render/boundary_quality.py` from `checkMesh -writeFields`. The basis differs per metric and is
recorded rather than implied: `skewness` is the boundary face's own value, judged against
checkMesh's boundary bar of 20 and not the internal 4, while `non_ortho` and `aspect_ratio` are the
owner **cell's**, because a boundary face has no non-orthogonality of its own and checkMesh writes
the patch as `uniform 0` or `zeroGradient`. It carries its own version number, because a region
whose fields checkMesh did not write still gets an index and still cuts, without colours.

### How a query becomes faces

`_grid` picks one edge for all three axes, so a ball of radius r touches the same number of buckets
whichever way it points. `want = min(4_000_000, n_faces // 32)` from `MAX_BUCKETS` and
`TARGET_FACES_PER_BUCKET`; `edge = (span.x * span.y * span.z / want) ** (1/3)`;
`dims = ceil(span / edge)`, then `edge *= 1.26` until `prod(dims) <= MAX_BUCKETS`, which halves the
bucket count per step and exists for a degenerate span. The flat id is
`(i * dims.y + j) * dims.z + k`. On the fluid side that is `want` = 678,499 and 697,203 buckets at
966.47 um, which reproduces the meta exactly.

`BoundaryIndex.faces_near(centre, radius)` then takes
`lo = clip(floor((c - r - origin) / edge))` and `hi` the same with `+ r`, which is at most
`(2r/edge + 2)^3` buckets; gathers one slice of `bucket_face` per (i, j) row rather than one per
bucket, because the k axis is contiguous in the flat id; and tests the candidates' centroids
exactly against r squared, dropping any face with fewer than three corners. The answer is the same
set a whole-boundary scan returns, at the cost of the window.

`patch_slices` then splits those ids by patch, and a face carrying `NO_PATCH` falls out rather than
taking the nearest patch's name. That sentinel is not decorative: initialised to zero, the faces
inside a `skip_names` hole would come back under the **first** patch's name, which is a reader who
asked for the coolant being handed the metal.

## 4. `_source_stamp`, and the failure it causes

The stamp is written into `meta.json` as `source` and checked on every `open_index`.

| file | what is stored | why |
|---|---|---|
| `faces` | size + sha256 of the first and last 1 MiB | a full hash of 8.5 to 34.6 GB is not affordable per request |
| `points` | size + sha256 of the first and last 1 MiB | same |
| `boundary` | size + **full** sha256 | about 1.5 KB, and it is the thing that says which surface this is |

Not mtime, deliberately. An index is meant to travel, and `tar -cpzf` / `tar -xpzf` came out with
the seconds intact and the nanoseconds zeroed, so an mtime stamp refused an index that had just
been shipped correctly. A guard that fires on the legitimate case is worse than none. Size alone is
not enough either: rewriting a test mesh with one cell stretched leaves `points` at exactly the
same byte count, because "(3 0 0)" and "(4 0 0)" are the same length, so a size-only stamp answered
a changed mesh from the old index.

### What a mismatch does, measured today

**The plate's fluid region is in this state right now.**

```
sidecar/plate/constant/fluid/polyMesh/boundary   1462 bytes on disk
meta.json source.boundary.size                   1526
                             sha256              MISMATCH
```

The file on disk types both ports as `patch`. The indexed one typed them `wall` and put them in the
wall group, which is the defect `fixed/ws_shell/README.txt` was written about. Somebody corrected
the boundary file in place, and `open_index` now raises `IndexUnavailable: the mesh's boundary
changed since this index was written`.

The consequence is not a silent drop on this mesh, it is worse. `detail_at` catches
`IndexUnavailable`, falls through to the polyMesh parse, finds 14,699,776 faces against
`MAX_SOURCE_FACES` = 2,000,000, and raises `DetailError: the fluid boundary is 14,699,776 faces and
this reads at most 2,000,000 at once, so its detail is not available`. Three things are wrong with
that, and all three are the stamp's doing:

1. The message names a face count as the cause. The cause is a 64-byte boundary file.
2. `DetailError` aborts the **whole** `detail_at`, not the one region, so the plate's metal side
   is lost with the coolant even though its own stamp matches.
3. The page still advertises the cut as live. `serve_view3.cut_regions` calls
   `delivered_regions`, which only globs for a `boundary` file and never opens the index, so the
   header badge reads "cuts live" and every cut returns 409.

Where a region **is** under 2 M faces the drop is genuinely quiet: the parse branch serves it with
no quality colours at all, because the per-face values are numbered against the index. A cut that
came back grey is the only symptom.

The assembly page reads its cuts from the plate and chip sidecars, so by the same path its plate
body now refuses and only the chip answers. I did not run the assembly path end to end; that one is
read off `serve_view3._detail`, not measured.

`open_index` has a second guard that is not optional, and it is the one that catches the rest:
every patch in the index must have the same face count in the live `boundary` file. Without it a
disagreeing boundary file does not error, it silently drops patches or returns one patch's faces
under another's name, because `detail_at` keeps only the faces whose patch the boundary names.

## 5. Decimation, and why the coolant looks smooth

`render/polymesh_budget.fit_to_budget` is what makes the payload possible, and it is why nothing in
the main view can be measured at cell scale. `DRAWN_MAX_TRIANGLES` is 800,000,
`MIN_TRIANGLES_PER_PATCH` is 64 and `_MAX_PASSES` is 6.

Per patch: triangulate the quads as a fan, pick a cluster size `h = sqrt(2 * area / allowance)`
from the patch's own area, snap every point to the centre of the `h` cell it falls in, reindex,
drop collapsed and repeated triangles, and if it still overshoots grow `h` by
`1.1 * sqrt(got / want)` and go again, up to six times. The budget is shared out in proportion to
each patch's face count with the floor above, so a conjugate mesh whose faces are nearly all on one
interface does not spend its whole allowance on the block sides.

The real ratios, read out of the `reduced` block of each payload:

| payload | delivered faces drawn | drawn triangles | faces to one drawn | coarsest cluster |
|---|---|---|---|---|
| `cbox2` | 25,611,173 | 586,162 | **43.7** | 1369.1 um |
| `plate` | 15,314,955 | 486,372 | 31.5 | 1907.2 um |
| `assembly` | 16,170,643 | 1,134,586 | 14.3 | 1907.2 um |
| `chip` | 855,688 | 648,214 | 1.32 | 223.8 um |

Per patch on `cbox2`:

| patch | delivered | drawn | ratio | cluster |
|---|---|---|---|---|
| `fluid/fluid_to_solid` | 21,708,529 | 478,388 | **45.4** | 438.9 um |
| `solid/solid_to_air` | 1,589,463 | 48,310 | 32.9 | 1369.1 um |
| `chip/chip_to_air` | 1,956,532 | 48,568 | 40.3 | 704.3 um |
| `chip/chip_to_solid` | 353,184 | 10,780 | 32.8 | 704.3 um |
| `fluid/port_a` / `port_b` | 1,759 / 1,706 | 54 / 62 | 32.6 / 27.5 | 1305.7 / 1285.7 um |

That 438.9 um is the number that answers the question. Every point of the drawn coolant has been
snapped to a 438.9 um grid. The mesh's microchannel cell is 22.0 um (`cell_stats.p10` = 21.9997 um
in the payload), the channel pitch is 201.263 um and the channel itself is about 100 um, so the
drawn coolant is clustered at about 20 cell widths and a little over two channel pitches. The
channels cannot survive that, and what is left reads as a smooth tube. Not a rendering artefact and
not the mesh being coarse: 45 faces becoming one.

The viewer says so rather than implying it. `inspect.mergeFactor` divides the two counts in
`reduced` and `odometerText` prints, whenever the factor is over 1.5, "merged surface, about 44
faces to one drawn" beside the pixel scale. Those two counts are named apart deliberately:
`delivered_faces` is the mesh's own quads and n-gons, `drawn_triangles` is triangles, and a first
version summed the fan triangulation into `delivered_faces` and reported 27,020,802 for a boundary
of 13,504,416. The drawn totals also land under the 800,000 budget rather than at it because each
patch gets its own allowance and the clustering overshoots downward within it.

## 6. The parts model

`render/mesh_facts.py` declares the parts; the viewer renders what is declared and decides nothing
about which entities exist. The kind set is closed on both sides on purpose: adding one is an edit
in `mesh_facts.KINDS` **and** in the viewer's label table, and that pairing is the point.

| kind (`mesh_facts`) | client label (`viewer.js` KIND) |
|---|---|
| `boundary` | Boundary patches |
| `region` | Regions |
| `cellGroup` | Cell types |
| `inspection` | Inspection structures |
| `poorCells` | Poor-quality cells |

A part carries an id, a label, a kind, whether geometry shipped for it, and how it should open. The
client adds two rules. First, `renderable` is the declared flag **and** the part actually having an
actor in this payload; entities resolve through `metadata.drawn_as`, a list, because a multi-region
engine draws `<region>/<patch>` and both regions can reach `ymin`. Second, a part whose role
encloses the domain opens hidden and translucent, decided by the backend from the case's declared
roles and never by name, because the first sight of a conjugate mesh should be the coolant and not
six grey rectangles sealing it in.

`cbox2`'s roster, from the payload:

| part | kind | renderable |
|---|---|---|
| `boundary:fluid_to_solid`, `boundary:port_a`, `boundary:port_b` | boundary | yes |
| `boundary:solid_to_air`, `boundary:chip_to_air`, `boundary:chip_to_solid` | boundary | yes |
| `boundary:solid_to_fluid`, `boundary:solid_to_chip` | boundary | **no** |
| `region:fluid`, `region:solid`, `region:chip` | region | yes |

The two un-renderable ones are the far sides of interfaces already drawn from the other region.
They stay in the list, inspect-only, because the case declares them.

Isolate is the subtle one. An actor's visibility follows the state of the part that **draws** it,
not the part that was picked, so `unhide(id)` clears the hidden flag on every part sharing an
entity with the picked one. Without that, isolating the metal left an empty viewport: the metal
region paints nothing itself, it is drawn by six surfaces that all open hidden.

`POOR_CELLS` is a kind of its own on both sides because these were arriving as ordinary boundary
patches, listed between `ymin` and `zmax` in a wall's palette colour. A highlight that looks like a
wall is not a highlight. On the client the colour is keyed off the drawn patch's **type**,
`_POOR_TYPE = 'poorQuality'`, painted `[0.93, 0.27, 0.22]`, while the roster groups by the part
**kind** `poorCells`: two different strings, both needed. No Corintis payload carries a `poorCells`
part, so that path is unexercised on these meshes, because `cfmesh_conjugate/viewer.py` caps the
highlight at `POOR_MAX_TRIANGLES` = 400,000 and the plate metal's concave-cell surface is 862 MB.
3,676,061 concave cells out of 241,984,911 is the staircase the octree cut, not a spot on it, so
the count goes in the panel instead.

## 7. The detail window

Click to inspect. It was one button called "Detail", built with `className='v-dlbtn'` and appended
to the downloads bar, cutting at `cam.getFocalPoint()` with no scale legend anywhere: three faults,
none of them the cut. It looked like a download, you had to aim the whole camera at a thing to read
it, and it never said how big anything was.

| control | what it does |
|---|---|
| Inspect | a mode, said three ways: the button's `aria-pressed`, a crosshair cursor, and the hint under the canvas |
| dial | the ball's radius, geometric from `4 * cell_stats.p10` to half the drawn diagonal, 101 steps, opening at 38 |
| Shell | opacity on the enclosing patches, so the channels are seen inside the block they are cut from |
| Clip | hides one side of an axis plane. Nothing is removed |
| Worst | flies to the worst face on the picked metric and opens the window on it |
| Fly | walks the camera along the part's long axis |

On `cbox2`, `p10` is 21.9997 um so the dial floor is 88.0 um. The top is half the drawn diagonal,
which I did not measure. The dial is geometric and not linear because the useful range spans four
decades, and a linear dial spends ninety of its hundred steps on radii that return the same
whole-part answer.

The query is `GET /api/v1/simulation/<job>/detail?x&y&z&radius`, the point being `cellCenter` of the
face `pickCell` returned. A drag is not a click: a pointer that moved more than 4 px does not cut,
or every orbit that ended over the mesh fired a request. The dial moves its label on every tick and
re-cuts only on `change`, because the plate reads from 1.66 GB of index and a slider dragged across
its range is forty requests.

What comes back, per patch: `name`, `type`, `face_count`, `faces_in_range`, `points_b64`,
`polys_b64`, and where the quality sidecar exists `quality_b64` per metric, `quality_basis` and
`quality_metrics`. The buffers are the same float32 xyz and uint32 `[n, ids...]` pair the main view
is drawn from, so the client draws a cut with the code it already has.

**Nothing in a cut is merged.** One delivered face is one polygon in the order the route packed it,
so a per-face number is a per-polygon number and painting it is honest. On the drawn surface it is
not: one drawn face stands for about 44 delivered ones and a colour on it was measured on none of
them. That is why the main view has no per-face colours and a cut does.

**The merged surface stays, as a ghost** at `GHOST_OPACITY` = 0.10 with its edges off. It used to
be switched off outright, which left a scrap of cells floating in black with no way to tell where
on a 178 mm plate they were.

**The budget is shared out, not eaten first come.** `MAX_RETURN_FACES` is 150,000 and `_share_out`
gives every patch with a face in range at least one, divides the rest by largest remainder, and
spreads the kept faces evenly within a patch rather than taking a prefix. Before that, a 4 mm
window with 684,660 faces in range came back as 150,000 faces of `fluid/fluid_to_solid` with the
metal side missing altogether and the payload saying only `truncated: true`.

Measured on `cbox2` on 2026-11-06, centre (0.0401005, 0.0219072, 0.0306438) m, which is a face
centroid on the coolant interface:

| radius | wall time | faces in range | returned | truncated | patches returned |
|---|---|---|---|---|---|
| 0.5 mm | 1.213 s | 3,340 | 3,340 | no | 2 |
| 1 mm | 0.592 s | 14,240 | 14,240 | no | 2 |
| 2 mm | 1.226 s | 114,692 | 114,692 | no | 4 |
| 5 mm | 1.302 s | 1,164,370 | 150,000 | **yes** | 5 |

`open_index` cost 0.341 s on the fluid side cold and 0.002 s on the solid side with the page cache
warm. The 1 mm cut returned 7,120 faces on `fluid/fluid_to_solid` and 7,120 on
`solid/solid_to_fluid`, which is the conjugate interface answering from both sides at equal count,
and 89,280 bytes of points against 142,400 of polys. All three metrics came back with their bases
(`skewness` patch, `non_ortho` and `aspect_ratio` owner_cell).

The label a reader gets is `inspect.cutText`: "14,240 real cells, unmerged", or on the 5 mm cut
"150,000 of 1,164,370 faces here, narrow the window for all of them". Narrow the window, not zoom
in: the window stopped being the camera's.

## 8. Traps and failure modes

| symptom | cause |
|---|---|
| the cut fails at an import, not at the data | run under the system python, which has no numpy. Use the repo venv |
| startup hangs with the port unbound and nothing on screen | gcloud waiting to re-prove a credential. It is now run with stdin closed and the fetch is skippable with `VIEWER_NO_FETCH=1`. Measured at 85 s before the fix |
| "cuts live" in the header and every cut 409s | `cut_regions` only globs for a `boundary` file; it never opens the index. The plate is in this state now, section 4 |
| cut refused naming a 14,699,776 face count | the boundary file's sha256 moved. The face count is not the cause |
| a cut comes back grey on a region that used to have colours | the index was refused and the parse branch served it. The parse branch has no per-face quality by construction |
| one region's stale index kills the whole cut | `DetailError` is raised inside the region loop in `detail_at` and aborts all of them |
| every reading stacked full width down a 2000 px page, 3D view below the fold | the page has no `#workbench` and `#app`, so `openViewer` took its inline fallback layout. Same viewer, wrong half of its own CSS |
| two panels of solid black on `/both`, payloads loaded and readings correct | grid auto-placement dropped `#workbench` into column 1, which `wb-collapsed` sets to 0 px, and vtk's `Math.max(2, ...)` floor gave a 2 pixel render buffer. Pinned with `#app.wb>#workbench{grid-column:2}`. If instead both go black only while the server works, that is `HTTPServer` answering one request at a time |
| a figure that outlived its mesh: "328,714,251 cells", or "486,372 boundary faces" on a 30,011,162-face boundary | both were typed in or summed from `face_count` after `fit_to_budget`, which counts drawn polygons. `_subtitle` now counts off the payload being served and states cells only where the payload states them |
| the chip page's refusal text says it parses straight from the polyMesh | stale. The chip sidecar does have a working `boundaryIndex` (855,688 faces, opens clean) and no `faces` or `points`, so it cuts from the index like the others |
| a cut on the assembly lands in the wrong place | the chip's index is in the chip's own frame. `_mover` reads the transform out of the payload, inverts it to ask and applies it to the points on the way back. Hard-coding those numbers here is how the drawing and the cutting drift apart |
| clicking Detail threw `ReferenceError` on every click | `tanHalf` was a `const` in one closure and called from a sibling one. A feature with a route, a bounded reader, a face budget, a button and thirty Python tests that had never once worked in a browser. `viewer_check.sh` now asserts exactly one `const tanHalf=` per copy |
| painting a metric with a cut open coloured nothing visible | it painted the merged surface, which is the surface the cut hides |

Two copies of the viewer JavaScript exist and they drift: `ui/js/viewer/viewer.js` (139,286 bytes)
is the source of truth and `apps/console/public/static/js/viewer/viewer.js` (115,452 bytes) is the
console's mirror, 507 differing lines apart today, which is why the stub mounts `/static/js` at
`ui/js`. `devtools/semiconductor/viewer_check.sh` checks the two things that need no browser: both
copies parse as ES modules, and `detailWindow`, `heatmap`, `radiusNow`, `tanHalf`, `closeDetail`
and `_vdbg` are in both. It checks nothing about whether any of it works.

### The test suite

`tests/ui/` drives the shipped page in a real Chrome over CDP, 146 tests across 30 files, about
four minutes. With no browser on PATH it **fails rather than skips**, deliberately: a release must
not pass with its browser validation quietly absent. The `chromium` apt package is a snap wrapper
and will not run in WSL, so install Google's own .deb. Six files import `openViewer` and drive
`window._vdbg[job]` directly, `test_detail_window.py` among them, one test per shipped defect and
each one checked by putting its defect back and watching it fail. `window._vdbg[job].detail()`
exposes `arm(on)`, `cut(at)` and the current `armed`, `centre`, `clip`, `xray`, `scale` and
`odometer`, so a test does not have to pick pixels.

Two ways the suite reports a wall of identical non-results, neither of them a page defect:

1. `pytest tests/ui tests/unit/...` in one invocation. With both directories collected,
   `from conftest import assert_clean` resolves to `tests/unit/conftest.py` and all 17 viewer files
   error on import. Run `tests/ui` on its own.
2. **The shared socket.** The `browser` fixture is session-scoped, and `test_thermal_results.py`
   runs a real mesher inside the test process, holding the interpreter long enough that the session
   browser's CDP websocket misses a keepalive and closes with `ConnectionClosedError: sent 1011
   keepalive ping timeout`. That file takes about 110 s where every other viewer file takes 7 to
   13, and it sits on the edge: it passes when it reaches the end and dies around 51 s when it does
   not, after which every test scheduled behind it errors at setup with the same message.
   pytest-randomly is installed, so the damage moves: 85 passed, then 110, then 132 with
   `-p no:randomly`. A whole-suite number is not the gate's verdict. One file per invocation and
   sum: 144 of 146 over 30 files, the two outstanding being that file's, which has been seen to
   pass alone.

## 9. What is not in it, and worth adding

**Nothing internal.** The index is the boundary and only the boundary, so no cut shows a cell's
interior or an internal face. `cbox2` has 197,788,633 internal faces in the coolant, 809,069,266 in
the metal and 71,364,562 in the chip, none of them reachable. A clip plane hides geometry, it does
not cut cells.

**No cell count on the coupled mesh.** `cbox2`'s payload carries no `cells`, correctly, because an
index knows its faces exactly and its cells not at all and the builder refuses to estimate. So the
panel says 47,672,886 boundary faces and the 413,273,323 cells live only in
`COUPLED_MESH_DELIVERY.md`. Closable by carrying the per-region counts through from checkMesh.

**"Worst" has nothing to fly to.** `cbox2`'s `quality_fields.hotspots` is `[]`. The sidecar can
produce them (`boundary_quality._sidecar_hotspots`, `MAX_HOTSPOTS_PER_METRIC` = 60) and the payload
builder did not ask. Cheapest real improvement here: the arrays already exist.

**No channel width anywhere.** The finding that matters most on this part is in
`MESH_QUANTISATION.md`: the mesh snaps every 100 um channel to 88.014, 110.017 or 132.021 um, 3.38x
flow-conductance scatter against the CAD's 1.09x. It was measured out of this same index by
raycasting the sidecar's own faces. Nothing in the viewer says it, and at a 438.9 um cluster
nothing in the drawn surface could.

**No air region**, so `chip_to_air` and `solid_to_air` draw as free surfaces with nothing behind
them. **The sidecar must be local and whole**: 2.7 GiB for `cbox2`, no range requests and no
partial index. `VIEWER_SIDECARS` points at a mounted bucket for Cloud Run, which I have not
measured, and `native.INDEX_SKIP_ABOVE_FACES` = 120,000,000 means a big enough mesh gets no index
at all.

**No guard tying the badge to reality.** `cut_regions` should open the index rather than glob for a
boundary file, and `detail_at` should refuse one region rather than the whole cut. Both are small,
and between them they are the entire plate failure in section 4.

## The 800,000-triangle budget is a transport artifact, not a rendering limit

Measured 2026-11-08 in the app's own browser pane, with the **vendored** vtk.js bundle (sha256
`393d9391b926ade6c59fda061eca0043bccba85e157e2ede5df11607085d8b7a`, matching
`apps/console/public/static/vendor/SHA256SUMS`), on an Intel UHD iGPU via ANGLE/D3D11, WebGL2,
canvas 1897x1200.

`polymesh_budget.py:14-26` states the reason for the budget in its own words: packing the whole
coolant came to "555.8 MB base64 - past the 512 MiB cap" with "one to three seconds of JSON parse".
That is a statement about **base64 in JSON**, and it has been read ever since as a statement about
what a browser can draw. It is not.

### One patch at 1:1, with real per-face colour

`solid_to_air`, **1,904,636 faces / 3,810,902 triangles**, one actor, per-face `non_ortho` colour:

| | |
|---|---|
| draw calls per frame | **1**, `GL_TRIANGLES`, 11,432,706 vertices = **exactly 3,810,902 triangles** |
| the per-face guard at `viewer.js:1363` | `1,904,636 === 1,904,636` -> **PASS** |
| construct + first frame | 1,066 ms, of which polydata/mapper/actor is **2.4 ms** |
| median frame, 119 frames | **18.10 ms = 55.2 fps**, vsync-bound |
| **median `render()` CPU** | **0.30 ms**, p95 0.60 ms |
| GL buffers | 174.4 MB in 2 `bufferData` calls |
| context lost | no |

**0.30 ms of CPU to re-issue 3.81 M triangles is the number that settles it.** There is no
per-polygon cost in vtk.js worth decimating 48:1 to avoid. Nothing was dropped: the draw call was
hooked and the triangle count matched exactly.

That the colours reach the screen was proved rather than asserted. Swapping only the per-face
scalar array on identical geometry moved the framebuffer from 37 distinct colour bins and 0 warm
pixels to 616 bins and 315,820 warm pixels. Lighting scales all three channels together and cannot
turn a blue ramp stop warm, so those pixels can only have come from per-face scalars.

### Binary against base64, same geometry, same construction path

| | binary | base64 JSON |
|---|---|---|
| wire | 60.0 MB | 87.2 MB |
| fetch | 551 ms | 682 ms |
| `JSON.parse` | | 58 ms |
| `atob` + three char loops | | **518 ms** |
| scalars to RGB | 23 ms, uint8 LUT | 121 ms, `rp.color` on float32 |
| **to the same polydata** | **574 ms** | **1,379 ms** |

Identical on the GPU side and a 0.14% pixel difference from uint8 quantisation. **Binary is 1.45x
smaller and 2.4x faster, and the 518 ms of `atob` is pure overhead the budget was set to avoid.**

### The ceiling is VRAM, and it is ~92 bytes per delivered face

Same patch as N independent actors:

| faces | triangles | VBO | draw calls | frame | first render |
|---|---|---|---|---|---|
| 1.90 M | 3.81 M | 174 MB | 1 | 11.4-14.8 ms | 1.10 s |
| 7.62 M | 15.24 M | 698 MB | 4 | 35.9 ms | 4.30 s |
| 15.24 M | 30.49 M | 1,396 MB | 8 | 71.5 ms | 8.68 s |

Linear, at **~92 B of VRAM per delivered face and ~2.3 ms per million triangles**. The cause is that
vtk.js **de-indexes** when cell scalars are in play: `drawArrays`, six vertices per quad, 12 B of
position plus 4 B of RGBA each, and no normals buffer because the fragment shader takes derivatives.
Per-face colour itself is +4 B per vertex, i.e. +43.6 MB per 1.9 M faces.

Extrapolated to all **30,350,633** delivered boundary faces (note: the often-quoted 56,460,654
double-counts, because `fluid_to_solid` and `solid_to_fluid` are one conformal interface stored
twice): ~60.7 M triangles, **~2.8 GB of VBO at roughly 7 fps.** Interactive but not smooth, and
2.8 GB would fail on a 4 GB discrete card. **So the honest answer is whole-patch streaming in
binary, not server-side rendering** - which no comparable product needs at this size either, though
Luminary Cloud did ship exactly that for >100 M CVs in February 2026.

### A latent bug: edge visibility loses the WebGL context

`viewer.js:1375` sets `setEdgeVisibility(true)` on every cut actor. At 1.9 M faces that adds a
second draw call of **7,620,174 line segments**, takes the VBO from 174.4 to 348.9 MB, and:

- frame time 18 ms -> **302.8 ms (3.3 fps)**
- **the context was lost**: `gl.isContextLost()` true, `getError()` **37442 `CONTEXT_LOST_WEBGL`**

Repeated losses then poisoned the GPU process for several later page loads, with `get3DContext`
refusing a context until the tab was replaced. **Edges must be switched off above a few hundred
thousand faces.** Today this is latent only because a cut is capped at `MAX_RETURN_FACES` = 150,000
(`detail_window.py:45`); it becomes a crash the moment a patch is streamed at 1:1.

### What this means for the roster

- The 48:1 decimation is not required by the renderer and can be removed patch by patch, binary
  first.
- The per-face quality heatmap on the **main** surface, which `viewer.js:1701` refuses because the
  array length never equals the drawn polygon count, becomes available as soon as a patch is drawn
  unmerged. That guard is correct and should stay; it just stops being binding.
- A camera-driven tile scheme is **not** the route. The bucket grid is not a balanced tile index:
  1,440 of 2,016 candidate tiles are empty (71.4%), the median tile holds 0 faces, the largest holds
  911,168, and the top 16 hold half of all faces. `_grid` sizes buckets by bounding VOLUME while a
  boundary is a SURFACE, so `TARGET_FACES_PER_BUCKET = 32` is never achieved (92.7% of fluid buckets
  empty, non-empty mean 427). Worse, full detail inside the 800 k budget reaches only **W = 4 mm on
  a 170.19 mm part**, and a cut at r = 4 mm already returns the 150,000-face cap today. Tiles would
  have removed the click, not the window.

Harness and artifacts: `/home/rehaan/vtkbench/`, served on port 8790,
`?src=bin|b64&ramp=true|eq|none&edges=0|1&copies=N`.
