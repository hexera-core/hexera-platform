# Responsibility: Verify the mid-run partial mesh is served from the job's own published pointer,
#                 through the ports, and never from a key a caller names.
from __future__ import annotations

import json

from tests.unit.api.test_viewer_endpoints import JOB, _client

import meshpipeline.api.v1.simulation as sim
import meshpipeline.events as E
import meshpipeline.settings.mesh_snapshot as mscfg

FRAME = json.dumps({"kind": "polymesh", "partial": True, "is_mesh": False,
                    "stage": "castellated", "patches": []}).encode()
KEY = "jobs/" + "a" * 64 + "/partial-castellated.json"


def _stream(monkeypatch, events: list[dict]) -> None:
    class _Sub:
        def __init__(self, job_id: str) -> None:
            self.job_id = job_id

        async def backlog(self) -> list[str]:
            return [json.dumps(e) for e in events]

    import meshpipeline.contracts.event_stream as es
    monkeypatch.setattr(es, "_subscription_factory", _Sub, raising=False)


def _exchange(monkeypatch, blobs: dict[str, bytes]) -> list[str]:
    read: list[str] = []

    def _reader(object_key: str):
        read.append(object_key)
        return blobs.get(object_key)

    import meshpipeline.contracts.mesh_partial as mp
    monkeypatch.setattr(mp, "_reader", _reader, raising=False)
    return read


def test_the_frame_is_served_from_the_pointer_the_run_published(monkeypatch):
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    client, _ = _client(monkeypatch)
    _stream(monkeypatch, [E.mesh_partial("builder", "castellated", KEY).wire()])
    read = _exchange(monkeypatch, {KEY: FRAME})

    r = client.get(f"/api/v1/simulation/{JOB}/mesh/partial")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["partial"] is True
    assert read == [KEY], "the route read something other than what this job published"


def test_a_key_the_module_could_not_have_derived_is_not_followed(monkeypatch):
    # Defence in depth: the pointer comes from the run's own log, and is still checked against
    # the shape this codebase derives before it is used as an object path.
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    client, _ = _client(monkeypatch)
    forged = {"type": "mesh_partial", "stage": "builder",
              "phase": "castellated", "object_key": "jobs/x/result.json"}
    _stream(monkeypatch, [forged])
    read = _exchange(monkeypatch, {"jobs/x/result.json": b"{}"})
    assert client.get(f"/api/v1/simulation/{JOB}/mesh/partial").status_code == 404
    assert read == []


def test_a_caller_cannot_name_the_object(monkeypatch):
    # The route takes no key at all: a query string is ignored, and a job that published no
    # pointer reaches nothing in the exchange.
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    client, _ = _client(monkeypatch)
    _stream(monkeypatch, [{"type": "mesh_stage", "key": "castellating"}])
    read = _exchange(monkeypatch, {KEY: FRAME, "jobs/other/result.json": b"{}"})

    r = client.get(f"/api/v1/simulation/{JOB}/mesh/partial?key=jobs/other/result.json")

    assert r.status_code == 404
    assert read == []


def test_a_deployment_that_does_not_publish_frames_has_no_route_to_one(monkeypatch):
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", False)
    client, _ = _client(monkeypatch)
    read = _exchange(monkeypatch, {KEY: FRAME})
    assert client.get(f"/api/v1/simulation/{JOB}/mesh/partial").status_code == 404
    assert read == []


def test_another_tenant_sees_nothing(monkeypatch):
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    client, _ = _client(monkeypatch, owner="someone-else")
    _stream(monkeypatch, [E.mesh_partial("builder", "castellated", KEY).wire()])
    read = _exchange(monkeypatch, {KEY: FRAME})
    assert client.get(f"/api/v1/simulation/{JOB}/mesh/partial").status_code == 404
    assert read == []


def test_a_frame_the_exchange_no_longer_holds_is_a_404_not_a_failure(monkeypatch):
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    client, _ = _client(monkeypatch)
    _stream(monkeypatch, [E.mesh_partial("builder", "castellated", KEY).wire()])
    _exchange(monkeypatch, {})                      # collected and tidied, as every run tidies
    assert client.get(f"/api/v1/simulation/{JOB}/mesh/partial").status_code == 404


def test_the_route_reaches_the_exchange_only_through_the_port(monkeypatch):
    # No adapter import in the API: composition binds the reader (contracts/mesh_partial), and
    # with nothing bound - a local stack with no remote exchange - the route simply has none.
    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", True)
    client, _ = _client(monkeypatch)
    _stream(monkeypatch, [E.mesh_partial("builder", "castellated", KEY).wire()])
    import meshpipeline.contracts.mesh_partial as mp
    monkeypatch.setattr(mp, "_reader", None, raising=False)
    assert client.get(f"/api/v1/simulation/{JOB}/mesh/partial").status_code == 404
    assert "adapters" not in open(sim.__file__).read().split("def get_mesh_partial")[1][:2000]
