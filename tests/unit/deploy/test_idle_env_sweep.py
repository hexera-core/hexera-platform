# Responsibility: Verify the weekly sweep names only idle personal environments, and never shared dev.
# Boundaries: it RUNS find-idle-envs.sh against stub gh and gcloud binaries and reads the workflow; it deletes nothing.
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parents[3]
FIND = REPO / "deploy" / "gcp" / "scripts" / "find-idle-envs.sh"
WF = REPO / ".github" / "workflows" / "sweep-personal-envs.yml"
DEPLOY_WF = REPO / ".github" / "workflows" / "deploy.yml"

NOW = int(datetime(2026, 11, 15, 10, tzinfo=UTC).timestamp())


def _ago(days: float) -> str:
    return datetime.fromtimestamp(NOW - days * 86400, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(tmp_path: Path, history: list[tuple[str, str]], labels: list[str], **env: str):
    """Run the finder with `gh` answering `history` and every gcloud listing carrying `labels`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "history.tsv").write_text("".join(f"{t}\t{at}\n" for t, at in history))
    run_items = [{"metadata": {"labels": {"deployment-id": label}}} for label in labels]
    (tmp_path / "run.json").write_text(json.dumps(run_items))
    (tmp_path / "empty.json").write_text("[]")
    (bin_dir / "gh").write_text(f'#!/bin/sh\ncat "{tmp_path}/history.tsv"\n')
    (bin_dir / "gcloud").write_text(
        "#!/bin/sh\n"
        f'case "$1 $2" in "run services") cat "{tmp_path}/run.json" ;;'
        f' *) cat "{tmp_path}/empty.json" ;; esac\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    out = tmp_path / "gh_output"
    out.write_text("")
    full = {"PATH": f"{bin_dir}:/usr/bin:/bin", "GITHUB_OUTPUT": str(out), "NOW": str(NOW), **env}
    p = subprocess.run(["bash", str(FIND)], env=full, capture_output=True, text=True)
    idle = None
    for line in out.read_text().splitlines():
        if line.startswith("idle="):
            idle = json.loads(line.removeprefix("idle="))
    return p.returncode, idle, p.stdout + p.stderr


def _deploy(slug: str, days: float, ref: str = "some/branch") -> tuple[str, str]:
    return (f"Deploy ({ref} -> {slug})", _ago(days))


def test_only_environments_past_the_threshold_are_named(tmp_path):
    history = [_deploy("fresh", 2), _deploy("stale", 20), _deploy("stale", 30), _deploy("edge", 13.9)]
    rc, idle, log = _run(tmp_path, history, ["dev-fresh", "dev-stale", "dev-edge"])
    assert rc == 0, log
    assert idle == ["stale"]


def test_the_newest_run_decides_not_the_first(tmp_path):
    history = [_deploy("pranav", 40), _deploy("pranav", 1), _deploy("pranav", 25)]
    rc, idle, log = _run(tmp_path, history, ["dev-pranav"])
    assert rc == 0, log
    assert idle == []


def test_an_environment_with_no_run_on_record_is_idle(tmp_path):
    rc, idle, log = _run(tmp_path, [_deploy("other", 1)], ["dev-forgotten"])
    assert rc == 0, log
    assert idle == ["forgotten"]


def test_shared_dev_and_malformed_labels_are_never_named(tmp_path):
    labels = ["dev", "hexera", "dev-prod", "dev-Bad", "dev-trailing-", "dev-waytoolongslug15"]
    rc, idle, log = _run(tmp_path, [_deploy("other", 1)], labels)
    assert rc == 0, log
    assert idle == []


def test_the_keep_list_exempts_a_slug(tmp_path):
    rc, idle, log = _run(tmp_path, [_deploy("a", 30), _deploy("b", 30)], ["dev-a", "dev-b"],
                         KEEP_SLUGS="a, c")
    assert rc == 0, log
    assert idle == ["b"]


def test_the_threshold_is_configurable(tmp_path):
    rc, idle, log = _run(tmp_path, [_deploy("a", 5)], ["dev-a"], IDLE_DAYS="4")
    assert rc == 0, log
    assert idle == ["a"]


@pytest.mark.parametrize("days", ["0", "2", "x", "-1"])
def test_a_threshold_that_would_sweep_active_work_is_refused(tmp_path, days):
    rc, idle, _ = _run(tmp_path, [_deploy("a", 5)], ["dev-a"], IDLE_DAYS=days)
    assert rc != 0 and idle is None


def test_an_empty_history_is_a_failure_not_permission_to_delete_everything(tmp_path):
    rc, idle, log = _run(tmp_path, [], ["dev-a", "dev-b"])
    assert rc != 0 and idle is None
    assert "refusing" in log


def test_named_slugs_skip_discovery(tmp_path):
    """The destroy job's re-check judges one slug against the history read just before deleting."""
    rc, idle, log = _run(tmp_path, [_deploy("a", 1), _deploy("b", 30)], [], SLUGS="a,b")
    assert rc == 0, log
    assert idle == ["b"]


def test_the_title_parsed_is_the_one_deploy_writes():
    text = DEPLOY_WF.read_text(encoding="utf-8")
    assert "format(' -> {0}', github.event.inputs.slug)" in text, (
        "deploy.yml's run-name no longer ends in ' -> <slug>)'; find-idle-envs.sh reads the last "
        "deploy of every environment from that title and would call all of them idle")


def _wf() -> dict:
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def test_the_sweep_runs_weekly_and_a_manual_run_defaults_to_dry():
    on = _wf()[True]
    assert on["schedule"], "the sweep has no schedule"
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True


def test_a_teardown_queues_behind_a_deploy_of_the_same_slug():
    destroy = _wf()["jobs"]["destroy"]
    assert destroy["concurrency"]["group"] == "deploy-env-${{ matrix.slug }}"
    assert "format('env-{0}', github.event.inputs.slug)" in DEPLOY_WF.read_text(encoding="utf-8"), (
        "deploy.yml's per-slug concurrency group changed; the sweep's destroy job must match it")
    steps = destroy["steps"]
    recheck = next(i for i, s in enumerate(steps) if s.get("id") == "recheck")
    destroy_step = next(i for i, s in enumerate(steps) if "destroy-env.sh" in s.get("run", ""))
    assert recheck < destroy_step, "the slug must be re-judged after the job leaves the queue"
