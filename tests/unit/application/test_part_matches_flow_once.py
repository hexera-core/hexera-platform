# Responsibility: Verify that one setting typed on a flow (CFD) mesh is listed once on the plan-step checklist: a cell
#       count typed at the plan step is a checklist row (NOTE-24) and also one of the CFD reader's lines; a count saved
#       in the team's meshing settings meets the typed one - each listed once, the typed value winning.
# Boundaries: application/part_matches' view over a stored reading; no model, no worker: the store is a dict.
from __future__ import annotations

import json

from meshpipeline.application import geometry_check as gc
from meshpipeline.application import part_matches as PM
from tests.unit.application.test_part_matches import SID, TABLE, world  # noqa: F401 - the fixture


def _flow_world(world, notes_sec: dict, text: str):
    """The world as a flow (CFD) model whose notes were read: the CFD reader's section stored with the reading."""
    from meshpipeline.contracts import user_notes as UN
    check = {"status": "ready", "session_id": SID, "facts": {PM.PARTS_KEY: {"route": "cfd", "table": TABLE}}}
    world.store.objects[gc.check_object_key(SID, "scout.json")] = json.dumps(check).encode()
    world.session.user_notes = UN.clean([UN.make(text, source=UN.SOURCE_CHAT, turn=1)])
    key = PM.key_of(PM.notes_of(world.session.user_notes, ""), TABLE)
    world.store.objects[gc.check_object_key(SID, PM.MATCHES)] = json.dumps(
        {"key": key, "status": "done", "section": {"status": "none"}, "notes": notes_sec}).encode()
    return PM.view(world.session)


def _cells_read(*rows) -> dict:
    from meshpipeline.engines import mesh_requirements as MR
    req = MR.empty()
    req["rows"] = [{"id": f"r{i}", "field": f, "asked": asked, "quote": asked, **({"from": src} if src else {})}
                   for i, (f, asked, src) in enumerate(rows, start=1)]
    return MR.section("used", requirements=req)


def test_a_cell_count_typed_on_a_flow_mesh_is_listed_once(world):
    # the slip: the count typed at the plan step was a checklist row (NOTE-24, read in code) AND the CFD reader's
    # line ("From your notes: about 2 million cells.") - the same setting listed twice; the typed row stays
    got = _flow_world(world, _cells_read(("cells:target", "about 2 million cells", ""),
                                         ("near_wall", "cells of 0.5 mm at the walls", "")),
                      "about 2M cells and 0.5 mm at the walls")
    assert [r["size"] for r in got["rows"]] == ["about 2M cells"]
    assert got["lines"] == ["From your notes: cells of 0.5 mm at the walls."]


def test_a_saved_team_count_and_a_typed_one_are_listed_once_with_the_typed_value(world):
    # Workspace > Meshing says about 5 million cells; the chat says about 2 million: one count, the typed one
    from meshpipeline.engines import meshing_standard as MS
    merged = MS.merge(_cells_read(("cells:target", "about 5 million cells", "")),
                      _cells_read(("cells:target", "about 2 million cells", "")), route="flow")
    got = _flow_world(world, merged, "about 2 million cells")
    said = [*(r["size"] for r in got["rows"]), *got.get("lines", [])]
    assert [s for s in said if "million cells" in s] == ["about 2 million cells"]


def test_a_count_of_another_bound_is_another_setting_and_stays():
    rows = [{"count": {"what": "target", "typed": "2 million"}, "size": "about 2 million cells"}]
    lines = ["From your settings: at most 8 million cells.", "From your notes: about 2 million cells.",
             "From your notes: 5 boundary layers."]
    assert PM.listed_once(lines, rows) == ["From your settings: at most 8 million cells.",
                                           "From your notes: 5 boundary layers."]
    assert PM.listed_once(lines, []) == lines
