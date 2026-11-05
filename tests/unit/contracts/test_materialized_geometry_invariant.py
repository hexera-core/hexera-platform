# Responsibility: Verify a geometry handle cannot exist without its file, and a stale path is never trusted.
from __future__ import annotations

from pathlib import Path

import pytest
from tests._geometry_support import interpretation_ref, source_ref, stale_geometry_state
from tests._geometry_support import materialized as make_materialized

from meshpipeline.contracts.geometry_source import (
    GeometrySourceError,
    MaterializedGeometry,
)
from meshpipeline.pipeline.geometry_state import geometry_path, geometry_ref, materialized

_SRC = source_ref()


@pytest.mark.parametrize("empty", ["", "   ", "\t\n", None])
def test_a_handle_without_a_file_cannot_be_constructed(empty):
    with pytest.raises(GeometrySourceError, match="real local file"):
        MaterializedGeometry(ref=_SRC, interpretation=interpretation_ref(geometry_source_id=_SRC.source_id), local_path=empty)


def test_state_carrying_no_local_file_yields_no_handle_but_keeps_identity():
    ref = source_ref()
    state = {"geometry": {"ref": ref.to_payload(), "local_path": ""}}
    assert materialized(state) is None
    assert geometry_path(state) == ""      # consumers already treat "" as "nothing to read"
    assert geometry_ref(state) == ref      # ...but provenance survives the absence of a file


def test_a_real_materialization_round_trips(tmp_path):
    mg = make_materialized(tmp_path)
    assert Path(mg.path).exists()
    restored = MaterializedGeometry.from_state(mg.to_state())
    assert restored is not None
    assert restored.ref == mg.ref
    assert restored.path == mg.path


def test_a_stale_path_still_deserializes_and_is_still_not_trusted(tmp_path):
    state = {"geometry": stale_geometry_state(tmp_path)}
    mg = materialized(state)
    assert mg is not None                    # structurally a handle
    assert not Path(mg.path).exists()        # but it names nothing in this process
    assert geometry_ref(state) is not None   # the durable identity is what survived the hop


# one identity, one attachment

def test_state_stores_exactly_one_reference(tmp_path):
    state = {"geometry": make_materialized(tmp_path).to_state()}
    assert geometry_ref(state) == materialized(state).ref
    # two durable facts and one ephemeral handle: which bytes, what size, where they are
    assert list(state["geometry"]) == ["ref", "interpretation", "local_path"]


def test_identity_only_state_is_a_legal_shape(tmp_path):
    ref = source_ref(tmp_path=tmp_path)
    state = {"geometry": {"ref": ref.to_payload()}}
    assert geometry_ref(state) == ref
    assert materialized(state) is None
    assert geometry_path(state) == ""


def test_rematerialisation_replaces_the_local_file_and_keeps_identity(tmp_path):
    first = make_materialized(tmp_path / "process-a")
    stale = {"geometry": first.to_state()}

    second_dir = tmp_path / "process-b"
    fresh = MaterializedGeometry(ref=first.ref, interpretation=interpretation_ref(geometry_source_id=first.ref.source_id),
                                 local_path=make_materialized(second_dir, source_id=first.ref.source_id).local_path)
    resumed = {"geometry": fresh.to_state()}

    assert geometry_ref(resumed) == geometry_ref(stale)          # same approved bytes
    assert geometry_path(resumed) != geometry_path(stale)        # different process, new file
    assert Path(geometry_path(resumed)).exists()
