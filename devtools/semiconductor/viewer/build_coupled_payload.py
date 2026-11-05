"""Build the coupled plate+chip viewer payload from the coupled mesh's OWN index.

WHAT THIS IS FOR. `build_plate_payload.py` beside this file does the same job for the two-region
plate, and it is deliberately the same shape: read the sidecar's own meta files, assemble a
manifest out of them and nothing else, draw the surface from the index so the drawing and the cuts
come out of one mesh. The difference is the number of regions. The coupled case delivers THREE -
coolant, plate metal and chip - and the chip is the body the whole exercise exists to model, so a
payload that quietly carried two would be the wrong answer rendered convincingly.

IT INVENTS NO FIGURE, same rule as the plate. Every number here is read out of the sidecar's own
meta.json files or counted off its arrays. In particular it states no cell count: a boundary index
knows its faces exactly and its cells not at all, and a cell count estimated from the internal face
count would sit beside measured figures looking exactly like one of them.

usage: build_coupled_payload.py [slug] [region ...]
       default: cbox2 fluid solid chip
"""
import json
import pathlib
import sys

REPO = pathlib.Path("/home/rehaan/hexera-platform-semi")
sys.path.insert(0, str(REPO / "src"))

SLUG = sys.argv[1] if len(sys.argv) > 1 else "cbox2"
REGIONS = tuple(sys.argv[2:]) or ("fluid", "solid", "chip")
WS = pathlib.Path("/home/rehaan/corintis_view/sidecar") / SLUG
OUT = pathlib.Path("/home/rehaan/corintis_view/%s_viewer_data.json" % SLUG)

print("=== 0. where it is reading from")
print("  sidecar %s" % WS)
print("  regions asked for: %s" % ", ".join(REGIONS))
if not (WS / "constant").is_dir():
    raise SystemExit("no sidecar bundle at %s - pull it down first" % WS)

print()
print("=== 1. what the sidecar declares about itself")
metas, quals = {}, {}
for region in REGIONS:
    pm = WS / "constant" / region / "polyMesh"
    mj = pm / "boundaryIndex" / "meta.json"
    if mj.is_file():
        metas[region] = m = json.loads(mj.read_text())
        print("  %-6s index v%s  boundary faces %-12s internal %-12s points %s"
              % (region, m.get("version"), f"{m.get('n_faces'):,}",
                 f"{m.get('first_face'):,}", f"{m.get('n_points'):,}"))
        for q in m.get("patches") or []:
            print("           %-20s %-12s %s faces" % (q.get("name"), q.get("type"),
                                                       f"{q.get('n_faces'):,}"))
    else:
        print("  %-6s NO INDEX at %s" % (region, mj))
    qj = pm / "boundaryQuality" / "meta.json"
    if qj.is_file():
        quals[region] = json.loads(qj.read_text())
if not metas:
    raise SystemExit("no region in %s has an index" % WS)

print()
print("=== 2. the manifest, assembled from those files and nothing else")
#: BARE NAMES IN THE MANIFEST, prefixed names in the drawing. The viewer draws "<region>/<patch>"
#: so three regions' identically named boundaries cannot replace one another in the roster, while
#: `mesh_facts.parts` maps a bare name back onto every surface that draws it. A name more than one
#: region owns therefore carries the SUM, which is what that named boundary is across the mesh.
roles, faces = {}, {}
for region, m in metas.items():
    for q in m.get("patches") or []:
        n = str(q.get("name"))
        roles.setdefault(n, str(q.get("type") or "wall"))
        faces[n] = faces.get(n, 0) + int(q.get("n_faces") or 0)
manifest = {
    "mesh_mode": "cfmesh_conjugate",
    "mesh_units": "m",
    "patch_types": roles,
    "patch_face_counts": faces,
    "quality": {"regions": [{"name": r, "type": r} for r in metas]},
}
print("  patch_types      :", json.dumps(roles))
print("  patch_face_counts:", json.dumps({k: f"{v:,}" for k, v in faces.items()}))

print()
print("=== 3. the drawable surface, from the index (no mesh on this disk)")
from meshpipeline.application.viewer_payload import _attach_facts  # noqa: E402
from meshpipeline.engines.cfmesh_conjugate.viewer import conjugate_viewer  # noqa: E402

resp = conjugate_viewer(WS, roles=roles, units="m")
if not resp:
    raise SystemExit("conjugate_viewer returned nothing - the sidecar has no usable region")
#: IT IS A DELIVERED MESH AND MUST SAY SO, same as the plate: `_attach_facts` only lists every
#: drawn surface, and `mesh_facts.quality` only runs at all, when `is_mesh` is true.
resp["is_mesh"] = True
_attach_facts(resp, manifest)

print("  kind=%s is_mesh=%s reduced=%s" % (resp.get("kind"), resp.get("is_mesh"),
                                           bool(resp.get("reduced"))))
for p in resp.get("patches") or []:
    print("     drawn  %-26s %-12s %s" % (str(p.get("name"))[:26],
                                          f"{int(p.get('face_count') or 0):,}", p.get("type")))
red = resp.get("reduced") or {}
print("  reduced: %s drawn of %s delivered, merged at %.3f mm"
      % (f"{red.get('drawn_triangles', 0):,}", f"{red.get('delivered_faces', 0):,}",
         1000 * (red.get("resolution_m") or 0)))
parts = resp.get("parts") or []
print("  parts: %d (%d pickable)" % (len(parts), sum(1 for p in parts if p.get("renderable"))))
for p in parts:
    print("     %-28s %-10s renderable=%-5s vis=%s"
          % (p.get("id"), p.get("kind"), p.get("renderable"), p.get("default_visibility")))

print()
print("=== 4. the figures, counted off the sidecar")
n_faces = {r: int(m.get("n_faces") or 0) for r, m in metas.items()}
quality = dict(resp.get("quality") or {})
quality.update({
    "engine": "cfmesh_conjugate",
    "mesh_units": "m",
    "regions": len(metas),
    "boundary_faces": sum(n_faces.values()),
    "boundary_faces_by_region": n_faces,
    "internal_faces_by_region": {r: int(m.get("first_face") or 0) for r, m in metas.items()},
})
for region, m in quals.items():
    figs = m.get("figures") or {}
    quality.setdefault("measured", {})[region] = {
        k: {kk: v[kk] for kk in ("max", "mean", "measured") if kk in v}
        for k, v in figs.items()}
    quality.setdefault("measured_by", m.get("source"))
print("  boundary faces : %s  (%s)" % (
    f"{sum(n_faces.values()):,}",
    ", ".join("%s %s" % (r, f"{n:,}") for r, n in sorted(n_faces.items()))))
qf = resp.get("quality_fields") or {}
print("  hotspots       : %d" % len(qf.get("hotspots") or []))
for k, v in (qf.get("metrics") or {}).items():
    print("     %-14s max %-10.4f over bar %s of %s"
          % (k, v.get("max") or 0, f"{v.get('n_over', 0):,}", f"{v.get('n_faces', 0):,}"))

payload = {"engine": "cfmesh_conjugate", "mesh_available": True,
           "quality": quality, "review": {}, "surface": resp}
OUT.write_text(json.dumps(payload))
print()
print("=== 5. written  %s  %.1f MB" % (OUT, OUT.stat().st_size / 1e6))
print("DONE")
