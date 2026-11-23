"""v4_eval inject_pool: the generator of injected-defect pool entries, against a fake injector, offline.

Every guard here is tested by making it REJECT something (a quarantined kind, a hash mismatch, an entry
with no recipe), not only by the happy path."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
EVAL = ROOT / "devtools/validation/v4_eval"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


pool = sys.modules.get("v4_pool") or _load("v4_pool", EVAL / "pool.py")
gen = _load("v4_inject_pool", EVAL / "inject_pool.py")

KINDS = ("loose_reapprox", "unsew_perturb", "tolerance_inflate", "pcurve_corrupt", "seam_variant",
         "small_entities", "face_delete", "assembly_overlap", "precision_truncate", "iges_roundtrip")
STATUS = {"loose_reapprox": "quarantined", "unsew_perturb": "structural", "tolerance_inflate": "quarantined",
          "pcurve_corrupt": "infeasible", "seam_variant": "quarantined", "small_entities": "structural",
          "face_delete": "structural", "assembly_overlap": "structural", "precision_truncate": "quarantined",
          "iges_roundtrip": "structural"}
BASE = "https://bucket.example/v4-injected"
DIGEST = "sha256:" + "d" * 64


class NotApplicable(ValueError):
    pass


class Infeasible(ValueError):
    pass


class FakeBackend:
    """Stands in for inject.py (which needs OCP). Mimics what matters: a wall-clock timestamp in the
    STEP header (the real writer does this), a label, and the real module's refusals."""

    KINDS = KINDS
    KIND_STATUS = STATUS
    QUARANTINED_KINDS = tuple(k for k in KINDS if STATUS[k] == "quarantined")
    DEFAULT_KINDS = tuple(k for k in KINDS if STATUS[k] == "structural")
    InjectionNotApplicable = NotApplicable
    InjectionInfeasible = Infeasible

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.skew = 0

    OCCT = "fake-ocp==7.9.1"
    FIDELITY = "damaged_input"
    RAISE: Exception | None = None

    def occt_version(self):
        return self.OCCT

    def inject(self, src_step, kind, seed, out_dir=None):
        if self.RAISE is not None:
            raise self.RAISE
        src = Path(src_step)
        self.calls.append((src.name, kind, seed))
        if kind == "pcurve_corrupt":
            raise Infeasible("pcurve_corrupt: infeasible")
        if kind == "small_entities" and b"FLAT" in src.read_bytes():
            raise NotApplicable("no room")
        payload = hashlib.sha256(src.read_bytes() + f"{kind}:{seed}".encode()).hexdigest()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_700_000_000 + self.skew))
        count = len(self.calls) + 40   # OCCT's per-process writer count
        self.skew += 1000   # every call writes a different clock reading, like OCCT does
        out = Path(out_dir) / f"{src.stem}.{kind}.{seed}.step"
        out.write_text("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('Open CASCADE Model'),'2;1');\n"
                       f"FILE_NAME('Open CASCADE Shape Model','{stamp}',('Author'),(\n    'Open CASCADE'),"
                       f"'Open CASCADE STEP processor 7.9','Open CASCADE 7.9'\n  ,'Unknown');\n"
                       f"ENDSEC;\nDATA;\n#7 = PRODUCT('Open CASCADE STEP translator 7.9 {count}','x','',(#8));\n"
                       f"/* {payload} */\nENDSEC;\nEND-ISO-10303-21;\n")
        label = {"kind": kind, "seed": seed, "status": STATUS[kind], "clean_source": str(src),
                 "fidelity_reference": self.FIDELITY,
                 "entities": [{"type": "face", "src_index": 3, "center": [1.0, 2.0, 3.0], "area": 4.5}]}
        if kind == "iges_roundtrip":
            label["iges_file"] = str(out.with_suffix(".igs"))   # a temp path: must not reach the manifest
        return types.SimpleNamespace(path=out, kind=kind, label=label)


def _clean_entry(tmp: Path, name: str, source: str = "abc_pass", body: bytes = b"CLEAN PART 1") -> tuple:
    path = tmp / "inputs" / hashlib.sha256(body).hexdigest() / Path(name).name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    licence = pool.LICENCE["occt" if source == "occt" else "nist" if source == "nist" else "abc"]
    return pool.PoolEntry(name, source, hashlib.sha256(body).hexdigest(), "https://x/chunk.7z", "9" * 64,
                          licence, None), path


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def clean(tmp_path):
    return [_clean_entry(tmp_path, "abc/0051_part_a.step"), _clean_entry(tmp_path, "occt/o1.stp", "occt", b"CLEAN 2")]


