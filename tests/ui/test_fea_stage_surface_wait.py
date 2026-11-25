# Responsibility: Verify on the FEA stage in a real browser that a CAD file with no solid, while it is still being read
# as a surface (SURF-18), shows no stop and no way out: Proceed is off, the hint beside it says the file is being read
# as a surface and that this takes a few minutes, and the shell fold says it is reading the surface (never "you can
# proceed now"). When the reading lands the same panel follows in place: Proceed on, the hint back, the surface line.
# Boundaries: the views are the server's shapes, built here from application/surface_check's own words; the skin is
# the mesh-as test's tube. Only the network is stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean
from test_fea_stage_mesh_as import _open, _proceed

from meshpipeline.application import surface_check

pytestmark = pytest.mark.ui


def _fold(pending: bool) -> dict:
    return {"question": "Mesh as", "state": "measuring" if pending else "ready", "said": "shell", "said_words": "",
            "mesh_as": "shell", "surface": True, "by": "surface file",
            "label": surface_check.PENDING_LABEL if pending else "This file is a surface, not a solid. It is meshed as "
            "a shell, 2 mm thick.", "reason": "", "measuring_words": surface_check.PENDING_MEASURING if pending else "",
            "bodies": [], "not_shelled": [], "open_edges": 0, "open_at_mm": [], "overlay": False}


def _check(pending: bool) -> dict:
    fea = {"question": "Is this your part and are the units right?", "bodies": 0, "clean": False,
           "clean_summary": "The surface is not closed.", "single_part_only": "", "way_out": "",
           "blocked_kind": surface_check.PENDING_KIND if pending else "", "can_proceed": not pending,
           "waiting": surface_check.PENDING_WORDS if pending else "", "measure": None,
           "clean_short": surface_check.PENDING_SHORT if pending else surface_check.CLEAN_SHORT,
           "clean_details": [], "shell": _fold(pending)}
    return {"status": "scouted" if pending else "ready", "named": not pending, "skin": True, "pictures": [],
            "proposal": {"part": "panel", "input_kind": "body-surface", "size_mm": [300.0, 100.0, 100.0],
                         "unit": "mm", "unit_basis": "file", "notes": [], "openings": [], "faces": [], "holes": []},
            "fea": fea}


def _seen(live, sid: str) -> dict:
    return live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      return {{blocked: !!root.querySelector('.fea-blocked'), off: root.querySelector('.gc-proceed').disabled,
               hint: root.querySelector('.gc-hint').textContent,
               sum: (root.querySelector('.fea-sec-shell .fea-sec-sum') || {{}}).textContent || '',
               panel: root.querySelector('.fea-panel').textContent}}; }})()""")


def test_a_surface_still_being_read_waits_with_no_dead_end(live):
    sid = "feasurfwait"
    _open(live, sid, _check(True))
    s = _seen(live, sid)
    assert not s["blocked"] and s["off"]
    assert s["hint"] == surface_check.PENDING_WORDS
    assert s["sum"].strip() == "reading the surface…"
    assert "coming" not in s["panel"] and "proceed now" not in s["panel"] and "upload" not in s["panel"].lower()
    # the reading lands: the same panel follows in place
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(_check(False))})")
    live.wait_for(f"!document.querySelector('#gstage-{sid} .gc-proceed').disabled", timeout=20,
                  what="Proceed to open once the surface is read")
    s = _seen(live, sid)
    assert not s["blocked"] and s["hint"] == "Right part, right unit?"
    assert "It is meshed as a shell" in s["panel"]
    assert _proceed(live, sid) is not None
    assert_clean(live, "a surface stage that waited for its reading")
