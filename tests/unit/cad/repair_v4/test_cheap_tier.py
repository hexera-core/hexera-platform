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


# --- state-adaptive: the cheap tier also meshes every member the START state fails (lane B8, D7's evidence) -------


def _start(**status: str):
    from types import SimpleNamespace
    return SimpleNamespace(panel=_results(**status))


def test_the_adaptive_cheap_tier_adds_every_member_the_start_fails():
    start = _start(gmsh_prod="pass", gmsh_default="pass", gmsh_alt="fail", netgen_occ="fail")
    names = [m.name for m in assemble.adaptive_cheap_members(start)]
    assert names == ["gmsh_default", "gmsh_prod", "gmsh_alt", "netgen_occ"]


def test_the_adaptive_cheap_tier_keeps_gmsh_prod_even_when_the_start_passes_everything():
    """A CLEAN start fails nothing, but a stage can still break gmsh_prod (B5's `condition`): the static pair stays."""
    start = _start(gmsh_prod="pass", gmsh_default="pass", gmsh_alt="pass", netgen_occ="pass")
    assert [m.name for m in assemble.adaptive_cheap_members(start)] == ["gmsh_default", "gmsh_prod"]


def test_the_adaptive_cheap_tier_never_invents_a_member_and_uses_the_full_panels_configs():
    import json
    start = _start(gmsh_prod="fail", gmsh_default="fail", gmsh_alt="fail", netgen_occ="crashed", m_unknown="fail")
    full = {m.name: json.dumps(m.config, sort_keys=True) for m in default_members("full")}
    got = assemble.adaptive_cheap_members(start)
    assert [m.name for m in got] == ["gmsh_default", "gmsh_prod", "gmsh_alt", "netgen_occ"]
    assert all(json.dumps(m.config, sort_keys=True) == full[m.name] for m in got)
    assert assemble.adaptive_cheap_members(None) and [m.name for m in assemble.adaptive_cheap_members(None)] == \
        ["gmsh_default", "gmsh_prod"]


def test_the_search_judges_cheap_with_the_members_its_start_fails(monkeypatch):
    """The search's start is judged FULL; its failing members join every cheap verdict of that search."""
    from meshpipeline.cad.repair_v4 import search as S

    seen: list = []
    judge = S.default_judge(h_mm=1.0, cache=None)
    monkeypatch.setattr(S, "cheap_verdict",
                        lambda pool, step, original, **kw: seen.append([m.name for m in kw["members"]]) or None)
    cheap = S.adaptive_cheap(judge, _start(gmsh_prod="pass", gmsh_default="pass", gmsh_alt="fail",
                                           netgen_occ="pass"))
    monkeypatch.setattr(S, "_judged", lambda fn, pool, ws, original, cache, **kw: fn(pool, "s", original, **kw))
    cheap(None, None, "o")
    assert seen == [["gmsh_default", "gmsh_prod", "gmsh_alt"]]


def test_stages_judge_their_transactions_with_the_members_they_are_given(monkeypatch):
    from meshpipeline.cad.repair_v4 import stages

    seen: list = []
    monkeypatch.setattr(stages, "original_facts", lambda pool, brep: (6, 1))
    monkeypatch.setattr(stages, "step_for", lambda pool, brep: brep)

    def cheap(pool, step, original, **kw):
        seen.append([m.name for m in kw.get("members") or []])
        raise stages.InconclusiveVerdict("stop here")

    monkeypatch.setattr(stages, "cheap_verdict", cheap)
    ws = stages.Workspace(brep_path="s0", ids={}, labels={}, history=[], original="s0")
    members = assemble.adaptive_cheap_members(_start(gmsh_prod="pass", gmsh_default="pass", gmsh_alt="fail",
                                                     netgen_occ="pass"))
    assert stages._judged(None, ws, cheap_members=members) is None
    assert seen and all(s == ["gmsh_default", "gmsh_prod", "gmsh_alt"] for s in seen)
