# Responsibility: Build engine-output workspaces that satisfy a bundle's declared deliverable contract.
# Boundaries: generated from the engine's own declaration, so a contract change is reflected rather than restated here.
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

#: How many concrete matches a GLOB member is realised as. A glob exists because the count is
#: chosen per run (cfmesh_conjugate delivers across `plan.ranks` processor directories), so a
#: fixture that writes a directory literally called `processor*` is not a fixture of anything -
#: and that is exactly what this builder did, because it took `m.path` as a path.
_GLOB_MATCHES = 2

_ASCII_STL = """solid {name}
facet normal 0 0 1
  outer loop
    vertex 0 0 0
    vertex 1 0 0
    vertex 0 1 0
  endloop
endfacet
endsolid {name}
"""


def engine_names() -> list[str]:
    from meshpipeline.engines.registry import ENGINE_CATALOG
    return [n for n, s in ENGINE_CATALOG.items() if s.implemented and s.deliverable]


def build_workspace(root: Path, engine: str, *, include_optional: bool = True,
                    omit: tuple[str, ...] = (), empty: tuple[str, ...] = (),
                    manifest: dict | None | str = None) -> Path:
    from meshpipeline.engines.registry import get_spec
    spec = get_spec(engine)
    ws = root / f"ws-{engine}"
    ws.mkdir(parents=True, exist_ok=True)

    members = [m for m in spec.deliverable.members
               if (m.required or include_optional) and m.path not in omit]
    for m in members:
        # A GLOB IS REALISED, NOT WRITTEN DOWN. `ws / "processor*"` makes a directory with a star
        # in its name, which satisfies a member check by accident (fnmatch matches the literal
        # against its own pattern) and is not the shape any delivery has.
        if getattr(m, "is_glob", False):
            for k in range(_GLOB_MATCHES):
                hit = ws / m.path.replace("*", str(k))
                hit.mkdir(parents=True, exist_ok=True)
                if m.path not in empty:
                    (hit / "content.txt").write_text(f"{engine} {hit.name}\n")
            continue
        target = ws / m.path
        if m.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
            if m.path not in empty:
                # triSurface holds staged geometry; anything else just needs to be non-empty
                if target.name == "triSurface":
                    (target / "geom.stl").write_text(_ASCII_STL.format(name="wall"))
                else:
                    (target / "content.txt").write_text(f"{engine} {m.path}\n")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if m.path in empty:
                target.write_text("")
            elif target.suffix == ".stl":
                # a real (tiny) ASCII solid: the viewer payload renders from staged surfaces, so
                # a placeholder string would make every engine look like it delivered nothing
                target.write_text(_ASCII_STL.format(name=target.stem))
            else:
                target.write_text(f"{engine} {m.path}\n")

    # WHAT THE BUNDLE HAS TO HAVE INSIDE IT (Deliverable.must_contain), because the member list is
    # about PATHS and this builder only ever wrote paths. cfmesh_conjugate's recipe names `constant`
    # and `system`, and a workspace with both of those and a `content.txt` in each is a workspace
    # whose bundle has no mesh in it - which is precisely what job b8f09ae9 delivered and what that
    # rule now refuses. A fixture that cannot satisfy the engine's own content rule is not a
    # complete workspace, so the rule is read from the engine and realised here.
    #
    # UNDER A DECLARED DIRECTORY, because a file the recipe does not reach never enters the tar.
    # `fnmatch` is what matches these patterns and its `*` crosses slashes, so a pattern's tail
    # written under the first declared directory satisfies it: `*/polyMesh/owner` is matched by
    # `constant/region_a/polyMesh/owner`.
    dir_members = [m.path for m in members if m.kind == "dir" and not getattr(m, "is_glob", False)]
    for pat in (spec.deliverable.must_contain if "must_contain" not in omit else ()):
        tail = str(pat).split("*")[-1].lstrip("/")
        if not tail:
            continue
        cells = ws / (dir_members[0] if dir_members else ".") / "region_a" / tail
        cells.parent.mkdir(parents=True, exist_ok=True)
        if not cells.exists():
            cells.write_text(f"{engine} {tail}\n")
        # AND THE FIXTURE PROVES IT SATISFIED THE RULE rather than hoping the arithmetic held: a
        # recipe that changes its patterns must break here, loudly, and not in eight bundle tests.
        rel = cells.relative_to(ws).as_posix()
        prefix = spec.deliverable.prefix
        assert fnmatch.filter([f"{prefix}/{rel}"], f"{prefix}/{pat}"), (
            f"{engine}: the fixture wrote {rel} for must_contain {pat!r} and it does not match")

    # the marker proves the deliverable exists; it may sit inside a declared directory
    marker = ws / spec.deliverable.marker
    if spec.deliverable.marker not in omit:
        marker.parent.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            marker.write_text(f"{engine} marker\n")

    # Every engine consumes a tessellated surface staged as input.stl (see
    # agents/builder/agent.py), whatever its own deliverable declares. The viewer payload falls
    # back to staged surfaces, so a workspace without one is not a faithful fixture.
    staged = ws / "input.stl"
    if not staged.exists() and "input.stl" not in omit:
        staged.write_text(_ASCII_STL.format(name="wall"))

    mpath = ws / "mesh_manifest.json"
    if manifest == "malformed":
        mpath.write_text("{not json")
    elif manifest is not None:
        mpath.write_text(json.dumps(manifest))
    elif "mesh_manifest.json" not in omit:
        mpath.write_text(json.dumps({
            "mesh_mode": engine, "cell_count": 1234, "mesh_units": "m",
            "patch_types": {"wall": "wall", "inlet": "patch", "outlet": "patch"},
        }))
    return ws
