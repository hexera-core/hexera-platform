# A gmsh run that stops on an error says WHY: gmsh's own reason reaches the user, not "the Builder
# did not produce a valid mesh deck" (CRM high-lift, lab 2026-10-05: "PLC Error: A segment and a
# facet intersect at point" was in the driver log while the user read the builder line).
from __future__ import annotations

import pytest

from meshpipeline.engines.gmsh import driver
from meshpipeline.engines.gmsh import gmsh_runner as R


def test_finalize_tells_gmshs_reason(tmp_path):
    (tmp_path / driver.STOP_REASON).write_text(
        "Exception: PLC Error:  A segment and a facet intersect at point")
    out = R.finalize(str(tmp_path), [], "gmsh")
    assert not out["success"]
    assert "gmsh stopped: Exception: PLC Error" in out["output"]
    assert "two faces of the closed surface cross or touch" in out["output"]


def test_without_a_reason_the_old_line_stands(tmp_path):
    assert R.finalize(str(tmp_path), [], "gmsh")["output"] == (
        "[GMSH] no mesh.inp - the Builder did not produce a valid mesh deck")


def test_the_driver_records_the_error_it_stops_on(tmp_path, monkeypatch):
    def _boom(ws):
        raise RuntimeError("no volume elements were generated")
    monkeypatch.setattr(driver, "main", _boom)
    with pytest.raises(RuntimeError):
        driver.run(str(tmp_path))
    assert "no volume elements" in (tmp_path / driver.STOP_REASON).read_text()
    monkeypatch.setattr(driver, "main", lambda ws: 0)
    assert driver.run(str(tmp_path)) == 0 and not (tmp_path / driver.STOP_REASON).exists()
