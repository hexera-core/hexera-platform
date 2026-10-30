# Responsibility: Verify a CFD mesh opens framed on the part, not on a ground plane many body lengths across, with the ground still drawn and "Fit all" taking in everything shown.
# Boundaries: the payload is the backend's own (three unit hexes) plus a flat ground patch in the same shape; only the network is stood in for. It reads the camera, never pixels.
from __future__ import annotations

import base64
import struct

import pytest
from conftest import assert_clean
from test_heatmap import _open, _payload

pytestmark = pytest.mark.ui

JOB = "frame-job"


def _ground(half: float = 30.0) -> dict:
    # a flat square 60 units across under the 3 x 1 x 1 row of cells: an external case's ground
    pts = [-half, -half, 0.0, half, -half, 0.0, half, half, 0.0, -half, half, 0.0]
    return {"name": "ground", "type": "wall", "face_count": 1,
            "points_b64": base64.b64encode(struct.pack("<12f", *pts)).decode(),
            "polys_b64": base64.b64encode(struct.pack("<5I", 4, 0, 1, 2, 3)).decode()}


def _camera(live) -> dict:
    return live.evaluate(f"""(() => {{ const d = window._vdbg['{JOB}'];
      return {{dist: d.dist(), focal: d.view().focal,
               fitAll: !document.getElementById('v-fitall-{JOB}').hidden}}; }})()""")


def test_a_cfd_mesh_opens_on_the_part_and_fit_all_takes_in_the_ground(live, tmp_path):
    # dev-areen 2026-10-30 (Ahmed body, job f5385d0f): the view opened on the ground plane and the
    # body was a speck in the middle of it
    surf = _payload(tmp_path, shear=0.0)
    surf["patches"].append(_ground())
    _open(live, surf, JOB)
    cam = _camera(live)
    # framed on the cells (0..3 x 0..1 x 0..1), close: the ground's own fit is ten times farther
    assert 0 <= cam["focal"][0] <= 3 and 0 <= cam["focal"][1] <= 1, cam
    assert cam["dist"] < 15, cam
    assert cam["fitAll"], cam
    live.evaluate(f"document.getElementById('v-fitall-{JOB}').click()")
    wide = _camera(live)
    assert wide["dist"] > 5 * cam["dist"], (cam, wide)
    # Fit to screen comes back to the part
    live.evaluate(f"document.getElementById('v-fitbtn-{JOB}').click()")
    back = _camera(live)
    assert abs(back["dist"] - cam["dist"]) < 0.05 * cam["dist"], (cam, back)
    assert_clean(live, "a CFD mesh framed on the part")


def test_a_mesh_with_no_ground_has_no_fit_all(live, tmp_path):
    _open(live, _payload(tmp_path, shear=0.0), JOB)
    assert not _camera(live)["fitAll"]
    assert_clean(live, "a mesh with no ground")
