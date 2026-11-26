# Responsibility: Hold MIXED-10 (engines/mixed_run.build): when the thin walls and the thick regions cannot be cut
# apart, the stop says the split's own reason; the walls too short for shell theory are named as the reason only
# when they were kept solid (MIXED-8 a gate, REACH_GATE True).
# Boundaries: the extraction runs on a built plate with a boss (cad/midsurface); the split is replaced by one that
# fails with a known reason, so no Boolean cut, mesher or solver runs.
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import mixed_split as mx  # noqa: E402
from meshpipeline.engines import mixed_run  # noqa: E402

WHY = "a Boolean cut failed"


def _failed_split(*_a, **_k):
    return SimpleNamespace(ok=False, why=WHY, cores=0, cut_distance=[], solid_volume=0.0, body_volume=1.0, notes=[])


def _stop(tmp_path, monkeypatch, gate: bool) -> str:
    step = tmp_path / "part.step"
    sf.write_step(sf.plate_with_boss(), step)
    monkeypatch.setattr(mx, "split_body", _failed_split)
    monkeypatch.setattr(mixed_run, "REACH_GATE", gate)
    # one wall said to be too short for shell theory, the rest enough to stay a shell
    monkeypatch.setattr(mixed_run, "too_thick", lambda record, reach: (["Mid_1"], 0.9, ""))
    rec = mixed_run.run(tmp_path, step, mm_per_unit=1.0)
    assert rec["made"] is False
    return rec["why"]


def test_a_failed_split_with_no_wall_kept_solid_says_the_split_s_own_reason(tmp_path, monkeypatch):
    why = _stop(tmp_path, monkeypatch, gate=False)
    assert why == mixed_run.WHY_SPLIT.format(why=WHY)
    assert "too thick" not in why and "kept solid" not in why


def test_with_the_reach_gate_on_the_walls_kept_solid_are_named(tmp_path, monkeypatch):
    why = _stop(tmp_path, monkeypatch, gate=True)
    assert why.startswith("Walls too thick for shells") and WHY in why
