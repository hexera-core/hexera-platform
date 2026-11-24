"""v4_eval collect: the pull flow. Hermetic - the bucket is a dict behind an injected Transport; no
test may reach GCS, gcloud or the network, and an autouse guard makes any accidental attempt fail."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

V4 = Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"
sys.path.insert(0, str(V4))
import run_arm  # noqa: E402  (loads evalspec / reprobe / pool under stable module names)

rp = run_arm.reprobe_mod
evalspec = run_arm.evalspec
pool_mod = run_arm.pool_mod


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"v4c_{name}", V4 / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"v4c_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


collect = _load("collect")
URI = "gs://fake-bucket/eval/reprobe-abc123def456"
N = 3


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"a test reached a subprocess/network: {a!r}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _sha(i: int) -> str:
    return f"{i:064x}"


def _row(i, layer, family, source="abc_fail", tier="cheap", clean=False):
    summary = {"tier": tier, "clean": clean, "counts_for_scoreboard": True,
               "hard": [family] if layer == "valid" else []}
    return rp.ProbeRow(f"f{i}.step", _sha(i), source, summary, layer, family, None)


def _population() -> list:
    rows = [_row(i, "panel", "plc_intersection") for i in range(1, 61)]
    rows += [_row(i, None, "", "abc_pass", tier="full", clean=True) for i in range(100, 130)]
    return rows


class FakeTransport:
    """A bucket in a dict. Records every call so a test can say what was (not) fetched."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.fetched: list[str] = []
        self.listed: list[str] = []

    def ls(self, uri: str) -> list[str]:
        self.listed.append(uri)
        return sorted(k for k in self.objects if k.startswith(uri))

    def fetch(self, uri: str, dest: Path) -> None:
        self.fetched.append(uri)
        dest.write_bytes(self.objects[uri])


def _put_shard(bucket: dict, i: int, n: int, rows, *, exit_code=0, commit="c" * 40, image="img@sha256:1",
               excluded=(), done=True, failed=False, skip=(), wall=600.0) -> None:
    base = f"{URI}/shard-{i}-of-{n}/"
    cheap = "".join(json.dumps(asdict(r), sort_keys=True) + "\n" for r in rows if r.verdict_summary["tier"] == "cheap")
    full = [r for r in rows if r.verdict_summary["tier"] == "full"]
    files = {
        "rows.json": json.dumps([asdict(r) for r in rows]),
        "cheap.jsonl": cheap,
        "EXCLUDED.json": json.dumps([asdict(x) for x in excluded]),
        "PROVENANCE.json": json.dumps({
            "exit_code": exit_code, "commit": commit, "image": image, "shard": f"{i}/{n}",
            "shard_files": len(rows) + len(excluded), "rows": len(rows), "cheap_rows": cheap.count("\n"),
            "full_rows": len(full), "excluded": len(excluded), "wall_seconds": wall,
            # the walls the shard ran with, as run_arm records them
            "file_timeout_s": 900.0, "check_valid_timeout_s": 300.0,
            "machine_type": "c2d-standard-8"}),
        "run.log": "noise",
    }
    for name, body in files.items():
        if name not in skip:
            bucket[base + name] = body.encode()
    if done:
        bucket[base + "DONE"] = b""
    if failed:
        bucket[base + "FAILED"] = b""


def _wave(n: int = N, rows=None, **shard_kw) -> tuple[dict, list]:
    rows = _population() if rows is None else rows
    per: dict[int, list] = {i: [] for i in range(1, n + 1)}
    for r in rows:
        for i in range(1, n + 1):
            if evalspec.shard_of(r.sha256, i, n):
                per[i].append(r)
    bucket: dict[str, bytes] = {}
    for i in range(1, n + 1):
        _put_shard(bucket, i, n, per[i], **shard_kw)
    return bucket, rows


def _pool(tmp_path: Path, rows, extra_shas=()) -> Path:
    entries = [pool_mod.PoolEntry(r.name, r.source, r.sha256, "https://example.invalid/x", None, "lic") for r in rows]
    entries += [pool_mod.PoolEntry(f"x{s}.step", "abc_fail", _sha(s), "https://example.invalid/x", None, "lic")
                for s in extra_shas]
    path = tmp_path / "pool.json"
    pool_mod.write_pool(entries, path, sources_included=sorted({e.source for e in entries}))
    return path


