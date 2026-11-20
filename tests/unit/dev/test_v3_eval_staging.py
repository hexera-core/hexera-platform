# Responsibility: Hold the CRS v3 verdict staging (T19/T29) to its refusals: arms that do not share
#                 one sample or one commit are not compared, an n2 machine is not launched, a flag
#                 the build does not read is caught before a fleet is paid for, and the 4x rule's
#                 degenerate case is reported as undefined rather than as met.
# Boundaries: hermetic. No gcloud, no docker, no CAD: the launcher is driven in --dry-run with gcloud
#             off PATH, and the collector is fed shard directories written here.
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
V3 = REPO / "devtools" / "validation" / "v3_eval"
MANIFEST = REPO / "devtools" / "validation" / "corpora" / "mesh-failure-real.json"
CLEAN_SAMPLES = (REPO / "devtools" / "validation" / "evidence" / "2026-11-19-corpus-harvest"
                 / "clean-samples.json")
CLEAN_MANIFEST = REPO / "devtools" / "validation" / "corpora" / "clean-control-100.json"
VERDICT = REPO / "devtools" / "validation" / "evidence" / "2026-11-20-crs-v3-verdict"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


spec = _load("v3_evalspec_under_test", V3 / "evalspec.py")


# ------------------------------------------------------------------ the launcher

def test_launcher_refuses_the_n2_family():
    for bad in ("n2-highcpu-8", "n2-standard-4", "n2d-highcpu-8"):
        with pytest.raises(ValueError, match="quota"):
            spec.validate_machine_type(bad)
    for bad in ("e2-standard-4", "c2d", "nonsense"):
        with pytest.raises(ValueError):
            spec.validate_machine_type(bad)
    assert spec.validate_machine_type("c2d-highcpu-4") == "c2d-highcpu-4"
    assert spec.validate_machine_type("c3-highcpu-8") == "c3-highcpu-8"


def _launch(*argv: str, tmp_path: Path):
    # gcloud is taken OFF the PATH: a dry run that reached for it would fail here, not launch.
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, str(V3 / "launch_fleet.py"), *argv],
                          capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)


COMMIT = "812ae022" + "0" * 32
IMAGE = "us-central1-docker.pkg.dev/hexera-dev/mesh/mesh@sha256:" + "a" * 64


