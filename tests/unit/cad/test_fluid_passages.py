"""The passages a staged internal surface closes, and which declared openings sit on each
(cad/fluid_passages): a mesher fills one connected region, so a port on any other one came back
with no faces - one carotid of two, one tube of a bundle, every port of a part whose port-less
solid was the larger."""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.cad.fluid_passages import (
    PASSAGES_READ_FILE,
    passage_surfaces,
    plan_passages,
    plan_staged,
    read_passages,
    write_passage,
)
from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary


def _box(lo, hi):
    """A closed box as (wall, lid): the lid is its +z face."""
    tris = _box_triangles(lo, hi)
    return tris[:-2], tris[-2:]


def _write(tmp_path: Path, groups: dict[str, list]) -> dict[str, str]:
    out = {}
    for name, tris in groups.items():
        p = tmp_path / f"{name}.stl"
        write_stl_binary(p, tris)
        out[name] = str(p)
    return out


def test_two_tubes_that_do_not_touch_are_two_passages_each_with_its_own_ports(tmp_path):
    wall_a, lid_a = _box((0, 0, 0), (1, 1, 4))
    wall_b, lid_b = _box((3, 0, 0), (4, 1, 4))
    srcs = _write(tmp_path, {"wall": wall_a + wall_b, "inlet": lid_a, "outlet_1": lid_b})
    plan = plan_passages(read_passages(srcs), inlet_names=("inlet",))
    assert [p.ports for p in plan.meshed] == [["inlet"], ["outlet_1"]]      # the inlet's passage first
    assert plan.several and not plan.stranded and not plan.left_out and plan.refusal() == ""
    notes = " ".join(plan.notes())
    assert "2 separate passages" in notes and "passage 2 has no opening named an inlet" in notes
    groups = passage_surfaces(plan)
    assert sorted(groups[0]) == ["inlet", "wall"] and sorted(groups[1]) == ["outlet_1", "wall"]
    assert len(groups[0]["wall"]) == len(wall_a) and len(groups[1]["outlet_1"]) == 2


def test_a_body_standing_inside_the_passage_is_part_of_its_boundary(tmp_path):
    wall, lid = _box((0, 0, 0), (2, 2, 4))
    rod = _box_triangles((0.8, 0.8, 1), (1.2, 1.2, 3))
    r = read_passages(_write(tmp_path, {"wall": wall + rod, "inlet": lid}))
    assert len(r.passages) == 1 and len(r.passages[0].pieces) == 2
    assert r.passages[0].volume == pytest.approx(16.0 - 0.32, rel=1e-6)


def test_a_lid_joined_to_no_wall_is_said_before_any_mesher_runs(tmp_path):
    wall, lid = _box((0, 0, 0), (1, 1, 4))
    free = [((5, 5, 0), (6, 5, 0), (6, 6, 0)), ((5, 5, 0), (6, 6, 0), (5, 6, 0))]   # a disc on its own
    plan = plan_passages(read_passages(_write(tmp_path, {"wall": wall, "inlet": lid, "outlet_2": free})))
    assert list(plan.stranded) == ["outlet_2"] and "joined to no wall" in plan.stranded["outlet_2"]
    assert "outlet_2" in plan.refusal() and "not an opening" in plan.refusal()


def test_a_port_into_a_closed_pocket_is_stranded_not_a_second_passage(tmp_path):
    wall, lid = _box((0, 0, 0), (10, 10, 10))
    pocket_wall, pocket_lid = _box((20, 0, 0), (20.5, 0.5, 0.5))           # 1/8000 of the passage
    plan = plan_passages(read_passages(_write(tmp_path, {
        "wall": wall + pocket_wall, "inlet": lid, "outlet_2": pocket_lid})))
    assert [p.ports for p in plan.meshed] == [["inlet"]]
    assert "pocket" in plan.stranded["outlet_2"]


def test_a_closed_piece_no_opening_touches_is_left_out_however_large(tmp_path):
    # cfMesh keeps the LARGEST closed region: a port-less solid bigger than the passage took
    # every port's faces (the hotend nozzle's STEP, every solid staged as wall)
    wall, lid = _box((0, 0, 0), (1, 1, 1))
    solid = _box_triangles((5, 5, 5), (9, 9, 9))
    plan = plan_passages(read_passages(_write(tmp_path, {"wall": wall + solid, "inlet": lid})))
    assert [p.ports for p in plan.meshed] == [["inlet"]] and len(plan.left_out) == 1
    assert not plan.several and "left out" in " ".join(plan.notes())
    assert len(passage_surfaces(plan)[0]["wall"]) == len(wall)


def test_one_closed_passage_changes_nothing_and_its_reading_is_recorded(tmp_path):
    wall, lid = _box((0, 0, 0), (1, 1, 1))
    srcs = _write(tmp_path, {"wall": wall, "inlet": lid})
    plan = plan_staged(tmp_path, srcs, "wall", [{"name": "inlet", "type": "inlet"}])
    assert plan is not None and not plan.several and not plan.left_out and not plan.stranded
    assert (tmp_path / PASSAGES_READ_FILE).exists()
    out = write_passage(passage_surfaces(plan)[0], tmp_path / "p0")
    assert sorted(out) == ["inlet", "wall"] and all(Path(v).exists() for v in out.values())
