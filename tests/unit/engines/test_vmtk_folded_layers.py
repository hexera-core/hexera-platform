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


def test_a_layered_fill_its_guard_stops_is_a_guard_trip_not_a_layer_free_mesh(tmp_path, monkeypatch):
    # docs/RULES.md TIME-1..3. Before D3 a layered fill got 60% of the clock and, when it ran out,
    # the layer-free fill was delivered instead - so whether a mesh kept its layers depended on how
    # busy the machine was. Now the layered step gets the whole guard, and a step the guard stops
    # ends the run as a guard trip (the attempt is replayed); the ladder never moves on a clock.
    from meshpipeline.engines import determinism as det
    clock = {'t': 0.0}
    calls: list[tuple[list[str], float]] = []

    def fake_run(argv, timeout=None, **kw):
        calls.append((list(argv), timeout))
        joined = ' '.join(argv)
        if 'vmtksurfaceremeshing' in joined:
            clock['t'] += 10.0
            (tmp_path / 'lumen.vtp').write_text('remeshed')
            return sp.CompletedProcess(argv, 0, stdout='Done.', stderr='')
        if '-boundarylayer 1' in joined:      # the layered fill runs past the run's guard
            clock['t'] += timeout
            raise sp.TimeoutExpired(argv, timeout)
        clock['t'] += 100.0
        (tmp_path / 'mesh.vtu').write_text('filled')
        return sp.CompletedProcess(argv, 0, stdout='Done executing vmtkmeshgenerator.', stderr='')

    monkeypatch.setattr(R, 'run_guarded', fake_run)
    monkeypatch.setattr(R, '_now', lambda: clock['t'])
    (tmp_path / 'vmtk_spec.json').write_text(json.dumps(R.resolve_strategy(
        {'sizing_array': 'LocalRadius', 'boundary_layers': 4})))
    (tmp_path / 'lumen_open.vtp').write_text('staged')
    with det.recording() as rec:
        res = R._run_vmtk_local(tmp_path, timeout=3000)
    gens = [(c, t) for c, t in calls if len(c) > 1 and c[1] == 'vmtkmeshgenerator']
    assert len(gens) == 1                                   # no layer-free fill after the trip
    assert gens[0][1] == 2990                               # the whole guard left, not a share
    assert res['timed_out'] is True and not (tmp_path / 'mesh.vtu').exists()
    assert [t['where'] for t in rec.guard_trips] == ['vmtk'] and rec.time_limited == []
    assert 'guard clock' in res['repair_note']


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
