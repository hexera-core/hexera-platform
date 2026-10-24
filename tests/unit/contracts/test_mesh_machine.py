"""RUN-1..4: where a mesh runs - the Cloud Run mesh job when the plan fits it, else a VM sized to fit,
decided before meshing; one machine up after a memory kill; the largest machine the last resort."""
from __future__ import annotations

import pytest

from meshpipeline.contracts import mesh_machine as MM


def test_a_plan_that_fits_the_job_runs_on_cloud_run():
    d = MM.choose(process_gib=10.0, workspace_gib=5.0)
    assert d["where"] == "cloud_run" and d["machine"] is None and "Cloud Run" in d["words"]


def test_the_in_memory_workspace_counts_on_cloud_run_but_not_on_a_vm():
    # 25 GiB of mesher fits the job's 32 GiB alone, but not with 10 GiB of files kept in memory beside it
    d = MM.choose(process_gib=25.0, workspace_gib=10.0)
    assert d["where"] == "gce" and d["machine"] == "n2-highmem-8" and d["need_gib"] == pytest.approx(25.0)


def test_the_smallest_vm_that_holds_the_mesher_is_chosen():
    assert MM.choose(process_gib=100.0, workspace_gib=30.0)["machine"] == "n2-highmem-16"
    assert MM.choose(process_gib=400.0, workspace_gib=30.0)["machine"] == "n2-highmem-64"
    assert MM.choose(process_gib=800.0, workspace_gib=30.0)["machine"] == MM.CEILING.name


def test_past_the_largest_machine_it_runs_there_and_says_the_plan_is_coarsened():
    d = MM.choose(process_gib=5000.0, workspace_gib=100.0)
    assert d["machine"] == MM.CEILING.name and d["over_ceiling"] and "coarsened" in d["words"]
    assert MM.ceiling_gib() == pytest.approx(MM.USABLE_SHARE * MM.CEILING.memory_gib)


def test_a_memory_kill_steps_up_one_machine_at_a_time_to_the_largest():
    d = MM.choose(process_gib=10.0, workspace_gib=1.0)
    seen = []
    while (d := MM.step_up(d)) is not None:
        seen.append(d["machine"])
        assert "same plan" in d["words"] and "nothing coarsened" in d["words"]
    assert seen == [m.name for m in MM.MACHINES]


def test_a_step_up_holds_the_peak_seen():
    d = MM.step_up(MM.choose(process_gib=10.0, workspace_gib=1.0), measured_gib=200.0)
    assert d is not None and d["machine"] == "n2-highmem-32"


def test_the_decision_travels_in_the_workspace(tmp_path):
    assert MM.read(tmp_path) is None
    d = MM.choose(process_gib=100.0, workspace_gib=1.0)
    MM.write(tmp_path, d)
    assert MM.read(tmp_path) == d


def test_the_job_memory_is_the_deploys_own():
    from meshpipeline.engines.fea_driver import MESH_JOB_MEMORY_GIB
    assert MM.CLOUD_RUN_GIB == MESH_JOB_MEMORY_GIB


def test_the_vm_runs_the_jobs_own_container_and_deletes_itself():
    from meshpipeline.adapters.mesh_execution import gce_vm as G

    body = G.instance_body(name="mesh-big-x", zone="us-central1-a", machine="n2-highmem-16",
                           image="reg/mesh@sha256:abc", account="mesh@p.iam", input_uri="gs://b/in",
                           output_uri="gs://b/out", engine="gmsh", timeout=13800, disk_gb=200)
    meta = {i["key"]: i["value"] for i in body["metadata"]["items"]}
    assert meta["mesh-image"] == "reg/mesh@sha256:abc" and meta["engine"] == "gmsh" and meta["timeout"] == "13800"
    assert "meshpipeline.runtime.mesh_runner" in meta["startup-script"]
    assert "instances/$(get instance/name)" in meta["startup-script"]          # deletes itself
    sched = body["scheduling"]
    assert sched["instanceTerminationAction"] == "DELETE"
    assert int(sched["maxRunDuration"]["seconds"]) == 13800 + G.LIFETIME_MARGIN_S
    assert body["serviceAccounts"][0]["email"] == "mesh@p.iam" and body["labels"]["purpose"] == G.LABEL
    # the same operation and machine give the same VM (a replacement worker finds it); a step up a new one
    assert G.instance_name("op1", "n2-highmem-16") == G.instance_name("op1", "n2-highmem-16")
    assert G.instance_name("op1", "n2-highmem-16") != G.instance_name("op1", "n2-highmem-32")
    assert len(G.instance_name("op1", "m1-ultramem-40")) <= 63
