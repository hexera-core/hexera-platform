# Responsibility: Hold the v3 eval's per-file artefacts to their path: repair_corpus writes repaired
#                 STEPs where REPAIR_CORPUS_OUT says (unchanged default otherwise), run_arm points
#                 it inside the shard's output directory (the only directory that leaves the VM),
#                 and the collector's pull leaves those artefacts in the bucket.
# Boundaries: hermetic. No gcloud, no docker, no CAD: run_arm.main runs against a stub
#             repair_corpus, and the pull is checked as an argv plus its exclude pattern.
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
V3 = REPO / "devtools" / "validation" / "v3_eval"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load("v3_artefacts_rc_under_test", REPO / "devtools" / "validation" / "repair_corpus.py")


# ------------------------------------------------------------------ (a) repair_corpus

def test_scratch_dir_defaults_to_the_old_path(monkeypatch):
    monkeypatch.delenv(rc.SCRATCH_ENV, raising=False)
    assert rc.scratch_dir() == Path(tempfile.gettempdir()) / "repair_corpus_out"


def test_scratch_dir_follows_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv(rc.SCRATCH_ENV, str(tmp_path / "artefacts"))
    assert rc.scratch_dir() == tmp_path / "artefacts"


def test_a_blank_env_is_the_default_not_the_cwd(monkeypatch):
    monkeypatch.setenv(rc.SCRATCH_ENV, "   ")
    assert rc.scratch_dir() == Path(tempfile.gettempdir()) / "repair_corpus_out"


# ------------------------------------------------------------------ (b) run_arm

def test_run_arm_points_repaired_files_inside_the_shard_output(monkeypatch, tmp_path):
    run_arm = _load("v3_artefacts_run_arm_under_test", V3 / "run_arm.py")
    out = tmp_path / "out"
    seen: dict = {}

    def fake_main():
        seen["scratch"] = rc_stub.scratch_dir()
        return 0

    rc_stub = types.SimpleNamespace(
        SCRATCH_ENV=rc.SCRATCH_ENV, operation_catalogue=lambda: [], main=fake_main,
        scratch_dir=rc.scratch_dir)

    class _Sink:
        def totals(self):
            return 0, 0.0

    # run_arm sets os.environ itself; registering the key first makes monkeypatch restore it.
    monkeypatch.setenv(rc.SCRATCH_ENV, "")
    monkeypatch.delenv(rc.SCRATCH_ENV)
    monkeypatch.setenv(run_arm.evalspec.MODEL_KEY_ENV, "test-key")
    monkeypatch.setattr(run_arm, "env_drift", lambda env: [])
    monkeypatch.setattr(run_arm, "settings_problems", lambda env, accept: ([], {}))
    monkeypatch.setattr(run_arm, "ftetwild", lambda: "/usr/bin/fTetWild")
    monkeypatch.setattr(run_arm, "failing_sample_sha", lambda root: ("0" * 64, 120))
    monkeypatch.setattr(run_arm, "install_model", lambda o: _Sink())
    monkeypatch.setattr(run_arm, "_load", lambda name, path: rc_stub)
    monkeypatch.setattr(run_arm.evalspec, "harness_argv", lambda *a, **k: [])

    code = run_arm.main(["--arm", "t29_deterministic", "--kind", "failing", "--shard", "1/21",
                         "--inputs", str(tmp_path), "--out", str(out),
                         "--code-root", str(REPO)])

    assert code == 0
    assert seen["scratch"] == out / run_arm.ARTEFACTS_SUBDIR
    assert run_arm.artefacts_dir(out) == out / "artefacts"
    prov = (out / "PROVENANCE.json").read_text()
    assert str(out / "artefacts") in prov


# ------------------------------------------------------------------ (c) collect

def test_collect_pull_excludes_the_artefacts():
    collect = _load("v3_artefacts_collect_under_test", V3 / "collect.py")
    cmd = collect.pull_command("gs://b/eval/v3_full-abc123def456", Path("/tmp/x/v3_full"))
    assert cmd[:4] == ["gcloud", "storage", "rsync", "-r"]
    assert cmd[cmd.index("-x") + 1] == collect.PULL_EXCLUDE
    assert cmd[-2:] == ["gs://b/eval/v3_full-abc123def456", "/tmp/x/v3_full"]

    skipped = ["failing/shard-3-of-32/artefacts/0060_00608290-repaired.step",
               "failing/shard-3-of-32/artefacts/0060_00608290.mesh-hints.json",
               "clean/shard-1-of-7/artefacts/sub/deeper.step"]
    kept = ["failing/shard-3-of-32/rows.json", "failing/shard-3-of-32/DONE",
            "failing/shard-3-of-32/bundle/rows.jsonl", "failing/shard-3-of-32/PROVENANCE.json",
            "failing/shard-3-of-32/transcripts.jsonl", "failing/shard-3-of-32/not_artefacts.json"]
    pattern = re.compile(collect.PULL_EXCLUDE)
    for path in skipped:
        assert pattern.match(path) and pattern.search(path), path
    for path in kept:
        assert not pattern.match(path) and not pattern.search(path), path
