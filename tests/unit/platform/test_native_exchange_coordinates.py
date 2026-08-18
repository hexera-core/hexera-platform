# Responsibility: Verify one native submission resolves to one set of exchange coordinates.
# Boundaries: derivation and validation; activating them in dispatch is the integration checkpoint's.
from __future__ import annotations

import inspect
import uuid

import pytest

from meshpipeline.adapters.mesh_execution import exchange_coordinates as ec
from meshpipeline.persistence.repositories import native_submission_repository as claims

JOB = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _key(job=JOB, generation=1) -> str:
    return claims.operation_key(job, generation)


def test_one_operation_key_always_yields_byte_identical_coordinates():
    first, second = ec.coordinates_for(_key()), ec.coordinates_for(_key())
    assert first == second and first.as_dict() == second.as_dict()


def test_a_rotated_token_in_the_same_generation_resolves_to_the_same_coordinates():
    # THE POINT of the derivation: a replacement holds a different token and must still find the
    # objects its predecessor used. The key has nowhere to put a token, so it cannot drift.
    assert ec.coordinates_for(claims.operation_key(JOB, 4)) == \
        ec.coordinates_for(claims.operation_key(JOB, 4))


def test_a_new_generation_resolves_to_different_coordinates():
    one, two = ec.coordinates_for(_key(generation=1)), ec.coordinates_for(_key(generation=2))
    assert one.exchange_id != two.exchange_id
    assert {one.input_object_key, one.output_object_key, one.result_object_key}.isdisjoint(
        {two.input_object_key, two.output_object_key, two.result_object_key})


def test_the_three_keys_are_distinct_and_inside_the_operations_namespace():
    coords = ec.coordinates_for(_key())
    keys = [coords.input_object_key, coords.output_object_key, coords.result_object_key]
    assert len(set(keys)) == 3
    for key in keys:
        assert key.startswith(f"jobs/{coords.exchange_id}/"), key
        assert ".." not in key and "//" not in key


def test_the_exchange_identifier_is_the_whole_operation_key():
    key = _key()
    coords = ec.coordinates_for(key)
    assert coords.exchange_id == key == coords.operation_key
    assert len(coords.exchange_id) == 64, "the identity was truncated"


@pytest.mark.parametrize("bad", [
    "", "  ", "z" * 64, "A" * 64, "a" * 63, "a" * 65, "a" * 32 + "/" + "b" * 31,
    "../" + "a" * 61, " " + "a" * 64, "a" * 64 + "\n", None, 12345,
])
def test_a_malformed_key_is_refused_before_any_storage_is_touched(bad):
    with pytest.raises(ec.InvalidOperationKey):
        ec.coordinates_for(bad)


def test_no_random_or_uuid_value_participates_in_the_derivation():
    # Checked as BEHAVIOUR and imports, not by grepping prose: the module's own commentary
    # legitimately explains why the historical identifier was random.
    import sys

    module = sys.modules[ec.__name__]
    assert not hasattr(module, "uuid") and not hasattr(module, "random")
    assert len({ec.coordinates_for(_key()).exchange_id for _ in range(25)}) == 1


def test_the_coordinates_carry_nothing_machine_specific():
    serialised = ec.coordinates_for(_key()).as_dict()
    flat = " ".join(serialised.values())
    assert "/home" not in flat and "/tmp" not in flat and "Bearer" not in flat
    assert set(serialised) == {"operation_key", "exchange_id", "input_object_key",
                               "output_object_key", "result_object_key"}
    assert ec.ExchangeCoordinates(**serialised) == ec.coordinates_for(_key())


def test_there_is_no_caller_supplied_exchange_identifier():
    assert list(inspect.signature(ec.coordinates_for).parameters) == ["operation_key"]


def test_the_shared_object_key_layout_is_reused_not_reinvented():
    from meshpipeline import artifact_keys

    coords = ec.coordinates_for(_key())
    assert coords.input_object_key == artifact_keys.input_key(coords.exchange_id)
    assert coords.output_object_key == artifact_keys.output_key(coords.exchange_id)
    assert coords.result_object_key == artifact_keys.result_key(coords.exchange_id)
    # the result key the remote side derives from the output key agrees with ours
    assert artifact_keys.result_key_beside(coords.output_object_key) == coords.result_object_key


