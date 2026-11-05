# Responsibility: Verify that a VMTK fill whose boundary layer folded into itself is not shipped:
#                 the repair ladder moves on to a thinner stack instead of stopping at it.
from __future__ import annotations

import json
import subprocess as sp

import pytest

from meshpipeline.engines.vmtk import vmtk_runner as R


def test_a_fill_whose_layers_folded_moves_to_a_thinner_stack(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "vmtksurfaceremeshing" in " ".join(argv):
            (tmp_path / "lumen.vtp").write_text("remeshed")
        else:
            (tmp_path / "mesh.vtu").write_text("filled")
        return sp.CompletedProcess(argv, 0, stdout="Done executing.", stderr="")

    folded = iter([0.277, 0.0])
    monkeypatch.setattr(R, "run_guarded", fake_run)
    monkeypatch.setattr(R, "_folded_share", lambda ws: next(folded))
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(R.resolve_strategy(
        {"sizing_array": "LocalRadius", "boundary_layers": 3})))
    (tmp_path / "lumen_open.vtp").write_text("staged")
    res = R._run_vmtk_local(tmp_path, timeout=100)
    gens = [c for c in calls if len(c) > 1 and c[1] == "vmtkmeshgenerator"]
    assert len(gens) == 2
    # straight past 'same stack, no generator remesh' to the half-thickness step
    assert "-thicknessfactor 0.333333" in " ".join(gens[1])
    assert "folded into itself" in res["repair_note"] and "27.7%" in res["repair_note"]
    assert json.loads((tmp_path / "vmtk_spec.json").read_text())[
        "boundary_layer_thickness_factor"] == pytest.approx(0.05)


