# Responsibility: Pin what the cheap tier is for and what it may never do. It ranks search states and judges each
#                 staged-healer transaction, so it must see the panel member whose failures the full panel kept
#                 catching (gmsh_prod, lane B8); it is a strict subset of the full panel, judged with the full
#                 panel's own member configs, and it can never certify a file clean.
# Boundaries: hermetic: member results are faked; no mesher runs.
from __future__ import annotations

import json

from meshpipeline.cad.repair_v4.verdict import assemble
from meshpipeline.cad.repair_v4.verdict.assemble import Declared, clean_of, default_members
from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
from meshpipeline.cad.repair_v4.verdict.panel import MemberResult
from meshpipeline.cad.repair_v4.verdict.valid import ValidReport


def test_the_cheap_tier_meshes_gmsh_default_and_gmsh_prod():
    """Snapshot v1: 11 of 11 states the cheap tier passed failed the full panel, every one on gmsh_prod alone
    (low_quality, or HXT 3D failing). Re-judged natively, layer 1 + gmsh_default agreed with the full panel on 6 of
    22 states and layer 1 + gmsh_default + gmsh_prod on 21 of 22."""
    assert [m.name for m in default_members("cheap")] == ["gmsh_default", "gmsh_prod"]


def test_the_cheap_tier_is_a_subset_of_the_full_panel_with_the_same_configs():
    """A cheap member is the FULL panel's own member, configured identically: a cheap failure is then a full failure
    by construction, so adding members to the cheap tier can only remove cheap-pass/full-fail disagreements, never
    create cheap-fail/full-pass ones."""
    full = {m.name: json.dumps(m.config, sort_keys=True) for m in default_members("full")}
    for m in default_members("cheap"):
        assert m.name in full and json.dumps(m.config, sort_keys=True) == full[m.name]
    assert len(default_members("cheap")) < len(default_members("full"))


def test_the_full_panel_is_unchanged():
    assert [m.name for m in default_members("full")] == ["gmsh_prod", "gmsh_default", "gmsh_alt", "netgen_occ"]


def _results(**status: str) -> list[MemberResult]:
    return [MemberResult(name=n, status=s, min_sicn=0.2 if s == "pass" else None) for n, s in status.items()]


def test_a_state_only_gmsh_prod_fails_is_no_longer_a_cheap_pass(monkeypatch):
    """The v1 miss: gmsh_default passes, gmsh_prod fails. The cheap verdict now scores it below a state both pass."""
    valid = ValidReport(True, [], [], "mm", 1, True)
    faithful = FaithfulReport(True, 0.0, 0.1, 1.0, 1.0, 1, 1)
    monkeypatch.setattr(assemble, "check_valid", lambda *a, **k: valid)
    monkeypatch.setattr(assemble, "check_faithful_steps", lambda *a, **k: faithful)
    v_miss = assemble._assemble(None, "s.step", "o.step", Declared(), "cheap", None, None,
                                lambda *a, **k: _results(gmsh_default="pass", gmsh_prod="fail"), "mm")
    v_ok = assemble._assemble(None, "s.step", "o.step", Declared(), "cheap", None, None,
                              lambda *a, **k: _results(gmsh_default="pass", gmsh_prod="pass"), "mm")
    assert assemble.score(v_miss)[1] == 1 and assemble.score(v_ok)[1] == 2
    assert assemble.score(v_miss) < assemble.score(v_ok)
    assert not v_miss.clean and not v_ok.clean          # a cheap verdict never certifies clean, however it scores
    assert not clean_of(tier="cheap", inconclusive=False, valid=valid, panel=_results(gmsh_default="pass",
                                                                                         gmsh_prod="pass"),
                        faithful=faithful)


def test_the_agreement_cells_count_a_cheap_pass_only_when_every_cheap_member_passed():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "devtools/validation/v4_eval"))
    import arms

    cells = arms.cheap_full_cells([
        {"cheap_score": [1, 2, 0.2, 0.0, 0], "clean": True},     # both cheap members pass
        {"cheap_score": [1, 1, 0.2, 0.0, 0], "clean": False},    # one passes (the v1 miss shape): a cheap FAIL now
    ])
    assert cells["cheap_pass_full_pass"] == 1 and cells["cheap_fail_full_fail"] == 1
    assert cells["cheap_pass_full_fail"] == 0
