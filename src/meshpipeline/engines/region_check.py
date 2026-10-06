# Responsibility: Check, for every engine alike, that each inlet and outlet the mesh delivers is the opening the user declared - the same size - so a mesh of the wrong region cannot pass with every other gate green.
# Owns: the delivered-vs-declared port area rule, the declared area of a port, and reading delivered patch areas off an OpenFOAM case's VTK boundary.
# Boundaries: it reads the manifest and the workspace; it meshes nothing. Each engine records its patch areas (manifest quality "patch_areas_m2") and declares the gate in its chain.
# Collaborates with: engines/gates.py (GateCtx, refuse), the engines' finalize steps, engines/vmtk (its staged-cap check calls port_area_misses).
"""IS EACH PORT THE OPENING THAT WAS ASKED FOR?

A mesh can be valid, well shaped, carry every approved patch name and still be the wrong thing: an
inlet patch that took in the wall round the opening, a fill of a pipe's metal skin instead of its
bore, an annulus meshed as a full pipe with its centre body dropped. The size of each delivered
inlet and outlet gives each of these away, whatever engine made it: it must match the opening the
user declared. On the HOME-TURF lab audit (2026-10-06, 212 passing runs) six gmsh fluid-domain
meshes passed every gate with an inlet or outlet 2.4 to 5.3 times the declared opening - the
builder's port groups had swept in wall faces next to the openings.

The band is wide on purpose: the declared size is the user's, often rounded, and the port binder
already holds the staged opening to 0.75-1.25 of it (engines/port_binding). A delivered port
outside PORT_AREA_BAND of the declared one is a different region, not a rounding.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

#: delivered / declared port area outside this band = the port is not the declared opening
PORT_AREA_BAND = (0.6, 1.6)


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def declared_port_area_m2(patch: Mapping) -> float | None:
    """The area the user declared for an inlet/outlet, in m2: a bore's disc, an annulus's ring, a
    rectangle, or a stated area. None for a port declared by location alone."""
    d, inner = _num(patch.get("diameter_mm")), _num(patch.get("inner_diameter_mm"))
    outer = _num(patch.get("outer_diameter_mm"))
    w, h, a = _num(patch.get("width_mm")), _num(patch.get("height_mm")), _num(patch.get("area_mm2"))
    if a:
        return a * 1e-6
    if inner and (outer or d):
        bore = max(outer or 0.0, d or 0.0)
        if bore > inner:
            return math.pi / 4.0 * (bore * bore - inner * inner) * 1e-6
    if d:
        return math.pi / 4.0 * d * d * 1e-6
    if w and h:
        return w * h * 1e-6
    return None


def port_area_misses(delivered: Mapping[str, float], expected: Mapping[str, float],
                     band: tuple[float, float] = PORT_AREA_BAND) -> list[dict]:
    """[{name, delivered_m2, expected_m2, ratio}] for every port whose delivered area falls outside
    `band` of the expected one. A port with no delivered area, or none expected, is not judged."""
    out = []
    for name, want in expected.items():
        got = delivered.get(name)
        w, g = _num(want), _num(got)
        if w is None or g is None:
            continue
        ratio = g / w
        if not band[0] <= ratio <= band[1]:
            out.append({"name": name, "delivered_m2": g, "expected_m2": w,
                        "ratio": round(ratio, 3)})
    return out


def patch_areas_from_vtk(workspace) -> dict[str, float]:
    """{patch: area m2} of an OpenFOAM case's boundary, read off the latest foamToVTK boundary
    files (the ones engines/surface_deliverable converts). {} when there are none."""
    from meshpipeline.engines.surface_deliverable import _patch_files
    out: dict[str, float] = {}
    for path in _patch_files(Path(workspace)):
        try:
            import pyvista as pv
            mesh = pv.read(path)
            if mesh.n_cells == 0:
                continue
            area = float(mesh.compute_cell_sizes(length=False, area=True, volume=False)
                         .cell_data["Area"].sum())
        except Exception:  # noqa: BLE001 - evidence; a patch it cannot read is not judged
            logger.debug("patch area of %s unreadable", path, exc_info=True)
            continue
        out[path.stem] = out.get(path.stem, 0.0) + area
    return out


def delivered_areas(manifest: Mapping) -> dict[str, float]:
    """{patch: area m2} the engine recorded in its quality report ({} when it records none)."""
    quality = manifest.get("quality")
    areas = quality.get("patch_areas_m2") if isinstance(quality, Mapping) else None
    return {str(k): float(v) for k, v in dict(areas or {}).items() if _num(v) is not None}


def gate_port_areas(ctx) -> tuple[bool, str]:
    """The shared gate: every declared, sized inlet/outlet delivered at about the declared size.
    A manifest without patch areas (an engine that does not record them) is not judged."""
    from meshpipeline.contracts.failure_cause import FailureCause
    from meshpipeline.engines.gates import refuse
    delivered = delivered_areas(ctx.manifest_or_load())
    if not delivered:
        return True, ""
    expected = {}
    for p in ctx.intake_patches or []:
        if isinstance(p, Mapping) and str(p.get("type") or "") in ("inlet", "outlet") and p.get("name"):
            a = declared_port_area_m2(p)
            if a:
                expected[str(p["name"])] = a
    misses = port_area_misses(delivered, expected)
    if not misses:
        return True, ""
    said = "; ".join(
        f"'{m['name']}' is {m['ratio']:g} times the opening declared "
        f"({m['delivered_m2'] * 1e6:,.0f} mm2 against {m['expected_m2'] * 1e6:,.0f} mm2)"
        for m in misses)
    detail = (f"the mesh's {said} - the patch is not that opening: it took in faces round it, or "
              "the mesh fills a different region than the fluid")
    return False, refuse(
        f"[PORT_REGION] {detail}. Bind each port to its own opening face (in gmsh, leave the "
        "port groups out of gmsh_spec.json and the engine binds them), then run_mesh again.",
        FailureCause.PATCH_NOT_CAPTURED, detail=detail, misses=misses)


__all__ = ["PORT_AREA_BAND", "declared_port_area_m2", "delivered_areas", "gate_port_areas",
           "patch_areas_from_vtk", "port_area_misses"]
