# Responsibility: Prove the exchange-coordinate and Cloud Run response contracts are load-bearing.
# Boundaries: one controlled mutation per control; the claim authority's own mutations live in tests/integration.
from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import uuid

import requests
from tests.mutation_control import prove, refuses, substitute

from meshpipeline.adapters.mesh_execution import cloud_run_client as crc
from meshpipeline.adapters.mesh_execution import exchange_coordinates as ec
from meshpipeline.artifact_keys import result_key_beside
from meshpipeline.contracts.mesh_execution import MeshExecutionError, SubmissionIndeterminate
from meshpipeline.persistence.repositories import native_submission_repository as claims

JOB = uuid.UUID("11111111-1111-4111-8111-111111111111")
KEY = claims.operation_key(JOB, 7)
#: Two well-formed identities that agree for twelve characters - the length the historical random
#: exchange identifier used. Nothing derives them; they are stated so the collision is exact.
TWIN_A = "abcdef012345" + "a" * 52
TWIN_B = "abcdef012345" + "b" * 52

VALID = "projects/mesh-prod/locations/europe-west4/operations/abc-123_x~y.z"
#: A token-shaped secret, never a real one. It exists to be looked for in logs and errors.
SECRET = "ya29." + "S3cr3t" * 6

#: Captured before any substitution, so a mutation that wraps the real implementation calls the
#: real one and not itself.
_REAL_COORDINATES = ec.coordinates_for
_REAL_OPERATION_REFERENCE = crc._operation_reference


# exchange coordinates


def _control_a_replacement_finds_its_predecessors_objects() -> None:
    # Process A uploads under its coordinates; process B holds only the operation key.
    store: dict[str, bytes] = {}
    a = ec.coordinates_for(KEY)
    store[a.input_object_key] = b"the prepared bundle"
    store[a.result_object_key] = b'{"rc": 0}'
    b = ec.coordinates_for(KEY)
    assert b.input_object_key in store, (
        "the replacement could not find its predecessor's input object")
    assert b.result_object_key in store, (
        "the replacement could not find its predecessor's result object")


def test_mutation_uuid_derived_exchange_coordinates():
    def uuid_derived(operation_key: str):
        # The historical derivation: an identifier with no identity in it at all.
        return _REAL_COORDINATES(uuid.uuid4().hex.ljust(64, "0")[:64])

    prove("exchange/1 restore UUID-derived exchange coordinates",
          _control_a_replacement_finds_its_predecessors_objects,
          lambda: substitute(ec, "coordinates_for", uuid_derived),
          expecting="could not find its predecessor's input object")


def _control_the_identity_is_neither_truncated_nor_collidable() -> None:
    coords = ec.coordinates_for(KEY)
    assert len(coords.exchange_id) == 64, "the identity was truncated"
    assert coords.exchange_id == KEY, "the exchange identifier is not the operation key"
    one, two = ec.coordinates_for(TWIN_A), ec.coordinates_for(TWIN_B)
    assert one.exchange_id != two.exchange_id, (
        "two distinct operations sharing a prefix collided on one exchange identifier")
    assert {one.input_object_key, one.output_object_key, one.result_object_key}.isdisjoint(
        {two.input_object_key, two.output_object_key, two.result_object_key}), (
        "two distinct operations sharing a prefix collided on one namespace")


def test_mutation_truncated_operation_key():
    def truncated(operation_key: str):
        full = _REAL_COORDINATES(operation_key)
        short = operation_key[:12]
        return ec.ExchangeCoordinates(
            operation_key=full.operation_key, exchange_id=short,
            input_object_key=f"jobs/{short}/input.tar.gz",
            output_object_key=f"jobs/{short}/output.tar.gz",
            result_object_key=f"jobs/{short}/result.json")

    prove("exchange/2 truncate the operation key",
          _control_the_identity_is_neither_truncated_nor_collidable,
          lambda: substitute(ec, "coordinates_for", truncated),
          expecting="the identity was truncated")


#: The token a mutated derivation would mix in. Rotating it is what a takeover does.
_ROTATING_TOKEN = {"value": "worker-a"}


def _control_a_rotated_token_resolves_the_same_coordinates() -> None:
    _ROTATING_TOKEN["value"] = "worker-a"
    first = ec.coordinates_for(KEY)
    _ROTATING_TOKEN["value"] = "worker-b"          # the takeover rotates only the token
    second = ec.coordinates_for(KEY)
    assert first == second, (
        "a rotated token in the same generation resolved different coordinates, so the "
        "replacement would look in the wrong namespace")


def test_mutation_worker_token_in_the_exchange_identity():
    def token_mixed(operation_key: str):
        salted = hashlib.sha256(
            (operation_key + _ROTATING_TOKEN["value"]).encode()).hexdigest()
        return _REAL_COORDINATES(salted)

    prove("exchange/3 include the worker token in the exchange identity",
          _control_a_rotated_token_resolves_the_same_coordinates,
          lambda: substitute(ec, "coordinates_for", token_mixed),
          expecting="resolved different coordinates")


