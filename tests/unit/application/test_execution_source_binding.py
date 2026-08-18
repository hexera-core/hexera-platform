# Responsibility: Verify what was approved is bound by identity and checksum, never by location or display name.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests._geometry_support import interpretation_ref, source_ref

from meshpipeline.agents.intake.admission_token import (
    approved_intent_canonical,
    approved_intent_fingerprint,
)
from meshpipeline.application.pipeline_run import JobRequest
from meshpipeline.contracts.geometry_source import GeometrySourceError
from meshpipeline.errors import FailureClass

_INTENT = {"engine": "gmsh", "purpose": "internal_cfd", "input_kind": "fluid-domain",
           "dimensionality": "3D", "patches": [{"name": "wall", "type": "wall"}],
           "engine_params": {}, "request_txt": "mesh the duct",
           "requested_mesh_fidelity": "draft"}


def _fp(ref):
    return approved_intent_fingerprint(source_ref=ref, **_INTENT)


# what approval binds

def test_the_same_source_and_intent_fingerprint_identically(tmp_path):
    ref = source_ref(tmp_path=tmp_path)
    assert _fp(ref) == _fp(ref)


#: A FIXED source id, deliberately not `uuid.uuid4()`. Parametrize arguments are evaluated at
#: COLLECTION time, so a random value became part of the node id - `[source_id-<uuid>]` differed
#: between processes, which breaks `--lf`, exact `-k`/node-id selection and any sharded run that
#: collects in one process and executes in another. The test only needs an id that DIFFERS from
#: the reference's own (which `source_ref` generates randomly per call), so a constant is exactly
#: as strong a negative case and is reproducible.
OTHER_SOURCE_ID = "00000000-0000-4000-8000-0000000000ff"


@pytest.mark.parametrize("field,value", [
    ("source_id", OTHER_SOURCE_ID),
    ("sha256", "b" * 64),
    ("size_bytes", 999_999),
    ("suffix_hint", ".stl"),
])
def test_changing_which_bytes_were_approved_changes_the_fingerprint(tmp_path, field, value):
    ref = source_ref(tmp_path=tmp_path)
    import dataclasses
    assert _fp(dataclasses.replace(ref, **{field: value})) != _fp(ref)


@pytest.mark.parametrize("field,value", [
    ("object_key", "sources/relocated-somewhere-else"),
    ("original_filename", "renamed-by-the-user.step"),
])
def test_relocating_or_renaming_the_upload_does_not_change_what_was_approved(tmp_path,
                                                                            field, value):
    import dataclasses
    ref = source_ref(tmp_path=tmp_path)
    assert _fp(dataclasses.replace(ref, **{field: value})) == _fp(ref)


def test_the_bound_identity_carries_no_location_or_display_name(tmp_path):
    ref = source_ref(tmp_path=tmp_path)
    geom = approved_intent_canonical(source_ref=ref, **_INTENT)["geometry"]
    assert set(geom) == {"identity_schema_version", "source_id", "sha256", "size_bytes",
                         "suffix_hint", "revision_id"}
    blob = repr(geom)
    assert ref.object_key not in blob
    assert ref.original_filename not in blob


def test_a_run_with_no_geometry_still_fingerprints(tmp_path):
    geom = approved_intent_canonical(source_ref=None, **_INTENT)["geometry"]
    assert geom["sha256"] == "" and geom["source_id"] == ""


# what JobRequest accepts

def test_a_request_round_trips_the_complete_reference(tmp_path):
    ref = source_ref(tmp_path=tmp_path)
    req = JobRequest(job_id="j", owner_id="owner-1", geometry_source=ref.to_payload())
    assert req.geometry_source == ref


def test_a_request_accepts_the_reference_itself(tmp_path):
    ref = source_ref(tmp_path=tmp_path)
    assert JobRequest(job_id="j", geometry_source=ref).geometry_source is ref


@pytest.mark.parametrize("absent", [None, "", {}])
def test_a_run_may_legitimately_carry_no_geometry(absent):
    assert JobRequest(job_id="j", geometry_source=absent).geometry_source is None


