"""Build the plate's viewer payload from ws_shell's OWN index, so the drawing and the cuts are
the same mesh.

WHY THIS AND NOT THE OLD PAYLOAD. The payload that was on this laptop draws a 328,714,251-cell
mesh that no longer exists anywhere, and its cuts had to refuse because no index for it survives.
ws_shell does exist, its index is here, and `render.index_surface.surface_from_index` is tested
byte-identical to the polyMesh parse - so the surface drawn from the index IS the surface of the
mesh the cuts come out of. One mesh, both ends.

WHAT THIS DOES NOT DO. It invents no figure. Every number below is read out of the sidecar's own
meta files or counted off its own arrays. In particular it does NOT state a cell count: a boundary
index knows its faces exactly and its cells not at all. The cells could be estimated from the
internal face count (hex-dominant, cells ~ (2*internal + boundary)/6, which lands within about 1%
of what the owner files said) and an estimate printed beside exact figures is the thing a reader
cannot tell apart from a measurement. So the roster carries faces, which are counted, and no
cells.
"""
import json
import pathlib
import sys

REPO = pathlib.Path("/home/rehaan/hexera-platform-semi")
sys.path.insert(0, str(REPO / "src"))

WS = pathlib.Path("/home/rehaan/corintis_view/sidecar/plate")
OUT = pathlib.Path("/home/rehaan/corintis_view/plate_viewer_data.json")
REGIONS = ("fluid", "solid")

print("=== 1. what the sidecar declares about itself")
metas, quals = {}, {}
for region in REGIONS:
    pm = WS / "constant" / region / "polyMesh"
    mj = pm / "boundaryIndex" / "meta.json"
    if mj.is_file():
        metas[region] = json.loads(mj.read_text())
        m = metas[region]
        print("  %-6s index v%s  boundary faces %-12s internal %-12s points %s"
              % (region, m.get("version"), f"{m.get('n_faces'):,}",
                 f"{m.get('first_face'):,}", f"{m.get('n_points'):,}"))
        for q in m.get("patches") or []:
            print("           %-18s %-12s %s faces" % (q.get("name"), q.get("type"),
                                                       f"{q.get('n_faces'):,}"))
    qj = pm / "boundaryQuality" / "meta.json"
    if qj.is_file():
        quals[region] = json.loads(qj.read_text())

print()
print("=== 2. the manifest, assembled from those two files and nothing else")
#: ROLES AND FACE COUNTS ARE THE MANIFEST'S, AND THE MANIFEST KEEPS BARE NAMES. The viewer draws
#: "<region>/<patch>" so the two regions' identically named boundaries cannot replace one another
#: in the roster; `mesh_facts.parts` maps a bare name back onto every surface that draws it. A
#: name both regions own - `port_a` here - therefore carries the SUM of the two, which is what
#: that named boundary is across the whole mesh.
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
    # the regions ARE named, so the roster lists each under its own name. No `cells`: see above.
    "quality": {"regions": [{"name": r, "type": r} for r in metas]},
}
print("  patch_types      :", json.dumps(roles))
print("  patch_face_counts:", json.dumps({k: f"{v:,}" for k, v in faces.items()}))

print()
print("=== 3. the drawable surface, from the index (no mesh on this disk)")
from meshpipeline.application.viewer_payload import _attach_facts
from meshpipeline.engines.cfmesh_conjugate.viewer import conjugate_viewer

resp = conjugate_viewer(WS, roles=roles, units="m")
if not resp:
    raise SystemExit("conjugate_viewer returned nothing - the sidecar has no usable region")

#: IT IS A DELIVERED MESH AND MUST SAY SO. `_attach_facts` only lists every drawn surface, and
#: `mesh_facts.quality` only runs at all, when `is_mesh` is true - left unset, the plate came up
#: with "0 parts (0 pickable)" and the Coolant / Metal buttons had no roster to drive.
resp["is_mesh"] = True
_attach_facts(resp, manifest)

print("  kind=%s is_mesh=%s reduced=%s" % (resp.get("kind"), resp.get("is_mesh"),
                                           bool(resp.get("reduced"))))
for p in resp.get("patches") or []:
    print("     drawn  %-24s %-9s %s" % (str(p.get("name"))[:24],
                                         f"{int(p.get('face_count') or 0):,}", p.get("type")))
red = resp.get("reduced") or {}
print("  reduced: %s of %s faces, merged at %.3f mm"
      % (f"{red.get('drawn_triangles', 0):,}", f"{red.get('delivered_faces', 0):,}",
         1000 * (red.get("resolution_m") or 0)))
parts = resp.get("parts") or []
print("  parts: %d (%d pickable)" % (len(parts), sum(1 for p in parts if p.get("renderable"))))
for p in parts:
    print("     %-26s %-10s renderable=%-5s vis=%s"
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
