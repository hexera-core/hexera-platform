# Responsibility: Check, for every engine alike, that each inlet and outlet the mesh delivers is the opening the user declared - the same size - so a mesh of the wrong region cannot pass with every other gate green.
# Owns: the delivered-vs-expected port area rule (expected = the opening measured on the geometry, else the size declared), the declared area of a port, the plain-words note when a typed size disagrees with the measured opening, the workspace record of measured openings, and reading delivered patch areas off an OpenFOAM case's VTK boundary.
# Boundaries: it reads the manifest and the workspace; it meshes nothing. Each engine records its patch areas (manifest quality "patch_areas_m2") and the openings its staging measured (quality "port_openings_m2"), and declares the gate in its chain.
# Collaborates with: engines/gates.py (GateCtx, refuse), the engines' finalize steps, engines/vmtk (its staged-cap check calls port_area_misses).
"""IS EACH PORT THE OPENING THAT WAS ASKED FOR?

A mesh can be valid, well shaped, carry every approved patch name and still be the wrong thing: an
inlet patch that took in the wall round the opening, a fill of a pipe's metal skin instead of its
bore, an annulus meshed as a full pipe with its centre body dropped. The size of each delivered
inlet and outlet gives each of these away, whatever engine made it: it must match the opening. On
the HOME-TURF lab audit (2026-10-06, 212 passing runs) six gmsh fluid-domain meshes passed every
gate with an inlet or outlet 2.4 to 5.3 times the declared opening - the builder's port groups had
swept in wall faces next to the openings.

WHICH SIZE IS THE OPENING. The one MEASURED on the geometry whenever the staging measured one: the
area the flow crosses at the port (flow_area_m2: a part declared a body has its fluid in the bore,
so a ring port face's inner wire less any centre body standing in it; a fluid solid's port face
itself; a surface staging's lid; gmsh measures the face its binder finds). The size the user TYPED
is used only when nothing was measured. A typed size is
often a guess: pvc_mixing_tee's spec says "about 20 mm" for a 32 mm bore (314 against 801 mm2),
and judged against the typed size every engine's correct mesh was refused. When the two disagree
the user is told so in plain words before meshing (size_notes) - not by a gate failure after it.

The band is wide on purpose: a staged lid and the delivered patch differ by the mesh's own
faceting, and a delivered port outside PORT_AREA_BAND of the opening is a different region, not a
rounding.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

#: delivered / expected port area outside this band = the port is not that opening
PORT_AREA_BAND = (0.6, 1.6)

#: a typed size within this band of the measured opening agrees with it (the port binder's own band,
#: engines/port_binding AREA_RATIO_LO/HI); outside it the user is told the measured size is used
SIZE_AGREES_BAND = (0.75, 1.25)

#: the openings the staging measured, {port: m2}, written beside the case for finalize and the gate
PORT_OPENINGS_FILE = "port_openings.json"


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


def flow_area_m2(rec: Mapping, *, bore: bool) -> float | None:
    """The area the flow crosses at one staged opening, in m2. A surface staging measured its lid
    ("flow_area"). A CAD port face (cad_tessellate) is read by which side is the fluid: with the
    fluid in the BORE of a part declared a body (`bore`), a ring face's inner wire, less the end
    face of a body standing in it (an annulus's centre rod, "filled"); otherwise - the solid is the
    fluid, or the port face is a plain disc - the port face itself."""
    if not isinstance(rec, Mapping):
        return None
    lid = _num(rec.get("flow_area"))
    if lid:
        return lid
    opening = rec.get("opening") if isinstance(rec.get("opening"), Mapping) else None
    inner = _num(opening.get("area")) if opening else None
    if bore and opening and inner:
        left = inner - (_num(opening.get("filled")) or 0.0)
        return left if left > 0.0 else inner
    return _num(rec.get("area"))


def measured_port_areas(openings: Mapping | None, *, bore: bool = True) -> dict[str, float]:
    """{port: the area the flow crosses there, m2} off a staging record's openings
    (flow_area_m2); openings with no measure are left out."""
    out: dict[str, float] = {}
    for name, rec in dict(openings or {}).items():
        a = flow_area_m2(rec, bore=bore) if isinstance(rec, Mapping) else None
        if a:
            out[str(name)] = a
    return out


def expected_port_areas(intake_patches, measured: Mapping[str, float] | None = None
                        ) -> dict[str, tuple[float, str]]:
    """{port: (area m2, "measured" | "declared")} for every declared inlet/outlet: the opening
    measured on the geometry where there is one, else the size the user declared; a port with
    neither is left out."""
    measured = {str(k): v for k, v in dict(measured or {}).items() if _num(v)}
    out: dict[str, tuple[float, str]] = {}
    for p in intake_patches or []:
        if not (isinstance(p, Mapping) and str(p.get("type") or "") in ("inlet", "outlet")
                and p.get("name")):
            continue
        name = str(p["name"])
        if name in measured:
            out[name] = (float(measured[name]), "measured")
        else:
            a = declared_port_area_m2(p)
            if a:
                out[name] = (a, "declared")
    return out


def _across_mm(area_m2: float) -> float:
    return math.sqrt(4.0 * area_m2 / math.pi) * 1000.0


def _typed(p: Mapping) -> str:
    d, inner = _num(p.get("diameter_mm")), _num(p.get("inner_diameter_mm"))
    w, h, a = _num(p.get("width_mm")), _num(p.get("height_mm")), _num(p.get("area_mm2"))
    if a:
        return f"{a:,.0f} mm2"
    if d and inner:
        return f"an annulus about {d:g} mm across round a {inner:g} mm centre"
    if d:
        return f"about {d:g} mm across"
    return f"{w:g} x {h:g} mm" if w and h else ""


def size_notes(intake_patches, measured: Mapping[str, float] | None,
               band: tuple[float, float] = SIZE_AGREES_BAND) -> list[str]:
    """One plain sentence for each declared inlet/outlet whose typed size disagrees with the
    opening measured on the geometry - said before meshing, and the measured one is used."""
    out = []
    measured = dict(measured or {})
    for p in intake_patches or []:
        if not (isinstance(p, Mapping) and str(p.get("type") or "") in ("inlet", "outlet")):
            continue
        name = str(p.get("name") or "")
        typed, got = declared_port_area_m2(p), _num(measured.get(name))
        if not (typed and got) or band[0] <= got / typed <= band[1]:
            continue
        round_bore = _num(p.get("diameter_mm")) and not _num(p.get("inner_diameter_mm"))
        size = (f"{_across_mm(got):.0f} mm across ({got * 1e6:,.0f} mm2)" if round_bore
                else f"{got * 1e6:,.0f} mm2 (about {_across_mm(got):.0f} mm across)")
        said = _typed(p) or f"{typed * 1e6:,.0f} mm2"
        out.append(f"{name}: you said {said}; the opening measures {size}; using the measured "
                   "opening.")
    return out


def record_port_openings(workspace, openings: Mapping | None, *, bore: bool = True
                         ) -> dict[str, float]:
    """Write the staging's measured openings ({port: m2}, measured_port_areas) beside the case,
    for finalize to carry into the manifest and the gate to judge by; returns them ({} and nothing
    written when none)."""
    import json
    measured = measured_port_areas(openings, bore=bore)
    if measured:
        (Path(workspace) / PORT_OPENINGS_FILE).write_text(
            json.dumps({k: round(v, 10) for k, v in measured.items()}, indent=1))
    return measured


def recorded_port_openings(workspace) -> dict[str, float]:
    """{port: m2} the staging recorded (record_port_openings); {} when none."""
    import json
    try:
        raw = json.loads((Path(workspace) / PORT_OPENINGS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in dict(raw or {}).items() if _num(v) is not None}


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


def _quality_map(manifest: Mapping, key: str) -> dict[str, float]:
    quality = manifest.get("quality")
    areas = quality.get(key) if isinstance(quality, Mapping) else None
    return {str(k): float(v) for k, v in dict(areas or {}).items() if _num(v) is not None}


def delivered_areas(manifest: Mapping) -> dict[str, float]:
    """{patch: area m2} the engine recorded in its quality report ({} when it records none)."""
    return _quality_map(manifest, "patch_areas_m2")


def measured_openings(manifest: Mapping) -> dict[str, float]:
    """{port: area m2} of the openings the engine's staging measured on the geometry ({} when
    it measured none)."""
    return _quality_map(manifest, "port_openings_m2")


def gate_port_areas(ctx) -> tuple[bool, str]:
    """The shared gate: every declared inlet/outlet delivered at about the size of its opening -
    the one measured on the geometry, else the one declared. A manifest without patch areas (an
    engine that does not record them) is not judged."""
    from meshpipeline.contracts.failure_cause import FailureCause
    from meshpipeline.engines.gates import refuse
    manifest = ctx.manifest_or_load()
    delivered = delivered_areas(manifest)
    if not delivered:
        return True, ""
    expected = expected_port_areas(ctx.intake_patches, measured_openings(manifest))
    misses = port_area_misses(delivered, {k: a for k, (a, _) in expected.items()})
    if not misses:
        return True, ""
    for m in misses:
        m["expected_from"] = expected[m["name"]][1]
    said = "; ".join(
        f"'{m['name']}' is {m['ratio']:g} times the opening "
        f"{'measured on the geometry' if m['expected_from'] == 'measured' else 'declared'} "
        f"({m['delivered_m2'] * 1e6:,.0f} mm2 against {m['expected_m2'] * 1e6:,.0f} mm2)"
        for m in misses)
    detail = (f"the mesh's {said} - the patch is not that opening: it took in faces round it, or "
              "the mesh fills a different region than the fluid")
    return False, refuse(
        f"[PORT_REGION] {detail}. Bind each port to its own opening face (in gmsh, leave the "
        "port groups out of gmsh_spec.json and the engine binds them), then run_mesh again.",
        FailureCause.PATCH_NOT_CAPTURED, detail=detail, misses=misses)


__all__ = ["PORT_AREA_BAND", "PORT_OPENINGS_FILE", "SIZE_AGREES_BAND", "declared_port_area_m2",
           "delivered_areas", "expected_port_areas", "flow_area_m2", "gate_port_areas",
           "measured_openings",
           "measured_port_areas", "patch_areas_from_vtk", "port_area_misses",
           "record_port_openings", "recorded_port_openings", "size_notes"]
