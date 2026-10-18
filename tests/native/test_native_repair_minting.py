# Responsibility: Prove review minting lines up with the real kernel: rim pairing and tried-operation matching.
# Boundaries: native tier. The mocked unit tests cover the union-find and matcher arithmetic; this
#             proves the signatures, wire shapes and recorded target format they assume are real.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_loose_faces_step, write_missing_bore_wall_step

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.application import repair_review_minting as minting
from meshpipeline.cad.repair.brep import _read_shape, inspect_brep_file
from meshpipeline.cad.repair.conservative import RepairRefused, repair_step_file
from meshpipeline.cad.repair.plan import RunHistory
from meshpipeline.cad.repair.triage import defect_from_entity, split

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)


def test_the_two_rims_of_a_real_missing_bore_wall_share_one_geometric_pairing_key(
        tmp_path, monkeypatch):
    import meshpipeline.cad.repair.conservative as conservative
    import meshpipeline.cad.repair.operations as operations

    source = write_missing_bore_wall_step(tmp_path / "bore.step")
    report = {"report": inspect_brep_file(source).to_dict()}
    rims = [e for e in report["report"]["entities"] if e["code"] == "open_shell"]
    assert len(rims) == 2 and all(r["fingerprint"] for r in rims)

    # spy on the REAL functions, so a signature mismatch cannot hide behind the fallback
    wires, paired = [], []
    real_wire, real_paired = operations.free_wire_containing, conservative._paired_rims

    def _wire(shape, index):
        found = real_wire(shape, index)
        wires.append(found)
        return found

    def _pairs(loops, shape=None):
        found = real_paired(loops, shape=shape)
        paired.append((len(loops), found))
        return found

    monkeypatch.setattr(operations, "free_wire_containing", _wire)
    monkeypatch.setattr(conservative, "_paired_rims", _pairs)

    history = RunHistory()
    for rim in rims:
        history.refuse(rim["fingerprint"], "paired_rim", "this loop pairs with another")
    shape = _read_shape(source)
    items = minting.mint_items(
        shape=shape, report=report, split=split(report),
        repair_run_measured={"survivors": rims, "operations_kept": 1,
                             "refusals": history.refusals})

    # (the per-item rules proposal also resolves a wire, hence >=)
    assert len(wires) >= 2 and all(w is not None for w in wires), "wire lookup did not run"
    assert (2, {0, 1}) in paired, "the real discriminator did not pair the two rims"
    keys = {i["pairing_key"] for i in items}
    assert len(items) == 2 and len(keys) == 1 and "" not in keys
    assert keys == {",".join(sorted(r["fingerprint"] for r in rims))}


def test_the_real_recorded_targets_credit_the_defect_the_operation_repaired(tmp_path):
    source = write_loose_faces_step(tmp_path / "loose.step")
    scanned = [defect_from_entity(e) for e in inspect_brep_file(source).entities]
    assert scanned and all(d is not None for d in scanned)

    try:
        report = repair_step_file(source, tmp_path / "loose-repaired.step")
        operations = list(report.operations)
    except RepairRefused as refused:
        operations = list(refused.measurements["operations"])
    assert operations, "the loop recorded no attempt to credit"

    credited = [d for d in scanned if minting._tried(d, operations)]
    assert credited, (f"no scanned defect matched any recorded target: "
                      f"{[op['targets'] for op in operations]} vs "
                      f"{[d.fingerprint for d in scanned]}")