def _lookup(clean):
    return {e.sha256: p for e, p in clean}.get


def _generate(clean, tmp_path, backend, kinds=None, seeds=(0,)):
    return gen.generate(clean, kinds=kinds, seeds=seeds, out_dir=tmp_path / "out", base_url=BASE, backend=backend)


# --------------------------------------------------------------------------- guards REJECT

@pytest.mark.parametrize("kind", ["loose_reapprox", "tolerance_inflate", "seam_variant", "precision_truncate"])
def test_quarantined_kind_is_refused_and_never_injected(clean, tmp_path, backend, kind):
    with pytest.raises(gen.RefusedKind, match="quarantined"):
        _generate(clean, tmp_path, backend, kinds=[kind])
    assert backend.calls == []   # refused BEFORE any injector ran


def test_infeasible_and_unknown_kinds_are_refused(clean, tmp_path, backend):
    with pytest.raises(gen.RefusedKind, match="infeasible"):
        _generate(clean, tmp_path, backend, kinds=["pcurve_corrupt"])
    with pytest.raises(gen.RefusedKind, match="unknown"):
        _generate(clean, tmp_path, backend, kinds=["made_up"])
    assert backend.calls == []


def test_a_quarantined_kind_hidden_in_a_valid_list_still_refuses_the_whole_run(clean, tmp_path, backend):
    with pytest.raises(gen.RefusedKind):
        _generate(clean, tmp_path, backend, kinds=["face_delete", "seam_variant"])
    assert backend.calls == [] and not (tmp_path / "out").exists()


def test_default_kinds_are_the_five_structural_ones(backend):
    assert set(gen.resolve_kinds(None, backend)) == {"unsew_perturb", "small_entities", "face_delete",
                                                     "assembly_overlap", "iges_roundtrip"}
    assert not set(gen.resolve_kinds(None, backend)) & set(backend.QUARANTINED_KINDS)


@pytest.mark.parametrize("source", ["f360", "abc_fail", "inject"])
def test_sources_that_are_not_clean_parts_are_refused(tmp_path, backend, source):
    entry, path = _clean_entry(tmp_path, "x/part.step", "abc_pass")
    entry = pool.PoolEntry(entry.name, source, entry.sha256, entry.url, entry.archive_sha256, entry.licence)
    with pytest.raises(gen.RefusedSource, match=source):
        _generate([(entry, path)], tmp_path, backend)
    assert backend.calls == []


def test_the_source_must_match_its_pool_hash(tmp_path, backend):
    entry, path = _clean_entry(tmp_path, "x/part.step")
    path.write_bytes(b"NOT WHAT THE MANIFEST HASHED")
    with pytest.raises(gen.GenerationError, match="sha256"):
        _generate([(entry, path)], tmp_path, backend)


# --------------------------------------------------------------------------- reproducibility

def test_bytes_are_reproducible_despite_the_header_timestamp(clean, tmp_path, backend):
    a = _generate(clean, tmp_path / "a", backend)
    b = _generate(clean, tmp_path / "b", backend)
    assert [e.sha256 for e in a.entries] == [e.sha256 for e in b.entries]
    for e in a.entries:
        written = (tmp_path / "a" / "out" / e.sha256 / Path(e.name).name).read_bytes()
        assert hashlib.sha256(written).hexdigest() == e.sha256
        assert b"2023-" not in written and b"1970-01-01T00:00:00" in written
        assert b"translator 7.9 1'" in written


