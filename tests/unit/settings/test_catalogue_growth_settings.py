import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.settings import inventory


def test_the_new_geometry_flags_exist_and_default_off():
    assert rcfg.CAD_REPAIR_FILL_NONPLANAR_HOLES is False
    assert rcfg.CAD_REPAIR_HEAL_SELF_INTERSECTIONS is False
    group = next(g for g in inventory.INVENTORY if g.title == "CAD repair")
    declared = {v.name: v for v in group.vars}
    for name in ("CAD_REPAIR_FILL_NONPLANAR_HOLES", "CAD_REPAIR_HEAL_SELF_INTERSECTIONS"):
        assert declared[name].default == "false"
        assert declared[name].kind == "bool" and declared[name].exposure == "internal"
    assert "curvature" in declared["CAD_REPAIR_FILL_NONPLANAR_HOLES"].help
    assert "only the faces" in declared["CAD_REPAIR_HEAL_SELF_INTERSECTIONS"].help

def test_the_heal_face_bound_is_not_a_setting():
    names = {v.name for g in inventory.INVENTORY for v in g.vars}
    assert "CAD_REPAIR_MAX_HEAL_FACES" not in names
