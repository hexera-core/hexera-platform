"""partial_reprobe.py: the reprobe-800 sample never touches held-out, is deterministic, and v2 is additive to v1."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "devtools/validation/v4_eval"))
import partial_reprobe as pr  # noqa: E402

pool_mod, rp, evalspec = pr.pool_mod, pr.reprobe_mod, pr.evalspec


def _entry(i: int, source: str, group: str | None = None) -> pool_mod.PoolEntry:
    return pool_mod.PoolEntry(name=f"{source}/{i}.step", source=source, sha256=f"{i:064x}", url="u",
                              archive_sha256=None, licence="l", group=group)


def _pool() -> tuple[list, dict[str, int]]:
    entries = ([_entry(i, "abc_fail") for i in range(0, 200)] + [_entry(i, "abc_pass") for i in range(200, 260)]
               + [_entry(i, "nist", group=f"g{i % 3}") for i in range(260, 272)] + [_entry(i, "occt") for i in range(272, 280)])
    sizes = {e.sha256: 10 ** (3 + (int(e.sha256, 16) % 5)) for e in entries}
    return entries, sizes


def _f(e) -> dict:
    return {"sha256": e.sha256, "group": e.group}


def test_the_sample_excludes_held_out_and_its_groups_and_keeps_v1() -> None:
    entries, sizes = _pool()
    held = [_f(entries[3]), _f(entries[261])]          # an abc file and a nist file (its group goes too)
    v1 = [_f(entries[5]), _f(entries[201])]
    m = pr.draw_sample(entries, sizes, n=100, heldout=held, v1=v1,
                       quota={"nist": "all", "occt": "all", "abc_pass": 20, "abc_fail": "rest"})
    shas = {f["sha256"] for f in m["files"]}
    assert m["n"] == 100 and len(shas) == 100
    assert entries[3].sha256 not in shas
    assert not [f for f in m["files"] if f["group"] == entries[261].group]   # the whole held-out group is out
    assert {entries[5].sha256, entries[201].sha256} <= shas
    assert sum(f["v1"] for f in m["files"]) == 2
    assert m["sampled_by_source"]["nist"] == 8 and m["sampled_by_source"]["occt"] == 8
    assert m["sampled_by_source"]["abc_pass"] == 21          # 20 drawn + 1 v1
    again = pr.draw_sample(list(reversed(entries)), sizes, n=100, heldout=held, v1=v1,
                           quota={"nist": "all", "occt": "all", "abc_pass": 20, "abc_fail": "rest"})
    assert again["files"] == m["files"]                      # deterministic, order-independent


def test_the_real_sample_never_holds_a_held_out_file() -> None:
    manifest = REPO / "devtools/validation/corpora/v4/provisional_v2/sample_manifest.json"
    m = json.loads(manifest.read_text())
    held = pr.split_files(pr.HELDOUT_FILES)
    groups = {f["group"] for f in held if f["group"]}
    assert not {f["sha256"] for f in held} & {f["sha256"] for f in m["files"]}
    assert not [f for f in m["files"] if f["group"] in groups]
    assert sum(f["v1"] for f in m["files"]) == 38 and m["seed"] == pr.SEED


def _row(i: int, source: str, layer: str | None) -> rp.ProbeRow:
    panel = [{"name": "gmsh_prod", "status": "fail" if layer == "panel" else "pass", "error_kind": "other",
              "min_sicn": 0.012}]
    return rp.ProbeRow(f"{source}/{i}.step", f"{i:064x}", source,
                       {"tier": "full", "clean": layer is None, "counts_for_scoreboard": True, "hard": [],
                        "panel": panel, "valid": layer != "valid"}, layer, "other" if layer else "")


def test_v2_splits_are_additive_and_carry_the_provisional_shape(tmp_path: Path) -> None:
    rows = [_row(i, "abc_fail", "panel" if i % 2 else "valid") for i in range(40)]
    rows += [_row(i, "abc_pass", None) for i in range(40, 60)]
    shard = tmp_path / "shards/shard-1-of-1"
    shard.mkdir(parents=True)
    (shard / "rows.json").write_text(json.dumps([rp.asdict(r) for r in rows]))
    (shard / "EXCLUDED.json").write_text(json.dumps([{"name": "x", "sha256": f"{99:064x}", "source": "abc_fail",
                                                      "reason": "file_timeout: 900 s"}]))
    (shard / "PROVENANCE.json").write_text(json.dumps({"commit": "c" * 40, "image": "img"}))
    v1 = {rows[0].sha256, rows[45].sha256}
    files = [{"sha256": r.sha256, "source": r.source, "size": 1, "size_band": "0-100000", "v1": r.sha256 in v1}
             for r in rows] + [{"sha256": f"{99:064x}", "source": "abc_fail", "size": 1, "size_band": "0-100000",
                                "v1": False}]
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"seed": 1, "files": files, "excluded_heldout": {"sha256": []}}))
    out = tmp_path / "out"
    out.mkdir()
    assert pr.main(["splits", "--from", str(tmp_path / "shards"), "--manifest", str(manifest), "--out-dir", str(out),
                    "--dev", "10", "--clean", "5"]) == 0
    dev = json.loads((out / "PROVISIONAL_dev_v2.json").read_text())
    clean = json.loads((out / "PROVISIONAL_clean_v2.json").read_text())
    assert dev["split"] == "dev" and clean["split"] == "clean" and "PROVISIONAL" in dev
    assert len(dev["files"]) == 10 and len(clean["files"]) == 5
    assert not v1 & {f["sha256"] for f in dev["files"] + clean["files"]}
    assert all(f["first_failing_layer"] for f in dev["files"]) and not any(f["first_failing_layer"] for f in clean["files"])
    assert [r.sha256 for r in rp.read_split(out / "PROVISIONAL_dev_v2.json")] == [f["sha256"] for f in dev["files"]]
    summary = json.loads((out / "SUMMARY.json").read_text())
    assert summary["excluded"] == 1 and summary["near_threshold"]["all"] == 60


def test_the_snap_v2_waves_are_normal_four_arm_waves() -> None:
    for name, split in (("snap_v2_dev", "dev"), ("snap_v2_clean", "clean")):
        w = evalspec.wave(name)
        assert w.wave_type == "normal" and w.split == split
        assert w.arms == ("input_only", "staged", "search_det", "search_ai")


def test_v1r_replaces_every_row_keeps_membership_and_lists_changes(tmp_path: Path) -> None:
    old = [_row(1, "abc_pass", None), _row(2, "abc_fail", "valid"), _row(3, "abc_fail", "panel")]
    v1 = tmp_path / "PROVISIONAL_clean.json"
    v1.write_text(json.dumps({"PROVISIONAL": "x", "split": "clean", "seed": 1, "files": [rp.asdict(r) for r in old]}))
    new = [_row(1, "abc_pass", "panel"), _row(2, "abc_fail", "valid")]          # 1 flipped, 3 excluded now
    out = tmp_path / "o"
    out.mkdir()
    rep = pr.write_v1r(out, new, [{"sha256": old[2].sha256, "reason": "file_timeout: 1800 s"}], (v1,))
    body = json.loads((out / "PROVISIONAL_clean_v1r.json").read_text())
    assert body["split"] == "clean" and body["relabel_of"] == "PROVISIONAL_clean.json"
    assert [f["first_failing_layer"] for f in body["files"]] == ["panel", "valid"]
    assert rep["clean"]["label_changed"] == 1 and rep["clean"]["dropped"] == 1
    assert body["dropped_no_engine_form_label"][0]["reason"].startswith("file_timeout")
