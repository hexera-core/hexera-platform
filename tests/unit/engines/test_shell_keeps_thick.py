# Responsibility: Verify MIXED-12, A SHELL KEEPS ITS THICK REGIONS AS SOLID: when the user asks for a shell, a body of
# thin walls and thick regions comes out as one mesh - shell on the walls, solid elements in the thick regions, joined
# (SHLINK-4 ties) - under the shell's file names, and the words say which regions were kept solid; a join that cannot
# be made leaves the shell as before (thick regions left out, SHSTAGE-9) and says why.
# Boundaries: one real shell run (gmsh, OpenCASCADE) on the plate with a 30 mm boss (tests/shell_fixtures).
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.engines import mixed_run, shell_delivery, shell_run  # noqa: E402
from meshpipeline.engines.shell_mesh import edge_users  # noqa: E402


def _run(tmp_path):
    step = sf.write_step(sf.plate_with_boss(), tmp_path / "boss.step")
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws, shell_run.run(ws, step, mm_per_unit=1.0)


def test_a_shell_keeps_its_thick_region_as_solid_joined_in_the_same_mesh(tmp_path):
    ws, rec = _run(tmp_path)
    assert rec["made"] is True, rec.get("why")
    assert rec["left_out"] == {} and rec["kept_solid"]["regions"] == 1
    c = rec["kept_solid"]["counts"]
    assert c["solid_elements"] > 0 and c["ties"] > 0 and c["links"] > 0
    assert "1 thick region (bosses, lugs, lips or thick flanges) kept as solid elements in the same mesh" in rec["words"]
    assert "not in this shell mesh" not in rec["words"]
    bdf = (ws / "shell.bdf").read_text()
    assert "CTETRA" in bdf and "MPC" in bdf and "CQUAD4" in bdf
    for name in shell_run.FILES:
        assert (ws / name).stat().st_size > 0, name
    assert rec["elements"] == c["shell_elements"] + c["overlap_elements"] + c["solid_elements"]
    assert (rec["quality"] or {}).get("valid") is not False
    # the delivery takes it as it takes a shell: one mesh is the result
    assert shell_delivery.deliver(ws, rec)


def test_the_junction_has_no_free_shell_edge_left_untied(tmp_path):
    """Every free shell edge on the boss's foot is carried into the solid and tied: the shell's free edges are the
    plate's own outline, and the overlap's inner row, whose every node is tied."""
    from meshpipeline.cad import midsurface as ms

    step = sf.write_step(sf.plate_with_boss(), tmp_path / "boss.step")
    out = tmp_path / "out"
    record = ms.extract_file(step, out, mm_per_unit=1.0)
    sub: dict = {}
    # built in a forked child, as a shell run builds it: a build run in the test process left a later forked join
    # (MIXED-12's guard) hanging to its 300 s stop (seen on b764aae5 too: this test, then a kept-solid test)
    joined = shell_run._guarded(lambda: mixed_run.build(step, out, mm_per_unit=1.0, rec=sub, record=record), 600.0,
                                tmp_path)
    tied = {t.node - 1 for t in joined.ties}
    xyz = joined.shell.xyz
    loose = []
    for (a, b), users in edge_users(joined.shell).items():
        if len(users) != 1 or (a in tied and b in tied):
            continue
        mid = (xyz[a] + xyz[b]) / 2.0
        on_rim = (abs(mid[0]) < 1e-6 or abs(mid[0] - 200.0) < 1e-6 or abs(mid[1]) < 1e-6 or abs(mid[1] - 100.0) < 1e-6)
        if not on_rim:
            loose.append((a, b))
    assert loose == []


def test_a_join_past_its_time_leaves_the_shell_as_before_and_says_so(tmp_path, monkeypatch):
    # MIXED-12's guard: a round-tube truss whose joints read thick lost its shell to the scorer's stop
    import time

    def slow(*a, **k):
        time.sleep(30)

    monkeypatch.setattr(mixed_run, "build", slow)
    monkeypatch.setattr(shell_run, "KEPT_BUDGET_S", 2.0)
    started = time.time()
    ws, rec = _run(tmp_path)
    assert time.time() - started < 28
    assert rec["made"] is True and "kept_solid" not in rec and rec["left_out"]["thick"] == 1
    assert "(it took longer than the 2 s allowed), so they are left out." in rec["words"]


