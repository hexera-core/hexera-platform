# Responsibility: Verify a declared patch set is delivered exactly, with no fuzzy match and no silent opt-out.
from __future__ import annotations

import json

import pytest

from meshpipeline.engines.contract import check_contract, contract_applicable
from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.registry import engine_names, get_spec

# A representative approved contract: two separately-named walls + a farfield.
_FIVE_ISH = [{"name": "wing", "type": "wall"}, {"name": "fuselage", "type": "wall"},
             {"name": "farfield", "type": "farfield"}]


def _delivered(names_types: dict) -> tuple[list, dict]:
    return list(names_types), dict(names_types)


# the shared contract: empty vs non-empty semantics (section 2 + 3)

def test_empty_declaration_is_not_applicable_never_fabricated():
    assert contract_applicable([]) is False
    assert contract_applicable(None) is False
    assert contract_applicable([{"name": "", "type": ""}]) is False   # normalises to nothing
    assert check_contract([], ["anything"], {"anything": "wall"}) == (True, "")


def test_non_empty_declaration_with_missing_or_empty_delivered_fails():
    assert contract_applicable(_FIVE_ISH) is True
    ok, _ = check_contract(_FIVE_ISH, [], {})
    assert ok is False
    ok, _ = check_contract(_FIVE_ISH, None, None)     # malformed delivered
    assert ok is False
    ok, _ = check_contract(_FIVE_ISH, "not-a-list", "not-a-dict")
    assert ok is False


def test_exact_delivery_passes_and_ordering_does_not_matter():
    p, t = _delivered({"wing": "wall", "fuselage": "wall", "farfield": "farfield"})
    assert check_contract(_FIVE_ISH, p, t)[0] is True
    assert check_contract(_FIVE_ISH, list(reversed(p)), t)[0] is True   # order-independent


def test_every_semantic_loss_fails():
    # one patch absent
    assert check_contract(_FIVE_ISH, *_delivered({"wing": "wall", "farfield": "farfield"}))[0] is False
    # two walls merged into one
    assert check_contract(_FIVE_ISH, *_delivered({"body": "wall", "farfield": "farfield"}))[0] is False
    # renamed
    assert check_contract(_FIVE_ISH, *_delivered(
        {"leftwing": "wall", "fuselage": "wall", "farfield": "farfield"}))[0] is False
    # role changed (wall -> farfield)
    assert check_contract(_FIVE_ISH, *_delivered(
        {"wing": "farfield", "fuselage": "wall", "farfield": "farfield"}))[0] is False


def test_no_fuzzy_matching_a_near_name_is_still_a_miss():
    # "fuselage1" is not "fuselage" - identity is exact, never string-similarity
    assert check_contract(_FIVE_ISH, *_delivered(
        {"wing": "wall", "fuselage1": "wall", "farfield": "farfield"}))[0] is False


# registry-driven parity: every implemented engine declares AND executes the gate

def test_every_implemented_engine_declares_a_user_contract_gate_or_opts_out_explicitly():
    for name in engine_names():
        sp = get_spec(name)
        key = sp.user_contract_gate_key
        if key is None:
            assert sp.user_contract_optout_reason, (
                f"{name} declares no user-contract gate and gives no opt-out reason - "
                "silence is not a valid policy")
            continue
        # the declared gate key must be present in the EXECUTABLE chain, not just named
        chain = {g.key for g in sp.gates}
        assert key in chain, (
            f"{name} declares user_contract_gate_key={key!r} but no such gate is in its chain "
            f"{sorted(chain)}")


def test_no_implemented_engine_silently_opts_out():
    for name in engine_names():
        sp = get_spec(name)
        assert sp.user_contract_gate_key is not None, (
            f"{name} opted out of the user patch contract - but it consumes intake_patches")


# the gate runs through the REAL executable chain, not an isolated check

def _manifest(ws, patch_types: dict, quality: dict | None = None):
    (ws / "mesh_manifest.json").write_text(json.dumps({
        "schema_version": "2.1",
        "patches": {n: [] for n in patch_types},
        "patch_types": patch_types,
        "quality": quality or {"cells": 1000, "fatal": []},
    }))


def _user_contract_gate(name):
    sp = get_spec(name)
    return next(g for g in sp.gates if g.key == sp.user_contract_gate_key)


@pytest.mark.parametrize("name", engine_names())
def test_the_gate_executes_and_rejects_a_dropped_patch_for_every_engine(name, tmp_path):
    gate = _user_contract_gate(name)
    # exact delivery → the gate passes
    _manifest(tmp_path, {"wing": "wall", "fuselage": "wall", "farfield": "farfield"})
    ctx = GateCtx(workspace=tmp_path, engine=name, intake_patches=list(_FIVE_ISH))
    ok, _ = gate.check(GateCtx(workspace=tmp_path, engine=name, intake_patches=list(_FIVE_ISH)))
    assert ok is True, f"{name}: exact delivery should pass the contract gate"

    # a merged wall → the gate rejects, terminally
    _manifest(tmp_path, {"body": "wall", "farfield": "farfield"})
    ok, diag = gate.check(GateCtx(workspace=tmp_path, engine=name, intake_patches=list(_FIVE_ISH)))
    assert ok is False and "CONTRACT" in diag, f"{name}: a merged patch must fail the gate"

    # not applicable when the user declared nothing
    ok, _ = gate.check(GateCtx(workspace=tmp_path, engine=name, intake_patches=[]))
    assert ok is True, f"{name}: an empty contract is not-applicable, not a failure"
