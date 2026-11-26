"""wave.py: the chain preflight -> stage -> launch -> watch -> collect -> triage -> report, as one command.

The refusals are tested directly. The chain is tested END TO END on 3 files against LocalBackend: a directory stands in
for the bucket, and each "VM" is a real `run_arm.py --mode arms` process (the argv vm_startup.sh builds) whose network
edges only are replaced - the marker store is a directory, the verdict cache and kernel pool are unused, and the per-file arm runner
returns a fixed row instead of meshing. One shard is made to FAIL once, so the watch's relaunch is exercised; triage.py
and report.py then run on what the shards wrote, checked against the cost-balanced shard plan.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

V4 = Path(__file__).resolve().parents[3] / "devtools" / "validation" / "v4_eval"
REPO = V4.parents[2]
sys.path.insert(0, str(V4))
import wave  # noqa: E402

import run_arm  # noqa: E402

evalspec, rp, pool_mod = run_arm.evalspec, run_arm.reprobe_mod, run_arm.pool_mod
HEAD = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
IMAGE = "us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh@sha256:" + "a" * 64
# the real bucket's own prefix: `preflight_argv` refuses a prefix that is not `<BUCKET>/eval/<name>` (it has to
# name the wave to preflight), and no test here reaches the bucket
PREFIX = wave.wave_spec.V4_PREFIX


def _args(**kw) -> wave.WaveArgs:
    base = {"kind": "reprobe", "wave_id": "reprobe-test-1", "commit": HEAD, "image": IMAGE, "image_commit": HEAD, "shards": 96,
                "max_run_hours": 12.0, "zone": "us-central1-b", "prefix": PREFIX, "pool": pool_mod.OUT}
    base.update(kw)
    return wave.WaveArgs(**base)


def _ok(_sha: str) -> bool:
    return True


# ------------------------------------------------------------------ the wave's own refusals

def test_the_reprobe_wave_passes_its_own_refusals() -> None:
    assert wave.own_refusals(_args(), commit_exists=_ok) == []


def test_a_wall_beyond_the_vm_backstop_is_refused() -> None:
    reasons = wave.own_refusals(_args(max_run_hours=1.0), commit_exists=_ok)
    assert any("budget" in r and "deleted" in r for r in reasons)


def test_heldout_needs_its_frozen_commit_and_runs_exactly_it(tmp_path: Path) -> None:
    split = tmp_path / "heldout.json"
    split.write_text(json.dumps({"split": "heldout", "files": []}))
    kw = {"kind": "heldout", "wave_id": "heldout-1", "split": split, "model_usd_cap": 250.0, "model_secret": "s", "shards": 8}
    assert any("frozen-commit" in r for r in wave.own_refusals(_args(**kw), commit_exists=_ok))
    other = "b" * 40
    assert any("not --commit" in r for r in wave.own_refusals(_args(frozen_commit=other, **kw), commit_exists=_ok))
    assert any("not in this repository" in r
               for r in wave.own_refusals(_args(frozen_commit=HEAD, **kw), commit_exists=lambda s: False))
    assert not [r for r in wave.own_refusals(_args(frozen_commit=HEAD, **kw), commit_exists=_ok)
                if "frozen" in r or "commit" in r]


def test_a_model_wave_needs_the_cap_its_ledger_enforces(tmp_path: Path) -> None:
    split = tmp_path / "dev.json"
    split.write_text(json.dumps({"split": "dev", "files": []}))
    kw = {"kind": "dev", "wave": "dev_search", "wave_id": "dev-search-1", "split": split, "model_secret": "s", "shards": 8}
    assert any("--model-usd-cap is required" in r for r in wave.own_refusals(_args(**kw), commit_exists=_ok))
    assert any("not enforced" in r for r in wave.own_refusals(_args(model_usd_cap=20.0, **kw), commit_exists=_ok))
    assert not [r for r in wave.own_refusals(_args(model_usd_cap=50.0, **kw), commit_exists=_ok) if "cap" in r]
    no_key = dict(kw, model_secret=None)
    assert any("--model-secret" in r for r in wave.own_refusals(_args(model_usd_cap=50.0, **no_key), commit_exists=_ok))


def test_the_launch_carries_the_shard_plan_and_the_wave() -> None:
    argv = wave.launcher_argv(_args(), dry_run=True)
    assert argv[argv.index("--shard-plan-uri") + 1] == f"{PREFIX}/shard-plans/reprobe-test-1.json"
    assert "--dry-run" in argv and argv[argv.index("--wave-type") + 1] == "normal"
    only = wave.launcher_argv(_args(), dry_run=False, only=["reprobe:pool:3/96"])
    assert only[only.index("--only") + 1] == "reprobe:pool:3/96" and "--dry-run" not in only


# ------------------------------------------------------------------ the chain, end to end, on 3 files

SHIM = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import run_arm as ra
from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

markers = sys.argv[2]
ra.make_marker_store = lambda bucket: (FileMarkerStore(markers), f"gcs/{bucket}")
ra.check_marker_binding = lambda m, backend, bucket: None   # the probe record is the bucket's; here a directory
ra.make_cache = lambda uri: None                            # the fake runner never judges
import meshpipeline.cad.repair_v4.worker_pool as wp          # nor meshes: no kernel worker on a laptop

class _NoKernel:
    def __init__(self, *a, **k): pass
    def close(self): pass

wp.WorkerPool = _NoKernel

def runner(entry, arm, source):
    row = {"name": entry.name, "sha256": entry.sha256, "source": entry.source, "arm": arm, "inconclusive": False,
           "model_usd": 0.0, "wall_s": 0.01, "in_clean": False, "in_panel_passes": 1, "in_valid": True,
           "in_faithful_ok": True, "out_valid": True, "out_faithful_ok": True,
           "out_panel_passes": 3 if arm == "staged" else 1, "panel_size": 3, "out_tier": "full",
           "out_clean": arm == "staged"}  # consistent with report's own clean = valid and faithful and panel all-pass
    return row, None

ra.ARM_RUNNER = runner
sys.exit(ra.main(sys.argv[3:]))
'''


def _sha(i: int) -> str:
    return hashlib.sha256(f"wave-e2e-{i}".encode()).hexdigest()


def test_the_whole_chain_runs_on_three_files_and_relaunches_a_failed_shard(tmp_path: Path) -> None:
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore

    root = tmp_path / "bucket"
    entries = [pool_mod.PoolEntry(f"abc/part{i}.step", "abc_fail", _sha(i), "u", None, "lic") for i in range(3)]
    pool = tmp_path / "pool.json"
    pool_mod.write_pool(entries, pool, sources_included=["abc"])
    inputs = root / PREFIX[len("gs://"):] / "inputs"
    for i, e in enumerate(entries):  # sizes differ, so the plan has a largest file to place first
        (inputs / e.sha256).mkdir(parents=True)
        (inputs / e.sha256 / Path(e.name).name).write_bytes(b"x" * (1000 * (i + 1) ** 3))
    rows = [rp.ProbeRow(e.name, e.sha256, e.source, {"tier": "full", "clean": False}, "panel", "low_quality")
            for e in entries]
    split = tmp_path / "dev.json"
    split.write_text(json.dumps({"split": "dev", "files": [asdict(r) for r in rows]}))
    shim = tmp_path / "shim.py"
    shim.write_text(SHIM)
    markers = tmp_path / "markers"
    w = wave.WaveArgs(kind="dev", wave="dev_deterministic", wave_id="dev-e2e-1", commit=HEAD, image=IMAGE,
                      image_commit=HEAD, shards=2, max_run_hours=3.0, zone="us-central1-b", prefix=PREFIX,
                      pool=pool, split=split)
    plan = evalspec.plan_wave(w.wave_def, wave_id=w.wave_id, n_shards=2, markers=FileMarkerStore(markers))

    def shard_cmd(wa: wave.WaveArgs, unit: str, out: Path) -> list[str]:  # the argv vm_startup.sh builds
        backend_path = backend.path
        return [sys.executable, str(shim), str(V4), str(markers),
                "--mode", "arms", "--wave", wa.wave_def.name, "--arms", ",".join(wa.wave_def.arms),
                "--split", "dev", "--split-file", str(backend_path(wa.split_uri)), *plan.provenance_argv(),
                "--shard-plan", str(backend_path(wa.plan_uri)), "--shard", unit.rsplit(":", 1)[1],
                "--inputs", str(inputs), "--out", str(out), "--pool", str(pool),
                "--cache-uri", f"{PREFIX}/verdict-cache", "--sources", "abc_fail", "--commit", wa.commit,
                "--image", wa.image, "--machine-type", wa.machine_type, "--wave-type", wa.wave_type,
                "--wave-id", wa.wave_id, "--n-shards", str(wa.shards),
                "--ledger-path", str(out.parent / f"ledger-{out.name}.jsonl"), "--marker-bucket", "dev-crsv3-fake"]

    backend = wave.LocalBackend(root, shard_cmd, fail_once=["arms_dev_e2e_1:pool:2/2"])
    wave_dir = tmp_path / "wave"
    w_obj = wave.Wave(w, backend, wave_dir, poll_s=0.0, sleep=lambda s: None)
    rc = w_obj.run()

    steps = [json.loads(line) for line in (wave_dir / "steps.jsonl").read_text().splitlines()]
    seen = [(s["step"], s["status"]) for s in steps]
    assert [s for s, _ in seen if s != "watch"] == ["preflight", "stage", "launch", "collect", "triage", "report"]
    assert ("watch", "relaunch") in seen and ("watch", "ok") in seen
    assert backend.launches[1] == ["arms_dev_e2e_1:pool:2/2"]  # only the failed shard was relaunched
    assert rc in (0, 2), (wave_dir / "report.log").read_text()
    report = json.loads((wave_dir / "report.json").read_text())
    assert report["status"] in ("OK", "UNRELIABLE") and (wave_dir / "triage.json").is_file()
    plan_doc = json.loads((wave_dir / "shard-plan.json").read_text())
    assert set(plan_doc["assign"]) == {e.sha256 for e in entries}
    for i in (1, 2):  # every shard was cut from the plan and says so
        prov = json.loads((wave_dir / "shards" / f"shard-{i}-of-2" / "PROVENANCE.json").read_text())
        assert prov["shard_plan"] == evalspec.plan_digest(plan_doc) and prov["exit_code"] == 0
    mirrored = backend.path(f"{PREFIX}/waves/dev-e2e-1")
    assert (mirrored / "steps.jsonl").is_file() and (mirrored / "report.json").is_file()


def test_a_dry_run_runs_every_gate_and_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "bucket"
    allowed, _ = rp.split_allowed(pool_mod.read_pool(pool_mod.OUT))
    inputs = root / PREFIX[len("gs://"):] / "inputs"
    for e in allowed[:5]:
        (inputs / e.sha256).mkdir(parents=True)
        (inputs / e.sha256 / "f.step").write_bytes(b"x")
    backend = wave.LocalBackend(root, lambda *a: pytest.fail("a dry run ran a shard"))
    w = wave.Wave(_args(), backend, tmp_path / "wave", dry_run=True)
    with pytest.raises(wave.WaveRefused, match="not staged"):  # only 5 of 10,887 staged: refused before any launch
        w.run()
    assert backend.launches == []


# ------------------------------------------------------------------ image provenance (Lane A job 3)

def test_the_wave_refuses_an_abbreviated_image_commit_before_any_cloud_call() -> None:
    reasons = wave.own_refusals(_args(image_commit=HEAD[:12]), commit_exists=_ok)
    assert any("--image-commit" in r and "40-hex" in r for r in reasons)
    assert not any("--image-commit" in r for r in wave.own_refusals(_args(image_commit=HEAD), commit_exists=_ok))


def test_the_waves_preflight_hands_preflight_the_image_commit_to_verify(monkeypatch) -> None:
    seen: list = []
    backend = wave.GcsBackend.__new__(wave.GcsBackend)
    monkeypatch.setattr(backend, "run", lambda argv, timeout=None: seen.append(list(argv)) or wave.Result(0, "", ""),
                        raising=False)
    backend.preflight(_args(image_commit=HEAD))
    argv = seen[0]
    assert argv[argv.index("--image-commit") + 1] == HEAD

def test_labels_reach_the_launcher_and_the_watch_filters_on_the_purpose() -> None:
    w = _args(labels=("deployment-id=dev-crsv4", "purpose=v4-reprobe-800", "owner=lead-2"))
    argv = wave.launcher_argv(w, dry_run=True)
    given = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
    assert given == ["deployment-id=dev-crsv4", "purpose=v4-reprobe-800", "owner=lead-2"]
    assert w.purpose == "v4-reprobe-800" and _args().purpose == "v4-eval"
    assert "--label" not in wave.launcher_argv(_args(), dry_run=True)


def test_preflight_checks_the_wave_s_own_prefix() -> None:
    sub = _args(prefix=f"{wave.wave_spec.BUCKET}/eval/v4-reprobe-800")
    argv = wave.preflight_argv(sub)
    assert argv[argv.index("--name") + 1] == "v4-reprobe-800"
    with pytest.raises(wave.WaveRefused):
        wave.preflight_argv(_args(prefix=f"{wave.wave_spec.BUCKET}/eval/a/b"))
