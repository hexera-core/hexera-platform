# Responsibility: Verify the Cloud Run trigger returns a validated operation reference and never guesses.
# Boundaries: the request and its response; persisting the reference is the integration checkpoint's.
from __future__ import annotations

import inspect

import pytest
import requests

from meshpipeline.adapters.mesh_execution import cloud_run_client as crc

VALID = "projects/mesh-prod/locations/europe-west4/operations/abc-123_x~y.z"


class _Response:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        self._body = body
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


@pytest.fixture()
def posted(monkeypatch):
    # The adapter's real request-building code runs; only the socket is replaced.
    calls: list = []
    monkeypatch.setattr(crc, "_access_token", lambda: "test-token")
    monkeypatch.setattr(crc.provcfg, "GCP_PROJECT_ID", "mesh-prod")
    monkeypatch.setattr(crc.provcfg, "GCP_REGION", "europe-west4")
    monkeypatch.setattr(crc.provcfg, "CLOUDRUN_JOB", "mesher")

    def install(outcome):
        def post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        import requests as real
        monkeypatch.setattr(real, "post", post)
        return calls
    return install


def _trigger():
    return crc._trigger_job(input_uri="gs://b/in", output_uri="gs://b/out",
                            engine="cfmesh", timeout=420)


def test_one_post_reaches_the_run_endpoint_with_the_existing_overrides(posted):
    calls = posted(_Response(body={"name": VALID}))
    _trigger()
    assert len(calls) == 1
    assert calls[0]["url"] == (
        "https://run.googleapis.com/v2/projects/mesh-prod/locations/europe-west4/jobs/mesher:run")
    env = calls[0]["json"]["overrides"]["containerOverrides"][0]["env"]
    assert {e["name"]: e["value"] for e in env} == {
        "INPUT_URI": "gs://b/in", "OUTPUT_URI": "gs://b/out",
        "ENGINE": "cfmesh", "TIMEOUT": "420"}
    assert calls[0]["timeout"] == 60


def test_a_valid_response_returns_the_exact_typed_reference(posted):
    posted(_Response(body={"name": VALID}))
    reference = _trigger()
    assert isinstance(reference, crc.CloudRunOperationReference)
    assert reference.operation_name == VALID and reference.location == "europe-west4"


def test_two_operations_remain_distinguishable(posted):
    posted(_Response(body={"name": VALID}))
    first = _trigger()
    other = VALID.replace("abc-123_x~y.z", "def-456")
    posted(_Response(body={"name": other}))
    assert _trigger().operation_name != first.operation_name


def test_the_trigger_no_longer_returns_none_on_success(posted):
    posted(_Response(body={"name": VALID}))
    assert _trigger() is not None
    assert inspect.signature(crc._trigger_job).return_annotation != "None"


# definitive versus indeterminate


def test_a_definitive_non_success_follows_the_existing_failure_path(posted):
    posted(_Response(status=403))
    with pytest.raises(requests.exceptions.HTTPError):
        _trigger()


@pytest.mark.parametrize("outcome", [
    requests.exceptions.Timeout("read timed out"),
    requests.exceptions.ConnectionError("connection reset"),
])
def test_a_transport_outcome_whose_acceptance_is_unknown_is_indeterminate(posted, outcome):
    posted(outcome)
    with pytest.raises(crc.SubmissionIndeterminate):
        _trigger()


@pytest.mark.parametrize("response", [
    _Response(body={}),                                   # no name
    _Response(body={"name": ""}),                         # empty name
    _Response(body={"name": "operations/loose"}),         # not a resource name
    _Response(body={"name": "projects/p/locations/l/executions/x"}),   # an execution, not an operation
    _Response(raw=b"<html>"),                             # unreadable body
])
def test_a_success_we_cannot_read_is_indeterminate_not_failure(posted, response):
    # THE most dangerous case: Cloud Run may have accepted the run. Calling it "not submitted"
    # is what would duplicate a 25-minute job.
    posted(response)
    with pytest.raises(crc.SubmissionIndeterminate):
        _trigger()


def test_an_operation_from_another_region_is_indeterminate(posted):
    posted(_Response(body={"name": VALID.replace("europe-west4", "us-central1")}))
    with pytest.raises(crc.SubmissionIndeterminate):
        _trigger()


def test_a_canonicalised_project_number_is_still_accepted(posted):
    # The API may return the numeric project number. Comparing project ids would reject a real
    # acceptance, so only the location is compared.
    posted(_Response(body={"name": VALID.replace("mesh-prod", "123456789012")}))
    assert _trigger().operation_name.startswith("projects/123456789012/")


def test_no_response_body_token_or_authorization_header_reaches_an_error(posted):
    secret = "ya29.super-secret-token"
    posted(_Response(status=200, body={"error": {"message": secret}, "trace": secret}))
    with pytest.raises(crc.SubmissionIndeterminate) as raised:
        _trigger()
    message = str(raised.value)
    assert secret not in message and "Bearer" not in message and "test-token" not in message


def test_the_reference_travels_back_for_the_submission_authority_to_record():
    # The adapter does not persist it - the authority above it does, in the same step that
    # released the claim - so the reference has to reach the caller.
    source = inspect.getsource(crc._exchange)
    assert 'operation.operation_name if operation else ""' in source
    assert "native_submission_repository" not in source, (
        "the adapter records the claim itself; that belongs to the submission authority")
