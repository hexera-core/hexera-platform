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
