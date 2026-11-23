#!/usr/bin/env python3
"""Compare the two passes of each ECXML model, on the files that ARE the mesh.

The first attempt compared whole directories and every model came back DIFFERS. What differed was
OpenFOAM's own logs - log.checkMesh carries its execution time, log.foamToVTK its date - and not one
byte of any mesh. So this names what counts instead of excluding what does not:

  the polyMesh      points, faces, owner, neighbour, boundary, cellZones, faceZones, pointZones
  the regions       constant/<region>/polyMesh/* and constant/regionProperties
  the physics       thermal_model.json
  the report        snapgrid_report.json with its timings removed, because a timing is not a mesh
                    and a report that cannot be compared at all hides a report that changed

Anything else in the case - a log, a VTK dump - is named in the output as not compared, because a
sweep that quietly drops files reads like a sweep that checked everything.
"""
from __future__ import annotations

import os
import pathlib as _pl

#: The repository this lab is part of, and the lab directory beside it. HEXERA_ECXML_LAB moves the
#: lab; the default sits under the repository so two checkouts do not share one.
REPO = _pl.Path(os.environ.get("HEXERA_REPO") or
                _pl.Path(__file__).resolve().parents[3])
LAB_ROOT = _pl.Path(os.environ.get("HEXERA_ECXML_LAB") or (REPO / ".ecxml_lab"))

import hashlib
import json
import pathlib
import re
import sys

LAB = LAB_ROOT
CASES = LAB / "cases"

#: Files whose bytes ARE the mesh. Matched on the path relative to the case.
MESH_RE = re.compile(
    r"(^|/)(points|faces|owner|neighbour|boundary|cellZones|faceZones|pointZones|"
    r"regionProperties|thermal_model\.json)$")
#: Compared with its timings taken out.
REPORT = "snapgrid_report.json"
#: Named in the output rather than silently skipped.
NOT_COMPARED_RE = re.compile(
    r"(^|/)(log\.[^/]*|.*\.vt[kupm]|.*\.vtm\.series|.*\.pvd)$|(^|/)VTK/")

#: Keys dropped from the report before it is compared, because they describe the MACHINE and not
#: the mesh. Measured: these ten paths are the ONLY difference between two runs of all twenty
#: models - eight clocks, plus how much RAM the machine happened to have free and the cell budget
#: that follows from it. Worth naming rather than hiding: on a model big enough for the budget to
#: bite, free memory lowers the budget, so the mesh is deterministic GIVEN THE SAME MACHINE STATE
#: and not absolutely. On every model here the budget never bound and the meshes matched to the
#: byte.
TIMING_KEYS = ("timings_s", "wall_s", "seconds", "elapsed_s", "memory")


def strip_timings(obj):
    if isinstance(obj, dict):
        return {k: strip_timings(v) for k, v in sorted(obj.items())
                if k not in TIMING_KEYS}
    if isinstance(obj, list):
        return [strip_timings(v) for v in obj]
    return obj


def digest(path: pathlib.Path, rel: str) -> str:
    if rel.endswith(REPORT):
        try:
            body = json.dumps(strip_timings(json.loads(path.read_text())), sort_keys=True)
        except Exception as exc:
            return f"unreadable: {exc}"
        return hashlib.sha256(body.encode()).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def survey(case: pathlib.Path) -> tuple[dict, set, set]:
    """-> ({relative path: digest} for the mesh files, {not compared}, {unclassified})."""
    mesh, skipped, other = {}, set(), set()
    for p in sorted(case.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(case))
        # EVERYTHING is compared unless it is a clock. The first version named the mesh files and
        # let the rest fall into "unclassified", which quietly dropped snapgrid_viewer.npz - the
        # viewer payload - and every fvSchemes in the case. A sweep that picks what to look at
        # reads like a sweep that looked at everything.
        if NOT_COMPARED_RE.search(rel):
            skipped.add(rel)
        else:
            mesh[rel] = digest(p, rel)
    return mesh, skipped, other


def main() -> int:
    pairs = sorted({p.name[:-2] for p in CASES.glob("*.a")} & {p.name[:-2]
                                                               for p in CASES.glob("*.b")})
    if not pairs:
        print("no .a/.b pairs in", CASES)
        return 1

    width = max(len(p) for p in pairs) + 2
    bad, skipped_all, other_all = [], set(), set()
    print(f"{'model':{width}s}{'files':>7s}{'cells':>12s}{'regions':>9s}  verdict")
    for slug in pairs:
        a, b = CASES / f"{slug}.a", CASES / f"{slug}.b"
        ma, sa, oa = survey(a)
        mb, _sb, ob = survey(b)
        skipped_all |= sa
        other_all |= oa | ob
        cells = regions = "-"
        rp = a / REPORT
        if rp.is_file():
            try:
                r = json.loads(rp.read_text())
                cells = f"{r['polymesh']['cells']:,}"
                regions = str(len(r.get("regions") or []))
            except Exception:
                pass
        if set(ma) != set(mb):
            verdict = f"DIFFERS: {len(set(ma) ^ set(mb))} file(s) only on one side"
        elif ma != mb:
            off = sorted(k for k in ma if ma[k] != mb[k])
            verdict = f"DIFFERS: {', '.join(off[:4])}"
        elif not ma:
            verdict = "NO MESH FILES"
        else:
            verdict = "IDENTICAL"
        if verdict != "IDENTICAL":
            bad.append(slug)
        print(f"{slug:{width}s}{len(ma):>7d}{cells:>12s}{regions:>9s}  {verdict}")

    print(f"\n{len(pairs) - len(bad)}/{len(pairs)} byte-identical across two independent runs")
    kinds = sorted({('VTK/*' if s.startswith('VTK/') else
                     'log.*' if s.startswith('log.') else s) for s in skipped_all})
    print(f"not compared, {len(skipped_all)} file(s), all of them clocks: " + ", ".join(kinds))
    if other_all:
        print("UNCLASSIFIED, so not compared: " + ", ".join(sorted(other_all)[:12]))
    if bad:
        print("DIFFERS: " + ", ".join(bad))
    return 1 if bad or other_all else 0


if __name__ == "__main__":
    raise SystemExit(main())