def test_normalize_rewrites_only_the_clock_and_the_writer_count():
    raw = (b"FILE_NAME('Open CASCADE Shape Model','2026-11-23T04:55:25',('Author'),(\n 'x'),'p','o','u');\n"
           b"DATA;\n#7 = PRODUCT('Open CASCADE STEP translator 7.9 17',\n  'Open CASCADE STEP translator 7.9 17','',(#8));\n"
           b"#1=X('2026-11-23T04:55:25');\n#2 = NEXT_ASSEMBLY_USAGE_OCCURRENCE('5','','',#5,#31,$);\n"
           b"#3 = NEXT_ASSEMBLY_USAGE_OCCURRENCE('9','','',#5,#32,$);\n")
    out = gen.normalize_step(raw)
    assert b"OCCURRENCE('1'" in out and b"OCCURRENCE('2'" in out and b"OCCURRENCE('5'" not in out
    assert b"'1970-01-01T00:00:00'" in out.split(b"DATA;")[0]
    assert out.count(b"translator 7.9 1'") == 2 and b"7.9 17" not in out
    assert out.split(b"#1=X")[1].split(b"#2")[0] == raw.split(b"#1=X")[1].split(b"#2")[0]  # the rest is untouched


def test_unnormalizable_output_is_an_error_not_a_silent_pass(tmp_path, backend):
    with pytest.raises(gen.GenerationError, match="FILE_NAME"):
        gen.normalize_step(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n")


def test_verify_passes_when_regenerated_bytes_match(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    assert gen.verify(res.recipes, res.entries, _lookup(clean),
                      backend) == []


def test_verify_rejects_a_hash_mismatch(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    res.recipes[0] = dataclasses.replace(res.recipes[0], sha256="0" * 64)
    problems = gen.verify(res.recipes, res.entries, _lookup(clean),
                          backend)
    assert any("0" * 64 in p or "differs" in p for p in problems)


def test_verify_rejects_a_changed_seed(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    r = res.recipes[0]
    res.recipes[0] = dataclasses.replace(r, seed=r.seed + 1)
    problems = gen.verify(res.recipes, res.entries, _lookup(clean),
                          backend)
    assert problems


def test_verify_reports_a_missing_source_rather_than_skipping_it(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    problems = gen.verify(res.recipes, res.entries, lambda sha: None, backend)
    assert len(problems) == len(res.recipes) and all("source" in p for p in problems)


# --------------------------------------------------------------------------- entries

def test_entries_are_direct_inject_files_fetchable_at_a_hash_addressed_url(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    assert res.entries and all(e.source == "inject" for e in res.entries)
    for e in res.entries:
        base = Path(e.name).name
        assert e.archive_sha256 is None                       # a direct file: the stager hashes it whole
        assert e.url == f"{BASE}/{e.sha256}/{base}"
        assert (tmp_path / "out" / e.sha256 / base).is_file()   # the upload tree mirrors the URL
        assert e.name.startswith("inject/")


def test_licence_is_inherited_from_the_clean_source_verbatim(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    by_sha = {e.sha256: e for e, _ in clean}
    for entry, recipe in zip(res.entries, res.recipes, strict=True):
        assert entry.licence == by_sha[recipe.source_sha256].licence
    assert {e.licence for e in res.entries} == {pool.LICENCE["abc"], pool.LICENCE["occt"]}


def test_entries_of_one_clean_part_share_a_group(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    groups: dict[str, set[str]] = {}
    for recipe, entry in zip(res.recipes, res.entries, strict=True):
        groups.setdefault(recipe.source_sha256, set()).add(entry.group or "")
    assert all(len(g) == 1 and "" not in g for g in groups.values())
    assert len({next(iter(g)) for g in groups.values()}) == len(groups)


def test_not_applicable_is_skipped_and_reported_not_dropped_silently(tmp_path, backend):
    flat = _clean_entry(tmp_path, "occt/flat.stp", "occt", b"FLAT part")
    res = _generate([flat], tmp_path, backend, kinds=["small_entities", "face_delete"])
    assert [r.kind for r in res.recipes] == ["face_delete"]
    assert len(res.skipped) == 1 and res.skipped[0].kind == "small_entities" and "no room" in res.skipped[0].reason


def test_infeasible_from_the_injector_aborts_instead_of_skipping(clean, tmp_path):
    class Rogue(FakeBackend):
        DEFAULT_KINDS = ("face_delete",)
        def inject(self, *a, **k):
            raise Infeasible("never works")
    with pytest.raises(Infeasible):
        _generate(clean, tmp_path, Rogue(), kinds=["face_delete"])


# --------------------------------------------------------------------------- labels

def test_the_label_is_carried_and_portable(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend, kinds=["iges_roundtrip"])
    for recipe in res.recipes:
        assert recipe.label["entities"] == [{"type": "face", "src_index": 3, "center": [1.0, 2.0, 3.0],
                                             "area": 4.5}]
        assert recipe.label["fidelity_reference"] == "damaged_input"
        assert "iges_file" not in recipe.label and str(tmp_path) not in json.dumps(recipe.label)
        assert recipe.label["clean_source"] in {e.name for e, _ in clean}
        assert recipe.label["clean_source_sha256"] == recipe.source_sha256


def test_an_injector_that_names_the_clean_source_as_fidelity_reference_is_overridden(clean, tmp_path):
    """Design 7.2: the reference is the damaged input. A backend label saying otherwise must not survive."""
    class Wrong(FakeBackend):
        FIDELITY = "clean_source"
    res = _generate(clean, tmp_path, Wrong())
    assert res.recipes
    for recipe in res.recipes:
        assert recipe.label["fidelity_reference"] == "damaged_input"
    for entry in res.entries:   # the entry points at the damaged file only
        assert entry.sha256 not in {e.sha256 for e, _ in clean}


# --------------------------------------------------------------------------- the sidecar and the pool

def test_records_round_trip_and_hold_what_regeneration_needs(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    path = tmp_path / "injected.json"
    gen.write_records(path, res.recipes, skipped=res.skipped)
    back = gen.read_records(path, backend)
    assert back == sorted(res.recipes, key=lambda r: r.sha256)
    row = json.loads(path.read_text())["records"][0]
    assert {"sha256", "name", "source_name", "source_sha256", "source_source", "kind", "seed", "label",
            "normalizer"} <= set(row)


def test_read_records_refuses_a_quarantined_kind(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    path = tmp_path / "injected.json"
    gen.write_records(path, res.recipes, skipped=[])
    doc = json.loads(path.read_text())
    doc["records"][0]["kind"] = "seam_variant"
    path.write_text(json.dumps(doc))
    with pytest.raises(gen.RefusedKind):
        gen.read_records(path, backend)


def test_check_pool_catches_an_entry_without_a_recipe(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    pool_entries = [e for e, _ in clean] + res.entries
    assert gen.check_pool(pool_entries, res.recipes, backend) == []
    problems = gen.check_pool(pool_entries, res.recipes[1:], backend)
    assert any("no recipe" in p for p in problems)


def test_check_pool_catches_a_recipe_without_an_entry(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    problems = gen.check_pool([e for e, _ in clean] + res.entries[1:], res.recipes, backend)
    assert any("no pool entry" in p for p in problems)


def test_check_pool_catches_an_invented_licence(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    e0 = res.entries[0]
    forged = pool.PoolEntry(e0.name, e0.source, e0.sha256, e0.url, e0.archive_sha256, "CC0 do what you like",
                            e0.group)
    problems = gen.check_pool([e for e, _ in clean] + [forged] + res.entries[1:], res.recipes, backend)
    assert any("licence" in p for p in problems)


def test_check_pool_catches_a_quarantined_kind_or_a_non_clean_source(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    r = res.recipes[0]
    bad_kind = [dataclasses.replace(r, kind="tolerance_inflate")] + res.recipes[1:]
    assert any("quarantined" in p for p in gen.check_pool([e for e, _ in clean] + res.entries, bad_kind, backend))
    f360 = [(pool.PoolEntry(e.name, "f360", e.sha256, e.url, e.archive_sha256, e.licence), p) for e, p in clean]
    assert any("f360" in p for p in gen.check_pool([e for e, _ in f360] + res.entries, res.recipes, backend))


def test_check_pool_catches_a_wrong_url(clean, tmp_path, backend):
    res = _generate(clean, tmp_path, backend)
    e0 = res.entries[0]
    wrong = pool.PoolEntry(e0.name, e0.source, e0.sha256, "https://elsewhere/x.step", None, e0.licence, e0.group)
    assert any("url" in p for p in gen.check_pool([e for e, _ in clean] + [wrong] + res.entries[1:],
                                                  res.recipes, backend))


def test_merge_into_pool_replaces_old_inject_entries_and_keeps_the_rest(clean, tmp_path, backend):
    entries = [e for e, _ in clean]
    manifest = tmp_path / "pool.json"
    pool.write_pool(entries, manifest, sources_included=["abc"], notes={"nist": "n"})
    first = _generate(clean, tmp_path, backend, kinds=["face_delete"])
    gen.merge_into_pool(manifest, first.entries)
    second = _generate(clean, tmp_path / "again", backend, kinds=["unsew_perturb"])
    gen.merge_into_pool(manifest, second.entries)
    back = pool.read_pool(manifest)
    assert {e.sha256 for e in back if e.source == "inject"} == {e.sha256 for e in second.entries}
    assert {e.sha256 for e in back if e.source != "inject"} == {e.sha256 for e in entries}
    doc = json.loads(manifest.read_text())
    assert doc["version"] == 3 and "inject" in doc["sources_included"] and "abc" in doc["sources_included"]
    assert "nist" in doc["source_notes"] and "inject" in doc["source_notes"]
    # the interned licences survive: an inject entry carries its source's text, resolved by read_pool
    assert {e.licence for e in back if e.source == "inject"} <= {e.licence for e in entries}


def test_pool_build_keeps_existing_inject_entries(clean, tmp_path, backend):
    """`pool.py build` rewrites pool.json from scratch; without this it would silently delete the injected set."""
    res = _generate(clean, tmp_path, backend)
    manifest = tmp_path / "pool.json"
    pool.write_pool([e for e, _ in clean] + res.entries, manifest, sources_included=["abc", "inject"])
    kept = pool.carry_forward_inject(manifest)
    assert {e.sha256 for e in kept} == {e.sha256 for e in res.entries}
    assert pool.carry_forward_inject(tmp_path / "does-not-exist.json") == []


# --------------------------------------------------------------------------- the stager, unmodified

def _stager():
    path = EVAL / "stage.py"
    if not path.exists():
        pytest.skip("stage.py (Task 8c) is not on this branch yet; run this test once it is merged")
    return _load("v4_stage", path)


def test_the_unmodified_stager_fetches_generated_entries(clean, tmp_path, backend):
    stage = _stager()
    res = _generate(clean, tmp_path, backend)
    served = {e.url: tmp_path / "out" / e.sha256 / Path(e.name).name for e in res.entries}

    def download(url: str, dest: Path) -> None:
        dest.write_bytes(served[url].read_bytes())    # no network: the transport is injected, as in pool.py

    inputs = tmp_path / "staged"
    report = stage.stage(res.entries, inputs, download=download)
    assert report.staged == len(res.entries)
    for e in res.entries:
        assert (inputs / e.sha256 / Path(e.name).name).read_bytes() == served[e.url].read_bytes()


def test_the_stager_rejects_a_tampered_generated_file(clean, tmp_path, backend):
    stage = _stager()
    res = _generate(clean, tmp_path, backend)

    def download(url: str, dest: Path) -> None:
        dest.write_bytes(b"swapped bytes")

    with pytest.raises(stage.StageError, match="sha256"):
        stage.stage(res.entries[:1], tmp_path / "staged", download=download)


# --------------------------------------------------------------------------- the CLI offline

def test_cli_generate_then_verify(clean, tmp_path, backend, monkeypatch):
    entries = [e for e, _ in clean]
    manifest = tmp_path / "pool.json"
    pool.write_pool(entries, manifest, sources_included=["abc"])
    records = tmp_path / "injected.json"
    rc = gen.main(["generate", "--pool", str(manifest), "--inputs", str(tmp_path / "inputs"),
                   "--out", str(tmp_path / "out"), "--base-url", BASE, "--records", str(records),
                   "--kinds", "face_delete,unsew_perturb", "--seeds", "1", "--max-sources", "2",
                   "--image-digest", DIGEST], backend=backend)
    assert rc == 0 and records.exists()
    assert sum(1 for e in pool.read_pool(manifest) if e.source == "inject") == 4
    assert gen.main(["verify", "--pool", str(manifest), "--inputs", str(tmp_path / "inputs"),
                     "--records", str(records)], backend=backend) == 0
    # a source that went missing from the inputs makes verify fail, not pass
    for p in (tmp_path / "inputs").rglob("*.step"):
        p.unlink()
    assert gen.main(["verify", "--pool", str(manifest), "--inputs", str(tmp_path / "inputs"),
                     "--records", str(records)], backend=backend) != 0


def test_cli_refuses_a_quarantined_kind_with_a_nonzero_exit(clean, tmp_path, backend):
    manifest = tmp_path / "pool.json"
    pool.write_pool([e for e, _ in clean], manifest, sources_included=["abc"])
    rc = gen.main(["generate", "--pool", str(manifest), "--inputs", str(tmp_path / "inputs"),
                   "--out", str(tmp_path / "out"), "--base-url", BASE, "--records", str(tmp_path / "r.json"),
                   "--kinds", "seam_variant"], backend=backend)
    assert rc != 0 and backend.calls == []
    assert not any(e.source == "inject" for e in pool.read_pool(manifest))


def test_module_imports_without_the_cad_kernel():
    code = ("import importlib.util,sys;"
            f"s=importlib.util.spec_from_file_location('m','{EVAL / 'inject_pool.py'}');"
            "m=importlib.util.module_from_spec(s);sys.modules['m']=m;s.loader.exec_module(m);"
            "assert 'OCP' not in sys.modules")
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# --------------------------------------------------------------------------- build pinning

def _env(backend, digest=DIGEST):
    return gen.current_environment(backend, digest)


def test_each_recipe_records_the_build_that_made_it(clean, tmp_path, backend):
    res = gen.generate(clean, kinds=None, seeds=(0,), out_dir=tmp_path / "out", base_url=BASE, backend=backend,
                       environment=_env(backend))
    assert res.recipes
    for r in res.recipes:
        assert (r.occt_version, r.image_digest) == ("fake-ocp==7.9.1", DIGEST)
        assert len(r.inject_revision) == 64
    path = tmp_path / "injected.json"
    gen.write_records(path, res.recipes, skipped=[])
    assert gen.read_records(path, backend) == sorted(res.recipes, key=lambda r: r.sha256)


def test_verify_explains_a_hash_difference_by_a_build_mismatch(clean, tmp_path, backend):
    res = gen.generate(clean, kinds=["face_delete"], seeds=(0,), out_dir=tmp_path / "out", base_url=BASE,
                       backend=backend, environment=_env(backend))
    tampered = [dataclasses.replace(r, sha256="0" * 64) for r in res.recipes]
    entries = [dataclasses.replace(e, sha256="0" * 64) for e in res.entries]
    newer = _env(backend, "sha256:" + "e" * 64)
    problems = gen.verify(tampered, entries, _lookup(clean), backend, newer)
    assert problems and all("EXPECTED, the build differs" in p and "mesh image digest" in p for p in problems)
    same = gen.verify(tampered, entries, _lookup(clean), backend, _env(backend))
    assert same and not any("EXPECTED" in p for p in same)   # same build: a real mismatch, no excuse


def test_cli_verify_warns_loudly_on_a_build_mismatch_and_generate_refuses_unpinned(clean, tmp_path, backend, capsys):
    manifest = tmp_path / "pool.json"
    pool.write_pool([e for e, _ in clean], manifest, sources_included=["abc"])
    records = tmp_path / "injected.json"
    base = ["--pool", str(manifest), "--inputs", str(tmp_path / "inputs"), "--records", str(records)]
    unpinned = ["generate", *base, "--out", str(tmp_path / "out"), "--base-url", BASE, "--kinds", "face_delete"]
    assert gen.main(unpinned, backend=backend) == 2 and not records.exists()
    assert gen.main([*unpinned, "--image-digest", DIGEST], backend=backend) == 0
    capsys.readouterr()
    assert gen.main(["verify", *base, "--image-digest", DIGEST], backend=backend) == 0
    assert "BUILD MISMATCH" not in capsys.readouterr().err
    backend.OCCT = "fake-ocp==7.10.0"
    gen.main(["verify", *base, "--image-digest", "sha256:" + "f" * 64], backend=backend)
    err = capsys.readouterr().err
    assert "BUILD MISMATCH" in err and "7.9.1" in err and "7.10.0" in err and "mesh image digest" in err


# --------------------------------------------------------------------------- errors are not "not applicable"

def test_a_plain_value_error_from_the_injector_is_not_swallowed_as_a_skip(clean, tmp_path):
    class Broken(FakeBackend):
        RAISE = ValueError("STEP read failed")
    with pytest.raises(ValueError, match="STEP read failed"):
        _generate(clean, tmp_path, Broken(), kinds=["face_delete"])


# --------------------------------------------------------------------------- regeneration hazards

def test_deduped_counts_this_run_not_every_run(clean, tmp_path, backend):
    manifest = tmp_path / "pool.json"
    pool.write_pool([e for e, _ in clean], manifest, sources_included=["abc"])
    res = _generate(clean, tmp_path, backend, kinds=["face_delete"])
    dup = dataclasses.replace(res.entries[0], name="inject/dup")   # same sha256 as another: dropped by the dedupe
    for _ in range(3):
        gen.merge_into_pool(manifest, [*res.entries, dup])
    assert json.loads(manifest.read_text())["deduped"] == 1


def test_a_partial_run_keeps_earlier_entries_instead_of_truncating(clean, tmp_path, backend):
    manifest = tmp_path / "pool.json"
    pool.write_pool([e for e, _ in clean], manifest, sources_included=["abc"])
    records = tmp_path / "injected.json"
    argv = ["generate", "--pool", str(manifest), "--inputs", str(tmp_path / "inputs"), "--out", str(tmp_path / "out"),
            "--base-url", BASE, "--records", str(records), "--image-digest", DIGEST]
    assert gen.main([*argv, "--kinds", "face_delete,unsew_perturb"], backend=backend) == 0
    full = {r.sha256 for r in gen.read_records(records, backend)}
    assert len(full) == 4
    assert gen.main([*argv, "--kinds", "face_delete", "--max-sources", "1"], backend=backend) == 0
    assert {r.sha256 for r in gen.read_records(records, backend)} == full
    assert {e.sha256 for e in pool.read_pool(manifest) if e.source == "inject"} == full
    assert gen.main(["verify", "--pool", str(manifest), "--inputs", str(tmp_path / "inputs"),
                     "--records", str(records), "--image-digest", DIGEST], backend=backend) == 0


def test_a_rerun_refreshes_a_recipe_instead_of_duplicating_it(clean, tmp_path, backend):
    manifest = tmp_path / "pool.json"
    pool.write_pool([e for e, _ in clean], manifest, sources_included=["abc"])
    records = tmp_path / "injected.json"
    argv = ["generate", "--pool", str(manifest), "--inputs", str(tmp_path / "inputs"), "--out", str(tmp_path / "out"),
            "--base-url", BASE, "--records", str(records), "--image-digest", DIGEST, "--kinds", "face_delete"]
    for _ in range(2):
        assert gen.main(argv, backend=backend) == 0
    assert len(gen.read_records(records, backend)) == 2
    assert sum(1 for e in pool.read_pool(manifest) if e.source == "inject") == 2


# --------------------------------------------------------------------------- pool.py build

def test_pool_build_survives_inject_entries_with_no_inject_note(clean, tmp_path, backend):
    """A manifest written by write_pool without notes (so no source_notes.inject) must not break `build` (KeyError)."""
    res = _generate(clean, tmp_path, backend)
    manifest = tmp_path / "pool.json"
    pool.write_pool([e for e, _ in clean] + res.entries, manifest, sources_included=["abc", "inject"])
    assert "inject" not in (json.loads(manifest.read_text()).get("source_notes") or {})
    assert pool.main(["build", "--out", str(manifest)]) == 0
    doc = json.loads(manifest.read_text())
    assert doc["source_notes"]["inject"] == pool.INJECT_NOTE
    back = pool.read_pool(manifest)
    assert {e.sha256 for e in back if e.source == "inject"} == {e.sha256 for e in res.entries}
    assert all(e.licence for e in back)


def test_the_declined_stratum_is_opt_in_by_name_never_a_default_draw() -> None:
    defaults = tuple(k for k in KINDS if STATUS[k] == "structural")

    class Stratum(FakeBackend):
        KINDS = (*KINDS, "face_delete_large")
        KIND_STATUS = {**STATUS, "face_delete_large": "declined_stratum"}
        DEFAULT_KINDS = defaults
        ADMITTED_KINDS = (*defaults, "face_delete_large")

    assert "face_delete_large" not in gen.resolve_kinds(None, Stratum())
    assert gen.resolve_kinds(["face_delete_large"], Stratum()) == ["face_delete_large"]
    with pytest.raises(gen.RefusedKind):            # a quarantined kind is still refused beside it
        gen.resolve_kinds(["face_delete_large", "seam_variant"], Stratum())