def test_a_join_that_cannot_be_made_leaves_the_shell_as_before_and_says_why(tmp_path, monkeypatch):
    def no(*a, **k):
        raise mixed_run._Stop("the thin walls and the thick regions could not be cut apart")

    monkeypatch.setattr(mixed_run, "build", no)
    ws, rec = _run(tmp_path)
    assert rec["made"] is True and "kept_solid" not in rec
    assert rec["left_out"]["thick"] == 1
    assert "could not be kept as solid and joined to the shell (the thin walls and the thick regions could not be " \
           "cut apart), so they are left out." in rec["words"]
    assert "CTETRA" not in (ws / "shell.bdf").read_text()


def test_the_shell_part_is_measured_as_delivered_with_its_overlap_quads(tmp_path):
    # MIXED-13: validity and Nastran's limits are read on the shell elements the deck holds, the join's too
    ws, rec = _run(tmp_path)
    c = rec["kept_solid"]["counts"]
    q = rec["quality"]
    assert q["elements"] == c["shell_elements"] + c["overlap_elements"]
    assert q["valid"] is True and "nastran_past" in q and "within_nastran" in q


def test_the_users_typed_size_and_count_reach_the_shell_part_as_in_a_shell_run(tmp_path):
    # MIXED-13: the mixed shell part goes through the shell-only path (sa.mesh_run): a count typed beside a global
    # size is set aside and said, the size applied and told asked against delivered
    from tests.unit.engines.test_shell_requirements import criteria

    step = sf.write_step(sf.plate_with_boss(), tmp_path / "boss.step")
    ws = tmp_path / "ws"
    ws.mkdir()
    rec = shell_run.run(ws, step, mm_per_unit=1.0, target={"elements": 20000, "mode": "about"},
                        requirements=criteria(1.0))
    assert rec["made"] is True and rec.get("kept_solid"), rec.get("why") or rec.get("kept_solid_why")
    assert rec["count_set_aside"] == 20000 and "target" not in rec
    assert rec["requirements"]["rows"]
    assert "Your element count (20,000) was not used: the global size you gave decides the sizes." in rec["words"]


def test_the_viewer_file_and_the_result_hold_the_solid_regions_too(tmp_path):
    # MIXED-14: shell.msh drew the shell part only; the result counted tetrahedra as "plate elements"
    import json

    import numpy as np

    ws, rec = _run(tmp_path)
    c = rec["kept_solid"]["counts"]
    text = (ws / shell_run.MSH).read_text()
    assert f'"{shell_run.SOLID_GROUP}"' in text
    solid = [f for f in rec["faces"] if f.get("solid")]
    assert len(solid) == 1 and solid[0]["elements"] == c["solid_elements"]
    nodes = text.split("$Nodes\n", 1)[1].split("$EndNodes", 1)[0].splitlines()[1:]
    z = np.array([float(ln.split()[3]) for ln in nodes])
    elems = text.split("$Elements\n", 1)[1].split("$EndElements", 1)[0].splitlines()
    skin = [ln for ln in elems[1:] if ln.split()[3] == str(solid[0]["tag"])]
    assert int(elems[0]) == c["shell_elements"] + c["overlap_elements"] + len(skin) and len(skin) > 0
    assert z.max() > 0.02          # the 30 mm boss stands above the plate's mid-surface (metres)
    assert any(solid[0]["tag"] in p["tags"] for p in rec["parts"])
    assert rec["words"].startswith("Shell and solid mesh:") and "10-node tetrahedra in the thick regions" in rec["words"]
    assert shell_delivery.deliver(ws, rec)
    q = json.loads((ws / "quality.json").read_text())
    assert q["element_counts"] == {"shell": c["shell_elements"] + c["overlap_elements"], "solid": c["solid_elements"]}
    assert q["cells"] == sum(q["element_counts"].values())
    res = shell_delivery.results(ws, q, {})
    assert f"{c['solid_elements']:,} 10-node tetrahedra" in res["summary"]
    assert res["shell"]["solid_elements"] == c["solid_elements"]
