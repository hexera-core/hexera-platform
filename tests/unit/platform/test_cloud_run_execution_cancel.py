# Responsibility: Verify a Cloud Run execution nobody will collect is cancelled - on a given-up run and at the deadline.
# Boundaries: the adapter's real request building and exchange path; only the sockets and the GCS client are doubles.
"""Shared dev, 2026-10-01: job 470c3eb9 was cancelled at 08:56 UTC and its Cloud Run execution
dev-mesh-796ll ran on for another 54 minutes. The worker's poll loop asked nothing while it waited,
and nothing ever called the executions cancel API. These tests hold the adapter's half of the fix:
the execution is named at acceptance, cancelled by that name, and cancelled whenever the run stops
waiting without collecting - given up (cancel, takeover, drain) or past the worker's deadline."""
from __future__ import annotations

import logging

import pytest
import requests

import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.mesh_execution import gcs_exchange
from meshpipeline.adapters.mesh_execution.exchange_coordinates import coordinates_for
from meshpipeline.contracts.mesh_execution import (
    RC_INFRASTRUCTURE,
    RC_TIMED_OUT,
    abandonment_watch,
)

OPERATION = "projects/hexera-dev/locations/us-central1/operations/0b5c-op"
EXECUTION = "projects/hexera-dev/locations/us-central1/jobs/dev-mesh/executions/dev-mesh-796ll"
KEY = "c" * 64


class _Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


class _Http:
    """Cloud Run's side of the POSTs, recorded; the request code that builds them is the
    adapter's own."""

    def __init__(self, monkeypatch):
        self.posts: list[dict] = []
        self.tokens = 0
        self.post_outcome: object = _Response(200, {"name": "projects/p/locations/l/operations/c"})
        monkeypatch.setattr(crc, "_access_token", self._token)
        monkeypatch.setattr(requests, "post", self._post)

    def _token(self):
        self.tokens += 1
        return "test-token"

    def _post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        if isinstance(self.post_outcome, Exception):
            raise self.post_outcome
        return self.post_outcome


@pytest.fixture()
def http(monkeypatch):
    monkeypatch.setattr(provcfg, "GCP_PROJECT_ID", "hexera-dev")
    monkeypatch.setattr(provcfg, "GCP_REGION", "us-central1")
    monkeypatch.setattr(provcfg, "CLOUDRUN_JOB", "dev-mesh")
    return _Http(monkeypatch)


# the execution is named at acceptance


def test_the_acceptance_names_the_execution_it_started(http):
    # RunJob's operation metadata IS the Execution. Reading the name there is what keeps the
    # cancel within run.executions.cancel - the only cancel permission the narrowest invoker role
    # (roles/run.jobsExecutorWithOverrides) has.
    http.post_outcome = _Response(200, {"name": OPERATION, "metadata": {"name": EXECUTION}})
    reference = crc._trigger_job(input_uri="gs://b/in", output_uri="gs://b/out",
                                 engine="snappy", timeout=2400)
    assert reference.operation_name == OPERATION
    assert reference.execution_name == EXECUTION
    assert reference.execution_id == "dev-mesh-796ll"


@pytest.mark.parametrize("body", [
    {"name": OPERATION},                                          # no metadata at all
    {"name": OPERATION, "metadata": "not-an-object"},
    {"name": OPERATION, "metadata": {"name": ""}},
    {"name": OPERATION, "metadata": {"name": "projects/p/locations/l/operations/x"}},
])
def test_an_acceptance_without_a_readable_execution_is_still_an_acceptance(http, body):
    # The operation name alone proves Cloud Run took the run. Doubting that over the metadata
    # would make an accepted run indeterminate - the one answer that can duplicate a mesh.
    http.post_outcome = _Response(200, body)
    reference = crc._trigger_job(input_uri="gs://b/in", output_uri="gs://b/out",
                                 engine="snappy", timeout=2400)
    assert reference.operation_name == OPERATION
    assert reference.execution_name == "" and reference.execution_id == ""


# the cancel itself


def test_a_cancel_posts_to_the_named_execution(http, caplog):
    caplog.set_level(logging.WARNING, logger=crc.__name__)
    ref = crc.CloudRunOperationReference(OPERATION, EXECUTION)
    assert crc.cancel_execution(ref, engine="snappy", why="the job was cancelled") is True
    assert len(http.posts) == 1
    call = http.posts[0]
    assert call["url"] == f"https://run.googleapis.com/v2/{EXECUTION}:cancel"
    assert call["headers"] == {"Authorization": "Bearer test-token"}
    assert call["timeout"] == 30
    assert "dev-mesh-796ll" in caplog.text and "cancelled" in caplog.text