def _control_a_malformed_key_is_refused_before_storage() -> None:
    for bad in ("a" * 64 + "\n", " " + "a" * 64, "a" * 32 + "/" + "b" * 31, "../" + "a" * 61,
                "a" * 63, "A" * 64, ""):
        refuses(ec.InvalidOperationKey, lambda bad=bad: ec.coordinates_for(bad),
                f"a malformed operation key {bad!r} reached the storage layout")


def test_mutation_permissive_operation_key_validation():
    # Separators, whitespace and a trailing newline all admitted - and `^...$`, which accepts a
    # trailing newline even when the character class does not.
    prove("exchange/4 permit separators, whitespace and a trailing newline",
          _control_a_malformed_key_is_refused_before_storage,
          lambda: substitute(ec, "_OPERATION_KEY", re.compile(r"^[0-9a-zA-Z./ ]{0,80}$")),
          expecting="reached the storage layout")


def _control_producer_and_consumer_agree_on_the_result_location() -> None:
    coords = ec.coordinates_for(KEY)
    keys = [coords.input_object_key, coords.output_object_key, coords.result_object_key]
    assert len(set(keys)) == 3, "two of the three exchange keys are the same object"
    # The installed consumer derives the result location from the OUTPUT key, so the producer's
    # result key must be the one it will look at.
    assert result_key_beside(coords.output_object_key) == coords.result_object_key, (
        "the producer and the consumer disagree on where the result lives")


def test_mutation_misderived_result_key():
    prove("exchange/5 misderive the result key from the output key",
          _control_producer_and_consumer_agree_on_the_result_location,
          lambda: substitute(ec, "result_key", lambda key: f"jobs/{key}/output.tar.gz"),
          expecting="are the same object")


def _control_one_operation_cannot_select_a_second_namespace() -> None:
    derived = ec.coordinates_for(KEY)
    refuses(TypeError,
            lambda: ec.coordinates_for(KEY, exchange_id="a-namespace-of-my-choosing"),
            "a caller was able to name the exchange namespace for a claimed operation")
    assert ec.coordinates_for(KEY) == derived, "the namespace moved under one operation identity"


def test_mutation_caller_supplied_exchange_identifier():
    def overridable(operation_key: str, exchange_id: str = ""):
        chosen = exchange_id or operation_key
        full = _REAL_COORDINATES(operation_key)
        return ec.ExchangeCoordinates(
            operation_key=full.operation_key, exchange_id=chosen,
            input_object_key=f"jobs/{chosen}/input.tar.gz",
            output_object_key=f"jobs/{chosen}/output.tar.gz",
            result_object_key=f"jobs/{chosen}/result.json")

    prove("exchange/6 accept a caller-supplied exchange identifier",
          _control_one_operation_cannot_select_a_second_namespace,
          lambda: substitute(ec, "coordinates_for", overridable),
          expecting="able to name the exchange namespace")


# the Cloud Run response


class _Response:
    def __init__(self, status=200, body=None, unreadable=False):
        self.status_code = status
        self._body = body
        self._unreadable = unreadable

    def json(self):
        if self._unreadable:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code))


