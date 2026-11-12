# Responsibility: Prove in CalculiX that a SHELL WITH SOLID PATCHES (MIXED-9: the shell run's own mesh, the regions
# it cannot shell meshed solid and tied by SHLINK-4) carries load and moment as the part does, against the FEA path's
# own all-solid mesh: a thin plate with a tall boss (free-free modes 7 to 12, a static bend through the boss), a
# T-bracket's web on a cast block foot (fixed under the block: the web's tip bend and first modes), and a frame of
# four spokes round a solid hub (spokes clamped at their ends, the hub pushed sideways), as one body and with the hub
# a body of its own (a whole body as a patch).
# Boundaries: needs CalculiX (HEXERA_CCX, or ccx on the path). mm, N, MPa; one thread. Minutes per case.
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests.unit.engines.test_mixed_textbook import (  # noqa: E402
    CCX,
    MAT,
    NU,
    REQUIRE_CCX,
    E,
    _modes,
    _n,
    _record,
    _static,
)

pytestmark = pytest.mark.skipif(not REQUIRE_CCX and not (CCX and Path(CCX).exists()),
                                reason="CalculiX (ccx) not installed - set HEXERA_CCX to run")
#: modes and deflections against the all-solid mesh
TOL = 0.05


def _gmsh_step(path: Path, build) -> Path:
    """A part made with gmsh's OpenCASCADE kernel, written as STEP (mm)."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("part")
        build(gmsh.model.occ)
        gmsh.model.occ.synchronize()
        gmsh.write(str(path))
    finally:
        gmsh.finalize()
    return path


def _fused(occ, ents):
    if len(ents) > 1:
        occ.fuse([ents[0]], ents[1:])


def boss_plate(occ):
    """A 200 x 100 x 2 plate with a 20 mm round boss standing 15 mm on it."""
    _fused(occ, [(3, occ.addBox(0, 0, 0, 200, 100, 2)), (3, occ.addCylinder(100, 50, 0, 0, 0, 17, 10))])


def tbracket_block(occ):
    """A 2 mm web, 60 wide and 100 tall, standing on a 40 x 60 x 20 block foot."""
    _fused(occ, [(3, occ.addBox(0, 0, 0, 40, 60, 20)), (3, occ.addBox(19, 0, 20, 2, 60, 100))])


SPOKES = ((14, -1, 80, 2), (-94, -1, 80, 2), (-1, 14, 2, 80), (-1, -94, 2, 80))


def frame_hub(occ):
    """Four 2 mm spokes, 80 long and 20 tall, round a 30 mm hub 20 tall (one body)."""
    _fused(occ, [(3, occ.addCylinder(0, 0, 0, 0, 0, 20, 15))] + [(3, occ.addBox(x, y, 0, w, h, 20)) for x, y, w, h in SPOKES])


def frame_hub_multi(occ):
    """The same frame with the hub a body of its own, the spokes' ends on its face (the bodies share those faces)."""
    hub = occ.addCylinder(0, 0, 0, 0, 0, 20, 15)
    spokes = [occ.addBox(x, y, 0, w, h, 20) for x, y, w, h in SPOKES]
    occ.cut([(3, s) for s in spokes], [(3, hub)], removeTool=False)
    occ.fragment(occ.getEntities(3), [])


def _patched(step: Path, ws: Path):
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines import mixed_mesh as mm
    from meshpipeline.engines import solid_patches as SP

    out = ws / "_shell"
    record = ms.extract_file(step, out, mm_per_unit=1.0)
    p = SP.build(step, out, record, mm_per_unit=1.0)
    m = p.joined
    mm.write_inp(ws / "patched.inp", m, units="mm", materials=dict.fromkeys(range(1, 8), MAT))
    xyz = np.vstack([m.shell.xyz, m.solid.xyz])
    return (ws / "patched.inp").read_text(), xyz, set(range(1, len(m.shell.xyz) + 1)), m


def _solid(step: Path, ws: Path):
    """The FEA path's own all-solid mesh of the part (its bodies fused into one: the reference for both frames)."""
    from tests.unit.engines.test_mixed_textbook import _solid as solid_of

    from meshpipeline.cad.scout import read_cad

    return solid_of(read_cad(step), ws)


def _case(tmp_path, name, build, solid_build=None):
    step = _gmsh_step(tmp_path / f"{name}.step", build)
    ref = _gmsh_step(tmp_path / f"{name}_ref.step", solid_build) if solid_build else step
    return _patched(step, tmp_path / "p"), _solid(ref, tmp_path / "s")


def _compare_modes(tmp_path, case, patched, solid, first=6, n=6):
    fp, fs = _modes(tmp_path, "patched_m", patched), _modes(tmp_path, "solid_m", solid)
    out = []
    for k, (a, b) in enumerate(zip(fp[first:first + n], fs[first:first + n], strict=True), start=first + 1):
        _record(case, "modal", f"mode {k} vs all-solid", a, b, TOL, _n(patched), solid_elements=_n(solid))
        out.append((k, a, b))
    return fp, out


