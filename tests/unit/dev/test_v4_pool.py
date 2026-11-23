"""v4_eval pool: dedupe, licence and NIST grouping, against local fixtures with no network."""

from __future__ import annotations

import hashlib
import importlib.util
import io
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


def test_nist_variants_grouped():
    page = ('<a href="/files/nist_ctc_01_asme1_ap203.stp">a</a>'
            '<a href="/files/NIST_CTC_01_ASME1_AP242.stp">b</a>'
            '<a href="/files/nist_ftc_06_asme1_ap242.stp">c</a>'
            '<a href="/about">not a model</a>')
    blobs = {"https://nist.test/files/nist_ctc_01_asme1_ap203.stp": b"one",
             "https://nist.test/files/NIST_CTC_01_ASME1_AP242.stp": b"two",
             "https://nist.test/files/nist_ftc_06_asme1_ap242.stp": b"three"}
    entries = pool.build_pool([pool.NistSource("https://nist.test/page", lambda u: page.encode()
                                               if u.endswith("page") else blobs[u], min_entries=3)])
    groups = {e.name: e.group for e in entries}
    assert len(entries) == 3
    assert groups["nist_ctc_01_asme1_ap203.stp"] == groups["NIST_CTC_01_ASME1_AP242.stp"] == "nist-ctc-01"
    assert groups["nist_ftc_06_asme1_ap242.stp"] == "nist-ftc-06"
    # the hash is computed from the downloaded bytes, never taken from the page
    assert {e.sha256 for e in entries} == {hashlib.sha256(b).hexdigest() for b in blobs.values()}


def test_f360_hashes_members_and_archive():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a/123.step", b"alpha")
        z.writestr("a/readme.txt", b"skip")
    data = buf.getvalue()
    entries = pool.build_pool([pool.F360Source("https://f360.test/ext.zip", lambda u: data, min_entries=1)])
    assert [e.name for e in entries] == ["a/123.step"]
    assert entries[0].sha256 == hashlib.sha256(b"alpha").hexdigest()
    assert entries[0].archive_sha256 == hashlib.sha256(data).hexdigest()
    assert entries[0].source == "f360"


def test_empty_nist_page_raises():
    page = b'<a href="/files/models.zip">zipped package</a>'
    with pytest.raises(ValueError, match="yielded 0 STEP files"):
        pool.build_pool([pool.NistSource("https://nist.test/page", lambda u: page, min_entries=1)])


def test_nist_below_minimum_raises():
    page = b'<a href="/f/nist_ctc_01_a.stp">a</a>'
    with pytest.raises(ValueError, match="expected at least 20"):
        pool.build_pool([pool.NistSource("https://nist.test/page", lambda u: page if u.endswith("page") else b"x")])


def test_nist_file_without_part_id_raises():
    page = b'<a href="/f/mystery_model.stp">a</a>'
    with pytest.raises(ValueError, match="cannot group"):
        pool.build_pool([pool.NistSource("https://nist.test/page", lambda u: page if u.endswith("page") else b"x",
                                         min_entries=1)])


def test_empty_f360_archive_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("nested.zip", b"not a step")
    with pytest.raises(ValueError, match="yielded 0 STEP files"):
        pool.build_pool([pool.F360Source("https://f360.test/ext.zip", lambda u: buf.getvalue(), min_entries=1)])


def test_identical_nist_variants_collapse_and_are_reported(caplog):
    page = (b'<a href="/f/nist_ctc_02_asme1_ap203.stp">a</a><a href="/f/nist_ctc_02_asme1_ap242.stp">b</a>')
    with caplog.at_level("WARNING"):
        entries = pool.build_pool([pool.NistSource(
            "https://nist.test/page", lambda u: page if u.endswith("page") else b"same-bytes", min_entries=1)])
    assert len(entries) == 1  # byte-identical exports are one file
    text = caplog.text
    assert "group nist-ctc-02" in text and "byte-identical" in text and "dropped 1 duplicate" in text


def test_write_pool_records_sources_included(tmp_path):
    out = tmp_path / "pool.json"
    pool.write_pool([_entry()], out, sources_included=["abc"], deduped=2)
    import json
    manifest = json.loads(out.read_text())
    assert manifest["sources_included"] == ["abc"] and manifest["deduped"] == 2
