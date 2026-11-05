# Responsibility: Build engine-output workspaces that satisfy a bundle's declared deliverable contract.
# Boundaries: generated from the engine's own declaration, so a contract change is reflected rather than restated here.
from __future__ import annotations

import json
from pathlib import Path

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
