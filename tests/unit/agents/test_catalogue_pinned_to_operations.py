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