def test_an_unnamed_execution_is_not_cancelled_and_nothing_is_called(http, caplog):
    # Looking it up would need run.operations.get, which the narrow invoker role does not have;
    # the run is left to finish, said plainly, and no credential is even minted.
    caplog.set_level(logging.WARNING, logger=crc.__name__)
    ref = crc.CloudRunOperationReference(OPERATION)
    assert crc.cancel_execution(ref, engine="snappy", why="deadline") is False
    assert http.posts == [] and http.tokens == 0
    assert "NOT cancelled" in caplog.text


@pytest.mark.parametrize("outcome", [
    _Response(403, {"error": {"message": "ya29.secret-provider-text"}}),
    _Response(400, {"error": {"message": "ya29.secret-provider-text"}}),   # already finished
    _Response(404),
    requests.exceptions.Timeout("read timed out"),
])
def test_a_cancel_that_does_not_land_is_logged_and_never_raised(http, caplog, outcome):
    caplog.set_level(logging.WARNING, logger=crc.__name__)
    http.post_outcome = outcome
    ref = crc.CloudRunOperationReference(OPERATION, EXECUTION)
    assert crc.cancel_execution(ref, engine="snappy", why="the job was cancelled") is False
    assert "NOT cancelled" in caplog.text and "dev-mesh-796ll" in caplog.text
    # the provider's own words and the credential never reach a log line
    assert "secret-provider-text" not in caplog.text and "test-token" not in caplog.text


def test_a_token_that_cannot_be_minted_is_not_raised_either(http, monkeypatch):
    def broken():
        raise RuntimeError("no credentials on this machine")
    monkeypatch.setattr(crc, "_access_token", broken)
    ref = crc.CloudRunOperationReference(OPERATION, EXECUTION)
    assert crc.cancel_execution(ref, engine="snappy", why="deadline") is False
    assert http.posts == []


# the exchange cancels whenever it stops waiting without collecting


class _Blob:
    def __init__(self, bucket, key):
        self._b, self.key = bucket, key

    def exists(self):
        return self.key in self._b.objects

    def download_as_bytes(self):
        return self._b.objects[self.key]

    def upload_from_string(self, data, content_type=None, timeout=None):
        self._b.objects[self.key] = data

    def delete(self):
        self._b.deleted.append(self.key)
        self._b.objects.pop(self.key, None)


class _Bucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def blob(self, key, chunk_size=None):
        return _Blob(self, key)


@pytest.fixture()
def exchange(monkeypatch, tmp_path):
    for k, v in {"GCP_PROJECT_ID": "hexera-dev", "GCP_MESH_BUCKET": "mesh-bkt",
                 "CLOUDRUN_JOB": "dev-mesh", "GCP_REGION": "us-central1"}.items():
        monkeypatch.setattr(provcfg, k, v)
    bucket = _Bucket()
    monkeypatch.setattr(gcs_exchange, "storage_client",
                        lambda: type("C", (), {"bucket": lambda self, name: bucket})())
    monkeypatch.setattr(crc.time, "sleep", lambda s: None)
    state = {"bucket": bucket, "triggers": 0, "cancels": []}

    def trigger(**kw):
        state["triggers"] += 1
        return crc.CloudRunOperationReference(OPERATION, EXECUTION)

    def cancel(operation, *, engine, why):
        state["cancels"].append((operation.execution_name, why))
        return True

    monkeypatch.setattr(crc, "_trigger_job", trigger)
    monkeypatch.setattr(crc, "cancel_execution", cancel)
    (tmp_path / "case").mkdir()
    (tmp_path / "case" / "geom.stl").write_text("solid x\nendsolid x\n")
    state["workspace"] = tmp_path
    return state


def _watch_after(polls: int, reason: str):
    calls = {"n": 0}

    def watch():
        calls["n"] += 1
        return reason if calls["n"] > polls else None
    return watch


