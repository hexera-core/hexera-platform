# Responsibility: Lane C's D21 grid: body ops at sites the det sweep does not reach (kind-gated, one keeper over many,
#                 past the pair cap, whole-group fuse), never repeating a det row; and NEW/OLD/unplaced classification.
# Boundaries: hermetic; fake sites and workspaces.
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "devtools" / "validation" / "v4_eval"))

import oracle_grid  # noqa: E402


def _site(kind: str, entities: list[str], whole_part: bool | None = None) -> SimpleNamespace:
    graph = {} if whole_part is None else {"coarse": {"whole_part": whole_part}}
    return SimpleNamespace(kind=kind, entities=entities, graph=graph)


def _rows(*sites: SimpleNamespace) -> list[tuple]:
    return [(c.op, tuple(c.targets), c.params.get("mode")) for c in oracle_grid.extra_rows(SimpleNamespace(sites=list(sites)))]


def test_a_self_overlap_site_with_bodies_gets_the_cuts_det_never_proposes() -> None:
    rows = _rows(_site("self_overlap", ["B10", "B13", "B14", "F18", "F20"]))
    assert ("conformalise", ("B10", "B14"), "cut_by:B14") in rows  # 0057's resolving cut
    assert ("conformalise", ("B10", "B13", "B14"), "cut_by:B14") in rows  # one keeper over all
    assert ("fuse", ("B10", "B13", "B14"), None) in rows
    assert ("conformalise", ("B10", "B13", "B14"), "fragment") in rows


def test_an_overlap_site_skips_the_det_pairs_and_adds_the_rest() -> None:
    rows = _rows(_site("overlap", ["B1", "B2", "B3", "B4"]))
    det_pairs = {("B1", "B2"), ("B1", "B3"), ("B1", "B4")}  # the first _MAX_CUT_PAIRS combinations
    pair_rows = {r[1] for r in rows if len(r[1]) == 2}
    assert not (pair_rows & det_pairs) and ("B2", "B3") in pair_rows
    assert ("conformalise", ("B1", "B2", "B3", "B4"), "cut_by:B4") in rows  # 0051's B4 over all four
    assert not any(r[2] == "fragment" for r in rows)  # det already fragments an overlap site


def test_one_body_or_none_gives_nothing() -> None:
    assert _rows(_site("sliver", ["F1", "F2"]), _site("overlap", ["B1", "F3"])) == []


def test_annotate_splits_new_old_unplaced_and_unlocated() -> None:
    parent = SimpleNamespace(ids={"F1": "fp1", "F2": "fp2"})
    new = SimpleNamespace(ids={"F1": "fp1", "F2": "fp2x", "F3": "fp3"})
    loc = lambda e: SimpleNamespace(entity=e)  # noqa: E731
    member = SimpleNamespace(name="gmsh_default", status="fail", error_kind="overlapping_facets",
                             located=[loc("fp1"), loc("fp2x"), loc("fp3"), loc("nowhere"), loc(None)])
    got = oracle_grid.annotate(parent, new, SimpleNamespace(panel=[member]))["gmsh_default"]
    assert got["old"] == ["F1"] and sorted(got["new"]) == ["F2", "F3"] and got["unplaced"] == 1 and got["unlocated"] == 1