def test_launcher_cli_refuses_n2_before_planning_anything(tmp_path):
    r = _launch("--arm", "deterministic", "--kind", "failing", "--image", IMAGE,
                "--commit", COMMIT, "--shards", "4", "--machine-type", "n2-highcpu-8",
                "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "n2" in r.stderr and "gcloud compute instances create" not in r.stdout


def test_launcher_refuses_an_unpinned_image_and_a_short_commit(tmp_path):
    r = _launch("--arm", "deterministic", "--kind", "failing", "--image", "repo/mesh:latest",
                "--commit", COMMIT, "--shards", "2", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "digest" in r.stderr
    r = _launch("--arm", "deterministic", "--kind", "failing", "--image", IMAGE,
                "--commit", "812ae022", "--shards", "2", "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 2 and "40" in r.stderr


def test_launcher_dry_run_prints_labelled_self_deleting_vms(tmp_path):
    r = _launch("--arm", "v3-full", "--kind", "failing", "--image", IMAGE, "--commit", COMMIT,
                "--shards", "3", "--dry-run", "--skip-preflight", tmp_path=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    creates = [ln for ln in r.stdout.splitlines() if "gcloud compute instances create" in ln]
    assert len(creates) == 3
    for i, line in enumerate(creates, start=1):
        assert f"{i}of3" in line
        assert "deployment-id=dev-crsv3" in line and "purpose=v3-eval" in line
        assert "arm=v3-full" in line
        assert "--instance-termination-action=DELETE" in line
        assert "--max-run-duration=" in line
        assert "--machine-type=c2d-highcpu-4" in line
        assert "n2-" not in line
        # results land under the detached root eval/<arm>-<commit12>/ (collectable from GCS alone)
        assert "v3-results=gs://dev-crsv3-transfer-224734058693/eval/v3-full-812ae0220000" in line
    # the secret is named, never valued
    assert "openai-api-key" in r.stdout and "OPENAI_API_KEY=" not in r.stdout


# ------------------------------------------------------------------ the sample

def test_sample_body_is_byte_for_byte_repair_corpus_writer(tmp_path):
    rc = _load("v3_rc_under_test", REPO / "devtools" / "validation" / "repair_corpus.py")
    manifest = json.loads(MANIFEST.read_text())
    entries = rc.stratified_sample(manifest, spec.SAMPLE_SIZE, seed=spec.SEED)
    written = rc.write_bundle_sample(tmp_path, entries, seed=spec.SEED)
    assert written.read_bytes() == spec.sample_body(entries, seed=spec.SEED)
    assert len(entries) == 120


def test_the_committed_sample_is_the_sampler_s_sample():
    rc = _load("v3_rc_under_test2", REPO / "devtools" / "validation" / "repair_corpus.py")
    manifest = json.loads(MANIFEST.read_text())
    entries = rc.stratified_sample(manifest, spec.SAMPLE_SIZE, seed=spec.SEED)
    assert (VERDICT / "sample.json").read_bytes() == spec.sample_body(entries, seed=spec.SEED)


def _prov(label, *, kind="failing", commit=COMMIT, image=IMAGE, sample="s" * 64):
    return {"label": label, "kind": kind, "commit": commit, "image": image,
            "sample_sha256": sample}


def test_arms_on_different_samples_are_refused():
    ok = spec.check_comparable([_prov("det/1"), _prov("v3/1")])
    assert ok["samples"]["failing"] == "s" * 64
    with pytest.raises(ValueError, match="failing sample"):
        spec.check_comparable([_prov("det/1"), _prov("v3/1", sample="t" * 64)])
    # the clean control is its own sample: differing from the failing one is fine
    spec.check_comparable([_prov("det/1"), _prov("det/c", kind="clean", sample="c" * 64)])


def test_mixed_commits_and_images_are_refused():
    with pytest.raises(ValueError, match="commit"):
        spec.check_comparable([_prov("det/1"), _prov("v3/1", commit="f" * 40)])
    with pytest.raises(ValueError, match="image"):
        spec.check_comparable([_prov("det/1"), _prov("v3/1", image=IMAGE[:-1] + "b")])
    with pytest.raises(ValueError, match="states no"):
        spec.check_comparable([_prov("det/1"), _prov("v3/1", commit="")])


# ------------------------------------------------------------------ the collector

def _shard(root: Path, arm: str, kind: str, i: int, n: int, *, commit=COMMIT, rows=None):
    d = root / arm / kind / f"shard-{i}-of-{n}"
    d.mkdir(parents=True)
    prov = {**_prov(f"{arm}/{kind}/{i}", kind=kind, commit=commit,
                    sample=("s" if kind == "failing" else "c") * 64),
            "arm": arm, "shard": f"{i}/{n}", "exit_code": 0, "machine_type": "c2d-highcpu-4",
            "started_at": "2026-11-20T00:00:00Z", "ended_at": "2026-11-20T00:30:00Z",
            "wall_seconds": 1800.0, "operations_catalogue": []}
    (d / "PROVENANCE.json").write_text(json.dumps(prov))
    (d / "rows.json").write_text(json.dumps(rows if rows is not None else [
        {"file": f"{arm}-{kind}-{i}.step", "outcome": "nothing_to_repair",
         "meshes_before": False, "meshes_after": None, "mesh_kind_before": "other",
         "mesh_kind_after": ""}]))
    (d / "DONE").write_text("0\n")
    return d


def test_collector_refuses_mixed_commits(tmp_path):
    collect = _load("v3_collect_under_test", V3 / "collect.py")
    _shard(tmp_path, "deterministic", "failing", 1, 1)
    _shard(tmp_path, "v3-full", "failing", 1, 1, commit="f" * 40)
    with pytest.raises(ValueError, match="commit"):
        collect.load_runs(tmp_path)


def test_collector_refuses_a_missing_shard(tmp_path):
    collect = _load("v3_collect_under_test2", V3 / "collect.py")
    _shard(tmp_path, "deterministic", "failing", 1, 2)
    with pytest.raises(ValueError, match="2/2"):
        collect.load_runs(tmp_path)


def test_collector_merges_shards_through_repair_corpus_aggregate(tmp_path):
    collect = _load("v3_collect_under_test3", V3 / "collect.py")
    _shard(tmp_path, "deterministic", "failing", 1, 2)
    _shard(tmp_path, "deterministic", "failing", 2, 2)
    runs = collect.load_runs(tmp_path)
    merged = runs["checked"]
    assert merged["commit"] == COMMIT
    rows = runs["arms"][("deterministic", "failing")]["rows"]
    assert len(rows) == 2
    summary = collect.summarise(rows)
    assert summary["meshing"]["files"] == 2 and summary["meshing"]["newly_broken"] == 0


# ------------------------------------------------------------------ the posture and the verdict

def test_preflight_catches_a_flag_the_build_does_not_read():
    env = spec.ARMS["v3-full"]["env"]
    settings = {k: v for k, v in env.items() if k.startswith("CAD_REPAIR_")}
    settings["CAD_REPAIR_ENABLED"] = True
    assert spec.preflight(settings, env) == []
    missing = {k: v for k, v in settings.items()
               if k not in ("CAD_REPAIR_TOURNAMENT", "CAD_REPAIR_TOURNAMENT_K")}
    problems = spec.preflight(missing, env)
    assert any("INERT CAD_REPAIR_TOURNAMENT=live" in p for p in problems)
    # asking for exactly what the code falls back to is harmless when the row is absent
    det = spec.ARMS["deterministic"]["env"]
    det_settings = {k: v for k, v in det.items()
                    if k.startswith("CAD_REPAIR_") and k not in ("CAD_REPAIR_TOURNAMENT",
                                                                 "CAD_REPAIR_MEMORY")}
    assert [p for p in spec.preflight(det_settings, det) if "TOURNAMENT" in p] == []
    # a fail-closed posture that read "off" is a mismatch, not a pass
    off = {**settings, "CAD_REPAIR_INVESTIGATOR": "off"}
    assert any("MISMATCH CAD_REPAIR_INVESTIGATOR" in p for p in spec.preflight(off, env))


def test_arms_differ_only_by_investigator_and_tournament():
    base = {k: v for k, v in spec.ARMS["deterministic"]["env"].items()
            if not k.startswith("CAD_REPAIR_TOURNAMENT") and k != "CAD_REPAIR_INVESTIGATOR"}
    for name, a in spec.ARMS.items():
        rest = {k: v for k, v in a["env"].items()
                if not k.startswith("CAD_REPAIR_TOURNAMENT") and k != "CAD_REPAIR_INVESTIGATOR"}
        assert rest == base, name
    assert spec.ARMS["v3-full"]["env"]["CAD_REPAIR_TOURNAMENT_K"] == "3"


def test_t29_degenerate_ratio_is_undefined_not_met():
    zero = {"newly_meshed": 0, "newly_broken": 0}
    out = spec.t29_verdict(zero, {"newly_meshed": 5, "newly_broken": 0},
                           clean_untouched=True, cost_per_fixed_part=0.4)
    assert out["ratio"] is None and "undefined" in out["ratio_rule"] and "= 5" in out["ratio_rule"]
    assert out["accepted"] is True
    out = spec.t29_verdict({"newly_meshed": 2, "newly_broken": 0},
                           {"newly_meshed": 7, "newly_broken": 0},
                           clean_untouched=True, cost_per_fixed_part=0.4)
    assert out["ratio_ok"] is False and out["accepted"] is False
    out = spec.t29_verdict({"newly_meshed": 2, "newly_broken": 0},
                           {"newly_meshed": 8, "newly_broken": 1},
                           clean_untouched=True, cost_per_fixed_part=0.4)
    assert out["ratio_ok"] is True and out["accepted"] is False


# ------------------------------------------------------------------ the clean control

def test_clean_control_manifest_is_100_seeded_passing_parts_with_sources():
    clean = _load("v3_clean_under_test", V3 / "clean_control.py")
    pool = json.loads(CLEAN_SAMPLES.read_text())
    committed = json.loads(CLEAN_MANIFEST.read_text())
    rebuilt = clean.build_manifest(pool, json.loads(MANIFEST.read_text()), n=100, seed=spec.SEED,
                                   metadata={})
    # the draw is the seed's alone: the committed list is exactly what the builder draws today
    # (the probe metadata beside each entry came from the harvest's local results and is not
    # re-derivable here, so only the identity fields are compared)
    ident = ("name", "sha256", "chunk", "archive_url", "archive_sha256")
    assert [{k: f[k] for k in ident} for f in committed["files"]] == \
        [{k: f[k] for k in ident} for f in rebuilt["files"]]
    assert committed["seed"] == spec.SEED
    files = committed["files"]
    assert len(files) == 100 and len({f["sha256"] for f in files}) == 100
    known = {e["sha256"]: (chunk, e["name"]) for chunk, es in pool.items() for e in es}
    for f in files:
        assert known[f["sha256"]] == (f["chunk"], f["name"])
        assert f["archive_url"].startswith("https://archive.nyu.edu/retrieve/")
        assert len(f["archive_sha256"]) == 64
    assert not any(k in f for f in files for k in ("step", "brep", "bytes_b64"))
