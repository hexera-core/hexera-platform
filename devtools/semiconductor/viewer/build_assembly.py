"""One scene: the cold plate with the chip mounted under it, placed by measurement.

THE PLACEMENT IS NO LONGER A CHOICE. The first version of this centred the die on the coolant's
footprint and said so, because nothing in either CAD file gives a shared datum. That was weak,
and it was wrong: it ignored the plate's own underside.

  * THE PLATE IS STEPPED. Only 19.9% of its footprint has an underside within 0.2 mm of the
    lowest plane. Over the die as first placed, 38.7% of the face had the plate standing off it
    by up to 6.04 mm - the chip was hanging in air over a third of its area, which no mounted
    die does.
  * A DIE SITS FLAT ON ITS PAD. That is a physical constraint, and it is strong enough to pick
    the answer: with the die 50 mm along x there is NO position anywhere on this plate that puts
    all of it on flat metal. Turned 90 degrees about the vertical there is, and it lands on 96.3%
    coolant coverage instead of 87.8%.

So: the thin axes must line up (geometry), the ports say which way is up (geometry), and the
die must lie flat on the pad (physics). Those three leave one placement, and this file finds it
rather than asserting it. The remaining freedom - which flat position, when several work - is
spent on coolant coverage, which is the only thing a cold plate is for.
"""
import array
import base64
import json
import pathlib
import sys

import numpy as np

REPO = pathlib.Path("/home/rehaan/hexera-platform-semi")
sys.path.insert(0, str(REPO / "src"))
from meshpipeline.render.boundary_index import open_index           # noqa: E402

HERE = pathlib.Path("/home/rehaan/corintis_view")
OUT = HERE / "assembly_viewer_data.json"
MM = 1000.0
H = 1.0 / MM                      # the placement grid
FLAT = 0.2 / MM                   # how far off the lowest plane still counts as the pad

plate = json.loads((HERE / "plate_viewer_data.json").read_text())
chip_d = json.loads((HERE / "chip_viewer_data.json").read_text())
ps, cs = plate["surface"], chip_d["surface"]
fl = open_index(HERE / "sidecar/plate/constant/fluid/polyMesh")
so = open_index(HERE / "sidecar/plate/constant/solid/polyMesh")
ci = open_index(HERE / "sidecar/chip/constant/polyMesh")

#: A rotation written as "output axis i takes input axis a[i], times s[i]". Both candidates are
#: proper rotations (determinant +1); a sign flip too many would mirror the chip, which would
#: still be the right size and still touch and still be wrong.
#:   base : (x,y,z) -> ( x, z, -y)   thin axis z -> thin axis y
#:   turn : (x,y,z) -> (-y, z, -x)   the same, then 90 deg about the vertical
CANDIDATES = {
    "base": {"axes": [0, 2, 1], "signs": [1.0, 1.0, -1.0]},
    "turned": {"axes": [1, 2, 0], "signs": [-1.0, 1.0, -1.0]},
}


def as_matrix(rot):
    m = np.zeros((3, 3))
    for i, (a, s) in enumerate(zip(rot["axes"], rot["signs"])):
        m[i, a] = s
    return m


def rotate(pts, rot):
    a, s = rot["axes"], rot["signs"]
    return np.column_stack([s[i] * pts[:, a[i]] for i in range(3)])


sp = np.asarray(so.points).reshape(-1, 3)
fy = np.asarray(fl.centroids).reshape(-1, 3)
contact = float(sp[:, 1].min())
X0, X1 = float(sp[:, 0].min()), float(sp[:, 0].max())
Z0, Z1 = float(sp[:, 2].min()), float(sp[:, 2].max())
NX = int(np.ceil((X1 - X0) / H)) + 1
NZ = int(np.ceil((Z1 - Z0) / H)) + 1


def grid_min(p):
    gi = np.clip(((p[:, 0] - X0) / H).astype(np.int64), 0, NX - 1)
    gk = np.clip(((p[:, 2] - Z0) / H).astype(np.int64), 0, NZ - 1)
    g = np.full(NX * NZ, np.inf)
    np.minimum.at(g, gi * NZ + gk, p[:, 1])
    return g.reshape(NX, NZ)


pad = np.isfinite(grid_min(sp)) & (grid_min(sp) < contact + FLAT)
cool = np.isfinite(grid_min(fy))
print("=== the plate's underside")
print("   %d x %d mm columns; pad (flat to %.1f mm) is %s of them (%.1f%%)"
      % (NX, NZ, FLAT * MM, f"{int(pad.sum()):,}", 100 * pad.mean()))


def integral(m):
    ii = np.zeros((NX + 1, NZ + 1), dtype=np.int64)
    ii[1:, 1:] = np.cumsum(np.cumsum(m.astype(np.int64), 0), 1)
    return ii


II_PAD, II_COOL = integral(pad), integral(cool)


def box(ii, p, q, w, d):
    return ii[p + w, q + d] - ii[p, q + d] - ii[p + w, q] + ii[p, q]


# the die is the chip's top face, rotated with the rest of it
po = np.asarray(ci.patch_of)
pid = [i for i, p in enumerate(ci.meta["patches"]) if p["name"] == "chip_zMax"][0]
die_pts = np.asarray(ci.centroids).reshape(-1, 3)[po == pid]

