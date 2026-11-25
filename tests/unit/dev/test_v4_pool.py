"""v4_eval pool: dedupe, licence and NIST grouping, against local fixtures with no network."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "v4_pool", Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval/pool.py"
)
assert _SPEC is not None and _SPEC.loader is not None
pool = importlib.util.module_from_spec(_SPEC)
sys.modules["v4_pool"] = pool  # dataclasses resolve annotations through sys.modules
_SPEC.loader.exec_module(pool)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _track() -> dict:
    return {
        "sources": [
            {"name": "occt-dataset-7.8.0", "archive_url": "https://x/occt.tar.xz", "archive_sha256": "1" * 64},
            {"name": "abc-chunk-0051", "archive_url": "https://x/abc51.7z", "archive_sha256": "2" * 64},
        ],
        "files": [
            {"name": "abc/f1.step", "source": "abc-chunk-0051", "sha256": SHA_A},
            {"name": "occt/o1.stp", "source": "occt-dataset-7.8.0", "sha256": SHA_B},
        ],
    }


def _clean() -> dict:
    # the same bytes as the failing ABC file, reachable from a second source
    return {"0051": [{"name": "00519643/dup.step", "sha256": SHA_A},
                     {"name": "00519643/ok.step", "sha256": "c" * 64}]}


def _entry(**kw):
    base = {"name": "n", "source": "abc_fail", "sha256": SHA_A, "url": "u",
            "archive_sha256": None, "licence": "L"}
    base.update(kw)
    return pool.PoolEntry(**base)


def test_pool_dedupes_by_sha():
    sources = [pool.AbcSource(_track(), _clean())]
    entries = pool.build_pool(sources)
    shas = [e.sha256 for e in entries]
    assert len(shas) == len(set(shas)) == 3
    kept = next(e for e in entries if e.sha256 == SHA_A)
    assert kept.source == "abc_fail"  # first source wins


def test_pool_records_licence():
    entries = pool.build_pool([pool.AbcSource(_track(), _clean())])
    assert entries and all(e.licence for e in entries)
    with pytest.raises(ValueError, match="licence"):
        pool.build_pool([pool.StaticSource([_entry(licence="")])])


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _fake_download(archives: dict[str, bytes]):
    def download(url: str, dest: Path) -> None:
        dest.write_bytes(archives[url])
    return download


NIST_URL = "https://nist.test/document/step-files"
NIST_URL_2 = "https://nist.test/document/ftc-ctc"


def _nist(members: dict[str, bytes], **kw):
    return pool.NistSource([NIST_URL], _fake_download({NIST_URL: _zip_bytes(members)}),
                           **({"min_entries": 1} | kw))


def test_nist_variants_grouped():
    members = {"Z/AP203 geometry only/nist_ctc_01_asme1_rd.stp": b"one",
               "Z/AP203 with PMI/nist_ctc_01_asme1_ap203.stp": b"two",
               "Z/nist_ctc_01_asme1_ap242-e1.stp": b"three",
               "Z/nist_ftc_06_asme1_ap242-e2.stp": b"four",
               "Z/PDF/nist_ctc_01_asme1_rd.pdf": b"not cad",
               "Z/README.txt": b"skip"}
    entries = pool.build_pool([_nist(members, min_entries=4)])
    by_name = {e.name.rsplit("/", 1)[-1]: e for e in entries}
    assert len(entries) == 4
    assert {by_name[n].group for n in ("nist_ctc_01_asme1_rd.stp", "nist_ctc_01_asme1_ap203.stp",
                                       "nist_ctc_01_asme1_ap242-e1.stp")} == {"nist-ctc-01"}
    assert by_name["nist_ftc_06_asme1_ap242-e2.stp"].group == "nist-ftc-06"
    # hashes come from the member bytes and the archive file, never from the remote
    assert {e.sha256 for e in entries} == {hashlib.sha256(b).hexdigest()
                                           for b in (b"one", b"two", b"three", b"four")}
    assert {e.archive_sha256 for e in entries} == {hashlib.sha256(_zip_bytes(members)).hexdigest()}
    assert all(e.url == NIST_URL and e.source == "nist" for e in entries)


def test_nist_group_spellings():
    assert pool.nist_group("CTC 01") == pool.nist_group("nist_ctc_01_x.stp") == "nist-ctc-01"
    assert pool.nist_group("NIST_STC_10_asme1_ap242-e2.stp") == "nist-stc-10"


def test_nist_archive_without_step_raises():
    native_only = {"S/CATIA/nist_stc_06.CATPart": b"x", "S/STC_06.pdf": b"y"}
    with pytest.raises(ValueError, match="holds no STEP files"):
        pool.build_pool([_nist(native_only)])


def test_nist_below_minimum_raises():
    with pytest.raises(ValueError, match="expected at least 30"):
        pool.build_pool([pool.NistSource([NIST_URL], _fake_download(
            {NIST_URL: _zip_bytes({"Z/nist_ctc_01_a.stp": b"x"})}))])


def test_nist_file_without_part_id_raises():
    with pytest.raises(ValueError, match="cannot group"):
        pool.build_pool([_nist({"Z/mystery_model.stp": b"x"})])


def test_identical_nist_variants_collapse_across_archives_and_are_reported(caplog):
    first = _zip_bytes({"A/nist_ctc_02_asme1_rc.stp": b"same-bytes"})
    second = _zip_bytes({"B/nist_ctc_02_asme1_rc.stp": b"same-bytes"})
    source = pool.NistSource([NIST_URL, NIST_URL_2],
                             _fake_download({NIST_URL: first, NIST_URL_2: second}), min_entries=2)
    with caplog.at_level("WARNING"):
        entries = pool.build_pool([source])
    assert len(entries) == 1  # byte-identical exports are one file
    assert "group nist-ctc-02" in caplog.text and "dropped 1 duplicate" in caplog.text


F360_NAME = "s2.0.1/breps/step/{}.step"


def test_f360_hashes_members_and_archive():
    data = _zip_bytes({F360_NAME.format("123"): b"alpha", "s2.0.1/breps/seg/123.seg": b"skip",
                       "s2.0.1/readme.txt": b"skip"})
    entries = pool.build_pool([pool.F360Source("https://f360.test/ext.zip",
                                               _fake_download({"https://f360.test/ext.zip": data}),
                                               min_entries=1)])
    assert [e.name for e in entries] == [F360_NAME.format("123")]
    assert entries[0].sha256 == hashlib.sha256(b"alpha").hexdigest()
    assert entries[0].archive_sha256 == hashlib.sha256(data).hexdigest()
    assert entries[0].source == "f360" and "Autodesk" in entries[0].licence


def test_empty_f360_archive_raises():
    data = _zip_bytes({"nested.zip": b"not a step"})
    with pytest.raises(ValueError, match="yielded 0 STEP files"):
        pool.build_pool([pool.F360Source("https://f360.test/ext.zip",
                                         _fake_download({"https://f360.test/ext.zip": data}), min_entries=1)])


def test_f360_minimum_is_the_documented_count():
    assert pool.F360_MIN_ENTRIES == 42_912


def test_f360_scale_note_is_written_to_the_manifest(tmp_path):
    out = tmp_path / "pool.json"
    pool.write_pool([_entry(source="f360")], out, sources_included=["abc", "f360"],
                    notes=pool.SOURCE_NOTES)
    import json
    assert "scaled by a factor of 10" in json.loads(out.read_text())["source_notes"]["f360"]


def test_write_pool_records_sources_included(tmp_path):
    out = tmp_path / "pool.json"
    pool.write_pool([_entry()], out, sources_included=["abc"], deduped=2)
    import json
    manifest = json.loads(out.read_text())
    assert manifest["sources_included"] == ["abc"] and manifest["deduped"] == 2


def test_nist_note_says_variants_are_not_vendor_translations(tmp_path):
    out = tmp_path / "pool.json"
    pool.write_pool([_entry(source="nist")], out, sources_included=["abc", "nist"], notes=pool.SOURCE_NOTES)
    import json
    note = json.loads(out.read_text())["source_notes"]["nist"]
    assert "NOT distinct vendor translations" in note and "single STEP each" in note


def test_licences_are_interned_and_round_trip(tmp_path):
    out = tmp_path / "pool.json"
    entries = [_entry(name="a", sha256="1" * 64), _entry(name="b", sha256="2" * 64),
               _entry(name="c", sha256="3" * 64, licence="OTHER")]
    pool.write_pool(entries, out, sources_included=["abc"])
    import json
    manifest = json.loads(out.read_text())
    assert len(manifest["licences"]) == 2 and all("licence" not in r for r in manifest["files"])
    assert all(r["licence_id"] in manifest["licences"] for r in manifest["files"])
    assert sorted(pool.read_pool(out), key=lambda e: e.name) == entries


def test_write_pool_refuses_an_empty_licence(tmp_path):
    with pytest.raises(ValueError, match="licence"):
        pool.write_pool([_entry(licence=" ")], tmp_path / "pool.json", sources_included=["abc"])


def test_occt_licence_carries_unverified_marker():
    assert pool.LICENCE["occt"].startswith("UNVERIFIED")


def test_archives_are_interned_and_round_trip(tmp_path):
    out = tmp_path / "pool.json"
    shared = {"url": "https://x/a.zip", "archive_sha256": "9" * 64}
    entries = [_entry(name=f"m{i}", sha256=f"{i:064x}", **shared) for i in range(3)]
    entries.append(_entry(name="solo", sha256="f" * 64, url="https://x/b.zip", archive_sha256=None))
    pool.write_pool(entries, out, sources_included=["abc"])
    import json
    manifest = json.loads(out.read_text())
    assert manifest["version"] == 3 and len(manifest["archives"]) == 2
    assert all({"url", "archive_sha256"}.isdisjoint(r) for r in manifest["files"])
    assert sorted(pool.read_pool(out), key=lambda e: e.name) == sorted(entries, key=lambda e: e.name)


def test_write_pool_refuses_an_empty_url(tmp_path):
    with pytest.raises(ValueError, match="url is required"):
        pool.write_pool([_entry(url=" ")], tmp_path / "pool.json", sources_included=["abc"])


# ------------------------------------------------------------------ inject entries carry their source's licence

def _clean_part(source: str = "abc_pass", licence: str | None = None):
    return pool.PoolEntry("part.step", source, SHA_A, "https://x/abc.7z", SHA_B,
                          licence if licence is not None else pool.LICENCE["abc"])


def _derived(clean, sha: str = "e" * 64):
    return pool.inject_entry(clean, name="inject/part.step", sha256=sha, url=f"https://h/{sha}/part.step",
                             label={"kind": "inject_kind", "status": "structural"})


def test_an_inject_entry_derives_its_licence_and_group_from_its_clean_source() -> None:
    clean = _clean_part()
    entry = _derived(clean)
    assert entry.licence == clean.licence  # verbatim
    assert entry.group == pool.inject_group(clean) == f"inject-{SHA_A[:12]}"
    assert (entry.kind, entry.kind_status, entry.source, entry.archive_sha256) == (
        "inject_kind", "structural", "inject", None)
    import inspect
    assert "licence" not in inspect.signature(pool.inject_entry).parameters  # it cannot be passed


@pytest.mark.parametrize("source", ["abc_fail", "inject", "f360"])
def test_an_inject_entry_cannot_be_derived_from_an_uncleared_or_damaged_source(source) -> None:
    with pytest.raises(ValueError, match="cannot derive"):
        _derived(_clean_part(source))


def test_a_hand_built_inject_entry_with_another_licence_is_refused_on_write(tmp_path) -> None:
    clean = _clean_part()
    forged = pool.PoolEntry("inject/part.step", "inject", "e" * 64, "https://h/x", None, "test licence",
                            pool.inject_group(clean), kind="k", kind_status="structural")
    with pytest.raises(ValueError, match="licence differs from its clean source"):
        pool.write_pool([clean, forged], tmp_path / "pool.json", sources_included=["abc", "inject"])
    assert not (tmp_path / "pool.json").exists()
    pool.write_pool([clean, _derived(clean)], tmp_path / "ok.json", sources_included=["abc", "inject"])
    back = {e.name: e for e in pool.read_pool(tmp_path / "ok.json")}
    assert back["inject/part.step"].licence == clean.licence


def test_the_licence_check_follows_the_source_when_its_licence_changes(tmp_path) -> None:
    """An entry derived under the old terms is stale once the source's licence text is corrected."""
    old = _clean_part(licence="old terms")
    entry = _derived(old)
    with pytest.raises(ValueError, match="licence differs"):
        pool.write_pool([_clean_part(licence="new terms"), entry], tmp_path / "p.json", sources_included=["abc", "inject"])


def test_a_pool_with_no_inject_entries_is_unchanged_by_the_licence_check(tmp_path) -> None:
    pool.write_pool([_clean_part()], tmp_path / "p.json", sources_included=["abc"])
    manifest = json.loads((tmp_path / "p.json").read_text())
    assert manifest["version"] == 3
    assert all("kind" not in row and "kind_status" not in row for row in manifest["files"])