def test_the_installed_mesh_consumer_accepts_the_identifier_unchanged():
    # `runtime/mesh_invocation` reads whole URIs - it never parses the exchange id or assumes a
    # length - so the full digest survives the environment override untouched.
    from meshpipeline.runtime.mesh_invocation import from_environment

    coords = ec.coordinates_for(_key())
    invocation = from_environment({
        "INPUT_URI": f"gs://bucket/{coords.input_object_key}",
        "OUTPUT_URI": f"gs://bucket/{coords.output_object_key}",
        "ENGINE": "cfmesh"})
    assert invocation.input_uri.endswith(coords.input_object_key)
    assert invocation.output_uri.endswith(coords.output_object_key)
    assert coords.exchange_id in invocation.input_uri


def test_production_dispatch_derives_its_exchange_from_the_operation_key():
    # The namespace is now the operation's, so a replacement re-derives it exactly.
    from meshpipeline.adapters.mesh_execution import cloud_run_client

    source = inspect.getsource(cloud_run_client._exchange)
    assert "coordinates_for(operation_key)" in source
    assert "uuid" not in source, "a random exchange identifier survived in the submission path"
    assert list(inspect.signature(cloud_run_client.run_mesh_remote).parameters) == \
        ["workspace", "engine", "timeout", "operation_key"]


# replacement visibility, through a local object store


class _LocalStore:
    # The ObjectStore port, backed by a dict. It exists to answer one question: can a
    # REPLACEMENT worker, holding only the operation key, reach what its predecessor wrote?
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_file(self, *, local_path, object_key, content_type=""):
        self.objects[object_key] = local_path.read_bytes()
        return object_key

    def download_file(self, *, object_key, destination):
        if object_key not in self.objects:
            raise KeyError(object_key)
        destination.write_bytes(self.objects[object_key])

    def exists(self, object_key: str) -> bool:
        return object_key in self.objects


def test_a_replacement_resolves_the_same_coordinates_and_sees_its_predecessors_objects(tmp_path):
    store = _LocalStore()
    key = _key(generation=7)

    # process A, holding its own token, uploads the input under the derived coordinates
    a = ec.coordinates_for(key)
    payload = tmp_path / "input.tar.gz"
    payload.write_bytes(b"mesh input")
    store.upload_file(local_path=payload, object_key=a.input_object_key)

    # process B derives the SAME coordinates from the SAME operation key - it has nothing else
    b = ec.coordinates_for(key)
    assert b == a
    assert store.exists(b.input_object_key), "the replacement cannot see the input"
    got = tmp_path / "seen.tar.gz"
    store.download_file(object_key=b.input_object_key, destination=got)
    assert got.read_bytes() == b"mesh input"

    # a result written by the remote side is discoverable the same way
    result = tmp_path / "result.json"
    result.write_bytes(b'{"rc": 0}')
    store.upload_file(local_path=result, object_key=a.result_object_key)
    assert store.exists(b.result_object_key)


def test_a_missing_result_is_not_evidence_that_nothing_was_submitted(tmp_path):
    # THE LINE this contract must not cross. Absence is "not yet observed" - a provider execution
    # may be running, or its write may be in flight. Only a positive reference settles it.
    store = _LocalStore()
    coords = ec.coordinates_for(_key(generation=9))
    assert store.exists(coords.result_object_key) is False
    # the coordinates say nothing about submission; the claim authority owns that question
    assert not hasattr(coords, "submitted") and not hasattr(coords, "accepted")


def test_unrelated_operations_cannot_reach_each_others_objects(tmp_path):
    store = _LocalStore()
    mine = ec.coordinates_for(_key(generation=1))
    theirs = ec.coordinates_for(_key(job=uuid.UUID("22222222-2222-4222-8222-222222222222")))
    payload = tmp_path / "x"
    payload.write_bytes(b"mine")
    store.upload_file(local_path=payload, object_key=mine.input_object_key)
    assert store.exists(theirs.input_object_key) is False
    assert not theirs.input_object_key.startswith(f"jobs/{mine.exchange_id}/")