def _args(tmp_path: Path, pool: Path, *extra: str) -> list[str]:
    return ["--pull", URI, "--to", str(tmp_path / "wave"), "--shards", str(N), "--pool", str(pool),
            "--out-dir", str(tmp_path / "out"), "--dev", "10", "--heldout", "10", "--clean", "10", *extra]


def _run(tmp_path, bucket, *extra, rows=None, extra_pool=()):
    pool = _pool(tmp_path, rows if rows is not None else _population(), extra_pool)
    t = FakeTransport(bucket)
    return collect.main(_args(tmp_path, pool, *extra), transport=t), t


# ------------------------------------------------------------------ the healthy wave

def test_pull_merges_a_complete_wave_and_writes_splits_and_report(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    rc, t = _run(tmp_path, bucket)
    assert rc == 0, capsys.readouterr().err
    out = tmp_path / "out"
    for name in ("dev", "heldout", "clean"):
        assert (out / f"{name}.json").is_file()
    wave = tmp_path / "wave"
    assert sorted(p.name for p in wave.glob("shard-*")) == [f"shard-{i}-of-{N}" for i in range(1, N + 1)]
    rep = json.loads((wave / "collect-report.json").read_text())
    assert rep["commit"] == "c" * 40 and rep["image"] == "img@sha256:1" and rep["shards"] == N
    assert rep["source"] == URI and rep["report"]["probed_conclusive"] == 90
    assert not list(wave.rglob("*.partial"))


def test_pull_fetches_only_what_collect_needs(tmp_path) -> None:
    bucket, _ = _wave()
    _, t = _run(tmp_path, bucket)
    assert not any(u.endswith("run.log") for u in t.fetched)
    assert all(u.startswith(URI) for u in t.fetched)  # reads only under the wave prefix


def test_dry_run_lists_but_fetches_and_writes_nothing(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    rc, t = _run(tmp_path, bucket, "--dry-run")
    assert rc == 0 and t.fetched == [] and t.listed
    assert not (tmp_path / "wave").exists() and not (tmp_path / "out").exists()


# ------------------------------------------------------------------ every refusal must refuse

def test_pull_refuses_a_partial_wave(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    bucket = {k: v for k, v in bucket.items() if "shard-3-of-3" not in k}
    rc, _t = _run(tmp_path, bucket)
    assert rc == 1 and "missing shard" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_pull_refuses_a_shard_with_a_FAILED_marker_and_downloads_nothing(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    bucket[f"{URI}/shard-2-of-3/FAILED"] = b""
    del bucket[f"{URI}/shard-2-of-3/DONE"]
    rc, t = _run(tmp_path, bucket)
    err = capsys.readouterr().err
    assert rc == 1 and "shard-2-of-3" in err and "FAILED" in err
    assert t.fetched == [] and not (tmp_path / "out").exists()


def test_pull_refuses_a_shard_holding_both_FAILED_and_DONE(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    bucket[f"{URI}/shard-2-of-3/FAILED"] = b""  # DONE stays: a rerun that failed after an earlier success
    assert f"{URI}/shard-2-of-3/DONE" in bucket
    rc, t = _run(tmp_path, bucket)
    err = capsys.readouterr().err
    assert rc == 1 and "shard-2-of-3" in err and "FAILED" in err
    assert t.fetched == [] and not (tmp_path / "out").exists()


def test_pull_refuses_a_shard_without_DONE_even_if_its_files_are_there(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    del bucket[f"{URI}/shard-1-of-3/DONE"]  # still uploading: rows.json may be partial
    rc, t = _run(tmp_path, bucket)
    assert rc == 1 and "DONE" in capsys.readouterr().err and t.fetched == []


def test_pull_refuses_a_shard_missing_a_required_file(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    del bucket[f"{URI}/shard-1-of-3/rows.json"]
    rc, _t = _run(tmp_path, bucket)
    assert rc == 1 and "rows.json" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_pull_refuses_a_shard_that_exited_nonzero(tmp_path, capsys) -> None:
    bucket, rows = _wave()
    sub = {k: v for k, v in bucket.items() if "shard-1-of-3" not in k}
    _put_shard(sub, 1, N, [r for r in rows if evalspec.shard_of(r.sha256, 1, N)], exit_code=4)
    rc, _t = _run(tmp_path, sub)
    assert rc == 1 and "exit_code" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_pull_refuses_mixed_commits(tmp_path, capsys) -> None:
    bucket, rows = _wave()
    sub = {k: v for k, v in bucket.items() if "shard-1-of-3" not in k}
    _put_shard(sub, 1, N, [r for r in rows if evalspec.shard_of(r.sha256, 1, N)], commit="d" * 40)
    rc, _t = _run(tmp_path, sub)
    assert rc == 1 and "commit" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_pull_refuses_mixed_images(tmp_path, capsys) -> None:
    bucket, rows = _wave()
    sub = {k: v for k, v in bucket.items() if "shard-1-of-3" not in k}
    _put_shard(sub, 1, N, [r for r in rows if evalspec.shard_of(r.sha256, 1, N)], image="img@sha256:2")
    rc, _t = _run(tmp_path, sub)
    assert rc == 1 and "image" in capsys.readouterr().err and not (tmp_path / "out").exists()


def _f360_wave():
    rows = _population() + [_row(500, "panel", "plc_intersection", source="f360")]
    bucket, _ = _wave(rows=rows)
    return bucket, rows


def test_pull_refuses_an_f360_row_even_when_the_pool_manifest_lists_it(tmp_path, capsys) -> None:
    bucket, rows = _f360_wave()
    rc, _t = _run(tmp_path, bucket, rows=rows)  # pool.json still LISTS F360; the allowed pool does not
    assert rc == 1 and "not in the pool" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_f360_row_is_refused_as_a_licence_hold_even_with_the_pool_check_off(tmp_path, capsys) -> None:
    bucket, rows = _f360_wave()
    rc, _t = _run(tmp_path, bucket, "--no-pool-check", rows=rows)
    err = capsys.readouterr().err
    assert rc == 2 and "f360" in err and "licence hold" in err and not (tmp_path / "out").exists()


def test_pull_refuses_when_the_planned_shard_count_differs(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    pool = _pool(tmp_path, _population())
    argv = _args(tmp_path, pool)
    argv[argv.index("--shards") + 1] = "4"
    assert collect.main(argv, transport=FakeTransport(bucket)) == 1
    assert "plan has 4" in capsys.readouterr().err


def test_pull_refuses_a_stale_local_shard_that_is_not_in_the_bucket(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    stale = tmp_path / "wave" / "shard-9-of-9"
    stale.mkdir(parents=True)
    rc, t = _run(tmp_path, bucket)
    assert rc == 1 and "shard-9-of-9" in capsys.readouterr().err and t.fetched == []


def test_pull_refuses_when_the_bucket_has_no_shards(tmp_path, capsys) -> None:
    rc, _t = _run(tmp_path, {f"{URI}/notes.txt": b"hi"})
    assert rc == 1 and "no shard" in capsys.readouterr().err


def test_pull_refuses_a_pool_file_nobody_probed(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    rc, _t = _run(tmp_path, bucket, extra_pool=(900,))  # pool lists a file no shard accounts for
    err = capsys.readouterr().err
    assert rc == 1 and "never probed" in err and not (tmp_path / "out").exists()


def test_pull_refuses_a_row_that_is_not_in_the_pool(tmp_path, capsys) -> None:
    bucket, rows = _wave()
    rc, _t = _run(tmp_path, bucket, rows=rows[:-1])  # pool lacks the last row
    assert rc == 1 and "not in the pool" in capsys.readouterr().err


def test_pull_refuses_a_row_filed_under_the_wrong_shard(tmp_path, capsys) -> None:
    bucket, rows = _wave()
    sub = {k: v for k, v in bucket.items() if "shard-1-of-3" not in k and "shard-2-of-3" not in k}
    # swap which shard each file sits in: same files, wrong partition
    _put_shard(sub, 1, N, [r for r in rows if evalspec.shard_of(r.sha256, 2, N)])
    _put_shard(sub, 2, N, [r for r in rows if evalspec.shard_of(r.sha256, 1, N)])
    rc, _t = _run(tmp_path, sub)
    assert rc == 1 and "partition" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_a_file_in_two_shards_is_refused(tmp_path, capsys) -> None:
    bucket, rows = _wave()
    dup = [r for r in rows if evalspec.shard_of(r.sha256, 1, N)][:1]
    sub = {k: v for k, v in bucket.items() if "shard-2-of-3" not in k}
    _put_shard(sub, 2, N, [r for r in rows if evalspec.shard_of(r.sha256, 2, N)] + dup)
    rc, _t = _run(tmp_path, sub)
    assert rc == 1 and "partition" in capsys.readouterr().err


def test_pull_shortfall_exits_2_and_writes_no_splits_then_allow_shortfall_writes(tmp_path, capsys) -> None:
    rows = [r for r in _population() if r.first_failing_layer is not None]  # no clean files at all
    bucket, _ = _wave(rows=rows)
    rc, _t = _run(tmp_path, bucket, rows=rows)
    assert rc == 2 and "splits are short" in capsys.readouterr().err and not (tmp_path / "out").exists()
    rc, _t = _run(tmp_path, bucket, "--allow-shortfall", rows=rows)
    assert rc == 0 and (tmp_path / "out/clean.json").is_file()


def test_pull_large_exclusion_exits_2_and_writes_no_splits_then_allow_excluded_writes(tmp_path, capsys) -> None:
    rows = _population()
    exs: list = []
    i = 700
    while len(exs) < 6:  # 6 of 96 = 6% excluded, over the 2% limit; all hash to shard 1
        i += 1
        if evalspec.shard_of(_sha(i), 1, N):
            exs.append(rp.Excluded(f"e{i}", _sha(i), "abc_fail", "file_timeout: 900s"))
    bucket, _ = _wave(rows=rows)
    _put_shard(bucket, 1, N, [r for r in rows if evalspec.shard_of(r.sha256, 1, N)], excluded=exs)
    extra = tuple(int(x.sha256, 16) for x in exs)
    rc, _t = _run(tmp_path, bucket, extra_pool=extra)
    assert rc == 2 and "excluded" in capsys.readouterr().err and not (tmp_path / "out").exists()
    rc, _t = _run(tmp_path, bucket, "--allow-excluded", extra_pool=extra)
    assert rc == 0 and (tmp_path / "out/dev.json").is_file()


# ------------------------------------------------------------------ the measured shard

def test_measure_accepts_one_shard_prints_a_reprojection_and_writes_no_splits(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    one = {k: v for k, v in bucket.items() if "shard-1-of-3" in k}
    t = FakeTransport(one)
    rc = collect.main(["--pull", URI, "--to", str(tmp_path / "wave"), "--measure", "--wave-shards", "96",
                       "--max-run-hours", "8", "--pool", str(_pool(tmp_path, _population())),
                       "--out-dir", str(tmp_path / "out")], transport=t)
    out = capsys.readouterr().out
    assert rc == 0 and "min/file" in out and "PROJECTED" in out and not (tmp_path / "out").exists()
    # and the same single shard is REFUSED as a wave
    assert collect.main(_args(tmp_path, _pool(tmp_path, _population())), transport=FakeTransport(one)) == 1


def test_measure_still_refuses_a_failed_shard(tmp_path, capsys) -> None:
    bucket, _ = _wave(exit_code=4)
    one = {k: v for k, v in bucket.items() if "shard-1-of-3" in k}
    rc = collect.main(["--pull", URI, "--to", str(tmp_path / "wave"), "--measure", "--wave-shards", "96",
                       "--pool", str(_pool(tmp_path, _population()))], transport=FakeTransport(one))
    assert rc == 1 and "exit_code" in capsys.readouterr().err


def _measure_two(tmp_path, bucket) -> tuple[int, FakeTransport]:
    t = FakeTransport(bucket)
    rc = collect.main(["--pull", URI, "--to", str(tmp_path / "wave"), "--measure", "--wave-shards", "96",
                       "--pool", str(_pool(tmp_path, _population()))], transport=t)
    return rc, t


def test_measure_still_refuses_mixed_commits(tmp_path, capsys) -> None:
    bucket, rows = _wave()
    sub = {k: v for k, v in bucket.items() if "shard-3-of-3" not in k and "shard-1-of-3" not in k}
    _put_shard(sub, 1, N, [r for r in rows if evalspec.shard_of(r.sha256, 1, N)], commit="d" * 40)
    rc, _t = _measure_two(tmp_path, sub)
    assert rc == 1 and "commit" in capsys.readouterr().err


def test_measure_still_refuses_mixed_images(tmp_path, capsys) -> None:
    bucket, rows = _wave()
    sub = {k: v for k, v in bucket.items() if "shard-3-of-3" not in k and "shard-1-of-3" not in k}
    _put_shard(sub, 1, N, [r for r in rows if evalspec.shard_of(r.sha256, 1, N)], image="img@sha256:2")
    rc, _t = _measure_two(tmp_path, sub)
    assert rc == 1 and "image" in capsys.readouterr().err


def test_measure_scales_the_estimates_by_what_the_shard_took() -> None:
    prov: dict = {"shard_files": 100, "cheap_rows": 0, "full_rows": 100, "wall_seconds": 0.0}
    shard: dict = {"prov": prov, "dir": Path("shard-1-of-96")}
    est_min = 100 * evalspec.FULL_MIN_PER_FILE  # the full verdict on every file: there is no cheap pass
    shard["prov"]["wall_seconds"] = 2 * est_min * 60
    m = collect.measure([shard])
    assert m["scale_vs_estimate"] == pytest.approx(2.0)
    assert m["full_min_per_file"] == pytest.approx(2 * evalspec.FULL_MIN_PER_FILE)
    assert "cheap_min_per_file" not in m
    p = collect.reproject(m, files=10_887, shards=96, machine_type="c2d-standard-8", max_run_hours=12)
    assert p.full_files == 10_887
    assert p.cpu_hours == pytest.approx(10_887 * 2 * evalspec.FULL_MIN_PER_FILE / 60)


def test_measure_refuses_a_shard_with_no_files_to_measure() -> None:
    shard = {"prov": {"shard_files": 0, "cheap_rows": 0, "full_rows": 0, "wall_seconds": 5.0}, "dir": Path("s")}
    with pytest.raises(collect.CollectRefused):
        collect.measure([shard])


# ------------------------------------------------------------------ status

def test_status_names_each_shard_and_exits_nonzero_unless_all_done(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    bucket[f"{URI}/shard-2-of-3/FAILED"] = b""
    del bucket[f"{URI}/shard-2-of-3/DONE"]
    del bucket[f"{URI}/shard-3-of-3/DONE"]
    rc = collect.main(["--status", URI, "--shards", str(N)], transport=FakeTransport(bucket))
    out = capsys.readouterr().out
    assert rc == 1
    assert "shard-1-of-3 DONE" in out and "shard-2-of-3 FAILED" in out and "shard-3-of-3 running" in out
    ok, _ = _wave()
    assert collect.main(["--status", URI, "--shards", str(N)], transport=FakeTransport(ok)) == 0


def test_status_reports_a_shard_that_never_started(tmp_path, capsys) -> None:
    bucket, _ = _wave()
    bucket = {k: v for k, v in bucket.items() if "shard-3-of-3" not in k}
    assert collect.main(["--status", URI, "--shards", str(N)], transport=FakeTransport(bucket)) == 1
    assert "shard-3-of-3 NOT STARTED" in capsys.readouterr().out


# ------------------------------------------------------------------ the real transport's commands

class _Run:
    def __init__(self, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=self.stdout, stderr="")


def test_gcloud_transport_only_lists_and_copies_down() -> None:
    run = _Run(f"{URI}/:\n{URI}/shard-1-of-3/DONE\n{URI}/shard-1-of-3/rows.json\n\n{URI}/shard-1-of-3/:\n")
    t = collect.GcloudTransport(run=run)
    assert t.ls(URI) == [f"{URI}/shard-1-of-3/DONE", f"{URI}/shard-1-of-3/rows.json"]
    t.fetch(f"{URI}/shard-1-of-3/rows.json", Path("/tmp/x/rows.json.partial"))
    assert run.calls[0][:3] == ["gcloud", "storage", "ls"]
    assert run.calls[1][:3] == ["gcloud", "storage", "cp"]
    assert run.calls[1][3].startswith("gs://") and not run.calls[1][4].startswith("gs://")  # remote -> local only
    for c in run.calls:
        assert not {"rm", "mv", "rsync", "delete", "objects"} & set(c)


def test_gcloud_transport_raises_when_the_command_fails() -> None:
    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="403 forbidden")

    with pytest.raises(collect.CollectRefused, match="403"):
        collect.GcloudTransport(run=failing).ls(URI)


def test_the_guard_bites_an_unfaked_run_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        collect.GcloudTransport().ls(URI)  # default run is subprocess.run: the autouse guard blocks it


# ------------------------------------------------------------------ the excluded stratum (task 55)

def test_the_excluded_stratum_names_every_file_and_counts_it_as_nothing() -> None:
    """A wall-killed file is reported as its OWN category: counted, NAMED, with the walls that produced it,
    and in no failing, clean or unclean count."""
    rows = _population()
    exs = [rp.Excluded("slow.step", _sha(901), "abc_fail", "file_timeout: file wall clock 1800s exceeded"),
           rp.Excluded("checker.step", _sha(902), "occt",
                       "checker_unavailable: check_valid: timeout: check_valid exceeded 900.0s; worker killed")]
    merged = {"rows": rows, "excluded": [asdict(x) for x in exs], "cheap": [], "full": [],
              "cheap_failed": set()}
    rep = collect.report(merged, None, collect.wall_values([_prov()]))
    block = rep["excluded_stratum"]

    assert block["files"] == 2
    assert [n["name"] for n in block["names"]] == ["checker.step", "slow.step"]  # named, sorted by reason
    assert {n["reason"].split(":")[0] for n in block["names"]} == {"file_timeout", "checker_unavailable"}
    assert block["by_reason"] == {"file_timeout": 1, "checker_unavailable": 1}
    assert block["by_source"] == {"abc_fail": 1, "occt": 1}
    assert "not a failure" in block["counted_as"] and "not clean" in block["counted_as"]
    # not a failure and not a pass: the counts the splits are cut from do not see it at all
    failing = len([r for r in rows if r.first_failing_layer is not None])
    assert rep["failing"] + rep["no_solids_not_repairable"] == failing
    assert rep["probed_conclusive"] == len(rows)
    assert sum(rep["by_layer"].values()) == len(rows)
    assert sum(rep["failing_by_stratum"].values()) == rep["failing"]


def test_a_shard_run_at_todays_constants_reports_them_because_it_ran_them() -> None:
    from meshpipeline.cad.repair_v4.limits import CHECKER_CALL_TIMEOUT_S

    ran = {"prov": {"file_timeout_s": evalspec.DEFAULT_FILE_TIMEOUT_S,
                    "check_valid_timeout_s": CHECKER_CALL_TIMEOUT_S}}
    walls = collect.wall_values([ran])
    assert walls["file_timeout_s"] == evalspec.DEFAULT_FILE_TIMEOUT_S
    assert walls["check_valid_timeout_s"] == CHECKER_CALL_TIMEOUT_S
    # the per-file wall bounds the SUM of a file's calls, so it has to be the larger of the two
    assert walls["file_timeout_s"] > walls["check_valid_timeout_s"]


def test_the_stratum_line_is_printed_on_every_run_including_when_it_is_empty(tmp_path, capsys) -> None:
    rc, _t = _run(tmp_path, _wave()[0])
    out = capsys.readouterr().out
    assert rc == 0
    assert "EXCLUDED STRATUM 0 file(s), 0.00% of the pool (limit 2%: under)" in out
    assert "file_timeout_s 900s, check_valid_timeout_s 300s" in out  # what the SHARDS ran, not today's constants
    assert f"{evalspec.DEFAULT_FILE_TIMEOUT_S:g}s" not in out


def test_over_the_limit_the_refusal_names_the_walls_and_points_at_the_stratum(tmp_path, capsys) -> None:
    rows = _population()
    exs: list = []
    i = 700
    while len(exs) < 6:  # over the 2% limit, all in shard 1
        i += 1
        if evalspec.shard_of(_sha(i), 1, N):
            exs.append(rp.Excluded(f"e{i}", _sha(i), "abc_fail", "file_timeout: 1800s"))
    bucket, _ = _wave(rows=rows)
    _put_shard(bucket, 1, N, [r for r in rows if evalspec.shard_of(r.sha256, 1, N)], excluded=exs)
    rc, _t = _run(tmp_path, bucket, extra_pool=tuple(int(x.sha256, 16) for x in exs))
    cap = capsys.readouterr()
    assert rc == 2 and not (tmp_path / "out").exists()
    assert "excluded_stratum.names" in cap.err and "walls" in cap.err
    # the refusal stands on its own; --allow-excluded is not what the stratum is for
    report = json.loads((tmp_path / "wave/collect-report.json").read_text())
    stratum = report["report"]["excluded_stratum"]
    assert report["accepted"] is False and stratum["over_limit"] is True and stratum["files"] == 6
    assert len(stratum["names"]) == 6


# ------------------------------------------------------------------ A7(a): the walls the SHARDS ran with

def _prov(**kw) -> dict:
    return {"prov": {"file_timeout_s": 900.0, "check_valid_timeout_s": 300.0, **kw}}


def test_the_stratum_reports_the_walls_the_shards_ran_with_not_todays_constants() -> None:
    """A wave run at 900/300 is still a wave run at 900/300 after the constants are raised: the stratum reads the
    walls out of each shard's provenance, so it can never claim the exclusions happened at a wall nobody ran."""
    walls = collect.wall_values([_prov(), _prov()])
    assert walls["file_timeout_s"] == 900.0 and walls["check_valid_timeout_s"] == 300.0
    assert walls["file_timeout_s"] != evalspec.DEFAULT_FILE_TIMEOUT_S  # today's constant is NOT what it reports
    assert not walls["disagreed"] and not walls["unrecorded"]


def test_shards_that_ran_different_walls_are_shown_disagreeing_not_collapsed() -> None:
    walls = collect.wall_values([_prov(), _prov(file_timeout_s=1800.0)])
    assert walls["file_timeout_s"] is None  # there is no single number to report, so none is invented
    assert walls["disagreed"] == {"file_timeout_s": [900.0, 1800.0]}
    assert walls["check_valid_timeout_s"] == 300.0
    assert "disagree" in collect.describe_excluded_stratum(
        collect.excluded_stratum({"rows": [], "excluded": []}, walls))


def test_a_wall_no_shard_recorded_is_unrecorded_never_todays_constant() -> None:
    walls = collect.wall_values([{"prov": {"file_timeout_s": 900.0}}])  # a shard from before the key existed
    assert walls["check_valid_timeout_s"] is None and walls["unrecorded"] == ["check_valid_timeout_s"]
    line = collect.describe_excluded_stratum(collect.excluded_stratum({"rows": [], "excluded": []}, walls))
    assert "check_valid_timeout_s unrecorded" in line


def test_the_pulled_wave_reports_its_own_shards_walls(tmp_path) -> None:
    bucket, _ = _wave()
    rc, _t = _run(tmp_path, bucket)
    assert rc == 0
    walls = json.loads((tmp_path / "wave/collect-report.json").read_text())["report"]["excluded_stratum"]["walls"]
    assert walls["file_timeout_s"] == 900.0 and walls["check_valid_timeout_s"] == 300.0


# ------------------------------------------------------------------ A7(b): one file is one file

def test_a_file_excluded_for_several_arms_is_one_file_in_the_stratum() -> None:
    """An arm wave writes one EXCLUDED entry per (file, arm). The stratum counts FILES, so four arms killed on one
    file is one file with four entries, not four files."""
    sha = _sha(901)
    xs = [{"name": "slow.step", "sha256": sha, "source": "abc_fail", "arm": a,
           "reason": "file_timeout: file wall clock 1800s exceeded"}
          for a in ("input_only", "staged", "search_det", "search_ai")]
    block = collect.excluded_stratum({"rows": [None] * 29, "excluded": xs}, collect.wall_values([_prov()]))
    assert block["files"] == 1 and block["entries"] == 4
    assert block["fraction_of_pool"] == pytest.approx(1 / 30)
    assert [n["sha256"] for n in block["names"]] == [sha]
    assert block["names"][0]["arms"] == ["input_only", "search_ai", "search_det", "staged"]
    assert block["by_reason"] == {"file_timeout": 1} and block["by_source"] == {"abc_fail": 1}


def test_the_reprobe_stratum_is_unchanged_one_entry_per_file() -> None:
    xs = [{"name": "a.step", "sha256": _sha(901), "source": "abc_fail", "reason": "file_timeout: 1800s"},
          {"name": "b.step", "sha256": _sha(902), "source": "occt", "reason": "checker_unavailable: x"}]
    block = collect.excluded_stratum({"rows": [None] * 28, "excluded": xs}, collect.wall_values([_prov()]))
    assert block["files"] == 2 and block["entries"] == 2
    assert all("arms" not in n for n in block["names"])  # no arm key in a re-probe entry: nothing to list
