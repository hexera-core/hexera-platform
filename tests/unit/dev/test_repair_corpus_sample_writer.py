"""The bundle sample writer: the gym's one guard against scoring two different samples."""
import hashlib
import importlib.util
import json
import pathlib

spec = importlib.util.spec_from_file_location(
    "rc", pathlib.Path(__file__).resolve().parents[3] / "devtools/validation/repair_corpus.py")
rc = importlib.util.module_from_spec(spec); spec.loader.exec_module(rc)

def test_writes_name_and_hash_only(tmp_path):
    entries = [{"name": "b.step", "sha256": "bb", "family": "open_loop", "path": "/secret/b.step"},
               {"name": "a.step", "sha256": "aa", "family": "no_volume", "path": "/secret/a.step"}]
    out = rc.write_bundle_sample(tmp_path, entries, seed=7)
    body = json.loads(out.read_text())
    assert body["seed"] == 7
    assert body["files"] == [{"name": "a.step", "sha256": "aa"}, {"name": "b.step", "sha256": "bb"}]
    assert "secret" not in out.read_text(), "a sample must never record a path"

def test_an_empty_sample_writes_nothing(tmp_path):
    assert rc.write_bundle_sample(tmp_path, [], seed=7) is None
    assert not (tmp_path / "sample.json").exists()

def test_two_arms_of_the_same_sample_agree_and_a_different_one_does_not(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    same = [{"name": "x", "sha256": "11"}, {"name": "y", "sha256": "22"}]
    rc.write_bundle_sample(a, same, seed=1)
    rc.write_bundle_sample(b, list(reversed(same)), seed=1)   # order must not matter
    rc.write_bundle_sample(c, [{"name": "x", "sha256": "11"}, {"name": "z", "sha256": "33"}], seed=1)

    def h(d):
        return hashlib.sha256((d / "sample.json").read_bytes()).hexdigest()

    assert h(a) == h(b), "the same files in another order are the same sample"
    assert h(a) != h(c), "a different file set must not score as the same sample"
