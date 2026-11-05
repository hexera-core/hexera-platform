# Responsibility: Prove the progressive-snapshot switch actually REACHES the process that reads it.
# Boundaries: the dispatch bodies only - it runs no mesh and talks to no cloud.
#
# WHY THIS TEST EXISTS. The mesher reads MESH_PROGRESSIVE_SNAPSHOT_ENABLED inside the mesh
# container (runtime/mesh_runner, engines/snappy/progressive), and that container carries NO env of
# its own: `deploy/gcp/cloud-run/mesh-job.yaml` deliberately declares none, and the big VM runs the
# same image. So the switch can only arrive as a per-execution override. It did not, which made the
# whole feature unreachable in every deployment however the environment was configured - a setting
# that reads true on the worker and false where it is used. Both routes are asserted together
# because disagreement between them would publish frames on small meshes and not on large ones.
from __future__ import annotations

import pytest


def _env_of(body: dict) -> dict[str, str]:
    """The container override env, as a plain mapping."""
    containers = body["overrides"]["containerOverrides"]
    return {e["name"]: e["value"] for e in containers[0]["env"]}


@pytest.fixture
def _trigger(monkeypatch):
    """`_trigger_job`'s request body, without reaching Cloud Run."""
    from meshpipeline.adapters.mesh_execution import cloud_run_client as crc

    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"name": "projects/p/locations/r/operations/o", "metadata": {}}

    def _post(url, json=None, headers=None, timeout=None):  # noqa: A002
        captured["body"] = json
        return _Resp()

    monkeypatch.setattr(crc, "_access_token", lambda: "t")
    monkeypatch.setattr(crc, "_operation_reference", lambda resp, engine: engine)
    import requests

    monkeypatch.setattr(requests, "post", _post)

    def run():
        crc._trigger_job(input_uri="gs://b/in", output_uri="gs://b/out",
                         engine="snappy", timeout=600)
        return _env_of(captured["body"])

    return run


@pytest.mark.parametrize("enabled,expected", [(True, "true"), (False, "false")])
def test_the_cloud_run_route_tells_the_mesher_the_answer(monkeypatch, _trigger, enabled, expected):
    from meshpipeline.adapters.mesh_execution import cloud_run_client as crc

    monkeypatch.setattr(crc, "_snapshots_enabled", lambda: enabled)
    env = _trigger()
    assert env["MESH_PROGRESSIVE_SNAPSHOT_ENABLED"] == expected
    # the four values the mesher has always needed are still there: this is an addition
    for key in ("INPUT_URI", "OUTPUT_URI", "ENGINE", "TIMEOUT"):
        assert key in env


@pytest.mark.parametrize("enabled,expected", [(True, "true"), (False, "false")])
def test_the_big_vm_route_tells_it_the_same_answer(monkeypatch, enabled, expected):
    from meshpipeline.adapters.mesh_execution import cloud_run_client as crc
    from meshpipeline.adapters.mesh_execution import gce_vm

    monkeypatch.setattr(crc, "_snapshots_enabled", lambda: enabled)
    body = gce_vm.instance_body(
        name="m-1", zone="us-central1-a", machine="n2-highmem-16",
        image="img@sha256:abc", account="mesh@p.iam.gserviceaccount.com",
        input_uri="gs://b/in", output_uri="gs://b/out",
        engine="snappy", timeout=600, disk_gb=100)
    meta = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert meta["mesh-snapshots"] == expected
    # and the startup script must actually hand it to the container, or the attribute is decoration
    assert "MESH_PROGRESSIVE_SNAPSHOT_ENABLED" in meta["startup-script"]
    assert "instance/attributes/mesh-snapshots" in meta["startup-script"]


def test_a_settings_failure_never_stops_a_mesh_being_dispatched(monkeypatch):
    """The switch is a progress nicety. If reading it raises, the mesh still goes."""
    import builtins

    from meshpipeline.adapters.mesh_execution import cloud_run_client as crc

    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if name == "meshpipeline.settings.mesh_snapshot":
            raise RuntimeError("settings are unreadable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert crc._snapshots_enabled() is False
