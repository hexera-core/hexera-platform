# The Corintis viewer

Serves the delivered meshes through the product's own viewer, on a laptop or in a container,
without the workspaces. Three pages: the assembly, the cold plate, the chip.

## What it is

`serve_view.py` stands in for the console. It serves the same three URLs the API serves, with the
same bodies, so the thing on screen is `ui/js/viewer/viewer.js` off this branch and the layout is
`apps/console/public/static/css/workbench.css`. Nothing here is a mock of the viewer; it is the
viewer, pointed at payloads that were produced by hand rather than by a job.

It exists because both delivered meshes were staged on a rented box rather than run as jobs, so
there is no job row to hang them on and no run to stream.

## Running it

```
cd <this directory>
VIEWER_NO_FETCH=1 <repo>/.venv/bin/python serve_view.py 8787
```

Use the repo's venv python. The system python has no numpy, and a detail cut fails at the import
rather than at the data.

| variable | what it does |
|---|---|
| `VIEWER_REPO` | where `src/` and the static assets are. Defaults to the laptop checkout. |
| `VIEWER_HOME` | where the payloads and sidecars live. Defaults beside this file. |
| `VIEWER_SIDECARS` | a mounted path holding the sidecars, for Cloud Run, where a volume mounts a whole bucket and cannot mount a prefix. |
| `VIEWER_NO_FETCH` | do not reach for the bucket. On a machine that stages its own files a fetch for something not there yet is a cost with no upside, and gcloud does not always decline quickly. |
| `HEXERA_ARTIFACTS_BUCKET` | the deployment's persistent bucket. Not the exchange bucket, which deletes at two days. |

A payload it cannot find is skipped with a line saying so, not a fatal error. One mesh missing
used to take the others down with it.

## What it needs

Per mesh, two things:

- a **payload** at `<VIEWER_HOME>/<name>_viewer_data.json`, or `jobs/<id>/viewer_data.json` in the
  bucket. This is what gets drawn.
- a **sidecar** at `<VIEWER_SIDECARS>/<slug>/constant/<region>/polyMesh/` holding `boundary` and
  `boundaryIndex/`. This is what gets cut.

The sidecar is the whole trick. `render.boundary_index.open_index` is built to answer with the
mesh absent, so a detail cut needs about 3 GB of index rather than a 72 GB workspace, and the
layout has to match a real workspace exactly because `delivered_regions` finds regions by
globbing `constant/*/polyMesh/boundary`.

## Building the payloads

`build_plate_payload.py` builds the plate from its own boundary index, so the surface that is
drawn is the surface the cuts come out of. `render.index_surface.surface_from_index` is tested
byte-identical to the polyMesh parse, which is what makes that claim safe.

`build_assembly.py` places the chip against the plate and writes the combined scene. The
placement is derived, not chosen: the thin axes have to line up, the plate's ports say which way
is up, and the die has to lie flat on the plate's machined pad. That last constraint is what
picks the orientation - with the die 50 mm along x there is no position anywhere on this plate
that puts all of it on the pad.

The transform it writes into the payload is read back by `serve_view.py` for the cuts, so the
drawing and the cutting cannot drift apart.

## Driving it without a mouse

`window._vdbg[job].detail()` exposes the inspect window: `arm(on)`, `cut(at)`, and the current
`armed`, `centre`, `clip`, `xray`, `scale` and `odometer`. The browser gate uses it so a test
does not have to pick pixels.
