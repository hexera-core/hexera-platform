# Responsibility: Hold the parametrised fleet launcher to its two promises: v3 launches are byte-identical
#                 to what they were before v4 existed, and NO wave (v3 or v4) can create a VM without
#                 passing the budget gate. Also the v4 binding: metadata, labels, names, run_arm path.
# Boundaries: hermetic. gcloud is off PATH or replaced by a recorder; vm_startup.sh runs against stub
#             curl / gcloud / docker. Nothing is created and nothing is spent.
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
V3 = REPO / "devtools" / "validation" / "v3_eval"
V4 = REPO / "devtools" / "validation" / "v4_eval"
POOL = REPO / "devtools" / "validation" / "corpora" / "v4" / "pool.json"
IMAGE = "us-central1-docker.pkg.dev/hexera-dev/mesh/app@sha256:" + "a" * 64
HEAD = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                      text=True).stdout.strip()


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lf = _load("fleet_launcher_under_test", V3 / "launch_fleet.py")


def _launch(*argv: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": "/usr/bin:/bin"}   # gcloud OFF the path: a dry run cannot reach it
    return subprocess.run([sys.executable, str(V3 / "launch_fleet.py"), *argv],
                          capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)


def _v4(*extra: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    wave = [] if "--wave-type" in extra else ["--wave-type", "normal", "--wave-id", "w-test"]
    return _launch("--profile", "v4", "--image", IMAGE, "--commit", HEAD, "--image-commit", HEAD,
                   "--skip-preflight", *wave, *extra, tmp_path=tmp_path)


def _creates(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if "gcloud compute instances create" in ln]


# ----------------------------------------------------------------- v3 is byte-identical

GOLDEN_V3_CREATE = [
    "gcloud",
    "compute",
    "instances",
    "create",
    "dev-crsv3-v3e-11221030-3of12",
    "--project=hexera-dev",
    "--zone=us-central1-a",
    "--machine-type=c2d-standard-8",
    "--provisioning-model=STANDARD",
    "--instance-termination-action=DELETE",
    "--max-run-duration=3h",
    "--image-family=ubuntu-2204-lts",
    "--image-project=ubuntu-os-cloud",
    "--boot-disk-size=100GB",
    "--boot-disk-type=pd-balanced",
    "--network=default",
    "--subnet=projects/hexera-dev/regions/us-central1/subnetworks/default",
    "--no-address",
    "--service-account=224734058693-compute@developer.gserviceaccount.com",
    "--scopes=cloud-platform",
    "--labels=deployment-id=dev-crsv3,purpose=v3-eval,commit=111111111111,launch=11221030,shards=2,arms=pass-passai,app=hexera,managed-by=v3-eval",
    "--metadata-from-file=startup-script=<REPO>/devtools/validation/v3_eval/vm_startup.sh,shutdown-script=<REPO>/devtools/validation/v3_eval/vm_shutdown.sh",
    "--metadata=^|^v3-image=us-central1-docker.pkg.dev/hexera-dev/mesh/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|v3-commit=1111111111111111111111111111111111111111|v3-image-commit=222222222222|v3-units=meshability_pass:failing:1/2,meshability_pass_ai:clean:1/1|v3-bucket=gs://dev-crsv3-transfer-224734058693|v3-prefix=gs://dev-crsv3-transfer-224734058693/eval/v3-verdict|v3-code=gs://dev-crsv3-transfer-224734058693/eval/v3-verdict/code-111111111111/code.tgz|v3-machine-type=c2d-standard-8|v3-model-secret=openai-api-key"
]

GOLDEN_V3_STARTUP = [
    "auth configure-docker IMG@sha256:x --quiet",
    "compute instances delete vm-1 --zone us-central1-a --project hexera-dev --quiet",
    "docker pull IMG@sha256:x",
    "docker run --rm --name v3eval-1 --cpus=1 -e PYTHONPATH=/eval/code/src --env-file ROOT/out/meshability_pass_failing_1_2.env --env-file ROOT/secret.env -v ROOT/code:/eval/code:ro -v ROOT/inputs/failing:/eval/inputs:ro -v ROOT/out/meshability_pass_failing_1_2:/eval/out -v ROOT/sample.json:/eval/sample.json:ro --entrypoint python IMG@sha256:x /eval/code/devtools/validation/v3_eval/run_arm.py --arm meshability_pass --kind failing --shard 1/2 --inputs /eval/inputs --out /eval/out --code-root /eval/code --sample-json /eval/sample.json --commit cccccccccccccccccccccccccccccccccccccccc --image-commit abc1234 --image IMG@sha256:x --machine-type c2d-standard-8",
    "docker run --rm --name v3eval-2 --cpus=1 -e PYTHONPATH=/eval/code/src --env-file ROOT/out/meshability_pass_ai_clean_1_1.env --env-file ROOT/secret.env -v ROOT/code:/eval/code:ro -v ROOT/inputs/clean:/eval/inputs:ro -v ROOT/out/meshability_pass_ai_clean_1_1:/eval/out -v ROOT/sample.json:/eval/sample.json:ro --entrypoint python IMG@sha256:x /eval/code/devtools/validation/v3_eval/run_arm.py --arm meshability_pass_ai --kind clean --shard 1/1 --inputs /eval/inputs --out /eval/out --code-root /eval/code --sample-json /eval/sample.json --commit cccccccccccccccccccccccccccccccccccccccc --image-commit abc1234 --image IMG@sha256:x --machine-type c2d-standard-8",
    "python3 ROOT/code/devtools/validation/v3_eval/evalspec.py env meshability_pass",
    "python3 ROOT/code/devtools/validation/v3_eval/evalspec.py env meshability_pass_ai",
    "secrets versions access latest --secret=openai-api-key --project=hexera-dev",
    "storage cp -r gs://B/eval/v3-verdict/inputs/clean/* ROOT/inputs/clean/",
    "storage cp -r gs://B/eval/v3-verdict/inputs/failing/* ROOT/inputs/failing/",
    "storage cp ROOT/out/meshability_pass_ai_clean_1_1.FAILED gs://B/eval/meshability_pass_ai-cccccccccccc/clean/shard-1-of-1/FAILED",
    "storage cp ROOT/out/meshability_pass_failing_1_2.FAILED gs://B/eval/meshability_pass-cccccccccccc/failing/shard-1-of-2/FAILED",
    "storage cp gs://B/eval/v3-verdict/code-cccccccccccc/code.tgz ROOT/code.tgz",
    "storage cp gs://B/eval/v3-verdict/sample.json ROOT/sample.json",
    "storage ls gs://B/eval/meshability_pass-cccccccccccc/failing/shard-1-of-2/DONE",
    "storage ls gs://B/eval/meshability_pass_ai-cccccccccccc/clean/shard-1-of-1/DONE",
    "storage rm gs://B/eval/meshability_pass-cccccccccccc/failing/shard-1-of-2/FAILED gs://B/eval/meshability_pass-cccccccccccc/failing/shard-1-of-2/INCOMPLETE",
    "storage rm gs://B/eval/meshability_pass_ai-cccccccccccc/clean/shard-1-of-1/FAILED gs://B/eval/meshability_pass_ai-cccccccccccc/clean/shard-1-of-1/INCOMPLETE",
    "storage rsync -r ROOT/out/meshability_pass_ai_clean_1_1 gs://B/eval/meshability_pass_ai-cccccccccccc/clean/shard-1-of-1",
    "storage rsync -r ROOT/out/meshability_pass_failing_1_2 gs://B/eval/meshability_pass-cccccccccccc/failing/shard-1-of-2"
]


def _v3_args(**over):
    base = {"image": IMAGE, "commit": "1" * 40, "image_commit": "2" * 12,
            "machine_type": "c2d-standard-8", "zone": "us-central1-a", "max_run_hours": 3,
            "prefix": lf.PREFIX}
    return argparse.Namespace(**{**base, **over})


def test_v3_create_command_is_byte_identical_to_the_pre_v4_launcher():
    # No `profile` on the namespace at all: the pre-v4 call shape must still mean v3.
    cmd = lf.create_command(_v3_args(), "11221030", 3, 12,
                            ["meshability_pass:failing:1/2", "meshability_pass_ai:clean:1/1"])
    assert cmd == [c.replace("<REPO>", str(REPO)) for c in GOLDEN_V3_CREATE]


def test_v3_dry_run_still_refuses_and_plans_as_before(tmp_path):
    r = _launch("--image", IMAGE, "--commit", HEAD, "--image-commit", HEAD, "--dry-run",
                "--skip-preflight", tmp_path=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    creates = _creates(r.stdout)
    assert len(creates) == 12
    assert all("purpose=v3-eval" in c and "v3-units=" in c for c in creates)
    # v3 carries no v4 metadata KEY. Judged on the keys, not the command string, whose text includes the
    # checkout path (a directory named `...v4-...` once decided this test).
    import shlex
    for c in creates:
        meta = next(a for a in shlex.split(c) if a.startswith("--metadata=^|^"))
        keys = [kv.split("=", 1)[0] for kv in meta[len("--metadata=^|^"):].split("|")]
        assert keys and not [k for k in keys if k.startswith("v4-")], keys
    assert all("dev-crsv3-v3e-" in c for c in creates)


# ----------------------------------------------------------------- vm_startup.sh under stubs

_CURL = f"""#!{sys.executable}
import json, os, sys
key = sys.argv[-1].split("computeMetadata/v1/")[1]
meta = json.load(open(os.environ["META"]))
if key == "instance/name": print("vm-1")
elif key == "instance/zone": print("projects/1/zones/us-central1-a")
elif key == "project/project-id": print("hexera-dev")
elif key.startswith("instance/attributes/") and key.split("/")[-1] in meta:
    print(meta[key.split("/")[-1]], end="")
else: sys.exit(22)
"""
_GCLOUD = """#!/bin/bash
echo "$*" > "$LOG/$$"
if [ "$1 $2" = "storage du" ]; then
  printf '%b' "${DU_OUT-1000 gs://B/eval/inputs\\n}"; exit "${DU_RC:-0}"
fi
if [ "$1 $2" = "storage cp" ]; then
  dst="${@: -1}"
  case "$dst" in gs://*|-) ;; *) mkdir -p "$(dirname "$dst")"
    case "$dst" in *code.tgz) tar -czf "$dst" -T /dev/null;; *) echo '{}' > "$dst";; esac;; esac
fi
if [ "$1 $2" = "storage ls" ]; then
  [ -n "${LS_OK:-}" ] && [[ "$*" == *${LS_OK}* ]] && exit 0
  exit 1
fi
exit 0
"""
_DF = """#!/bin/bash
printf '%b\\n' "${DF_OUT-Avail\\n99999999999}"
"""
_DOCKER = """#!/bin/bash
if [ "$1" = "ps" ]; then echo a; echo b; echo c; echo d; exit 0; fi
echo "docker $*" > "$LOG/$$"
exit 0
"""


def _startup(tmp_path: Path, meta: dict[str, str], expect_rc: int = 0,
             extra_env: dict[str, str] | None = None) -> list[str]:
    b = tmp_path / "bin"
    b.mkdir()
    for name, body in (("curl", _CURL), ("gcloud", _GCLOUD), ("docker", _DOCKER),
                       ("df", _DF), ("sleep", "#!/bin/bash\nexit 1\n"), ("chown", "#!/bin/bash\nexit 0\n"),
                       ("python3", '#!/bin/bash\necho "python3 $*" > "$LOG/$$"\nexit 0\n')):
        (b / name).write_text(body)
        (b / name).chmod(0o755)
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    (tmp_path / "log").mkdir()   # one file per stub call: parallel shards cannot tear a line
    env = {"PATH": f"{b}:/usr/bin:/bin", "META": str(tmp_path / "meta.json"),
           "LOG": str(tmp_path / "log"), "V3EVAL_WORKDIR": str(tmp_path / "w"),
           "V3EVAL_RETRY_SLEEP": "0", **(extra_env or {})}
    r = subprocess.run(["bash", str(V3 / "vm_startup.sh")], env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == expect_rc, r.stderr
    return sorted(ln.replace(str(tmp_path / "w"), "ROOT")
                  for f in (tmp_path / "log").iterdir() for ln in f.read_text().splitlines())


V3_META = {
    "v3-image": "IMG@sha256:x", "v3-commit": "c" * 40, "v3-image-commit": "abc1234",
    "v3-units": "meshability_pass:failing:1/2,meshability_pass_ai:clean:1/1", "v3-bucket": "gs://B",
    "v3-prefix": "gs://B/eval/v3-verdict",
    "v3-code": "gs://B/eval/v3-verdict/code-cccccccccccc/code.tgz",
    "v3-machine-type": "c2d-standard-8", "v3-model-secret": "openai-api-key"}


def test_v3_startup_runs_exactly_the_same_commands_as_before(tmp_path):
    assert _startup(tmp_path, V3_META) == GOLDEN_V3_STARTUP


def test_v4_startup_runs_the_v4_run_arm_with_the_v4_arguments(tmp_path):
    units = "reprobe:pool:1/2,reprobe:pool:2/2"
    meta = {"v4-image": "IMG@sha256:x", "v4-commit": "c" * 40, "v4-image-commit": "abc1234",
            "v4-units": units, "v4-bucket": "gs://B", "v4-prefix": "gs://B/eval/v4-reprobe",
            "v4-code": "gs://B/eval/v4-reprobe/code-cccccccccccc/code.tgz",
            "v4-machine-type": "c2d-standard-8",
            "v4-run-arm": "devtools/validation/v4_eval/run_arm.py",
            "v4-pool": "gs://B/eval/v4-reprobe/pool.json",
            "v4-cache-uri": "gs://B/eval/v4-reprobe/verdict-cache", "v4-sources": "abc_fail,abc_pass",
            "v4-wave-id": "w-test", "v4-wave-type": "normal", "v4-n-shards": "2",
            "v4-marker-bucket": "B"}
    log = _startup(tmp_path, meta)
    runs = [ln for ln in log if ln.startswith("docker run")]
    assert len(runs) == 2
    for run in runs:
        assert "--name v4eval-" in run and "v3" not in run
        assert "/eval/code/devtools/validation/v4_eval/run_arm.py" in run
        assert "--mode reprobe" in run and "--pool /eval/pool.json" in run
        assert "--cache-uri gs://B/eval/v4-reprobe/verdict-cache" in run
        assert "--sources abc_fail,abc_pass" in run
        assert "sample.json" not in run and "secret.env" not in run   # no model key for the checker
    assert any(ln.startswith("storage cp gs://B/eval/v4-reprobe/pool.json") for ln in log)
    # the checker needs no MODEL key and the markers use the VM's own identity: NO secret is read at all
    assert not any("secrets" in ln for ln in log)
    assert any("gs://B/eval/reprobe-cccccccccccc/pool/shard-1-of-2/DONE" in ln for ln in log)


# ----------------------------------------------------------------- the v4 binding

def test_v4_dry_run_is_one_wave_of_v4_labelled_self_deleting_vms(tmp_path):
    r = _v4("--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    creates = _creates(r.stdout)
    assert len(creates) == 12                      # 96 shards, 8 per c2d-standard-8
    units: list[str] = []
    for j, line in enumerate(creates, start=1):
        assert "dev-crsv3-v4e-" in line and f"{j}of12" in line
        assert "deployment-id=dev-crsv3" in line and "purpose=v4-eval" in line
        assert "managed-by=v4-eval" in line and "arms=reprobe" in line
        for gone in ("v3-", "v3e", "purpose=v3", "crsv3-verdict", "openai-api-key"):
            assert gone not in line.replace("gs://dev-crsv3-transfer", "").replace("dev-crsv3", ""), gone
        for key in ("v4-image=", "v4-commit=", "v4-units=", "v4-code=", "v4-run-arm=",
                    "v4-pool=", "v4-cache-uri=", "v4-sources="):
            assert key in line, key
        assert "v4-run-arm=devtools/validation/v4_eval/run_arm.py" in line
        assert f"v4-code=gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe/code-{HEAD[:12]}/" in line
        assert "--instance-termination-action=DELETE" in line and "--machine-type=c2d-standard-8" in line
        units += line.split("v4-units=")[1].split("|")[0].split(",")
    assert sorted(units) == sorted(f"reprobe:pool:{i}/96" for i in range(1, 97))


def test_pool_sources_match_the_pool_module_s_source_type():
    import typing
    ev = _load("v4_evalspec_under_test2", V4 / "evalspec.py")
    pool = _load("v4_pool_under_test", V4 / "pool.py")
    assert set(ev.POOL_SOURCES) == set(typing.get_args(pool.Source))


def test_v4_vms_are_swept_by_the_crsv3_destroy_and_still_distinguishable(tmp_path):
    r = _v4("--dry-run", tmp_path=tmp_path)
    assert all("deployment-id=dev-crsv3," in c and "purpose=v4-eval" in c for c in _creates(r.stdout))


def test_v4_startup_refuses_without_the_source_list(tmp_path):
    meta = {"v4-image": "IMG@sha256:x", "v4-commit": "c" * 40, "v4-image-commit": "abc1234",
            "v4-units": "reprobe:pool:1/2", "v4-bucket": "gs://B", "v4-prefix": "gs://B/p",
            "v4-code": "gs://B/p/code-cccccccccccc/code.tgz", "v4-machine-type": "c2d-standard-8",
            "v4-run-arm": "devtools/validation/v4_eval/run_arm.py", "v4-wave-id": "w-test",
            "v4-wave-type": "normal", "v4-n-shards": "2", "v4-marker-bucket": "B"}
    log = _startup(tmp_path, meta, expect_rc=1)
    assert not any(ln.startswith("docker run") for ln in log)
    assert any(ln.endswith("/FAILED") or "FAILED" in ln for ln in log)


def test_v4_default_sources_exclude_fusion_360_and_count_the_cleared_files(tmp_path):
    r = _v4("--dry-run", tmp_path=tmp_path)
    assert "v4-sources=abc_fail,abc_pass,inject,nist,occt" in r.stdout
    assert "f360" not in r.stdout.replace("not cleared", "")
    pool = json.loads(POOL.read_text())["files"]
    cleared = [f for f in pool if f["source"] != "f360"]
    assert len(pool) > len(cleared) > 0, "the committed pool must carry F360 for this test to bite"
    assert f"{len(cleared)} files" in r.stdout


def test_v4_refuses_fusion_360_until_the_owner_clears_it(tmp_path):
    r = _v4("--dry-run", "--sources", "abc_fail,f360", tmp_path=tmp_path)
    assert r.returncode == 2 and "f360" in r.stderr and "not cleared" in r.stderr
    assert not _creates(r.stdout)


def test_the_source_list_is_pluggable_data_not_launcher_code():
    ev = _load("v4_evalspec_under_test", V4 / "evalspec.py")
    assert "f360" in ev.POOL_SOURCES and "f360" not in ev.CLEARED_SOURCES
    assert set(ev.CLEARED_SOURCES) <= set(ev.POOL_SOURCES)
    assert ev.resolve_sources(["abc_fail", "inject"]) == ("abc_fail", "inject")
    with pytest.raises(ValueError, match="not cleared"):
        ev.resolve_sources(["f360"])
    with pytest.raises(ValueError, match="unknown"):
        ev.resolve_sources(["nonsense"])
    # clearing F360 is one data change: the same resolver then accepts it, no launcher edit
    assert ev.resolve_sources(["f360"], cleared=(*ev.CLEARED_SOURCES, "f360")) == ("f360",)


def test_the_source_filter_reaches_the_vms_and_run_arm_applies_it(tmp_path):
    ok = _v4("--dry-run", "--sources", "abc_fail", tmp_path=tmp_path)
    assert ok.returncode == 0, ok.stderr
    assert all("v4-sources=abc_fail|" in c for c in _creates(ok.stdout))
    ra = _load("v4_run_arm_under_test", V4 / "run_arm.py")
    pool = ra.pool_mod.read_pool(POOL)
    kept = ra.select_sources(pool, "abc_fail,inject")
    assert kept and {e.source for e in kept} == {"abc_fail"}      # the committed pool has no inject
    # an EMPTY filter (missing flag / metadata key) means the cleared sources, never the whole pool
    empty = ra.select_sources(pool, "")
    assert empty and all(e.source != "f360" for e in empty) and len(empty) < len(pool)
    assert {e.source for e in empty} == {e.source for e in pool} - {"f360"}
    with pytest.raises(ValueError, match="not cleared"):
        ra.select_sources(pool, "abc_fail,f360")                     # run_arm refuses it too


# ----------------------------------------------------------------- the budget gate

def test_v4_over_ceiling_wave_is_refused(tmp_path):
    r = _v4("--dry-run", "--files", "400000", "--max-run-hours", "400", tmp_path=tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "VM-hour ceiling" in r.stderr and "--allow-over-budget" in r.stderr


def test_v3_over_ceiling_wave_is_refused_too(tmp_path):
    # 12 VMs x 60 h cap = 720 worst-case VM-hours > 600
    r = _launch("--image", IMAGE, "--commit", HEAD, "--image-commit", HEAD, "--dry-run",
                "--skip-preflight", "--max-run-hours", "60", tmp_path=tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "VM-hour ceiling" in r.stderr


def test_allow_over_budget_waives_the_ceiling_and_says_so(tmp_path):
    r = _v4("--dry-run", "--files", "400000", "--max-run-hours", "400", "--allow-over-budget",
            tmp_path=tmp_path)
    assert "overridden" in r.stderr
    assert "REFUSE" not in r.stderr and r.returncode == 0, r.stderr
    # the override waives money only: a wall past the VM's own deletion is still refused
    r = _v4("--dry-run", "--files", "400000", "--allow-over-budget", tmp_path=tmp_path)
    assert r.returncode == 1 and "deleted before their shards finish" in r.stderr


class _Recorder:
    """Stands in for subprocess.run: records every command, creates nothing."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def creates(self):
        return [c for c in self.calls if c[:4] == ["gcloud", "compute", "instances", "create"]]


@pytest.fixture
def live(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(lf.subprocess, "run", rec)
    monkeypatch.setattr(lf, "live_checks", lambda *a, **k: ([], []))
    monkeypatch.setattr(lf, "done_units", lambda *a: set())
    monkeypatch.setattr(lf, "running_units", lambda *a: (set(), []))
    return rec


def _main(*argv: str) -> int:
    if "v4" not in argv:
        return lf.main(["--image", IMAGE, "--commit", HEAD, "--image-commit", HEAD, "--skip-preflight",
                        *argv])
    # a v4 wave is gated on the conditional-create probe and the wave-count limits: give it a marker
    # store whose probe has passed, in a temp directory (nothing reaches a bucket)
    import tempfile
    ml = _load("model_ledger_under_test",
               REPO / "src" / "meshpipeline" / "cad" / "repair_v4" / "model_ledger.py")
    mp = _load("marker_probe_under_test_main", V4 / "marker_probe.py")
    backend = "gcs/dev-crsv3-transfer-224734058693"
    markers = ml.FileMarkerStore(tempfile.mkdtemp())
    mp.record_pass(markers, backend=backend, run_id="t", rounds=50)
    return lf.main(["--image", IMAGE, "--commit", HEAD, "--image-commit", HEAD, "--skip-preflight",
                    "--wave-type", "normal", "--wave-id", "w-test", *argv], gates=(markers, backend))


@pytest.mark.parametrize("profile_args", [[], ["--profile", "v4"]], ids=["v3", "v4"])
def test_a_live_over_ceiling_wave_creates_no_vm(live, profile_args, capsys):
    rc = _main(*profile_args, "--max-run-hours", "60")
    assert rc == 1 and live.creates() == [], live.calls
    assert "VM-hour ceiling" in capsys.readouterr().err


@pytest.mark.parametrize("profile_args", [[], ["--profile", "v4"]], ids=["v3", "v4"])
def test_a_live_within_ceiling_wave_does_create_its_vms(live, profile_args):
    assert _main(*profile_args, "--max-run-hours", "8") == 0
    assert len(live.creates()) == 12            # the gate is a gate, not a wall


def test_the_override_is_the_only_way_past_the_gate_live(live):
    assert _main("--max-run-hours", "60", "--allow-over-budget") == 0
    assert len(live.creates()) == 12


def test_create_wave_is_gated_even_when_called_directly(live):
    ev = lf.budget_spec
    over = ev.Projection(files=1, full_files=0, shards=96, vms=12, vcpus_per_vm=8, cpu_hours=1.0,
                         wall_hours=1.0, vm_hours=700.0, max_run_hours=3, worst_vm_hours=700.0)
    with pytest.raises(ev.BudgetRefused):
        lf.create_wave([["gcloud", "compute", "instances", "create", "x"]], over, profile=lf.V3)
    assert live.calls == []


def test_no_code_path_creates_a_vm_outside_create_wave():
    src = (V3 / "launch_fleet.py").read_text()
    assert src.count("check=True") == 1 and "def create_wave" in src
    body = src.split("def create_wave")[1].split("\ndef ")[0]
    assert "check=True" in body and "enforce_budget" in body


# ----------------------------------------------------------------- a wave refuses without a shared verdict cache

@pytest.mark.parametrize("meta", [[], ["v4-cache-uri="], ["v4-cache-uri=/var/cache"],
                                  ["v4-cache-uri=gs://b/p", "v4-cache-uri=gs://b/q"]],
                         ids=["absent", "blank", "directory", "twice"])
def test_v4_cache_problems_refuses_what_the_shards_would_not_share(meta):
    assert lf.v4_cache_problems(meta)


def test_v4_cache_problems_accepts_what_the_launcher_actually_emits():
    args = lf.build_parser().parse_args(["--profile", "v4", "--image", IMAGE, "--commit", HEAD,
                                         "--image-commit", HEAD, "--wave-type", "normal", "--wave-id", "w"])
    args.sources = ["abc_fail"]
    args.prefix = lf.PREFIX
    args.shards_total = 96
    assert lf.v4_cache_problems(lf.V4.extra_metadata(args)) == []


def test_a_dry_run_reaches_the_cache_refusal_and_creates_nothing(monkeypatch, capsys):
    # --dry-run runs no gcloud and live_checks is skipped; the cache refusal must not live only there
    monkeypatch.setattr(lf.wave_spec, "verdict_cache_uri", lambda prefix: "")
    assert _main("--profile", "v4", "--dry-run") == 1
    assert "REFUSE  verdict cache" in capsys.readouterr().err


# ------------------------------------------------------------------ image provenance: verified by label, recorded

def _v4_args():
    args = lf.build_parser().parse_args(["--profile", "v4", "--image", IMAGE, "--commit", HEAD,
                                         "--image-commit", HEAD, "--wave-type", "normal", "--wave-id", "w"])
    args.sources, args.prefix, args.shards_total, args.pool = ["abc_fail"], lf.PREFIX, 96, "pool.json"
    return args


def test_the_launcher_has_preflight_check_the_image_label_and_records_what_it_verified(monkeypatch):
    seen = {}

    class FakePf:
        @staticmethod
        def gcloud_runner():
            return None

        @staticmethod
        def locations_for_prefix(prefix, commit):
            return None

        @staticmethod
        def run_preflight(**kw):
            seen.update(kw)
            check = type("C", (), {"name": "image_commit_label", "ok": True, "caveat": "",
                                   "detail": f"verified: org.opencontainers.image.revision={HEAD}"})
            return type("R", (), {"checks": [check]})

    monkeypatch.setattr(lf, "_load", lambda name, path: FakePf)
    args = _v4_args()
    assert "v4-image-commit-link=unverified" in lf.V4.extra_metadata(args)  # nothing checked yet: says so
    problems, _notes = lf.V4.wave_preflight(args)
    assert problems == [] and seen["image_commit"] == HEAD
    assert f"v4-image-commit-link=verified: org.opencontainers.image.revision={HEAD}" in lf.V4.extra_metadata(args)


def test_a_failed_label_check_is_a_launch_problem_and_records_nothing_verified(monkeypatch):
    class FakePf:
        gcloud_runner = staticmethod(lambda: None)
        locations_for_prefix = staticmethod(lambda prefix, commit: None)

        @staticmethod
        def run_preflight(**kw):
            check = type("C", (), {"name": "image_commit_label", "ok": False, "caveat": "",
                                   "detail": "the image has no org.opencontainers.image.revision label"})
            return type("R", (), {"checks": [check]})

    monkeypatch.setattr(lf, "_load", lambda name, path: FakePf)
    args = _v4_args()
    problems, _ = lf.V4.wave_preflight(args)
    assert problems == ["preflight image_commit_label: the image has no org.opencontainers.image.revision label"]
    assert "v4-image-commit-link=unverified" in lf.V4.extra_metadata(args)

# ----------------------------------------------------------------- identity label overrides (reprobe-800)

def test_a_label_override_renames_the_vms_relabels_them_and_keeps_the_reaper(tmp_path):
    r = _v4("--dry-run", "--shards-total", "16", "--files", "800", "--max-run-hours", "4", "--label", "deployment-id=dev-crsv4",
            "--label", "purpose=v4-reprobe-800", "--label", "owner=lead-2", tmp_path=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    creates = _creates(r.stdout)
    assert len(creates) == 2
    for line in creates:
        assert " dev-crsv4-v4e-" in line and "dev-crsv3-v4e-" not in line
        labels = line.split("--labels=")[1].split()[0]
        assert "deployment-id=dev-crsv4" in labels and "purpose=v4-reprobe-800" in labels
        assert "owner=lead-2" in labels and "purpose=v4-eval" not in labels
        assert f"commit={HEAD[:12]}" in labels and "managed-by=v4-eval" in labels
        # the server-side reaper is on every VM, whatever the labels say
        assert "--instance-termination-action=DELETE" in line and "--max-run-duration=4h" in line


def test_a_derived_label_cannot_be_overridden(tmp_path):
    r = _v4("--dry-run", "--label", "commit=deadbeef", tmp_path=tmp_path)
    assert r.returncode == 2 and "derived" in r.stderr and not _creates(r.stdout)
    r = _v4("--dry-run", "--label", "Owner=Lead", tmp_path=tmp_path)
    assert r.returncode == 2 and "KEY=VALUE" in r.stderr


def test_label_overrides_are_v4_only(tmp_path):
    r = _launch("--image", IMAGE, "--commit", HEAD, "--image-commit", HEAD, "--skip-preflight", "--dry-run",
                "--label", "owner=x", tmp_path=tmp_path)
    assert r.returncode == 2 and "--label" in r.stderr
