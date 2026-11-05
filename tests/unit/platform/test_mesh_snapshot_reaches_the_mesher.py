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


def _vm_meta(**over) -> dict:
    from meshpipeline.adapters.mesh_execution import gce_vm

    body = gce_vm.instance_body(
        name="m-1", zone="us-central1-a", machine="n2-highmem-16",
        image="img@sha256:abc", account="mesh@p.iam.gserviceaccount.com",
        input_uri="gs://b/in", output_uri="gs://b/out",
        engine="snappy", timeout=600, disk_gb=100, **over)
    return {i["key"]: i["value"] for i in body["metadata"]["items"]}


@pytest.mark.parametrize("enabled,expected", [(True, "true"), (False, "false")])
def test_both_routes_tell_the_mesher_the_same_answer(monkeypatch, _trigger, enabled, expected):
    """FROM THE SETTING ITSELF, not from either adapter's helper.

    The two routes agreeing is the invariant (disagreement publishes frames on small meshes and
    not on large ones), and it is only actually asserted if the test states the SETTING and lets
    each route read whatever it reads: stubbing one adapter's reader proves that adapter passes
    its own answer on, and nothing about the other. Both read
    settings/mesh_snapshot.snapshots_enabled_at_dispatch, which is why this can be written.
    """
    import meshpipeline.settings.mesh_snapshot as mscfg

    monkeypatch.setattr(mscfg, "MESH_PROGRESSIVE_SNAPSHOT_ENABLED", enabled)
    env = _trigger()
    assert env["MESH_PROGRESSIVE_SNAPSHOT_ENABLED"] == expected
    # the four values the mesher has always needed are still there: this is an addition
    for key in ("INPUT_URI", "OUTPUT_URI", "ENGINE", "TIMEOUT"):
        assert key in env
    meta = _vm_meta()
    assert meta["mesh-snapshots"] == expected == env["MESH_PROGRESSIVE_SNAPSHOT_ENABLED"]
    # and the startup script must actually hand it to the container, or the attribute is decoration
    assert "MESH_PROGRESSIVE_SNAPSHOT_ENABLED" in meta["startup-script"]
    assert "instance/attributes/mesh-snapshots" in meta["startup-script"]


def test_the_vm_route_reads_the_setting_and_not_its_sibling_adapter():
    """Two execution adapters, ONE owner of the switch.

    `gce_vm` used to import `cloud_run_client._snapshots_enabled` - a private helper of a sibling
    adapter, which the architecture boundaries do not catch (both are under `adapters/`) and which
    makes "the two routes must agree" brittle rather than enforced: there were two answers to who
    reads the switch at dispatch.
    """
    from pathlib import Path

    src = (Path(__file__).parents[3] / "src" / "meshpipeline" / "adapters" / "mesh_execution"
           / "gce_vm.py").read_text()
    assert "snapshots_enabled_at_dispatch" in src
    assert "_snapshots_enabled" not in src


def test_a_settings_failure_never_stops_a_vm_being_started(monkeypatch):
    """The same promise the Cloud Run route makes, on the route that carries the big jobs."""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if name == "meshpipeline.settings.mesh_snapshot":
            raise RuntimeError("settings are unreadable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert _vm_meta()["mesh-snapshots"] == "false"


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


# THE RANK PIN IS HALF THE FEATURE. A frame is only ever read from a SERIAL snappy run, and the
# module that decides rank count (engines/determinism.snappy_ranks) runs inside the mesh
# container. So a deployment can hold MESH_PROGRESSIVE_SNAPSHOT_ENABLED=true, mean it, and still
# see nothing - which is the exact confusion the setting's own documentation warns about. Both
# routes therefore carry the pin beside the switch, and these assert they agree.
@pytest.mark.parametrize("pin", ["1", "4", ""])
def test_the_cloud_run_route_carries_the_rank_pin(monkeypatch, _trigger, pin):
    import meshpipeline.engines.determinism as dcfg

    monkeypatch.setattr(dcfg, "MESH_SNAPPY_RANKS", pin, raising=False)
    assert _trigger()["MESH_SNAPPY_RANKS"] == pin


@pytest.mark.parametrize("pin", ["1", "4", ""])
def test_the_big_vm_route_carries_the_same_rank_pin(monkeypatch, pin):
    import meshpipeline.engines.determinism as dcfg
    from meshpipeline.adapters.mesh_execution import gce_vm

    monkeypatch.setattr(dcfg, "MESH_SNAPPY_RANKS", pin, raising=False)
    body = gce_vm.instance_body(
        name="m-1", zone="us-central1-a", machine="n2-highmem-16",
        image="img@sha256:abc", account="mesh@p.iam.gserviceaccount.com",
        input_uri="gs://b/in", output_uri="gs://b/out", engine="snappy",
        timeout=600, disk_gb=100)
    meta = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert meta["mesh-snappy-ranks"] == pin
    # and the startup script must hand it to the container, or the attribute is decoration
    assert "MESH_SNAPPY_RANKS" in meta["startup-script"]
    assert "instance/attributes/mesh-snappy-ranks" in meta["startup-script"]


def test_an_unreadable_rank_setting_does_not_stop_a_dispatch(monkeypatch, _trigger):
    """Sizing is a hint. If reading it raises, the mesh still goes and the mesher sizes itself."""
    import builtins

    real = builtins.__import__

    def _boom(name, *a, **k):
        if name == "meshpipeline.engines.determinism":
            raise RuntimeError("settings are unreadable")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert _trigger()["MESH_SNAPPY_RANKS"] == ""
