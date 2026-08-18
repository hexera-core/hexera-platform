# Responsibility: Verify an engine's staging hook receives the interpretation once and its output is used unscaled.
from __future__ import annotations

import pytest
from tests._geometry_support import materialized

from meshpipeline.cad.staging import prepare_surface
from meshpipeline.contracts.geometry_units import LengthUnit

#: raw coordinate span of the surface the fake hook emits, before any conversion
RAW = 8.0
UNITS = [(LengthUnit.metre, 1.0), (LengthUnit.millimetre, 1e-3),
         (LengthUnit.centimetre, 1e-2), (LengthUnit.inch, 0.0254)]


class _RecordingEngine:

    def __init__(self):
        self.calls = []

    def tessellate_to_stl(self, geom_path, out_stl, *, context=None, prepared=None):
        import pyvista as pv
        self.calls.append(prepared)
        factor = prepared.to_metres
        pv.Box(bounds=(0.0, RAW * factor, 0.0, RAW * factor, 0.0, RAW * factor)) \
          .triangulate().save(str(out_stl))
        return out_stl


def _span(path):
    import pyvista as pv
    b = pv.read(str(path)).bounds
    return b[1] - b[0]


@pytest.fixture()
def _hook(monkeypatch):
    engine = _RecordingEngine()
    monkeypatch.setattr("meshpipeline.engines.runtime.get_engine", lambda name: engine)
    return engine


@pytest.mark.parametrize(("unit", "scale"), UNITS)
def test_the_hooks_output_is_used_as_is(tmp_path, _hook, unit, scale):
    geom = materialized(tmp_path / "m", filename="surface.vtp",
                        unit=unit)
    prepared = prepare_surface(geom, tmp_path / "out" / "input.stl", engine="vmtk")
    assert _span(prepared.path) == pytest.approx(RAW * scale, rel=1e-6)


@pytest.mark.parametrize(("unit", "scale"), UNITS)
def test_the_hook_receives_the_interpretation_exactly_once(tmp_path, _hook, unit, scale):
    geom = materialized(tmp_path / "m", filename="surface.vtp", unit=unit)
    prepare_surface(geom, tmp_path / "out" / "input.stl", engine="vmtk")
    assert len(_hook.calls) == 1
    assert _hook.calls[0].to_metres == pytest.approx(scale)


def test_a_second_scaling_would_be_detected(tmp_path, _hook):
    from meshpipeline.cad.normalise import scale_stl_file

    broken = []
    for unit, scale in UNITS:
        geom = materialized(tmp_path / f"m-{unit.value}", filename="surface.vtp",
                            unit=unit)
        dest = tmp_path / f"out-{unit.value}" / "input.stl"
        prepared = prepare_surface(geom, dest, engine="vmtk")
        if prepared.consumed.to_metres != 1.0:          # the deleted lines, verbatim
            scale_stl_file(dest, dest, prepared.consumed)
        if _span(dest) != pytest.approx(RAW * scale, rel=1e-6):
            broken.append(unit.value)
    assert sorted(broken) == ["cm", "in", "mm"], (
        "restoring the second scaling must corrupt every non-metre unit; metre stays correct "
        "because 1.0 squared is 1.0, which is precisely why this went unnoticed")
