# Responsibility: Verify an oversized geometry report reaches the builder degraded, never as an
# unactionable over-cap notice (the model cannot make a parameterless tool "return a smaller result").
from __future__ import annotations

import json

import pytest
from tests._geometry_support import materialized

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.agents.builder import tools as T
from meshpipeline.agents.builder.tool_context import BuilderToolContext
from meshpipeline.agents.builder.tools.geometry import _CAP_MARGIN, _bounded_report


def _ctx(tmp_path, *, engine="gmsh"):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    geo = materialized(tmp_path / "mat")
    return BuilderToolContext(workspace=ws, geometry=geo, job_id="", execution_id="exec-1",
                              execution_generation=1, engine=engine, mesh_fidelity="standard",
                              loop_deadline=None)


def _volute_shaped_report(n_surfaces=57, n_curves=161) -> dict:
    # The shape of the report that killed job d0fc1033: a 2-solid pump volute whose 57
    # surfaces fit comfortably but whose 161-row curve table pushed the whole report past
    # the tool cap. Full-precision floats, exactly as the engine rounds them.
    surfaces = [{"tag": t, "area": round(0.00785398 + t * 1.7e-05, 8),
                 "centroid": [round(-0.041 + t * 1.3e-03, 6), round(0.45 - t * 7.9e-03, 6),
                              round(0.1975 - t * 2.3e-03, 6)]}
                for t in range(1, n_surfaces + 1)]
    curves = [{"tag": t, "length": round(0.31415927 + t * 3.1e-05, 8),
               "midpoint": [round(-0.045 + t * 6.7e-04, 6), round(0.6623 - t * 4.1e-03, 6),
                            round(0.4405 - t * 2.7e-03, 6)]}
              for t in range(1, n_curves + 1)]
    return {"volumes": 2, "surfaces": surfaces, "curves": curves,
            "bbox": [-0.045249, -0.212309, -0.192503, 0.063245, 0.45, 0.247988],
            "diag": 0.8017060260782365}


# the degradation ladder, step by step

def test_a_report_that_fits_is_returned_untouched():
    small = {"volumes": 2, "surfaces": [{"tag": 1, "area": 1.0, "centroid": [0, 0, 0]}],
             "curves": [], "bbox": [0, 0, 0, 1, 1, 1], "diag": 1.732}
    assert _bounded_report(small, 16000) == small


def test_a_solid_models_curve_table_is_dropped_first_and_counted():
    report = _volute_shaped_report()
    cap = 16000
    assert len(json.dumps(report)) > cap, "precondition: the volute-shaped report is oversized"
    out = _bounded_report(report, cap)
    assert len(json.dumps(out)) <= cap - _CAP_MARGIN
    assert out["curves"] == [] and out["curves_omitted"] == 161
    # every surface tag survives, uncompacted - the curves alone were the bulk
    assert [s["tag"] for s in out["surfaces"]] == list(range(1, 58))
    # and the engine's own dict was not mutated
    assert len(report["curves"]) == 161


def test_a_planar_models_curves_are_its_binding_surface_and_are_never_dropped():
    report = dict(_volute_shaped_report(), volumes=0)
    out = _bounded_report(report, 16000)
    assert "curves_omitted" not in out
    assert out["curves"] == report["curves"], (
        "a 2D case binds its contracted groups onto CURVE tags; dropping them strands the builder")


def test_surfaces_compact_to_arrays_before_anything_is_elided():
    report = _volute_shaped_report(n_surfaces=120, n_curves=0)
    dict_size = len(json.dumps(report))
    out = _bounded_report(report, dict_size - 1000 + _CAP_MARGIN)
    assert out["surfaces_format"] == "[tag, area, cx, cy, cz]"
    assert [row[0] for row in out["surfaces"]] == list(range(1, 121)), (
        "compaction must retain EVERY tag - the numbers shrink, the truth does not")
    assert "surfaces_omitted" not in out
    src = {s["tag"]: s for s in report["surfaces"]}
    for row in out["surfaces"]:
        assert row[1] == src[row[0]]["area"] and row[2:] == src[row[0]]["centroid"]


def test_the_last_resort_elides_the_smallest_faces_loudly():
    report = _volute_shaped_report(n_surfaces=200, n_curves=0)
    cap = 4000
    out = _bounded_report(report, cap)
    assert len(json.dumps(out)) <= cap - _CAP_MARGIN
    kept = [row[0] for row in out["surfaces"]]
    assert kept == sorted(kept), "kept rows stay in tag order"
    assert out["surfaces_omitted"] == 200 - len(kept) > 0
    assert "SMALLEST-area" in out["surfaces_note"]
    # areas rise with tag in the fixture, so the largest (highest) tags must all survive
    assert kept[-1] == 200 and min(kept) > 1


# the regression the volute died on, end to end through the dispatcher

def test_an_oversized_engine_report_reaches_the_model_usable_not_as_an_error(tmp_path, monkeypatch):
    import meshpipeline.engines.runtime as runtime
    report = _volute_shaped_report()
    assert len(json.dumps(report)) > rtcfg.MAX_TOOL_OUTPUT_CHARS, (
        "precondition: this report loses to the tool cap without the bound")

    real = runtime.get_engine

    def _spy(name, *_a, **_k):
        eng = real(name)

        class _Proxy:
            def __getattr__(self, attr): return getattr(eng, attr)

            def inspect_stl(self, workspace, *, context=None):
                return report
        return _Proxy()
    monkeypatch.setattr(runtime, "get_engine", _spy)

    out = json.loads(T._dispatch_tool(_ctx(tmp_path), "geometry_report", {}))
    assert "error" not in out, out
    assert "too large to return" not in json.dumps(out)
    assert [s["tag"] for s in out["surfaces"]] == list(range(1, 58)), (
        "the builder must learn every surface tag - that is the whole point of the report")
    assert out["curves_omitted"] == 161


@pytest.mark.parametrize("shape", [
    {"error": "geometry.step missing - the workspace was not staged for gmsh"},
    {"volumes": 1, "surfaces": "not-a-list", "curves": None},
])
def test_non_report_shapes_pass_through_unharmed(shape):
    assert _bounded_report(dict(shape), 16000) == shape