print("\n=== placing the die: it must lie flat on the pad, then take the most coolant")
best = None
for name, rot in CANDIDATES.items():
    r = rotate(die_pts, rot)
    w = int(round(r[:, 0].ptp() / H))
    d = int(round(r[:, 2].ptp() / H))
    hit = None
    for p in range(0, NX - w + 1):
        for q in range(0, NZ - d + 1):
            if box(II_PAD, p, q, w, d) < w * d:       # any column off the pad disqualifies it
                continue
            c = box(II_COOL, p, q, w, d)
            if hit is None or c > hit[0]:
                hit = (c, p, q)
    if hit is None:
        print("   %-7s die %3d x %3d mm along x,z  ->  NO position sits wholly on the pad"
              % (name, w, d))
        continue
    c, p, q = hit
    cx = X0 + (p + w / 2.0) * H
    cz = Z0 + (q + d / 2.0) * H
    print("   %-7s die %3d x %3d mm along x,z  ->  %.1f%% coolant, flat, centre x %.2f z %.2f mm"
          % (name, w, d, 100.0 * c / (w * d), cx * MM, cz * MM))
    if best is None or c / (w * d) > best["frac"]:
        best = {"name": name, "rot": rot, "frac": c / (w * d), "cx": cx, "cz": cz}

if best is None:
    raise SystemExit("no orientation puts the die flat on the plate - placement needs a human")
rot = best["rot"]
print("   -> %s" % best["name"].upper())

cpts = np.asarray(ci.points).reshape(-1, 3)
rot_all = rotate(cpts, rot)
rot_die = rotate(die_pts, rot)
DX = best["cx"] - float((rot_die[:, 0].min() + rot_die[:, 0].max()) / 2)
DY = contact - float(rot_all[:, 1].max())
DZ = best["cz"] - float((rot_die[:, 2].min() + rot_die[:, 2].max()) / 2)
M = as_matrix(rot)
print("\n=== transform")
print("   rotation determinant %+.1f (%s)"
      % (np.linalg.det(M), "proper" if np.linalg.det(M) > 0 else "MIRRORED - refuse"))
if np.linalg.det(M) < 0:
    raise SystemExit("the chosen rotation mirrors the chip")
print("   offset  DX %+8.3f  DY %+8.3f  DZ %+8.3f mm" % (DX * MM, DY * MM, DZ * MM))


def fwd_arr(a):
    v = np.frombuffer(a, dtype="<f4").reshape(-1, 3).astype(np.float64)
    out = rotate(v, rot) + np.array([DX, DY, DZ])
    return out.astype("<f4").tobytes()


moved = []
for p in cs.get("patches") or []:
    q = dict(p)
    if p.get("points_b64"):
        q["points_b64"] = base64.b64encode(fwd_arr(base64.b64decode(p["points_b64"]))).decode()
    q["name"] = "chip/" + str(p.get("name"))
    q.pop("encloses_domain", None)
    moved.append(q)

chk = rotate(cpts, rot) + np.array([DX, DY, DZ])
print("   chip now x %.2f..%.2f  y %.2f..%.2f  z %.2f..%.2f mm"
      % (chk[:, 0].min() * MM, chk[:, 0].max() * MM, chk[:, 1].min() * MM,
         chk[:, 1].max() * MM, chk[:, 2].min() * MM, chk[:, 2].max() * MM))
print("   highest chip point %.6f mm vs the plate's underside %.6f mm  ->  %s"
      % (chk[:, 1].max() * MM, contact * MM,
         "touching, no intersection" if chk[:, 1].max() <= contact + 1e-9 else "INTERSECTS"))

resp = dict(ps)
resp["patches"] = list(ps.get("patches") or []) + moved
_pr, _cr = ps.get("reduced") or {}, cs.get("reduced") or {}
if _pr or _cr:
    resp["reduced"] = {
        "delivered_faces": int(_pr.get("delivered_faces") or 0) + int(_cr.get("delivered_faces") or 0),
        "drawn_triangles": int(_pr.get("drawn_triangles") or 0) + int(_cr.get("drawn_triangles") or 0),
        "resolution_m": max(float(_pr.get("resolution_m") or 0.0), float(_cr.get("resolution_m") or 0.0)),
        "patches": {**(_pr.get("patches") or {}),
                    **{"chip/" + k: v for k, v in (_cr.get("patches") or {}).items()}},
    }

roles, faces = {}, {}
for src in (ps, cs):
    for p in src.get("patches") or []:
        n = str(p.get("name")).split("/", 1)[-1]
        roles.setdefault(n, str(p.get("type") or "wall"))
        faces[n] = faces.get(n, 0) + int(p.get("face_count") or 0)
manifest = {"mesh_mode": "cfmesh_conjugate + cfmesh", "mesh_units": "m",
            "patch_types": roles, "patch_face_counts": faces,
            "quality": {"regions": [{"name": "fluid", "type": "fluid"},
                                    {"name": "solid", "type": "solid"},
                                    {"name": "chip", "type": "solid"}]}}
from meshpipeline.application.viewer_payload import _attach_facts   # noqa: E402

resp["is_mesh"] = True
resp.pop("parts", None)
_attach_facts(resp, manifest)

quality = dict(plate.get("quality") or {})
quality["assembly"] = {
    "note": "the die lies flat on the plate's pad and takes the most coolant of the positions "
            "that do; the orientation is the only one in which the die fits the pad at all",
    "transform": {"axes": rot["axes"], "signs": rot["signs"],
                  "dx_m": DX, "dy_m": DY, "dz_m": DZ,
                  "orientation": best["name"]},
    "contact_plane_y_m": contact,
    "coolant_over_die": best["frac"],
}
OUT.write_text(json.dumps({"engine": "cfmesh_conjugate + cfmesh", "mesh_available": True,
                           "quality": quality, "review": {}, "surface": resp}))
tri = sum(int(p.get("face_count") or 0) for p in resp["patches"])
print("\n=== written %s  %.1f MB, %s triangles, %d parts"
      % (OUT, OUT.stat().st_size / 1e6, f"{tri:,}", len(resp.get("parts") or [])))
print("DONE")
