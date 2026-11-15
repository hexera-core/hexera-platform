from meshpipeline.agents.repair_review import _wire
from meshpipeline.cad.repair.operations import FILL_OPS, OPERATIONS


def test_the_model_catalogue_is_exactly_the_operation_registry():
    assert set(_wire.CATALOGUE) == {op.name for op in OPERATIONS}
    assert len(_wire.CATALOGUE) == len(OPERATIONS)


def test_every_catalogued_operation_has_a_one_line_description():
    lines = dict(line[2:].split(": ", 1) for line in _wire.CATALOGUE_TEXT.splitlines())
    assert set(lines) == set(_wire.CATALOGUE)


def test_the_model_side_fill_list_is_the_guarded_fill_set():
    assert set(_wire.FILL_CATALOGUE) == set(FILL_OPS)
    assert FILL_OPS <= {op.name for op in OPERATIONS}


# BOTH POSTURES OF CAD_REPAIR_SPLIT_CLOSED_FACES: the model side and the registry read one flag.

import pytest  # noqa: E402

from meshpipeline.cad.repair.operations import catalogue  # noqa: E402


def _described(text: str) -> set[str]:
    return set(dict(line[2:].split(": ", 1) for line in text.splitlines()))


@pytest.mark.parametrize("flag", [False, True], ids=["split_off", "split_on"])
def test_the_model_catalogue_follows_the_registry_in_both_flag_states(flag):
    names = _wire.catalogue(split_closed_faces=flag)
    registry = {op.name for op in catalogue(split_closed_faces=flag)}
    assert set(names) == registry and len(names) == len(registry)
    assert _described(_wire.catalogue_text(split_closed_faces=flag)) == set(names)
    assert ("split_closed_faces" in names) is flag


@pytest.mark.parametrize("flag", [False, True], ids=["split_off", "split_on"])
def test_the_flag_is_read_when_the_model_side_is_not_told(monkeypatch, flag):
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_SPLIT_CLOSED_FACES", flag)
    assert set(_wire.catalogue()) == {op.name for op in catalogue()}
    assert ("split_closed_faces" in _described(_wire.catalogue_text())) is flag


# EVERY POSTURE OF THE TWO MESH-OPERATION FLAGS: CAD_REPAIR_SPLIT_CLOSED_FACES and
# CAD_REPAIR_FRAGMENT_BODIES are read the same way on both sides.

_POSTURES = [(s, f) for s in (False, True) for f in (False, True)]


@pytest.mark.parametrize("split, fragment", _POSTURES,
                         ids=[f"split_{s}-fragment_{f}" for s, f in _POSTURES])
def test_the_model_catalogue_follows_the_registry_in_every_posture(split, fragment):
    names = _wire.catalogue(split_closed_faces=split, fragment_bodies=fragment)
    registry = {op.name for op in catalogue(split_closed_faces=split, fragment_bodies=fragment)}
    assert set(names) == registry and len(names) == len(registry)
    assert _described(_wire.catalogue_text(split_closed_faces=split,
                                           fragment_bodies=fragment)) == set(names)
    assert ("fragment_bodies" in names) is fragment
    assert ("split_closed_faces" in names) is split


@pytest.mark.parametrize("flag", [False, True], ids=["fragment_off", "fragment_on"])
def test_the_fragment_flag_is_read_when_the_model_side_is_not_told(monkeypatch, flag):
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", flag)
    assert set(_wire.catalogue()) == {op.name for op in catalogue()}
    assert ("fragment_bodies" in _described(_wire.catalogue_text())) is flag


# EVERY POSTURE OF THE THREE MESH-OPERATION FLAGS (2^3): CAD_REPAIR_REBUILD_FACE joins the two
# above, read the same way on both sides.

_POSTURES_3 = [(s, f, r) for s in (False, True) for f in (False, True) for r in (False, True)]


@pytest.mark.parametrize("split, fragment, rebuild", _POSTURES_3,
                         ids=[f"split_{s}-fragment_{f}-rebuild_{r}" for s, f, r in _POSTURES_3])
def test_the_model_catalogue_follows_the_registry_in_every_three_flag_posture(
        split, fragment, rebuild):
    flags = {"split_closed_faces": split, "fragment_bodies": fragment, "rebuild_face": rebuild}
    names = _wire.catalogue(**flags)
    registry = {op.name for op in catalogue(**flags)}
    assert set(names) == registry and len(names) == len(registry)
    assert _described(_wire.catalogue_text(**flags)) == set(names)
    assert ("rebuild_face" in names) is rebuild
    assert ("fragment_bodies" in names) is fragment
    assert ("split_closed_faces" in names) is split


@pytest.mark.parametrize("flag", [False, True], ids=["rebuild_off", "rebuild_on"])
def test_the_rebuild_flag_is_read_when_the_model_side_is_not_told(monkeypatch, flag):
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_REBUILD_FACE", flag)
    assert set(_wire.catalogue()) == {op.name for op in catalogue()}
    assert ("rebuild_face" in _described(_wire.catalogue_text())) is flag


def test_the_limits_text_is_flag_aware_and_unchanged_when_off(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg

    off = _wire.catalogue_limits(rebuild_face=False)
    assert off == (
        "the repair catalogue can only merge small edges, sweep away a few measured sliver faces, "
        "fix a face boundary, sew faces, fill a planar hole, cap a small curved hole with a "
        "constrained patch, re-trim only the faces the kernel says cross each other, or run a "
        "whole-shape fix; beyond those slivers it cannot move, redraw or delete geometry, change "
        "dimensions, or edit the part by hand")
    on = _wire.catalogue_limits(rebuild_face=True)
    assert "re-trim one face from its own boundary" in on and "overlapping facets" in on
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REBUILD_FACE", True)
    assert _wire.catalogue_limits() == on
