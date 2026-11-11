"""Serve the two delivered Corintis meshes to the REAL Hexera viewer, in the REAL layout.

WHY A STUB AND NOT THE CONSOLE. `/api/v1/simulation/{job}/surface` returns a STORED payload: the
worker uploads `jobs/<id>/viewer_data.json` to the object store and the api fetches it by the
`artifacts` row's `storage_key`. No workspace is read. Both of these meshes were staged by hand on
a rented box rather than run as jobs, so there is no job row to hang them on and no run to stream.
This serves the same three URLs with the same bodies, for two jobs instead of one.

WHY THE FIRST VERSION LOOKED WRONG, and this one does not. The viewer has two layouts and picks
between them itself (viewer.js, `openViewer`): with a `#workbench` and an `#app` on the page the
delivered mesh TAKES THE STAGE - the viewport fills the window and the readings move into a
sidebar beside it - and without them it falls back to opening inline after its anchor, which is
the embedded form meant for a page that has other things on it. The first stub had neither
element and did not load workbench.css, so every reading stacked full-width down a 2000px page
with the 3D view pushed below the fold. Same viewer, same payload, wrong half of its own CSS.

WHAT IS REAL HERE: the viewer is `ui/js/viewer/viewer.js` off the branch, the layout is
`apps/console/public/static/css/workbench.css`, each payload is the one `build_viewer_payload`
produced through an engine hook, the vtk export is `render.vtk_export.surface_to_vtk`, and the
viewer config is read from `settings.policy`.

WHAT IS NOT: the drawer that holds the conversation. The console puts the chat, the brief and the
run timeline in the left column of that same grid; there is no run here, so the column opens
folded. The fold control in the header is the viewer's own.

-------------------------------------------------------------------------------------------------
2026-11-03: THE PAYLOADS NOW LIVE IN hexera-dev, AND `/detail` CAN BE REAL.

Two changes, both because the old `hexera` project is being wound down and the team's project is
`hexera-dev`.

1. EACH PAYLOAD IS FETCHED FROM THE OBJECT STORE IF IT IS NOT ON THIS DISK, from the key the
   product itself would use - `jobs/<id>/viewer_data.json` in the deployment's artifacts bucket.
   So this stub is no longer tied to one laptop: on a machine with neither file it downloads both
   and serves them. The local copies stay the fast path and nothing is uploaded from here.

2. `/detail` IS NO LONGER A FLAT REFUSAL. It used to say the cut could not be done because the
   workspaces are 72 GB and 3.4 GB on a box in the old project. That was true of the WORKSPACE and
   not of the cut: `render.boundary_index.open_index` is built to answer with the mesh absent -
   "the index carries the boundary's own points and connectivity, so it answers without the mesh
   it was built from, which is how a detail window works on a host the workspace was never copied
   to" - and `detail_at` needs, per region, only the ~1 KB `boundary` file and the `boundaryIndex`
   sidecar beside it. On the plate that is about 3 GB against the workspace's 72.

   So a SIDECAR BUNDLE shaped like a workspace - sidecar/<slug>/constant/<region>/polyMesh/ with
   `boundary` and `boundaryIndex/` in it - is enough, and when one is present this serves the same
   cut the console serves, through the same `detail_at` and `detail_response`. When it is absent
   the refusal stays, and now names what is missing instead of calling it impossible.

   THIS NEEDS NUMPY, so run it with the repo's venv python, not the system one:
       /home/rehaan/hexera-platform-semi/.venv/bin/python serve_view3.py 8787
   The system python3 has no numpy, and under it a cut fails at the import rather than at the data.
-------------------------------------------------------------------------------------------------
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# PATHS COME FROM THE ENVIRONMENT, with this laptop's layout as the default, so one file runs
# both here and in a container. In the container there is no checkout: meshpipeline is installed
# into site-packages and the static assets are copied in, so VIEWER_REPO names where they
# landed. PORT is read from the environment first because Cloud Run assigns it and passes no
# argv at all.
REPO = Path(os.environ.get("VIEWER_REPO", "/home/rehaan/hexera-platform-semi"))
HERE = Path(os.environ.get("VIEWER_HOME", "/home/rehaan/corintis_view"))
PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8787))

#: The deployment's own artifacts bucket in hexera-dev - the PERSISTENT one. Not the exchange
#: bucket: `deploy/gcp/storage/exchange-bucket-lifecycle.json` puts a flat "delete at age 2" on
#: that one, so anything parked there is gone in two days. This bucket has no lifecycle rule.
BUCKET = os.environ.get("HEXERA_ARTIFACTS_BUCKET", "dev-rehaan-artifacts-224734058693")

#: Where a sidecar bundle goes, one directory per job, each shaped like a workspace so
#: `detail_window.delivered_regions` finds its regions by the same glob it uses on a real one.
#:
#: OVERRIDABLE because a Cloud Run cloud-storage volume mounts a WHOLE bucket and cannot mount a
#: prefix. The sidecars live under corintis/sidecar/<slug>/ in the artifacts bucket, so a volume
#: mounted at /mnt/artifacts puts them at /mnt/artifacts/corintis/sidecar/<slug> - which this names
#: directly instead of moving the objects to the bucket root to suit a mount path. Unset, it is
#: HERE/sidecar exactly as before, so nothing about running this on a laptop changes.
SIDECARS = Path(os.environ["VIEWER_SIDECARS"]) if os.environ.get("VIEWER_SIDECARS") \
    else HERE / "sidecar"

# Only when there IS a checkout. In the container meshpipeline is an installed package and this
# directory does not exist; inserting a missing path is harmless but says the wrong thing.
if (REPO / "src").is_dir():
    sys.path.insert(0, str(REPO / "src"))

# job id -> what it is. `where` is the one thing /detail needs to be honest about.
JOBS: dict[str, dict] = {
    #: THE PITCH-8 DELIVERABLE, and the newest mesh here. Same four-zone box domain and the same
    #: three delivered regions as cbox2b, on a COARSER commensurate lattice: 25.1578 um instead of
    #: 20.1263 um, which puts the identical 100.631 um channel on 4 cells instead of 5 because
    #: 4 x 25.1578 = 5 x 20.12625 exactly. 270,424,307 cells delivered - fluid 55,303,441, solid
    #: 199,394,482, chip 15,726,384 - against cbox2b's 440,668,679, and the whole chain including
    #: layers took 3,925 s, 65.4 minutes, against 5.79 hours.
    #:
    #: 36,490,208 boundary faces: fluid 16,622,416, solid 18,104,116, chip 1,763,676. The
    #: conjugate interface is 16,619,777 faces, `fluid_to_solid` against `solid_to_fluid`, equal
    #: to the face; the chip contact is 270,816, likewise. Both ports are split and present,
    #: `port_a` 1,331 faces and `port_b` 1,308.
    #:
    #: ITS INDEX WAS NOT BUILT FROM A RECONSTRUCTED MESH, because there is not one. cbox8 was
    #: split, checked and archived decomposed and in BINARY, and `write_index` reads serial ascii
    #: only. The index here is the union of each region's non-`processor` patches across the 8
    #: processor directories, read straight out of the binary `faceCompactList` by seeking rather
    #: than scanning. That union is the region's boundary exactly: every boundary face lives on
    #: one processor, so it neither drops nor doubles. Checked before it was trusted - real
    #: patches plus `processor` patches equals checkMesh's own global (faces - internal faces) on
    #: all three regions, to the face - and the build refuses to write an index that misses those
    #: counts. What does NOT exist, and must not be read into this page, is a serial pitch-8
    #: volume mesh.
    #:
    #: THIS DRAWS THE UNLAYERED SURFACE, same as `corintis-coolant` does, and for the same reason:
    #: the 3-layer stack in `bl8` is made of INTERIOR cells, and a boundary index holds "the
    #: boundary and nothing else". The layered coolant is 102,536,327 cells with 95.5530% layer
    #: coverage and 471 faces in error; its wetted surface is this one. To see the layers at all,
    #: use the two section pages, which cut the layered mesh open - that is what they are for.
    #:
    #: NO QUALITY COLOURING HERE YET. cbox8's checkMesh was run without `-writeFields`, so there
    #: are no per-face skewness / non-orthogonality / aspect-ratio arrays to paint, and the page
    #: says so rather than showing a blank legend. cbox2b has them; this one does not.
    #: THE MESH THAT IS ACTUALLY DELIVERED, layers and all. Every other page here is a step on
    #: the way: `corintis-pitch8` below is this same coupled mesh BEFORE the layer pass, which is
    #: why it says 270,424,307 cells against this page's 317,706,038. The earlier pages are kept
    #: because the comparison between them is the record of how the mesh got here.
    #:
    #: ITS COOLANT INDEX IS THE DELIVERED COOLANT'S OWN, built 2026-11-09 from the ascii
    #: decomposed mesh in 28 s. The plate and the chip are carried over from cbox8 unchanged,
    #: because only the coolant moved between them - the layer pass adds cells INSIDE the fluid
    #: and leaves fluid_to_solid at 16,619,777 faces, which is why the conjugate interface still
    #: matches the metal's side exactly.
    #:
    #: DRAWN AT 8,000,000 TRIANGLES, not the 800,000 default. 4,386,134 triangles of 19,599,615
    #: delivered faces, merged at 0.446 mm against cbox8's 0.631 mm, and 128.4 MB of payload
    #: against 76.5 MB. The budget is a transport figure and the transport changed: binary carries
    #: the same payload in 574 ms where base64 takes 1,379 ms, and 1,904,636 faces drew at 55.2
    #: fps with 0.30 ms of CPU per frame, so the browser was never the thing that was limiting.
    #:
    #: NO QUALITY COLOURING ON THE COOLANT YET. checkMesh was run on it without `-writeFields`,
    #: so there are no per-face arrays to paint; the plate and the chip have theirs. The page says
    #: so rather than showing a blank legend.
    "corintis-delivered": {
        "payload": HERE / "deliver_viewer_data.json",
        "slug": "deliver",
        "title": "Delivered mesh",
        "up": "+y",
        "sub": "what ships - 317,706,038 cells, coolant layered, 0.446 mm merge",
        "where": "the delivered coolant's own boundary index, built from the ascii decomposed "
                 "mesh and gated against checkMesh's counts (16,619,777 + 1,331 + 1,308); the "
                 "plate and the chip carried over unchanged, because the layer pass adds cells "
                 "inside the fluid and leaves the conjugate interface exactly where it was.",
        "views": [("Coolant", "region:fluid"), ("Metal", "region:solid"),
                  ("Chip", "region:chip"), ("All", None)],
    },
    #: THE SAME DELIVERED MESH, DRAWN EXACTLY. Everything else on this server draws a plate
    #: through `polymesh_budget.fit_to_budget`, which clusters faces until the surface fits a
    #: triangle budget, so the drawn surface approximates the mesh. This page merges only the
    #: coplanar axis-aligned faces, into the rectangles they already tile, which changes the
    #: geometry by nothing.
    #:
    #: WHAT THAT IS WORTH, measured against /deliver on this machine, not asserted:
    #:   19,599,615 delivered faces as 1,297,428 polygons = 2,635,540 triangles, against 5,487,927
    #:   76.2 MB against 152.6 MB
    #:   5.0 s to load against 18.7 s, in the same browser at the same camera
    #:   area error 1.0e-12 against the budget route's 1.35% (it draws 98.65% of the coolant)
    #:
    #: WHAT IT IS NOT WORTH, so nobody repeats the claim I made first: the two pictures look
    #: almost the same. At an identical camera they differ in 0.59% to 0.76% of canvas pixels. The
    #: budget route is a good approximation that is slightly wrong everywhere, NOT a picture with
    #: the channel array missing. The win here is cost and exactness, not visible detail.
    #:
    #: IT IS A SECOND PAGE AND NOT A REPLACEMENT because the lossy one is the evidence, and
    #: /both puts them side by side. `render/exact_surface.py` has the measurements and, more
    #: usefully, what it REFUSES to merge and why.
    "corintis-exact": {
        "payload": HERE / "exact_viewer_data.json",
        "slug": "exact",
        "title": "Delivered mesh, exact",
        "up": "+y",
        #: the same 1.9 GB bundle the lossy page cuts from - this is the same mesh
        "sidecar_slug": "deliver",
        "where": "the delivered mesh's own boundary indexes, the same ones the lossy page cuts "
                 "from - this page differs only in how the surface is DRAWN, not in what was "
                 "measured.",
        "views": [("Coolant", "region:fluid"), ("Metal", "region:solid"),
                  ("Chip", "region:chip"), ("All", None)],
    },
    "corintis-pitch8": {
        "payload": HERE / "cbox8_viewer_data.json",
        "slug": "cbox8",
        "title": "Plate + chip (pitch 8)",
        # +y: the plate carries its ports on +y and the chip sits under it, same frame as cbox2b
        "up": "+y",
        "sub": "the deliverable - 270,424,307 cells, 65.4 min end to end with layers",
        "where": "this mesh's own boundary index, one per delivered region, built from the "
                 "decomposed binary mesh on the box and checked against checkMesh's own counts "
                 "before it was written.",
        "views": [("Coolant", "region:fluid"), ("Metal", "region:solid"),
                  ("Chip", "region:chip"), ("All", None)],
    },
    #: THE DELIVERABLE. The plate and the chip are one part in service, so they are one scene
    #: here: two meshes from two engines, in two CAD frames, placed against each other by the
    #: transform `build_assembly.py` computed off their own extents and wrote into the payload.
    #: Its `sources` say which sidecar each body's cuts come from and which of them has to be
    #: moved on the way out, so a cut works anywhere in the assembly and lands where it was asked
    #: for. See the payload's `quality.assembly` for what that placement is forced by and what it
    #: is a choice about.
    "corintis-assembly": {
        "payload": HERE / "assembly_viewer_data.json",
        "slug": "assembly",
        "title": "Assembly",
        # +y: the plate carries its ports on +y and the chip hangs underneath it
        "up": "+y",
        "where": "both bodies' boundary indexes - the plate's read in its own frame, the chip's "
                 "read in its own and moved into the assembly on the way back.",
        "views": [("Coolant", "region:fluid"), ("Metal", "region:solid"),
                  ("Chip", "region:chip"), ("All", None)],
        "sources": [{"slug": "plate", "prefix": "", "moved": False},
                    {"slug": "chip", "prefix": "chip/", "moved": True}],
    },
    #: THE COUPLED MESH. One mesh, four zones, three of them delivered: the coolant, the plate
    #: metal and the chip. It is not the assembly page's two bodies placed against each other by a
    #: transform - the chip and the plate were cut from the same block, so the contact between them
    #: is a conformal interface of 353,184 faces that the mesher built rather than a placement this
    #: viewer computed. The air zone was split off and never delivered.
    "corintis-coupled": {
        "payload": HERE / "cbox2_viewer_data.json",
        "slug": "cbox2",
        "title": "Plate + chip",
        # +y: the plate carries its ports on +y and the chip sits under it, same frame as the plate
        "up": "+y",
        "sub": "conjugate, coolant + metal + chip, one mesh",
        "where": "this mesh's own boundary index, one per delivered region, written on the box the "
                 "mesh was built on. If a cut is refusing, the index did not reach this machine - "
                 "not that the cut cannot be made.",
        "views": [("Coolant", "region:fluid"), ("Metal", "region:solid"),
                  ("Chip", "region:chip"), ("All", None)],
    },
    #: THE COUPLED MESH REBUILT, and the one the delivery now stands on. Same four-zone box
    #: domain as cbox2 and the same three delivered regions, with two things changed. The
    #: microchannel band is stretched 3x instead of 6x, and the whole chain - mesh, topoSet,
    #: split, four parallel checks, three reconstructs and three serial checkMeshes - came to
    #: 10,796 s, 3.00 hours, with cartesianMesh itself 799 s of it. And the two port mouths, which
    #: arrive from the box-domain route as ONE patch called `fluid_to_air`, are split into
    #: `port_a` and `port_b`, because an inlet and an outlet take different boundary conditions
    #: and a CHT run needs them apart. The split is by x at 95 mm, which separates mouth centres
    #: at 89.3396 and 99.8820 mm. They are not called inlet and outlet because Corintis has not
    #: said which is which.
    #:
    #: 440,668,679 cells delivered: fluid 82,529,515, solid 327,126,204, chip 31,012,960. Each is
    #: `checkMesh -constant` on that region's own reconstructed mesh and each agrees exactly with
    #: topoSet's `cellZoneSet` size for the same zone, which is an independent count. The air
    #: zone's 65,693,365 cells are not in that total: the air was split off and never delivered,
    #: same as cbox2. `quality.cell_count_source` in the payload says where the figures came from.
    "corintis-coupled-b": {
        "payload": HERE / "cbox2b_viewer_data.json",
        "slug": "cbox2b",
        "title": "Plate + chip (b)",
        # +y: the plate carries its ports on +y and the chip sits under it, same frame as cbox2
        "up": "+y",
        "where": "this mesh's own boundary index, one per delivered region, written on the box the "
                 "mesh was built on. If a cut is refusing, the index did not reach this machine - "
                 "not that the cut cannot be made.",
        "views": [("Coolant", "region:fluid"), ("Metal", "region:solid"),
                  ("Chip", "region:chip"), ("All", None)],
    },
    #: THE COOLANT ON ITS OWN PAGE, with the two ports apart. Worth a page of its own because the
    #: coolant is the thing the resolution was chosen for and the thing a reader of a cold plate
    #: is actually looking at: alone it gets the whole triangle budget (590,990 on the wetted wall
    #: against 518,038 when it shares the budget with the metal and the chip), and the port split
    #: is pickable one mouth at a time instead of being two chips in a roster of eight.
    #:
    #: THIS IS NOT THE LAYERED MESH, AND THE PAGE SAYS 82,529,515 CELLS BECAUSE OF IT. The slug is
    #: `bl3` because that is what the bucket calls the sidecar, and the name is from a layer run -
    #: but that run's `snappyHexMesh -addLayers` exited 1 (LAYER_TIMES.txt: `snappy 1 689`) and
    #: the index written afterwards was written on the UNLAYERED coolant. Three independent proofs,
    #: none of them needing the mesh: the index's own meta.json gives `first_face` 237,313,600,
    #: which is cbox2b's fluid region's internal face count exactly against the layered mesh's
    #: 420,774,788; the index records its source `faces` file as 10,166,681,134 bytes with the same
    #: end-hash as cbox2b's fluid, where the layered mesh's would be about 17 GB; and all nine
    #: files under sidecar/bl3/constant/fluid are md5-identical to sidecar/cbox2b/constant/fluid.
    #: So this page and the Coolant view of `corintis-coupled-b` draw the same surface from the
    #: same arrays, and both state the cell count of the mesh those arrays came from.
    #:
    #: The layered mesh - 143,236,900 cells, 3 layers at firstLayerThickness 4.63 um on
    #: fluid_to_solid - has NO boundary index anywhere in the bucket, so nothing here can draw it.
    #: And a boundary index could not show its layers in any case: `boundary_index` writes "the
    #: boundary and nothing else", the layers are interior cells, and the layered mesh's boundary
    #: is the same wetted surface as this one to within 160 port-rim faces out of 25,690,241
    #: (fluid_to_solid is 25,685,941 in both). What an index of it would change is the figures
    #: stated beside the drawing, not the drawing.
    "corintis-coolant": {
        "payload": HERE / "bl3_viewer_data.json",
        "slug": "bl3",
        "title": "Coolant",
        # +y: the ports stand off the plate's +y face, same frame as the coupled mesh
        "up": "+y",
        "where": "this region's own boundary index, written on the box the mesh was built on. If a "
                 "cut is refusing, the index did not reach this machine - not that the cut cannot "
                 "be made.",
        # the wetted wall and each mouth on its own, which is what the port split bought
        "views": [("Wetted wall", "boundary:fluid_to_solid"), ("Port A", "boundary:port_a"),
                  ("Port B", "boundary:port_b"), ("All", None)],
    },
    "corintis-plate": {
        "payload": HERE / "plate_viewer_data.json",
        "slug": "plate",
        "title": "Cold plate",
        "up": "+y",
        "sub": "conjugate · coolant + metal · 328,714,251 cells",
        "where": "this mesh's own boundary index, which is in dev-rehaan. If this page is "
                 "refusing a cut, the index did not reach this machine - not that the cut "
                 "cannot be made.",
        # the quick views this mesh offers, as [label, part id]; None restores the default
        "views": [("Coolant", "region:fluid"), ("Metal", "region:solid"), ("All", None)],
    },
    "corintis-chip": {
        "payload": HERE / "chip_viewer_data.json",
        "slug": "chip",
        "title": "Chip",
        "up": "+z",
        "sub": "one solid region · 17,182,848 cells · all hexahedra",
        "where": "this mesh's own boundary, which is 855,688 faces - well inside "
                 "detail_window.MAX_SOURCE_FACES, so this one does not even need an index and "
                 "parses straight from the polyMesh. Only the polyMesh is missing.",
        "views": [],
    },
    #: THE INTERIOR. A slab cut two cells thick out of the LAYERED pitch-8 coolant, 20 mm of the
    #: channel array. subsetMesh puts the slab's exposed interior faces into `oldInternalFaces`, so
    #: the cut plane is boundary geometry and the ordinary index and payload path renders it. This is
    #: the only page here that shows the mesh INTERIOR, and so the only one where the 3-layer
    #: boundary stack is visible at all: layers are interior cells and every other page draws the
    #: skin. 25,149 cut faces drawn unmerged, with checkMesh's own per-face quality painted on them.
    "slab-cut-8": {
        "payload": HERE / "slab8_viewer_data.json",
        "slug": "slab8",
        "title": "Section, layered",
        "up": "+y",
        "sub": "cross-section of the layered coolant, 25,149 cut faces, unmerged",
        "where": "the slab's own boundary index, written locally. The parent mesh is 102,536,327 "
                 "cells and is not needed here.",
        "views": [("Cut faces", "boundary:oldInternalFaces"),
                  ("Wetted wall", "boundary:fluid_to_solid"),
                  ("Slab", "region:slab"), ("All", None)],
    },
    #: THE WHOLE INTERIOR, in two planes. One cellSet is the UNION of two bands, so a single
    #: subsetMesh gives one mesh whose oldInternalFaces holds BOTH cut planes: one across the part
    #: (channels end-on, the layer stack visible) and one along it (channels down their run). 174,086
    #: cut faces = about 348k triangles, inside the 800,000 budget, so NOTHING is merged - this is
    #: the whole part at full resolution rather than a window. The estimate before cutting was 1.8M
    #: faces and a 4.5:1 merge; the coolant is far sparser than that, so the merge never happened.
    "slab-wide": {
        "payload": HERE / "slabw_viewer_data.json",
        "slug": "slabw",
        "title": "Section, whole part",
        "up": "+y",
        "sub": "two cut planes through the whole plate, 174,086 faces, unmerged",
        "where": "the section's own boundary index, written locally from a 185,498-cell subset of "
                 "the 102,536,327-cell layered coolant.",
        "views": [("Cut planes", "boundary:oldInternalFaces"),
                  ("Wetted wall", "boundary:fluid_to_solid"),
                  ("Slab", "region:slab"), ("All", None)],
    },
}

BY_SLUG = {j["slug"]: k for k, j in JOBS.items()}


#: TWO ROUTES TO THE BUCKET, because the two places this runs hold different credentials.
#: On a laptop `gcloud` is on PATH and already carries a human credential. In a container there is no
#: gcloud at all, but Cloud Run hands every instance a service-account token from the metadata
#: server, so the JSON API is reachable with nothing installed and no key file mounted. The metadata
#: route is tried first and simply does not answer off GCP, which is how one file serves both.
_META = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def _metadata_token():
    """The instance's own access token, or None when this is not running on GCP."""
    import urllib.request
    req = urllib.request.Request(_META, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.load(r).get("access_token")
    except Exception:
        return None


def _api_get(path, token, params=""):
    """One object, straight off the JSON API. None, with a word, if it could not be read."""
    import urllib.parse
    import urllib.request
    url = "https://storage.googleapis.com/storage/v1/b/%s/o/%s%s" % (
        BUCKET, urllib.parse.quote(path, safe=""), params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.read()
    except Exception as exc:
        print("   %s: %s" % (path, str(exc)[:120]))
        return None


#: Skip the bucket entirely. On a machine where the payloads and sidecars are staged by hand - a
#: laptop mid-transfer - a fetch for something that is not there yet is a cost with no upside, and
#: `gcloud` does not always decline quickly: a credential that needed re-proving held this server's
#: startup past 85 s with the port unbound and nothing on screen. With this set, what is on disk is
#: what is served and what is missing is named and skipped.
NO_FETCH = os.environ.get("VIEWER_NO_FETCH", "") not in ("", "0", "false", "no")


def _fetch(key: str, dest: Path) -> bool:
    """Pull one object down from the artifacts bucket. False, with a word, if it could not be."""
    if NO_FETCH:
        print("   %s is not on this disk and VIEWER_NO_FETCH is set; not fetching it" % dest.name)
        return False
    print("   %s is not on this disk; fetching gs://%s/%s" % (dest.name, BUCKET, key))
    dest.parent.mkdir(parents=True, exist_ok=True)
    token = _metadata_token()
    if token:
        blob = _api_get(key, token, "?alt=media")
        if blob is None:
            return False
        dest.write_bytes(blob)
        return dest.is_file()
    try:
        # stdin CLOSED, deliberately. gcloud asks for a password when its credential needs
        # re-proving, and with a terminal on the other end it waits: this server came up with
        # fd 7 on /dev/ptmx and sat there, port unbound, until the 600 s timeout - a viewer that
        # looked hung because a token had aged. With no stdin gcloud says it cannot prompt and
        # exits, which is an answer this can act on.
        r = subprocess.run(["gcloud", "storage", "cp", "gs://%s/%s" % (BUCKET, key), str(dest)],
                           capture_output=True, text=True, timeout=180,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        print("   no gcloud and no metadata server either (%s)" % exc)
        return False
    if r.returncode != 0:
        print("   %s" % (r.stderr or "").strip().splitlines()[-1:] or "fetch failed")
        return False
    return dest.is_file()


def _fetch_prefix(prefix: str, dest_root: Path) -> int:
    """Pull every object under one prefix, keeping its layout. Returns how many arrived.

    THE LAYOUT IS THE WHOLE POINT. `detail_window.delivered_regions` finds a region by globbing
    constant/*/polyMesh/boundary, and `boundary_index.open_index` memory-maps the files sitting
    beside it, so a sidecar is only usable if it lands shaped exactly like the workspace it was
    built in. Flattening it would leave every file present and every cut impossible.
    """
    import urllib.parse
    import urllib.request
    token = _metadata_token()
    if not token:
        try:
            r = subprocess.run(["gcloud", "storage", "cp", "-r",
                                "gs://%s/%s*" % (BUCKET, prefix), str(dest_root)],
                               capture_output=True, text=True, timeout=1800,
                               stdin=subprocess.DEVNULL)   # see _fetch: it must not prompt
        except (OSError, subprocess.SubprocessError) as exc:
            print("   could not run gcloud (%s)" % exc)
            return 0
        if r.returncode != 0:
            print("   %s" % ((r.stderr or "").strip().splitlines() or ["fetch failed"])[-1])
            return 0
        return sum(1 for f in dest_root.rglob("*") if f.is_file())

    names = []
    page = None
    while True:
        q = "?prefix=%s&maxResults=1000" % urllib.parse.quote(prefix, safe="")
        if page:
            q += "&pageToken=" + page
        url = "https://storage.googleapis.com/storage/v1/b/%s/o%s" % (BUCKET, q)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
        except Exception as exc:
            print("   could not list %s: %s" % (prefix, str(exc)[:120]))
            return 0
        names += [o["name"] for o in d.get("items", []) if not o["name"].endswith("/")]
        page = d.get("nextPageToken")
        if not page:
            break
    got = 0
    for n in names:
        rel = n[len(prefix):].lstrip("/")
        if rel and _fetch(n, dest_root / rel):
            got += 1
    return got


def _span(surface: dict):
    """How far the drawn surface reaches on each axis, in metres, or None.

    MEASURED, NOT REMEMBERED - the same rule `_subtitle` follows and for the same reason. The
    side-by-side page states both parts' envelopes to explain why they are not drawn in one
    scene, and those numbers were typed in: they described the plate that this page no longer
    serves, and nothing would have changed them when the payload did.

    Exactly, not sampled. A sampled extreme is a figure that is nearly right, which is the worst
    kind to put on a page: `array` over the whole buffer costs about a second at startup, once.
    """
    import array
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for p in surface.get("patches") or []:
        b = p.get("points_b64")
        if not b:
            continue
        a = array.array("f")
        a.frombytes(base64.b64decode(b))
        for k in range(3):
            axis = a[k::3]
            if not axis:
                continue
            lo[k] = min(lo[k], min(axis))
            hi[k] = max(hi[k], max(axis))
    if any(v == float("inf") for v in lo):
        return None
    return tuple(hi[k] - lo[k] for k in range(3))


def _mm(span) -> str:
    return " &times; ".join("%.1f" % (v * 1000.0) for v in span) + " mm" if span else "an unread extent"


def _subtitle(surface: dict, top: dict | None = None) -> str:
    """What this mesh is, counted off the payload being served - not typed in beside it.

    THE SUBTITLE USED TO BE A STRING LITERAL AND IT OUTLIVED ITS MESH. The plate's read
    "conjugate - coolant + metal - 328,714,251 cells" for days after that mesh had stopped
    existing anywhere, because nothing recomputed it when the payload underneath changed. A figure
    shown to a reader should be read out of the thing it claims to describe, so that swapping the
    payload swaps the figure or neither moves.

    Cells are stated only where the payload states them. A boundary index knows its faces exactly
    and its cells not at all, so a plate drawn from one says faces and does not guess cells from a
    number somebody remembered.
    """
    q = surface.get("quality") or {}
    top = top or {}
    pats = surface.get("patches") or []
    regions = {str(p.get("name", "")).split("/")[0] for p in pats if "/" in str(p.get("name", ""))}
    bits = []
    if surface.get("engine") or q.get("engine"):
        bits.append(str(surface.get("engine") or q.get("engine")))
    if regions >= {"fluid", "solid"}:
        bits.append("coolant + metal")
    elif len(regions) == 1:
        bits.append("one region")
    cells = surface.get("cell_count") or q.get("cells")
    if cells:
        bits.append(f"{int(cells):,} cells")
    else:
        # NOT the sum of `face_count`. After `fit_to_budget` that is the count of DRAWN polygons,
        # so summing it printed "486,372 boundary faces" for a mesh whose boundary is 30,011,162 -
        # the drawn figure wearing the mesh's label, which is the one mistake this subtitle exists
        # to avoid. The mesh's own count comes from the payload's quality block, or failing that
        # from `reduced.delivered_faces`, which the budget records precisely because the two are
        # different numbers.
        red = surface.get("reduced") or {}
        boundary = (top.get("boundary_faces") or q.get("boundary_faces")
                    or red.get("delivered_faces")
                    or sum(int(p.get("face_count") or 0) for p in pats))
        if boundary:
            bits.append(f"{int(boundary):,} boundary faces")
    # HOW IT IS DRAWN IS PART OF WHAT IT IS, and the two routes are not the same claim. `reduced`
    # means faces were clustered until the surface fit a budget, so the drawn surface is an
    # approximation of the mesh and its resolution is the thing a reader needs to know. `exact`
    # means coplanar faces were merged into the rectangles they already tiled, so the drawn
    # surface IS the mesh and there is no resolution to state. Printing the second as if it were
    # the first - or the first as if it were the second - is the whole misunderstanding this page
    # exists to clear up.
    red = surface.get("reduced") or {}
    ex = surface.get("exact") or {}
    drawn = red.get("drawn_triangles")
    if ex.get("drawn_triangles"):
        bits.append(f"drawn EXACTLY as {int(ex['drawn_triangles']):,} triangles")
    elif drawn:
        res = red.get("resolution_m")
        bits.append(f"drawn as {int(drawn):,} triangles"
                    + (f" at {res * 1e6:.1f} um" if res else ""))
    return " · ".join(b for b in bits if b)


#: A JOB WITH NO PAYLOAD IS SKIPPED, NOT FATAL. This used to `SystemExit` on the first payload it
#: could not read, which was right when it served one mesh and wrong the moment it served two: a
#: plate still being staged took the chip down with it, and the page that worked could not be
#: looked at until the page that did not was fixed. Each mesh now stands on its own and the ones
#: that are here are served.
_missing: list[tuple[str, str]] = []

for _name, _j in JOBS.items():
    _p: Path = _j["payload"]
    if not _p.is_file():
        _fetch(f"jobs/{_name}/viewer_data.json", _p)
    if not _p.is_file():
        _missing.append((_name, str(_p)))
        print(f"   {_name}: no payload - neither {_p} nor "
              f"gs://{BUCKET}/jobs/{_name}/viewer_data.json could be read; skipping it")
        continue
    _j["data"] = json.loads(_p.read_text())
    _j["surface"] = _j["data"]["surface"]
    _j["sub"] = _subtitle(_j["surface"], _j["data"].get("quality"))
    _j["span"] = _span(_j["surface"])
    # `sidecar_slug` lets two PAGES of the same mesh share one 1.9 GB bundle. The exact-surface
    # page is the same delivered plate drawn differently, so its cuts come from the delivered
    # bundle; without this it would look for sidecar/exact, find nothing, and refuse every cut on
    # a mesh whose index is sitting right there.
    _j["sidecar"] = SIDECARS / _j.get("sidecar_slug", _j["slug"])
    # THE SIDECAR IS WHAT MAKES A CUT POSSIBLE, so try the bucket for it the same way. Absent is
    # not an error: every page still renders and /detail answers 409 naming exactly what is
    # missing, which is the whole reason the `where` string above exists.
    # Not when VIEWER_SIDECARS names a mount: that directory is read-only and already holds the
    # data, so a fetch would be both impossible and pointless.
    # not for an assembly: its cuts come from its bodies' sidecars, named in `sources`,
    # and it has no bundle of its own to look for
    if (not _j.get("sources") and not (_j["sidecar"] / "constant").is_dir()
            and not os.environ.get("VIEWER_SIDECARS")):
        _n = _fetch_prefix("corintis/sidecar/%s/" % _j["slug"], _j["sidecar"])
        print("   sidecar for %s: %s" % (
            _j["slug"], ("%d files" % _n) if _n else "not in the bucket yet, cuts will refuse"))

for _name, _why in _missing:
    del JOBS[_name]
#: REBUILT, because a slug that no longer has a job must not resolve to one. BY_SLUG is what the
#: `/plate` and `/chip` routes read, so a stale entry would route a request to a job whose
#: `surface` was never loaded and fail inside the page rather than at the door.
BY_SLUG = {j["slug"]: k for k, j in JOBS.items()}
if not JOBS:
    raise SystemExit("no mesh has a payload on this machine or in gs://%s - there is nothing to "
                     "show.\n  %s" % (BUCKET, "\n  ".join(n for n, _ in _missing)))
print("   serving %d mesh(es): %s" % (len(JOBS), ", ".join(sorted(BY_SLUG))))

try:
    from meshpipeline.settings import policy as polcfg
    VIEWER_CFG = {
        "grid_px": polcfg.VIEWER_GRID_PX,
        "fine_fill": polcfg.VIEWER_FINE_FILL,
        "frame_frac": polcfg.VIEWER_FRAME_FRAC,
        "flag_span_factor": polcfg.VIEWER_FLAG_SPAN_FACTOR,
        "max_flags": polcfg.DISPUTE_MAX_FLAGS,
    }
except Exception as exc:                        # noqa: BLE001 - the viewer has its own fallbacks
    print("   settings.policy unavailable (%s); the viewer uses VIEWER_FALLBACK" % exc)
    VIEWER_CFG = {}


def _mover(t: dict):
    """The assembly's placement, as the two functions a cut needs, read off the payload.

    ONE SOURCE OF TRUTH. The numbers live in the payload because that is where the thing they
    placed lives: if the chip is ever moved, the surface and the cuts move together or the cut
    lands somewhere the reader is not looking. Hard-coding them here is how those two drift.

    Forward is what `build_assembly.py` applied to the drawn points - a -90 degree turn about x
    and an offset. Inverse takes a point the reader clicked in the assembly back into the chip's
    own frame, which is the frame its boundary index is in.
    """
    d = (float(t["dx_m"]), float(t["dy_m"]), float(t["dz_m"]))
    #: "output axis i takes input axis ax[i], times sg[i]". Both are permutations with signs, so
    #: the inverse is the same table read the other way and 1/s is s. The defaults are the first
    #: placement's rotation, for a payload written before the table existed.
    ax = [int(v) for v in (t.get("axes") or [0, 2, 1])]
    sg = [float(v) for v in (t.get("signs") or [1.0, 1.0, -1.0])]

    def fwd(q):
        return tuple(sg[i] * q[ax[i]] + d[i] for i in range(3))

    def inv(q):
        out = [0.0, 0.0, 0.0]
        for i in range(3):
            out[ax[i]] = (q[i] - d[i]) * sg[i]
        return tuple(out)

    return fwd, inv


def _move_points(raw: bytes, fwd) -> bytes:
    """A cut's packed float32 points, moved into the assembly."""
    import array
    a = array.array("f")
    a.frombytes(raw)
    for i in range(0, len(a), 3):
        a[i], a[i + 1], a[i + 2] = fwd((a[i], a[i + 1], a[i + 2]))
    return a.tobytes()


def cut_sources(job: str) -> list[dict]:
    """Where this job's cuts come from: one sidecar for a single mesh, several for an assembly."""
    j = JOBS[job]
    spec = j.get("sources")
    if not spec:
        return [{"slug": j["slug"], "sidecar": j["sidecar"], "prefix": "", "fwd": None,
                 "inv": None}]
    t = ((j.get("data") or {}).get("quality") or {}).get("assembly", {}).get("transform") or {}
    out = []
    for s in spec:
        fwd = inv = None
        if s.get("moved"):
            if not t:
                continue              # a body we cannot place is a body we must not cut
            fwd, inv = _mover(t)
        out.append({"slug": s["slug"], "sidecar": SIDECARS / s["slug"],
                    "prefix": s.get("prefix", ""), "fwd": fwd, "inv": inv})
    return out


def cut_regions(job: str) -> list:
    """The regions a real cut could be made from for this job, or []. Never raises.

    This is `detail_window.delivered_regions` on the sidecar bundles rather than on a workspace,
    which is the whole point of the bundle: the two look the same from here. An assembly asks
    each of its bodies and reports them under the names the scene draws them by.
    """
    found: list = []
    for src in cut_sources(job):
        ws = src["sidecar"]
        if not (ws / "constant").is_dir():
            continue
        try:
            from meshpipeline.render.detail_window import delivered_regions
            for name, pm in delivered_regions(ws):
                found.append((src["prefix"] + (name or src["slug"]), pm))
        except Exception as exc:                # noqa: BLE001 - "can a cut be made" answers no
            print("   %s: sidecar %s unreadable (%s)" % (job, ws, exc))
    return found


# Mounted so the page's imports resolve exactly as they do in the console.
#
# WHY ui/js AND NOT apps/console/public/static/js. They are supposed to be mirrors and they are 47
# lines apart on HEAD: the console's copy predates the `drawn_as` work. The source copy is the one
# to look at.
MOUNTS = {
    "/static/js": REPO / "ui/js",
    "/static/vendor": REPO / "apps/console/public/static/vendor",
    "/static/css": REPO / "apps/console/public/static/css",
    "/static/assets": REPO / "apps/console/public/static/assets",
}

# workbench.css LAST: it is the sheet that turns the viewer into the stage.
_CSS = "".join('<link rel="stylesheet" href="/static/css/%s.css">' % n
               for n in ("tokens", "theme", "chrome", "shell", "result", "viewer", "workbench"))

_CHROME = """
  html,body{height:100%}
  body{margin:0;background:var(--bg,#0e1013);color:var(--text,#e6e8ea);
       font:14px/1.55 var(--sans,system-ui,-apple-system,"Segoe UI",sans-serif);overflow:hidden}
  #app{height:100vh}
  #app>header{display:flex;align-items:center;gap:14px;padding:10px 18px;
              border-bottom:1px solid #222834;background:#171b22}
  header .brand{font-weight:650;color:#f2f5f9;letter-spacing:.2px}
  header nav{display:flex;gap:6px}
  header a,header button.q{padding:5px 11px;border:1px solid #2a313d;border-radius:8px;
            color:#aab4c2;text-decoration:none;font-size:12.5px;background:transparent;
            font-family:inherit;cursor:pointer}
  header a:hover,header button.q:hover{border-color:#4d5a6e;color:#e6ecf5}
  header a.on{background:#1d232d;border-color:#3b82f6;color:#dce8ff}
  header .vsep{width:1px;height:20px;background:#2a313d}
  header .right{margin-left:auto;display:flex;align-items:center;gap:12px;
                font-size:11.5px;color:#6d7787;font-family:var(--mono,ui-monospace)}
  header .stub{color:#8a7a52;border:1px solid #3a2f12;background:#1b1507;padding:3px 9px;
               border-radius:999px;font-size:11px;cursor:help}
  header .stub.live{color:#6f9a6a;border-color:#1d3a18;background:#0d1a0b}
  /* IT WRAPS, IT DOES NOT SQUEEZE. Side by side, each page gets half the window, and the title,
     three view buttons, the badge and a subtitle naming 30,011,162 faces come to about the whole
     of it. With nothing allowed to wrap, flex shrank the badge and the subtitle into tall narrow
     columns of one word each. A second line is fine; a column of single words is not. */
  /* PIN THE STAGE INTO THE STAGE COLUMN. `#app.wb` is the console's two-column grid - the
     conversation drawer, then the mesh - and this page has no drawer, so grid auto-placement
     dropped `#workbench` into column 1. `wb-collapsed` sets that column to 0 px. Measured on
     /both: `grid-template-columns: 0px 470px` with the workbench 0 wide, `.v-canvas` 0 wide, and
     vtk's own `Math.max(2, ...)` floor giving a 2-pixel-wide render buffer - two panels of solid
     black with the payloads loaded and the readings all correct behind them. */
  #app.wb>#workbench{grid-column:2;grid-row:2/-1;min-width:0;min-height:0}
  #app>header{flex-wrap:wrap;row-gap:6px}
  header .stub{white-space:nowrap}
  header .right{flex-wrap:wrap;justify-content:flex-end;row-gap:4px;min-width:0}
  header .right>span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
"""

_PAIR = """<!doctype html>
<html><head><meta charset="utf-8"><title>Corintis - both delivered meshes</title>
<style>
  html,body{height:100%;margin:0;background:#0e1013;color:#e6e8ea;
            font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;overflow:hidden}
  /* A COLUMN, NOT A SUBTRACTION. `.grid` used to be `calc(100vh - 43px)`, which is the bar's
     height only while the bar is one line - and the reason paragraph wraps to two on a wide
     screen and three on a narrow one. Past that the grid ran off the bottom of a body with
     overflow hidden and the two viewers were cut off. */
  body{display:flex;flex-direction:column}
  .bar{display:flex;align-items:center;gap:14px;padding:10px 18px;flex:none;
       border-bottom:1px solid #222834;background:#171b22}
  .bar .brand{font-weight:650;color:#f2f5f9}
  .bar a{padding:5px 11px;border:1px solid #2a313d;border-radius:8px;color:#aab4c2;
         text-decoration:none;font-size:12.5px}
  .bar a.on{background:#1d232d;border-color:#3b82f6;color:#dce8ff}
  /* THE REASON IS A FOOTNOTE, NOT A HEADLINE. It used to sit open in the bar at max-width 62%
     and right-aligned, which is a tall narrow column of text: on a 705 px window it took 300 px
     of a 546 px page and left the two viewers 125 px each. It is worth reading once. It is not
     worth more of the screen than the meshes it is about. */
  .bar details.why{margin-left:auto;position:relative;font-size:11.5px;color:#8b95a5}
  .bar details.why summary{cursor:pointer;list-style:none;white-space:nowrap;padding:5px 11px;
       border:1px solid #2a313d;border-radius:8px;color:#aab4c2}
  .bar details.why summary::-webkit-details-marker{display:none}
  .bar details.why[open] summary{background:#1d232d;border-color:#3b82f6;color:#dce8ff}
  .bar details.why p{position:absolute;right:0;top:calc(100% + 6px);width:min(520px,86vw);
       margin:0;padding:11px 13px;background:#171b22;border:1px solid #2a313d;border-radius:10px;
       line-height:1.5;text-align:left;z-index:9;
       box-shadow:0 14px 34px -14px rgba(0,0,0,.85)}
  /* A FLOOR UNDER EACH PANEL, and the page scrolls rather than squeezing. Two 3D viewers each
     with a readings panel do not fit in a 364 px window, and `50%` of one gave them 157 px each:
     everything rendered, nothing could be seen. A minimum of 420 px and a scrollbar is the
     honest answer to a window that is too small. */
  .grid{display:grid;grid-template-columns:1fr 1fr;grid-auto-rows:minmax(420px,1fr);
        flex:1;min-height:0;overflow:auto}
  .grid iframe{border:0;width:100%;height:100%;min-height:420px;
               border-right:1px solid #222834}
  @media(max-width:1100px){.grid{grid-template-columns:1fr}}
</style></head><body>
<div class="bar"><span class="brand">Hexera</span>
  __NAV__<a href="/both" class="on">Both</a>
  <details class="why"><summary>Why side by side?</summary><p>__WHY__</p></details></div>
<div class="grid">__FRAMES__</div></body></html>
"""


def _pair_page() -> bytes:
    """The two meshes side by side, with the reason measured off the two payloads.

    THE REASON USED TO BE TYPED IN - "the plate's coolant spans 170.2 x 27.2 x 97.0 mm" - and it
    described a plate this page no longer serves. The argument it makes is a good one and it is
    about the numbers, so the numbers have to be the ones on screen or the argument is decoration.
    """
    nav = "".join('<a href="/%s">%s</a>' % (j["slug"], j["title"]) for j in JOBS.values())
    frames = "".join('<iframe src="/%s?bare=1" title="%s"></iframe>' % (j["slug"], j["title"])
                     for j in JOBS.values())
    spans = {j["slug"]: j.get("span") for j in JOBS.values()}
    why = "<b>They are not in the same frame.</b> "
    if spans.get("plate") and spans.get("chip"):
        p, c = spans["plate"], spans["chip"]
        thin = lambda s: "xyz"[min(range(3), key=lambda k: s[k])]       # noqa: E731
        why += ("The plate spans %s with the thickness on %s; the chip spans %s with the "
                "thickness on %s. " % (_mm(p), thin(p), _mm(c), thin(c)))
        # the original observation, re-derived rather than repeated: it only holds if it is true
        # of THESE two meshes, and it was first written about a plate that is gone.
        if c[1] > p[1]:
            why += ("The chip is wider in y than the plate's whole y span, so the two CAD files "
                    "share neither an origin nor an up axis. One scene would put the die "
                    "somewhere definite and wrong.")
        else:
            why += ("They are drawn apart because nothing in either file says where one sits "
                    "against the other. One scene would put the die somewhere definite and wrong.")
    else:
        why += ("Each is drawn in its own frame: nothing in either CAD file says where one sits "
                "against the other.")
    return (_PAIR.replace("__NAV__", nav).replace("__FRAMES__", frames)
                 .replace("__WHY__", why)).encode()


def _page(job: str, *, bare: bool) -> bytes:
    j = JOBS[job]
    slug = j["slug"]
    q = j["surface"].get("quality") or {}
    opts = json.dumps({"metrics": {"cells": q.get("cells"), "engine": q.get("engine")},
                       "files": [], "unreviewed": False})
    quick = "".join(
        '<button class="q" data-view="%s">%s</button>' % (pid or "", label)
        for label, pid in j["views"])
    # NO PAGE TABS. The assembly holds both bodies in the frame they actually sit in, and the
    # isolate buttons beside it show either one alone - so a tab per mesh, and a tab for the
    # split screen the assembly replaced, were four ways to reach three states. /plate, /chip
    # and /both still answer for anyone holding a link; nothing offers them.
    nav = ""
    head = ('<span class="brand">%s</span>' % ("Hexera" if not bare else j["title"]))
    # THE BADGE SAYS WHETHER A CUT IS LIVE, because that is the one thing about this page a reader
    # cannot find out by looking at it until they click the button.
    live = bool(cut_regions(job))
    stub = ('<span class="stub%s" title="The panel is ui/js/viewer/viewer.js off the branch and '
            'the layout is workbench.css. What is missing is the conversation drawer, because '
            'neither mesh was run as a job: both were staged by hand on a rented box, so there is '
            'no job row and no run to show. %s">served from a local stub%s</span>'
            % (" live" if live else "",
               ("A detail cut is live: it reads the boundary index in "
                + str(j["sidecar"]) + ", not the workspace.") if live else
               ("A detail cut needs " + j["where"]),
               " · cuts live" if live else " · no cuts"))
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{j['title']} - delivered mesh</title>{_CSS}
<style>{_CHROME}</style></head><body>
<div id="app">
  <header>{head}{nav}{'<span class="vsep"></span>' if quick else ''}{quick}
    <span class="right">{stub}<span>{j['sub']}</span>
      <button id="wb-toggle" hidden>Hide conversation</button></span></header>
  <div id="workbench" aria-label="Delivered mesh"></div>
</div>
<script type="module">
  import {{ openViewer }} from '/static/js/viewer/viewer.js';
  const JOB = {json.dumps(job)};
  const wb = document.getElementById('workbench');
  await openViewer(JOB, wb, {opts});
  // THE DRAWER HOLDS THE CONVERSATION and there is no conversation here, so it opens folded.
  // openViewer clears wb-collapsed on the way in, which is right in the console - a new result
  // should not appear behind a folded drawer - so it is set after.
  document.getElementById('app').classList.add('wb-collapsed');
  const tg = document.getElementById('wb-toggle'); if (tg) tg.hidden = true;

  // THE QUICK VIEWS DRIVE THE PART ROSTER, they do not reach past it: each button selects the
  // part a reader would click and then presses the viewer's own Isolate. So whatever these do,
  // a reader can do by hand in the panel, and the panel stays the single source of what is shown.
  for (const b of document.querySelectorAll('header [data-view]')) {{
    b.onclick = () => {{
      const id = b.dataset.view;
      if (!id) {{ document.getElementById('v-bnd-restore-' + JOB)?.click(); return; }}
      const chip = document.querySelector(
        '#v-bnd-chips-' + JOB + ' [data-p="' + id + '"]');
      if (!chip) return;
      if (!chip.classList.contains('sel')) chip.click();
      const iso = document.querySelector('#v-bnd-note-' + JOB + ' [data-a="iso"]');
      if (iso && !iso.classList.contains('on')) iso.click();
    }};
  }}
</script></body></html>"""
    return html.encode()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("   %s\n" % (fmt % args))

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, ctype: str, code=200, disposition: str = ""):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # NO-STORE, AND NOT AS A PRECAUTION. This server sent no cache headers at all, and a
        # browser with no headers to go on may keep what it has. A page left open while
        # viewer.js was being edited kept running the old script and drew the confetti the edit
        # had just removed, with nothing on screen to say the script was stale. A viewer that
        # misreports the mesh is worse than one that re-reads a local file per load.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def _window(self, job: str, qs: dict):
        """Every delivered face inside the box the reader is LOOKING at, or a refusal to send any.

        The deep-zoom half of /detail. A cut is a ball a reader opened on something and may be
        sampled; a window is the whole screen and may not - a sample of the whole screen is a
        screen with holes in it, and nobody can tell a hole in the sample from a hole in the mesh.
        So this either carries every face in the box or carries none and says how many there were.

        ONE BODY ONLY. An assembly's bodies live in different frames and a box is not
        rotation-invariant the way a ball is, so a moved body would need its box transformed into
        its own frame and the result is no longer a box. /detail does that with a radius because a
        rotation preserves distance; this refuses instead of guessing.
        """
        if not cut_regions(job):
            return self._json({"detail": "A window reads %s" % JOBS[job]["where"]}, code=409)
        try:
            lo = tuple(float(qs[k][0]) for k in ("x0", "y0", "z0"))
            hi = tuple(float(qs[k][0]) for k in ("x1", "y1", "z1"))
        except (KeyError, IndexError, ValueError):
            return self._json({"detail": "a window needs x0,y0,z0 and x1,y1,z1"}, code=422)
        srcs = cut_sources(job)
        if any(s["fwd"] for s in srcs):
            return self._json({"detail": "this scene places a body by a transform, and a box "
                                         "does not survive one - use a single-body page"},
                              code=409)

        from meshpipeline.render.detail_window import DetailError, detail_box, detail_response
        pats: list = []
        in_box = returned = 0
        fits = True
        for src in srcs:
            try:
                out = detail_box(src["sidecar"], lo=lo, hi=hi)
            except DetailError as exc:
                return self._json({"detail": str(exc)}, code=409)
            except Exception as exc:            # noqa: BLE001 - say which body, not just "500"
                sys.stderr.write("   window failed on %s/%s: %r\n" % (job, src["slug"], exc))
                return self._json({"detail": "%s: %s" % (src["slug"], exc)}, code=500)
            in_box += int(out.get("faces_in_box") or 0)
            if not out.get("fits"):
                fits = False
                continue
            for q in out.get("patches") or []:
                q["name"] = src["prefix"] + str(q.get("name") or "")
            pats += out.get("patches") or []
            returned += int(out.get("faces_returned") or 0)
        if not fits:
            # A 200, NOT A 4xX. This is the routine answer at every zoom above the threshold; the
            # client asks again on the next camera move, and an error here would fill a reader's
            # console with failures for the normal case.
            return self._json({"job_id": job, "fits": False, "patches": [],
                               "faces_in_box": in_box,
                               "why": "%s faces in this view" % format(in_box, ",")})
        body = detail_response(job, {"centre": [(lo[i] + hi[i]) / 2 for i in range(3)],
                                     "radius": 0.0, "faces_in_range": in_box,
                                     "faces_returned": returned, "truncated": False,
                                     "patches": pats})
        return self._json({**body, "fits": True, "faces_in_box": in_box,
                           "lo": list(lo), "hi": list(hi)})

    def _detail(self, job: str, qs: dict):
        """The same cut the console serves, when there is a sidecar bundle to serve it from.

        THE REFUSAL IS THE INTERESTING HALF. The viewer shows `detail` verbatim on the button, so
        when a cut cannot be made this has to say which thing is missing - not that the cut is
        impossible, which is what it used to say and which was wrong about the plate.
        """
        j = JOBS[job]
        if not cut_regions(job):
            where = ", ".join(f"{k}={qs.get(k, [''])[0]}" for k in ("x", "y", "z", "radius"))
            return self._json({"detail": "A detail cut reads %s Asked for %s."
                                         % (j["where"], where)}, code=409)
        try:
            x, y, z = (float(qs[k][0]) for k in ("x", "y", "z"))
            radius = float(qs["radius"][0])
        except (KeyError, IndexError, ValueError):
            return self._json({"detail": "a detail needs x, y, z and a radius"}, code=422)

        # one body or several: an assembly asks each of them and answers once
        from meshpipeline.render.detail_window import DetailError, detail_at, detail_response
        pats: list = []
        in_range = returned = 0
        trunc = False
        refused: list[str] = []
        for src in cut_sources(job):
            # INTO THE BODY'S OWN FRAME TO ASK, AND BACK INTO THE SCENE TO ANSWER. The index is
            # in the frame its mesh was built in; the reader clicked in the assembly. A rotation
            # preserves distance, so the radius travels unchanged.
            centre = src["inv"]((x, y, z)) if src["inv"] else (x, y, z)
            try:
                out = detail_at(src["sidecar"], centre=centre, radius=radius)
            except DetailError as exc:
                refused.append("%s: %s" % (src["slug"], exc))
                continue
            except Exception as exc:            # noqa: BLE001 - one body failing is not the cut
                sys.stderr.write("   detail failed on %s/%s: %r\n" % (job, src["slug"], exc))
                refused.append("%s: %s" % (src["slug"], exc))
                continue
            for q in out.get("patches") or []:
                if src["fwd"]:
                    q["points"] = _move_points(q["points"], src["fwd"])
                q["name"] = src["prefix"] + str(q.get("name") or "")
                pats.append(q)
            in_range += int(out.get("faces_in_range") or 0)
            returned += int(out.get("faces_returned") or 0)
            trunc = trunc or bool(out.get("truncated"))
        if not pats and refused:
            return self._json({"detail": "; ".join(refused)}, code=409)
        return self._json(detail_response(job, {
            "centre": (x, y, z), "radius": radius, "faces_in_range": in_range,
            "faces_returned": returned, "truncated": trunc, "patches": pats}))

    def do_GET(self):                           # noqa: N802 - the base class's name
        u = urlparse(self.path)
        p = u.path
        bare = parse_qs(u.query).get("bare", ["0"])[0] == "1"

        if p in ("/", "/index.html"):
            # THE ASSEMBLY IS THE DELIVERABLE, so it is what the root opens. The plate and the
            # chip keep their own pages for reading one body at a time; the root used to be
            # hard-wired to /plate and 404ed on a machine where only the chip had a payload.
            for _d in ("assembly", "plate", "chip"):
                if _d in BY_SLUG:
                    p = "/" + _d
                    break
            else:
                p = "/" + next(iter(sorted(BY_SLUG)), "plate")
        if p.lstrip("/") in BY_SLUG:
            return self._bytes(_page(BY_SLUG[p.lstrip("/")], bare=bare),
                               "text/html; charset=utf-8")
        if p == "/both":
            return self._bytes(_pair_page(), "text/html; charset=utf-8")

        if p == "/api/v1/client-config":
            return self._json({"viewer": VIEWER_CFG, "intake": {}, "auth": {}})

        if p.startswith("/api/v1/simulation/"):
            rest = p[len("/api/v1/simulation/"):]
            job, _, tail = rest.partition("/")
            j = JOBS.get(job)
            if j is None:
                return self._json({"detail": "no such job in this stub: %s" % job}, code=404)
            if tail == "surface":
                # the api adds job_id and up_axis to the stored surface and nothing else.
                # WHICH WAY IS UP IS THE MESH'S, NOT A CONSTANT. This sent "+z" for everything,
                # which is right for the chip and wrong for the plate and the assembly: the
                # plate's two ports stand off its +y face, so +y is up and a +z-up camera opens
                # the part lying on its side with the chip somewhere off to one edge.
                return self._json({"job_id": job, **j["surface"],
                                   "up_axis": j.get("up", "+z")})
            if tail == "surface.vtk":
                from meshpipeline.render.vtk_export import surface_to_vtk
                body = surface_to_vtk(j["surface"])
                if isinstance(body, str):
                    body = body.encode()
                return self._bytes(body, "application/octet-stream",
                                   disposition='attachment; filename="mesh_quality_%s.vtk"' % job)
            if tail == "detail":
                return self._detail(job, parse_qs(u.query))
            if tail == "window":
                return self._window(job, parse_qs(u.query))
            return self._json({"detail": "not served by this stub"}, code=404)

        for mount, root in MOUNTS.items():
            if p.startswith(mount + "/"):
                f = (root / p[len(mount) + 1:]).resolve()
                if root.resolve() not in f.parents and f != root.resolve():
                    return self._json({"detail": "outside the mount"}, code=403)
                if not f.is_file():
                    return self._json({"detail": "no such file: %s" % p}, code=404)
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if f.suffix in (".js", ".mjs"):
                    ctype = "text/javascript"
                return self._bytes(f.read_bytes(), ctype)

        return self._json({"detail": "not found: %s" % p}, code=404)


if __name__ == "__main__":
    for name, j in JOBS.items():
        q = j["surface"].get("quality") or {}
        pickable = [p for p in (j["surface"].get("parts") or []) if p.get("renderable")]
        print("=== %-16s %6.1f MB  %14s cells  %2d drawn patches  %2d parts (%d pickable)"
              % (name, j["payload"].stat().st_size / 1048576.0,
                 format(int(q.get("cells") or 0), ","),
                 len(j["surface"].get("patches") or []),
                 len(j["surface"].get("parts") or []), len(pickable)))
        regions = cut_regions(name)
        srcs = cut_sources(name)
        if regions:
            print("    detail cuts LIVE from %s: %s"
                  % (", ".join(str(s["sidecar"]) + (" (moved)" if s["fwd"] else "") for s in srcs),
                     ", ".join(r or "(single region)" for r, _ in regions)))
        else:
            print("    detail cuts not available: no sidecar bundle at %s"
                  % ", ".join(str(s["sidecar"]) for s in srcs))
    print("=== payloads mirrored at gs://%s/jobs/<id>/viewer_data.json" % BUCKET)
    print("=== viewer from %s, layout from workbench.css" % MOUNTS["/static/js"])
    print("=== http://localhost:%d/   %s  /both"
          % (PORT, "  ".join("/" + s for s in sorted(BY_SLUG))))
    # THREADED, BECAUSE /both IS TWO VIEWERS AT ONCE. `HTTPServer` answers one request at a time,
    # so the second iframe queued behind the first for every module, vtk.js and a 14-19 MB
    # payload - both panels sat black while the server worked through one of them. Two pages that
    # each open several connections cannot be served one connection at a time.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
