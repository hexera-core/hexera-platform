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
