"""Repair foamMeshToFluent output so ANSYS Fluent imports it correctly.

Fixes the three defects found by external ANSYS validation of Hexera meshes:
  1. WRONG ZONE TYPES - every non-wall boundary is exported as `pressure-outlet`
     in the (39) zone list while its (13) face-section header carries bc code 4
     (pressure-inlet); the two disagree AND both are wrong for inlets.
  2. UNSTABLE ZONE IDS - ids follow OpenFOAM patch order, which varies mesh to
     mesh. Renumbered deterministically: fluid=1, interior=2, then boundary
     zones by role priority (velocity-inlet, pressure-outlet, wall, symmetry,
     rest) alphabetically within a role, starting at 10.
  3. INVERTED BOUNDARY NORMALS - verified geometrically per zone by sampling
     faces and testing the winding normal against the owner-cell direction;
     zones pointing inward get their node winding reversed.

Types come from patch NAMES (inlet -> velocity-inlet, outlet -> pressure-outlet,
wall/body -> wall, sym -> symmetry). A single enclosing `farfield` patch has no
perfect Fluent type for incompressible flow; default velocity-inlet, override
with --map farfield=pressure-far-field if the solve is compressible.

Usage:
  python fluent_repair.py IN.msh [-o OUT.msh] [--check] [--map name=type ...]
                                 [--report report.json] [--samples N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BC_CODE = {"interior": 2, "wall": 3, "pressure-inlet": 4, "pressure-outlet": 5,
           "symmetry": 7, "pressure-far-field": 9, "velocity-inlet": 10,
           "mass-flow-inlet": 20, "outflow": 31}
ROLE_PRIORITY = ["velocity-inlet", "mass-flow-inlet", "pressure-inlet",
                 "pressure-outlet", "outflow", "wall", "symmetry",
                 "pressure-far-field"]
NAME_RULES = [("inlet", "velocity-inlet"), ("outlet", "pressure-outlet"),
              ("wall", "wall"), ("body", "wall"), ("sym", "symmetry"),
              ("farfield", "velocity-inlet"), ("far-field", "velocity-inlet"),
              ("far_field", "velocity-inlet")]

RE_Z39 = re.compile(r"^\((39|45)\s+\((\d+)\s+(\S+)\s+(\S+)\)\(\)\)")
RE_F13 = re.compile(r"^\(13\s+\(([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)"
                    r"\s+([0-9a-fA-F]+)\s*([0-9a-fA-F]*)\)")
RE_N10 = re.compile(r"^\(10\s+\(([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)"
                    r"\s+([0-9a-fA-F]+)")


def infer_type(name: str, overrides: dict) -> str | None:
    low = name.lower()
    if low in overrides:
        return overrides[low]
    for key, typ in NAME_RULES:
        if key in low:
            return typ
    return None


def scan(path: Path, n_samples: int):
    """Pass 1: zone tables + sampled boundary faces (nodes + owner cell)."""
    zones39 = {}      # decimal id -> (kind, type_str, name)
    fzones = []       # face zones: dict(id, first, last, bc, ftype)
    samples = {}      # zone id -> list of (node_ids, owner)
    cur = None
    idx = 0
    with path.open("r", errors="replace") as fh:
        for line in fh:
            m = RE_Z39.match(line)
            if m:
                zones39[int(m.group(2))] = (m.group(1), m.group(3), m.group(4))
                continue
            m = RE_F13.match(line)
            if m:
                zid = int(m.group(1), 16)
                if zid == 0:
                    cur = None
                    continue
                z = {"id": zid, "first": int(m.group(2), 16),
                     "last": int(m.group(3), 16), "bc": int(m.group(4), 16),
                     "ftype": int(m.group(5) or "0", 16)}
                fzones.append(z)
                cur = z
                idx = 0
                n = z["last"] - z["first"] + 1
                step = max(1, n // n_samples)
                cur["_take"] = set(range(0, n, step))
                samples[zid] = []
                continue
            if cur is not None:
                s = line.strip()
                if not s or s.startswith(")") or s.startswith("("):
                    if s.startswith(")"):
                        cur = None
                    continue
                if idx in cur["_take"] and cur["bc"] != 2:
                    parts = [int(p, 16) for p in s.split()]
                    if cur["ftype"] == 0:
                        n = parts[0]
                        nodes, c0, c1 = parts[1:1 + n], parts[-2], parts[-1]
                    else:
                        nodes, c0, c1 = parts[:-2], parts[-2], parts[-1]
                    owner = c0 if c0 else c1
                    samples[zid].append((nodes, owner, c0, c1))
                idx += 1
    return zones39, fzones, samples


def gather_geometry(path: Path, wanted_cells: set):
    """Pass 2: node coordinates + face-centroid accumulator for wanted cells."""
    coords = {}
    acc = {c: [0.0, 0.0, 0.0, 0] for c in wanted_cells}
    nid = None
    cur = None
    with path.open("r", errors="replace") as fh:
        for line in fh:
            m = RE_N10.match(line)
            if m and int(m.group(1), 16) != 0:
                nid = int(m.group(2), 16)
                cur = None
                continue
            m = RE_F13.match(line)
            if m:
                nid = None
                cur = ({"ftype": int(m.group(5) or "0", 16)}
                       if int(m.group(1), 16) != 0 else None)
                continue
            s = line.strip()
            if not s or s.startswith(")"):
                if s.startswith(")"):
                    nid = None
                    cur = None
                continue
            if s == "(":
                continue    # the section's body-opening paren, not a header
            if s.startswith("("):
                # some other section header (12 cells, 0 comment, 39 zones...)
                nid = None
                cur = None
                continue
            if nid is not None:
                try:
                    x, y, z = (float(p) for p in s.split()[:3])
                except ValueError:
                    continue
                coords[nid] = (x, y, z)
                nid += 1
                continue
            if cur is not None:
                parts = [int(p, 16) for p in s.split()]
                if cur["ftype"] == 0:
                    n = parts[0]
                    nodes, c0, c1 = parts[1:1 + n], parts[-2], parts[-1]
                else:
                    nodes, c0, c1 = parts[:-2], parts[-2], parts[-1]
                for c in (c0, c1):
                    if c in acc:
                        cx = sum(coords[v][0] for v in nodes) / len(nodes)
                        cy = sum(coords[v][1] for v in nodes) / len(nodes)
                        cz = sum(coords[v][2] for v in nodes) / len(nodes)
                        a = acc[c]
                        a[0] += cx; a[1] += cy; a[2] += cz; a[3] += 1
    return coords, acc


def zone_orientation(samples, coords, cell_centroids):
    """Fraction of sampled faces whose winding normal points OUT of the owner."""
    out = 0
    tot = 0
    for nodes, owner, c0, c1 in samples:
        cc = cell_centroids.get(owner)
        if not cc or cc[3] == 0:
            continue
        ccx, ccy, ccz = cc[0] / cc[3], cc[1] / cc[3], cc[2] / cc[3]
        pts = [coords[v] for v in nodes if v in coords]
        if len(pts) < 3:
            continue
        fx = sum(p[0] for p in pts) / len(pts)
        fy = sum(p[1] for p in pts) / len(pts)
        fz = sum(p[2] for p in pts) / len(pts)
        # Newell normal over the polygon winding
        nx = ny = nz = 0.0
        for i in range(len(pts)):
            ax, ay, az = pts[i]
            bx, by, bz = pts[(i + 1) % len(pts)]
            nx += (ay - by) * (az + bz)
            ny += (az - bz) * (ax + bx)
            nz += (ax - bx) * (ay + by)
        # Fluent convention: right-hand winding normal points from c0 to c1;
        # boundary faces (c1=0) must therefore point AWAY from the owner c0.
        dot = nx * (fx - ccx) + ny * (fy - ccy) + nz * (fz - ccz)
        if (c1 == 0 and dot > 0) or (c0 == 0 and dot < 0):
            out += 1
        tot += 1
    return (out / tot) if tot else None


def repair(path: Path, out: Path, check_only: bool, overrides: dict,
           n_samples: int, report_path: Path | None):
    zones39, fzones, samples = scan(path, n_samples)
    report = {"file": str(path), "zones": [], "warnings": []}

    # decide types
    newtype = {}
    for zid, (_kind, typ, name) in zones39.items():
        if typ in ("fluid", "interior"):
            continue
        want = infer_type(name, overrides)
        if want is None:
            report["warnings"].append(
                f"zone '{name}' (id {zid}): no rule for this name - type left as '{typ}'")
            want = typ
        if "farfield" in name.lower() and name.lower() not in overrides:
            report["warnings"].append(
                f"zone '{name}': typed velocity-inlet by default - for compressible "
                "solves use --map farfield=pressure-far-field")
        newtype[zid] = want

    # deterministic renumbering: boundary zones from 10 by (role priority, name)
    bzones = [(zid, zones39[zid][2], newtype.get(zid, zones39[zid][1]))
              for zid in sorted(zones39) if zones39[zid][1] not in ("fluid", "interior")]
    order = sorted(bzones, key=lambda t: (
        ROLE_PRIORITY.index(t[2]) if t[2] in ROLE_PRIORITY else 99, t[1]))
    remap = {zid: 10 + i for i, (zid, _, _) in enumerate(order)}

    # normals verification
    wanted = {o for zid, ss in samples.items() for _, o, _, _ in ss}
    coords, acc = gather_geometry(path, wanted)
    flip = set()
    for z in fzones:
        if z["bc"] == 2 or z["id"] not in samples or not samples[z["id"]]:
            continue
        frac = zone_orientation(samples[z["id"]], coords, acc)
        name = zones39.get(z["id"], ("", "", "?"))[2]
        entry = {"name": name, "old_id": z["id"], "new_id": remap.get(z["id"], z["id"]),
                 "old_bc_code": z["bc"],
                 "new_type": newtype.get(z["id"], ""),
                 "new_bc_code": BC_CODE.get(newtype.get(z["id"], ""), z["bc"]),
                 "outward_fraction": None if frac is None else round(frac, 3)}
        if frac is not None and frac < 0.5:
            flip.add(z["id"])
            entry["normals"] = "INVERTED - winding reversed"
        else:
            entry["normals"] = "ok" if frac is not None else "unverified"
        report["zones"].append(entry)

    if check_only:
        print(json.dumps(report, indent=1))
        if report_path:
            report_path.write_text(json.dumps(report, indent=1))
        return report

    # Pass 3: rewrite
    cur_zone = None
    with path.open("r", errors="replace") as fh, out.open("w") as oh:
        for line in fh:
            m = RE_Z39.match(line)
            if m:
                zid = int(m.group(2))
                typ = newtype.get(zid, m.group(3))
                if int(m.group(2)) in remap or zid in newtype:
                    nid = remap.get(zid, zid)
                    oh.write(f"({m.group(1)} ({nid} {typ} {m.group(4)})())\n")
                    continue
                oh.write(line)
                continue
            m = RE_F13.match(line)
            if m:
                zid = int(m.group(1), 16)
                cur_zone = None
                if zid != 0 and zid in remap:
                    nid = remap[zid]
                    typ = newtype.get(zid)
                    bc = BC_CODE.get(typ, int(m.group(4), 16)) if typ else int(m.group(4), 16)
                    ftype = int(m.group(5) or "0", 16)
                    tail = line[m.end():]
                    oh.write(f"(13 ({nid:x} {m.group(2)} {m.group(3)} {bc:x} {ftype:x})"
                             f"{tail if tail.strip() else chr(10)}")
                    if zid in flip:
                        cur_zone = {"ftype": ftype}
                    continue
                oh.write(line)
                continue
            if cur_zone is not None:
                s = line.strip()
                if s.startswith(")"):
                    cur_zone = None
                    oh.write(line)
                    continue
                if s and not s.startswith("("):
                    parts = s.split()
                    if cur_zone["ftype"] == 0:
                        n = int(parts[0], 16)
                        nodes = parts[1:1 + n]
                        rest = parts[1 + n:]
                        oh.write(" ".join([parts[0]] + nodes[::-1] + rest) + "\n")
                    else:
                        nodes, rest = parts[:-2], parts[-2:]
                        oh.write(" ".join(nodes[::-1] + rest) + "\n")
                    continue
            oh.write(line)

    report["output"] = str(out)
    print(json.dumps(report, indent=1))
    if report_path:
        report_path.write_text(json.dumps(report, indent=1))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mesh")
    ap.add_argument("-o", "--out")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--map", action="append", default=[],
                    help="name=fluent-type override, e.g. farfield=pressure-far-field")
    ap.add_argument("--report")
    ap.add_argument("--samples", type=int, default=40)
    a = ap.parse_args()
    overrides = {}
    for kv in a.map:
        k, _, v = kv.partition("=")
        if v not in BC_CODE:
            sys.exit(f"unknown fluent type '{v}' (know: {', '.join(BC_CODE)})")
        overrides[k.lower()] = v
    src = Path(a.mesh)
    out = Path(a.out) if a.out else src.with_suffix(".fixed.msh")
    repair(src, out, a.check, overrides, a.samples,
           Path(a.report) if a.report else None)


if __name__ == "__main__":
    main()
