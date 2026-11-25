# Responsibility: Verify CFDREQ-12 - a place in the notes named by the role the user confirmed at intake ("the outlet") is the opening with that role, whatever the CAD called it; several with one role and no number are all of them, said; a role beats a name that is another opening's, said; with no roles confirmed, names only; not found lists every opening with its role.
"""m484x D1, round 13: straight_reducer_002's openings are p_1 and p_2 in the file; "refine around the outlet to 2 mm"
was told "not applied - no opening named 'the outlet' in this model (its openings: p_1, p_2)", though the run knew
p_2 was the outlet."""
from __future__ import annotations

import json

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR

BASE_M = 0.032       # 32 mm background cells: 2 mm is level 4


def _ask(where: str, mm: float = 2.0) -> CR.Ask:
    quote = f"refine around {where} to {mm:g} mm"
    answer = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "physics": [], "element_kind": [],
              "limits": [], "other": [], "local": [{"where": where, "what": "size", "value": mm, "unit": "mm",
                                                     "quote": quote}]}
    req, dropped, _na = MR.check(json.dumps(answer), quote, route="flow")
    assert not dropped, dropped
    return CR.Ask(req, "snappy")


def _levels(ask, names, roles):
    return CR.snappy_port_levels(ask, {k: 1 for k in names if k != "wall"}, names, "wall", base_m=BASE_M,
                                 roles=roles)


NAMES = {"wall": "wall", "a": "p_1", "b": "p_2"}


def _row(ask) -> dict:
    return next(iter(ask.rows.values()))


def test_the_outlet_is_the_opening_confirmed_as_the_outlet_whatever_the_cad_called_it():
    ask = _ask("the outlet")
    out = _levels(ask, NAMES, {"p_1": "inlet", "p_2": "outlet"})
    assert out["b"] == 4 and out["a"] == 1
    row = _row(ask)
    assert row["status"] == "applied" and row["built"] == "cells of 2 mm on p_2 (the outlet you confirmed)"


def test_two_outlets_with_no_number_are_both_and_said_with_a_number_one():
    names = {"wall": "wall", "a": "p_1", "b": "p_2", "c": "p_3"}
    roles = {"p_1": "inlet", "p_2": "outlet", "p_3": "outlet"}
    ask = _ask("the outlet")
    out = _levels(ask, names, roles)
    assert out["b"] == out["c"] == 4 and out["a"] == 1
    assert _row(ask)["built"] == "cells of 2 mm on p_2 and p_3 (all 2 openings you confirmed as outlets)"
    ask = _ask("outlet 2")
    out = _levels(ask, names, roles)
    assert out["c"] == 4 and out["b"] == 1
    assert _row(ask)["built"] == "cells of 2 mm on p_3 (the outlet you confirmed)"


def test_the_role_the_user_confirmed_beats_a_name_that_is_another_openings():
    names = {"wall": "wall", "a": "outlet", "b": "p_2"}
    ask = _ask("the outlet")
    out = _levels(ask, names, {"outlet": "inlet", "p_2": "outlet"})
    assert out["b"] == 4 and out["a"] == 1
    assert _row(ask)["built"] == ("cells of 2 mm on p_2 (the outlet you confirmed; the opening named 'outlet' is your "
                                  "inlet, so it was not used)")


def test_with_no_roles_confirmed_names_only_as_before():
    ask = _ask("the outlet")
    _levels(ask, NAMES, {})
    row = _row(ask)
    assert row["status"] == "not_applied" and "its openings: p_1, p_2" in row["built"]
    ask = _ask("p_2")
    assert _levels(ask, NAMES, {})["b"] == 4


def test_not_found_lists_every_opening_with_its_role():
    ask = _ask("the side port")
    _levels(ask, NAMES, {"p_1": "inlet", "p_2": "outlet"})
    row = _row(ask)
    assert row["status"] == "not_applied"
    assert "no opening named 'the side port' in this model (its openings: p_1 (inlet), p_2 (outlet))" in row["built"]


def test_the_roles_are_read_from_the_port_declaration(tmp_path):
    (tmp_path / "port_declaration.json").write_text(json.dumps([
        {"name": "p_1", "type": "inlet"}, {"name": "p_2", "type": "outlet"}, {"name": "wall", "type": "wall"}]))
    assert CR.port_roles(tmp_path) == {"p_1": "inlet", "p_2": "outlet"}
    assert CR.port_roles(tmp_path / "none") == {}


def test_cfmesh_reads_the_outlet_by_its_role_too():
    ask = _ask("the outlet")
    patches = [{"name": "wall", "type": "wall"}, {"name": "p_1", "type": "patch"}, {"name": "p_2", "type": "patch"}]
    entries, _boxes, _wall = CR.cfmesh_local(ask, patches, "wall", max_cell=0.032, wall_cell=0.004, floor=0.0005,
                                             roles={"p_1": "inlet", "p_2": "outlet"})
    assert "p_2 {" in entries and "p_1 {" not in entries and "wall {" not in entries
    assert _row(ask)["built"].endswith("on p_2 (the outlet you confirmed)")