def test_a_run_given_up_while_waiting_cancels_its_execution(exchange):
    # THE INCIDENT, at the adapter: the result never comes, and the run is given up after a few
    # polls. The execution it was waiting on is cancelled by name, and the answer says why.
    with abandonment_watch(_watch_after(2, "the job was cancelled")):
        out = crc.run_mesh_remote(exchange["workspace"], engine="snappy", timeout=2400,
                                  operation_key=KEY)
    assert exchange["triggers"] == 1
    assert exchange["cancels"] == [(EXECUTION, "the job was cancelled")]
    assert out["rc"] == RC_INFRASTRUCTURE and out["timed_out"] is False
    assert out["log_tail"].startswith(crc.ABANDONED_MARKER)
    assert "the job was cancelled" in out["log_tail"]
    assert "provider_reference" not in out, "a given-up run must never read as an acceptance"
    # nothing was collected, so nothing is released: the objects stay like any uncollected run
    assert exchange["bucket"].deleted == []


def test_a_result_already_in_is_collected_not_cancelled(exchange):
    # The result is checked BEFORE the watch: a mesh that finished is delivered (or fenced above),
    # never thrown away over a cancel that came a moment too late.
    import io
    import json
    import tarfile
    coords = coordinates_for(KEY)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("log.txt")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"ok"))
    exchange["bucket"].objects[coords.output_object_key] = buf.getvalue()
    exchange["bucket"].objects[coords.result_object_key] = json.dumps(
        {"rc": 0, "timed_out": False}).encode()
    # still owned at the trigger; cancelled by the time the first poll would ask
    with abandonment_watch(_watch_after(1, "the job was cancelled")):
        out = crc.run_mesh_remote(exchange["workspace"], engine="snappy", timeout=2400,
                                  operation_key=KEY)
    assert out["rc"] == 0 and out["provider_reference"] == OPERATION
    assert exchange["cancels"] == []


def test_a_run_given_up_before_its_trigger_starts_nothing(exchange):
    with abandonment_watch(lambda: "the worker is shutting down"):
        out = crc.run_mesh_remote(exchange["workspace"], engine="snappy", timeout=2400,
                                  operation_key=KEY)
    assert exchange["triggers"] == 0, "an execution was started for a run already given up"
    assert exchange["cancels"] == []
    assert out["rc"] == RC_INFRASTRUCTURE and crc.ABANDONED_MARKER in out["log_tail"]


def test_the_worker_deadline_cancels_the_execution_instead_of_leaving_it_running(exchange):
    # THE 50-MINUTE DEADLINE (timeout + 600 s). -600 makes it zero, so the loop gives up at once:
    # no result, and the execution is stopped rather than left to run unread.
    out = crc.run_mesh_remote(exchange["workspace"], engine="snappy", timeout=-600,
                              operation_key=KEY)
    assert exchange["triggers"] == 1
    assert len(exchange["cancels"]) == 1
    name, why = exchange["cancels"][0]
    assert name == EXECUTION and "deadline" in why
    # and the answer is still the timeout the driver re-plans smaller on, reference and all
    assert out["rc"] == RC_TIMED_OUT and out["timed_out"] is True
    assert out["log_tail"].startswith(crc.RESULT_TIMEOUT_MARKER)
    assert out["provider_reference"] == OPERATION


def test_a_cancel_that_does_not_land_never_changes_the_answer(exchange, monkeypatch):
    refused: list = []
    monkeypatch.setattr(crc, "cancel_execution", lambda *a, **k: refused.append(a) or False)
    with abandonment_watch(_watch_after(1, "the job was cancelled")):
        out = crc.run_mesh_remote(exchange["workspace"], engine="snappy", timeout=2400,
                                  operation_key=KEY)
    assert len(refused) == 1
    assert out["rc"] == RC_INFRASTRUCTURE and crc.ABANDONED_MARKER in out["log_tail"]


def test_a_collection_given_up_stops_waiting_without_a_cancel_it_cannot_name(exchange):
    # A replacement reading an earlier worker's run holds no execution reference of its own; it
    # stops waiting, and starts nothing.
    with abandonment_watch(_watch_after(1, "the job was cancelled")):
        out = crc.collect_mesh_remote(exchange["workspace"], engine="snappy", timeout=2400,
                                      operation_key=KEY)
    assert exchange["triggers"] == 0 and exchange["cancels"] == []
    assert crc.ABANDONED_MARKER in out["log_tail"]


def test_with_no_watch_bound_the_wait_runs_as_before(exchange):
    # Unbound - a local runner, a test, anything outside the submission authority - the loop asks
    # nothing and waits out its deadline exactly as it always did.
    out = crc.run_mesh_remote(exchange["workspace"], engine="snappy", timeout=-600,
                              operation_key=KEY)
    assert crc.ABANDONED_MARKER not in out["log_tail"]