@contextlib.contextmanager
def _endpoint(outcome):
    # The adapter's real request building, validation and classification run; only the socket is
    # replaced. Restored on exit, so no control can leak a transport double into another.
    def post(url, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with substitute(crc, "_access_token", lambda: "test-token"), \
            substitute(crc.provcfg, "GCP_PROJECT_ID", "mesh-prod"), \
            substitute(crc.provcfg, "GCP_REGION", "europe-west4"), \
            substitute(crc.provcfg, "CLOUDRUN_JOB", "mesher"), \
            substitute(requests, "post", post):
        yield


def _trigger():
    return crc._trigger_job(input_uri="gs://b/in", output_uri="gs://b/out",
                            engine="cfmesh", timeout=420)


def _control_a_successful_response_yields_a_recordable_reference() -> None:
    with _endpoint(_Response(body={"name": VALID})):
        reference = _trigger()
    assert reference is not None, "the successful response produced no operation reference"
    assert getattr(reference, "operation_name", "") == VALID, (
        f"no durable provider operation reference survived the response: {reference!r}")


def test_mutation_discard_the_successful_response():
    prove("response/1 discard the successful response",
          _control_a_successful_response_yields_a_recordable_reference,
          lambda: substitute(crc, "_operation_reference", lambda response, *, engine: None),
          expecting="produced no operation reference")


def _control_a_success_without_a_name_is_not_acceptance() -> None:
    for body in ({}, {"name": ""}, {"name": "   "}, None):
        with _endpoint(_Response(body=body)):
            refuses(SubmissionIndeterminate, _trigger,
                    f"a 200 carrying {body!r} was treated as an acceptance")


def test_mutation_nameless_success_treated_as_accepted():
    def unguarded(response, *, engine: str):
        body = response.json() or {}
        return crc.CloudRunOperationReference(str(body.get("name", "") or ""))

    prove("response/2 treat a 2xx without an operation name as accepted",
          _control_a_success_without_a_name_is_not_acceptance,
          lambda: substitute(crc, "_operation_reference", unguarded),
          expecting="was treated as an acceptance")


def _control_a_lost_transport_outcome_is_indeterminate() -> None:
    for failure in (requests.exceptions.Timeout("timed out"),
                    requests.exceptions.ConnectionError("connection reset")):
        with _endpoint(failure):
            exc = refuses(MeshExecutionError, _trigger,
                          f"{type(failure).__name__} was not refused at all")
        assert isinstance(exc, SubmissionIndeterminate), (
            f"{type(failure).__name__} was classified as a definitive non-submission "
            f"({type(exc).__name__}), which permits an unsafe automatic retry")


def test_mutation_transport_loss_treated_as_definitive_non_submission():
    class _Definitive(MeshExecutionError):
        pass

    prove("response/3 treat timeout or connection loss as a definitive non-submission",
          _control_a_lost_transport_outcome_is_indeterminate,
          lambda: substitute(crc, "SubmissionIndeterminate", _Definitive),
          expecting="classified as a definitive non-submission")


def _control_only_a_long_running_operation_is_accepted() -> None:
    # The `:run` endpoint returns an Operation. An execution resource is a DIFFERENT thing that
    # does not exist yet at this moment, and accepting one would invite a lookup that 404s.
    for wrong in ("projects/mesh-prod/locations/europe-west4/jobs/mesher/executions/mesher-abc",
                  "projects/mesh-prod/locations/europe-west4/executions/mesher-abc",
                  "operations/abc-123"):
        with _endpoint(_Response(body={"name": wrong})):
            refuses(SubmissionIndeterminate, _trigger,
                    f"{wrong!r} was accepted as a long-running operation reference")


def test_mutation_execution_resource_accepted_as_an_operation():
    prove("response/4 treat the returned name as a final execution id",
          _control_only_a_long_running_operation_is_accepted,
          lambda: substitute(crc, "_OPERATION_NAME", re.compile(
              r"\Aprojects/[^/]+/locations/[^/]+/(jobs/[^/]+/)?(operations|executions)"
              r"/[A-Za-z0-9._~-]+\Z")),
          expecting="was accepted as a long-running operation reference")


@contextlib.contextmanager
def _captured(logger_name: str):
    records: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger(logger_name)
    handler = _Sink()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _control_no_secret_reaches_a_log_or_an_error() -> None:
    body = {"error": {"message": f"denied for {SECRET}"}, "authorization": f"Bearer {SECRET}"}
    with _captured(crc.__name__) as records:
        with _endpoint(_Response(status=200, body=body)):
            exc = refuses(SubmissionIndeterminate, _trigger,
                          "a response with no operation name was accepted")
    assert SECRET not in str(exc), "a token-shaped secret reached the raised error"
    assert not any(SECRET in line for line in records), (
        "a token-shaped secret reached a log record")
    assert not any("Bearer" in line for line in records), (
        "an authorization header reached a log record")


def test_mutation_response_body_reaches_a_log():
    def chatty(response, *, engine: str):
        crc.logger.error("cloud run response for %s: %s", engine, response.json())
        return _REAL_OPERATION_REFERENCE(response, engine=engine)

    prove("response/5 log the response body, header or token-shaped secret",
          _control_no_secret_reaches_a_log_or_an_error,
          lambda: substitute(crc, "_operation_reference", chatty),
          expecting="reached a log record")


# the composed production executor has no unowned path


def test_an_unowned_submission_is_refused_rather_than_sent():
    # THE RELEASE BOUNDARY: production installs this authority around the Cloud Run executor and
    # nothing else, so an unowned run reaching the provider would submit with no claim and no
    # namespace of its own. It must fail closed instead.
    from meshpipeline.application import execution_fence
    from meshpipeline.application.native_submission import (
        RC_INFRASTRUCTURE,
        ClaimingMeshExecutor,
    )

    sent: list = []

    class _Provider:
        def run(self, workspace, *, engine, timeout, operation_key):
            sent.append(operation_key)
            return {"rc": 0, "provider_reference": "projects/p/locations/l/operations/x"}

    assert execution_fence.current_ownership() is None
    result = ClaimingMeshExecutor(_Provider()).run("/tmp", engine="cfmesh", timeout=60)

    assert sent == [], "an unowned run reached the provider"
    assert result["rc"] == RC_INFRASTRUCTURE, result
    assert "no execution ownership is bound" in result["log_tail"]
    # and it says so without naming a bucket, a key, or a token
    assert "gs://" not in result["log_tail"] and "jobs/" not in result["log_tail"]