def test_a_plate_with_a_tall_boss(tmp_path):
    patched, solid = _case(tmp_path, "boss_plate", boss_plate)
    m = patched[3]
    assert m.counts["links"] == 1 and m.links[0].closed and len(m.regions) == 1
    fp, rows = _compare_modes(tmp_path, "patch_plate_boss", patched, solid)
    assert max(fp[:6]) < 1e-3 * fp[6]                  # free-free: six rigid modes, no mechanism in the join
    for _k, a, b in rows:
        assert a == pytest.approx(b, rel=TOL)

    def edge(xyz):
        return np.abs(xyz[:, 0]) < 1e-6

    def top(xyz):
        return (np.abs(xyz[:, 2] - 17.0) < 1e-6) & (np.hypot(xyz[:, 0] - 100, xyz[:, 1] - 50) < 10 + 1e-6)

    got = _static(tmp_path, "patched_s", patched, edge, top, (30.0, 0.0, -20.0), top)
    ref = _static(tmp_path, "solid_s", solid, edge, top, (30.0, 0.0, -20.0), top)
    for d, ax in ((2, "z"), (0, "x")):
        _record("patch_plate_boss", "static", f"boss top {ax} vs all-solid", got[d], ref[d], TOL, _n(patched))
        assert got[d] == pytest.approx(ref[d], rel=TOL)


def test_a_t_bracket_web_on_a_block_foot(tmp_path):
    patched, solid = _case(tmp_path, "tbracket_block", tbracket_block)
    assert patched[3].counts["links"] == 1

    def base(xyz):
        return np.abs(xyz[:, 2]) < 1e-6

    def tip(xyz):
        return np.abs(xyz[:, 2] - 120.0) < 1e-6

    got = _static(tmp_path, "patched_x", patched, base, tip, (20.0, 0.0, 0.0), tip)[0]
    ref = _static(tmp_path, "solid_x", solid, base, tip, (20.0, 0.0, 0.0), tip)[0]
    # THE WEB IS 30 WALLS WIDE: a shell bends it as a plate (E / (1 - nu^2)); the solid lets its free sides curl and
    # bends between plate and beam (measured 11 Nov: patched tip 0.7222 mm, clamped plate theory 0.7218, all-solid
    # 0.7758 - 6.9% softer: the shell idealisation's own error on a wide web, not the join's). The join is held to
    # the shell's own answer, the web clamped where it leaves the block; the all-solid number is recorded
    plate = 20.0 * 100.0 ** 3 * (1 - NU ** 2) / (3 * E * 60.0 * 2.0 ** 3 / 12)
    _record("patch_tbracket_block", "static", "web tip bend vs all-solid", got, ref, 0.08, _n(patched),
            solid_elements=_n(solid))
    _record("patch_tbracket_block", "static", "web tip bend vs clamped plate theory", got, plate, 0.03, _n(patched))
    assert got == pytest.approx(plate, rel=0.03)
    assert plate <= ref <= plate / (1 - NU ** 2)            # the solid between plate and beam, as it must be
    # fixed under the block: the first three modes (the web's bend, its twist, its second bend)
    fp, fs = _modes(tmp_path, "patched_fm", (_fixed(patched, base))), _modes(tmp_path, "solid_fm", _fixed(solid, base))
    # (the same plate-against-solid gap shows in the modes, as the square root of the stiffness: about 3.5%)
    for k, (a, b) in enumerate(zip(fp[:3], fs[:3], strict=True), start=1):
        _record("patch_tbracket_block", "modal", f"fixed mode {k} vs all-solid", a, b, 0.08, _n(patched))
        assert a == pytest.approx(b, rel=0.08)


def _fixed(model, where):
    """The model with `where` held (translations; a shell node's rotations too), for a fixed modal analysis."""
    from tests.unit.engines.test_mixed_textbook import _ids

    deck, xyz, shell_nodes, m = model
    ids = np.flatnonzero(where(xyz)) + 1
    text = deck + "\n".join(_ids("FIXM", ids) + ["*BOUNDARY", "FIXM, 1, 3"]
                            + [f"{i}, 4, 6" for i in ids.tolist() if i in shell_nodes]) + "\n"
    return text, xyz, shell_nodes, m


def _ends(xyz):
    return (np.abs(np.abs(xyz[:, 0]) - 94.0) < 1e-6) | (np.abs(np.abs(xyz[:, 1]) - 94.0) < 1e-6)


def _hub_top(xyz):
    return (np.abs(xyz[:, 2] - 20.0) < 1e-6) & (np.hypot(xyz[:, 0], xyz[:, 1]) < 15.0 - 1e-6)


@pytest.mark.parametrize("build", [frame_hub, frame_hub_multi], ids=["one_body", "hub_its_own_body"])
def test_a_frame_of_spokes_round_a_solid_hub(tmp_path, build):
    patched, solid = _case(tmp_path, build.__name__, build, frame_hub)
    m = patched[3]
    assert m.counts["links"] == 4 and len(m.regions) == 1
    case = f"patch_{build.__name__}"
    got = _static(tmp_path, "patched_y", patched, _ends, _hub_top, (0.0, 200.0, 0.0), _hub_top)[1]
    ref = _static(tmp_path, "solid_y", solid, _ends, _hub_top, (0.0, 200.0, 0.0), _hub_top)[1]
    _record(case, "static", "hub sideways vs all-solid", got, ref, TOL, _n(patched), solid_elements=_n(solid))
    assert got == pytest.approx(ref, rel=TOL)
    fp, fs = _modes(tmp_path, "patched_fm", _fixed(patched, _ends)), _modes(tmp_path, "solid_fm", _fixed(solid, _ends))
    for k, (a, b) in enumerate(zip(fp[:3], fs[:3], strict=True), start=1):
        _record(case, "modal", f"fixed mode {k} vs all-solid", a, b, TOL, _n(patched))
        assert a == pytest.approx(b, rel=TOL)