@pytest.mark.parametrize("field", ["source_id", "owner_id", "object_key", "sha256", "size_bytes"])
def test_a_partial_source_snapshot_is_refused(tmp_path, field):
    payload = source_ref(tmp_path=tmp_path).to_payload()
    del payload[field]
    with pytest.raises(GeometrySourceError):
        JobRequest(job_id="j", geometry_source=payload)


@pytest.mark.parametrize("field,bad", [
    ("sha256", "not-a-digest"),
    ("sha256", "A" * 64),          # upper case is not the canonical form
    ("size_bytes", 0),
    ("size_bytes", -1),
    ("source_id", "  "),
])
def test_a_malformed_source_snapshot_is_refused(tmp_path, field, bad):
    payload = source_ref(tmp_path=tmp_path).to_payload()
    payload[field] = bad
    with pytest.raises(GeometrySourceError):
        JobRequest(job_id="j", geometry_source=payload)


# who is blamed

def _materialize(ref, *, tmp_path, row_ref=None, store=None):
    from meshpipeline.application import geometry_materializer as gm
    with patch.object(gm, "get_object_store", return_value=store or MagicMock()):
        return gm.materialize(ref, interpretation_ref(geometry_source_id=ref.source_id), workspace=tmp_path / "ws", row_ref=row_ref)


def test_a_snapshot_that_disagrees_with_the_row_is_an_integrity_failure(tmp_path):
    import dataclasses
    ref = source_ref(tmp_path=tmp_path)
    row = dataclasses.replace(ref, sha256="c" * 64)
    with pytest.raises(GeometrySourceError) as exc:
        _materialize(ref, tmp_path=tmp_path, row_ref=row)
    assert exc.value.failure_class is FailureClass.DATA_INTEGRITY


def test_a_vanished_object_is_an_integrity_failure_not_an_outage(tmp_path):
    store = MagicMock()
    store.exists = MagicMock(return_value=False)
    with pytest.raises(GeometrySourceError) as exc:
        _materialize(source_ref(tmp_path=tmp_path), tmp_path=tmp_path, store=store)
    assert exc.value.failure_class is FailureClass.DATA_INTEGRITY


def test_an_unreachable_store_is_infrastructure_and_retryable(tmp_path):
    from meshpipeline.contracts.object_storage import StorageError
    store = MagicMock()
    store.exists = MagicMock(return_value=True)
    store.download_file = MagicMock(side_effect=StorageError("endpoint refused"))
    with pytest.raises(GeometrySourceError) as exc:
        _materialize(source_ref(tmp_path=tmp_path), tmp_path=tmp_path, store=store)
    assert exc.value.failure_class is FailureClass.DEPENDENCY_DOWN
    assert exc.value.failure_class.is_retryable


def test_altered_bytes_are_an_integrity_failure(tmp_path):
    ref = source_ref(tmp_path=tmp_path)
    store = MagicMock()
    store.exists = MagicMock(return_value=True)

    def _write(object_key, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"substituted content, same name")

    store.download_file = MagicMock(side_effect=_write)
    with pytest.raises(GeometrySourceError) as exc:
        _materialize(ref, tmp_path=tmp_path, store=store)
    assert exc.value.failure_class is FailureClass.DATA_INTEGRITY


def test_no_failure_message_names_infrastructure(tmp_path):
    from meshpipeline.contracts.object_storage import StorageError
    ref = source_ref(tmp_path=tmp_path)
    store = MagicMock()
    store.exists = MagicMock(return_value=True)
    store.download_file = MagicMock(side_effect=StorageError(
        "s3://prod-geometry-bucket/sources/abc: AccessDenied for arn:aws:iam::9:user/svc"))
    with pytest.raises(GeometrySourceError) as exc:
        _materialize(ref, tmp_path=tmp_path, store=store)
    text = str(exc.value)
    for leak in ("s3://", "bucket", "AccessDenied", "arn:", ref.object_key, ref.sha256):
        assert leak not in text
