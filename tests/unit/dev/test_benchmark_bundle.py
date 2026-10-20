# Responsibility: Pin the publication bundle envelope: layout, manifest completeness, digest
#                 verification, and the loader's refusal of partial or tampered bundles.
# Boundaries: hermetic - temp directories and dicts only. No CAD, no git beyond a throwaway repo.
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "benchmark_bundle_under_test", REPO / "devtools" / "validation" / "benchmark_bundle.py")
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec because the module defines a dataclass: field resolution looks the
    # module up in sys.modules, and an unregistered module makes dataclasses raise on import.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bb = _load()


def _manifest(**overrides):
    base = dict(
        benchmark="cad-repair", track="occt-real-defect", corpus_name="OCCT 7.8.0 bug corpus",
        corpus_provenance="fetched from OCCT bug tracker attachments; see docs/research",
        redistribution_status="review-pending", input_hashes={"a.step": "0" * 64},
        engine_commit="deadbeef", settings_digest="", hardware_class="dev-host-arm64")
    base.update(overrides)
    return bb.BundleManifest(**base)


def test_round_trip_preserves_rows_settings_and_manifest(tmp_path):
    writer = bb.BundleWriter(tmp_path / "bundle")
    writer.write_row({"file": "a.step", "before": 3, "after": 0})
    writer.write_row({"file": "b.step", "before": 1, "after": 1})
    writer.seal(_manifest(), settings={"limit": 2}, summary={"repair_yield": 0.5})

    manifest, rows, summary, settings = bb.load_bundle(tmp_path / "bundle")
    assert manifest["benchmark"] == "cad-repair"
    assert manifest["schema_version"] == 1
    assert [r["file"] for r in rows] == ["a.step", "b.step"]
    assert summary["row_count"] == 2
    assert summary["repair_yield"] == 0.5
    assert settings == {"limit": 2}


def test_seal_refuses_unknown_redistribution_status(tmp_path):
    writer = bb.BundleWriter(tmp_path / "bundle")
    with pytest.raises(ValueError, match="redistribution_status"):
        writer.seal(_manifest(redistribution_status="probably-fine"), settings={}, summary={})


def test_loader_refuses_partial_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "rows.jsonl").write_text("{}\n")
    with pytest.raises(FileNotFoundError, match="missing"):
        bb.load_bundle(bundle)


def test_loader_refuses_tampered_settings(tmp_path):
    writer = bb.BundleWriter(tmp_path / "bundle")
    writer.seal(_manifest(), settings={"limit": 2}, summary={})
    (tmp_path / "bundle" / "settings.json").write_text(json.dumps({"limit": 99}))
    with pytest.raises(ValueError, match="settings_digest"):
        bb.load_bundle(tmp_path / "bundle")


def test_loader_refuses_row_count_mismatch(tmp_path):
    writer = bb.BundleWriter(tmp_path / "bundle")
    writer.write_row({"file": "a.step"})
    writer.seal(_manifest(), settings={}, summary={})
    with (tmp_path / "bundle" / "rows.jsonl").open("a") as handle:
        handle.write(json.dumps({"file": "sneaky.step"}) + "\n")
    with pytest.raises(ValueError, match="row_count"):
        bb.load_bundle(tmp_path / "bundle")


def test_hash_inputs_keys_are_corpus_relative(tmp_path):
    (tmp_path / "sub").mkdir()
    file_a = tmp_path / "sub" / "a.step"
    file_a.write_bytes(b"solid")
    hashes = bb.hash_inputs(tmp_path, [file_a])
    assert list(hashes) == ["sub/a.step"]
    assert len(hashes["sub/a.step"]) == 64