def test_a_layer_stage_that_dies_goes_straight_to_a_layer_free_fill(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        joined = ' '.join(argv)
        if 'vmtksurfaceremeshing' in joined:
            (tmp_path / 'lumen.vtp').write_text('remeshed')
            return sp.CompletedProcess(argv, 0, stdout='Done.', stderr='')
        if '-boundarylayer 1' in joined:      # the layer stage dies (an orifice plate's edges)
            return sp.CompletedProcess(argv, -11, stdout='Generating boundary layer', stderr='')
        (tmp_path / 'mesh.vtu').write_text('filled')
        return sp.CompletedProcess(argv, 0, stdout='Done executing vmtkmeshgenerator.', stderr='')

    monkeypatch.setattr(R, 'run_guarded', fake_run)
    (tmp_path / 'vmtk_spec.json').write_text(json.dumps(R.resolve_strategy(
        {'sizing_array': 'LocalRadius', 'boundary_layers': 5})))
    (tmp_path / 'lumen_open.vtp').write_text('staged')
    res = R._run_vmtk_local(tmp_path, timeout=100)
    gens = [c for c in calls if len(c) > 1 and c[1] == 'vmtkmeshgenerator']
    assert len(gens) == 2 and '-boundarylayer 0' in ' '.join(gens[1])
    assert 'last generator stage: Generating boundary layer' in res['repair_note']
    assert json.loads((tmp_path / 'vmtk_spec.json').read_text())['boundary_layers'] == 0


def _ladder_run(tmp_path, monkeypatch, layered_end):
    """A staged run whose layered fill ends as `layered_end` says ('stall' / 'wall'); the clock is
    the run's own (R._now), every other step finishes."""
    from meshpipeline.sandbox.safe_exec import StalledExpired

    clock = {'t': 0.0}
    calls: list[tuple[list[str], float]] = []

    def fake_run(argv, timeout=None, **kw):
        calls.append((list(argv), timeout))
        assert kw.get('stall_s'), "a pype is not watched"
        joined = ' '.join(argv)
        if 'vmtksurfaceremeshing' in joined:
            clock['t'] += 10.0
            (tmp_path / 'lumen.vtp').write_text('remeshed')
            return sp.CompletedProcess(argv, 0, stdout='Done.', stderr='')
        if '-boundarylayer 1' in joined:
            if layered_end == 'stall':          # no sign of life for the stall window
                clock['t'] += 1200.0
                raise StalledExpired(argv, timeout, silence_s=900.0, ran_s=1200.0)
            clock['t'] += timeout               # still working when the machine's wall came
            raise sp.TimeoutExpired(argv, timeout)
        clock['t'] += 100.0
        (tmp_path / 'mesh.vtu').write_text('filled')
        return sp.CompletedProcess(argv, 0, stdout='Done executing vmtkmeshgenerator.', stderr='')

    monkeypatch.setattr(R, 'run_guarded', fake_run)
    monkeypatch.setattr(R, '_now', lambda: clock['t'])
    (tmp_path / 'vmtk_spec.json').write_text(json.dumps(R.resolve_strategy(
        {'sizing_array': 'LocalRadius', 'boundary_layers': 4})))
    (tmp_path / 'lumen_open.vtp').write_text('staged')
    res = R._run_vmtk_local(tmp_path, timeout=13_800)
    gens = [(c, t) for c, t in calls if len(c) > 1 and c[1] == 'vmtkmeshgenerator']
    return res, gens


def test_a_stuck_layered_fill_is_a_guard_trip_not_a_layer_free_mesh(tmp_path, monkeypatch):
    """NO SHARE OF THE CLOCK (contracts/native_time): the layered fill may run to the machine's wall -
    it used to get 60% of 3,000 s. And a STUCK one (no sign of life for the stall window) is a guard
    trip (docs/RULES.md TIME-1..3): the attempt is replayed as it was, never the layer-free fill
    because a clock ran out - so whether a mesh keeps its layers never depends on a clock."""
    from meshpipeline.engines import determinism as det
    with det.recording() as rec:
        res, gens = _ladder_run(tmp_path, monkeypatch, 'stall')
    assert len(gens) == 1                                   # no layer-free fill after the trip
    assert gens[0][1] >= 13_800 - 10 - 1                  # the whole wall, not a share of it
    assert res['timed_out'] is True and not (tmp_path / 'mesh.vtu').exists()
    assert [t['where'] for t in rec.guard_trips] == ['vmtk'] and rec.time_limited == []
    assert 'guard clock' in res['repair_note']


def test_a_layered_fill_still_working_at_the_wall_starts_no_other_step(tmp_path, monkeypatch):
    """At the machine's wall the run is handed back still working (timed out, not stalled), so the
    same case runs again on a VM (RUN-5) - never a layer-free fill squeezed into what is left."""
    res, gens = _ladder_run(tmp_path, monkeypatch, 'wall')
    assert len(gens) == 1
    assert res['timed_out'] and not res.get('stalled')
    assert 'still working' in res['repair_note']
    assert not (tmp_path / 'mesh.vtu').exists()


@pytest.mark.parametrize('surface_s', [10.0, 2500.0])
def test_the_ladder_takes_the_same_steps_whatever_the_time_left(tmp_path, monkeypatch, surface_s):
    # a slow surface stage (2,500 s of a 3,000 s guard) used to leave "under 60 s" for later
    # steps, which were then not started; now the same failures take the same steps either way
    clock = {'t': 0.0}
    calls: list[list[str]] = []
    folded = iter([0.277, 0.0])

    def fake_run(argv, timeout=None, **kw):
        calls.append(list(argv))
        if 'vmtksurfaceremeshing' in ' '.join(argv):
            clock['t'] += surface_s
            (tmp_path / 'lumen.vtp').write_text('remeshed')
        else:
            clock['t'] += 200.0
            (tmp_path / 'mesh.vtu').write_text('filled')
        return sp.CompletedProcess(argv, 0, stdout='Done executing.', stderr='')

    monkeypatch.setattr(R, 'run_guarded', fake_run)
    monkeypatch.setattr(R, '_now', lambda: clock['t'])
    monkeypatch.setattr(R, '_folded_share', lambda ws: next(folded))
    monkeypatch.setattr(R, 'export_openfoam_case', lambda ws, timeout: None)
    (tmp_path / 'vmtk_spec.json').write_text(json.dumps(R.resolve_strategy(
        {'sizing_array': 'LocalRadius', 'boundary_layers': 3})))
    (tmp_path / 'lumen_open.vtp').write_text('staged')
    res = R._run_vmtk_local(tmp_path, timeout=3000)
    gens = [c for c in calls if len(c) > 1 and c[1] == 'vmtkmeshgenerator']
    assert len(gens) == 2, "the folded stack is followed by a thinner one, however late"
    assert not res.get('timed_out')
    assert 'folded into itself' in res['repair_note'] and 'guard' not in res['repair_note']
