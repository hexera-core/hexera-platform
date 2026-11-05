# Responsibility: Verify an engine is implemented or planned with nothing between, citing evidence per capability.
from __future__ import annotations

import dataclasses
from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.engines import registry as ec  # noqa: E402
from meshpipeline.engines.base import EngineSpec  # noqa: E402

_FORBIDDEN = ("experimental", "wrapper_supported", "wrapper-supported",
              "upstream_supported", "upstream-supported", "wrapper-unsupported",
              "not wired", "not implemented in this wrapper", "release_status",
              "validation_tier", "hidden")


def test_engine_spec_has_no_status_field():
    names = {f.name for f in dataclasses.fields(EngineSpec)}
    for bad in ("release_status", "capability_status", "validation_tier"):
        assert bad not in names, f"EngineSpec must not carry a maturity field: {bad}"


def test_runtime_metadata_carries_no_multistate_vocabulary():
    for n in ec.engine_names():
        sp = ec.get_spec(n)
        for field in ("descriptor", "intake_guidance", "validation_notes"):
            text = (getattr(sp, field, "") or "").lower()
            for bad in _FORBIDDEN:
                assert bad not in text, f"{n}.{field} contains {bad!r}"


def test_catalog_menu_has_no_status_disclosures():
    menu = ec.catalog_menu().lower()
    for bad in ("experimental", "not yet validated", "evidence level"):
        assert bad not in menu, f"intake menu leaks a maturity state: {bad!r}"


def test_every_implemented_engine_cites_its_evidence():
    for n in ec.engine_names():
        sp = ec.get_spec(n)
        if sp.implemented:
            assert (sp.validation_notes or "").strip(), \
                f"{n} registers capabilities but cites no delivery evidence"


def _load_evidence() -> dict:
    import json
    path = APP / "engines" / "validation_evidence.json"
    assert path.is_file(), f"evidence record missing: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_evidence_record_is_well_formed():
    data = _load_evidence()
    assert isinstance(data.get("evidence"), list) and data["evidence"], "no evidence entries"
    for e in data["evidence"]:
        for key in ("engine", "input_kind", "output_kind", "supported", "tier", "deliveries"):
            assert key in e, f"evidence entry missing {key!r}: {e}"
        assert e["supported"] is True, f"non-supported entry has no place in the record: {e}"
        assert e["deliveries"], f"{e['engine']}: no deliveries listed"
        for d in e["deliveries"]:
            assert d.get("job") and d.get("date") and d.get("summary"), (
                f"{e['engine']}: a delivery lacks job/date/summary: {d}")


def test_every_registered_capability_has_supported_evidence():
    data = _load_evidence()
    recorded = {(e["engine"], e["input_kind"], e["output_kind"])
                for e in data["evidence"] if e.get("supported") is True}
    for n in ec.engine_names():
        sp = ec.get_spec(n)
        if not sp.implemented:
            continue
        for c in sp.capabilities:
            assert (n, c.input_kind, c.output_kind) in recorded, (
                f"{n}: capability {c.input_kind} → {c.output_kind} is registered but has "
                "no supported entry in engines/validation_evidence.json - deliver it first")


def test_the_evidence_record_carries_no_orphan_capabilities():
    data = _load_evidence()
    registered = set()
    for n in ec.engine_names():
        sp = ec.get_spec(n)
        if sp.implemented:
            registered |= {(n, c.input_kind, c.output_kind) for c in sp.capabilities}
    for e in data["evidence"]:
        key = (e["engine"], e["input_kind"], e["output_kind"])
        assert key in registered, (
            f"evidence entry {key} matches no registered capability - stale record")
