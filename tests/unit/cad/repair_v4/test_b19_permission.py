# Responsibility: B19 - the search lacked PERMISSION, not ops. Every op family is offered at EVERY site kind, aimed
#                 at the site's own faces and bodies, with the kind's preferred families first; and the k cut takes
#                 a fair share across families so a right candidate cannot be dropped under the cap.
# Boundaries: hermetic. No kernel, no pool, no model: the det sweep's row building and the cut, nothing else.
from __future__ import annotations

from typing import Any

from meshpipeline.cad.repair_v4 import search as S
from meshpipeline.cad.repair_v4.ops import REGISTRY
from meshpipeline.cad.repair_v4.types import Site


def site(kind: str, entities: list[str], **graph: Any) -> Site:
    return Site(id="S1", kind=kind, entities=entities, xyz=[], meshers_failing=["gmsh_prod"],
                dist_over_h=None, graph=graph, view_png=None)


def rows_at(kind: str, entities: list[str], diag: float | None = 100.0, **graph: Any) -> list[tuple]:
    gen = S.DetGenerator(diagonal=lambda _ws: diag)
    return gen._sweep(site(kind, entities, **graph), diag)


def ops_at(kind: str, entities: list[str], **graph: Any) -> set[str]:
    return {row[0] for row in rows_at(kind, entities, **graph)}


# ---- lane A's finding: a face op at a kind that used to forbid it ------------------------------

def test_split_angle_is_offered_at_every_kind_not_only_three() -> None:
    """Lane A measured split_angle at 60 and 45 reaching CLEAN IN ONE STEP on 3596803faa, with an op and a rung the
    sweep already had - it was never allowed to propose them at that site's kind."""
    for kind in ("proximity", "self_overlap", "periodic", "invalid", "sliver", "timeout", "pole", "low_quality"):
        got = [r for r in rows_at(kind, ["F1", "F2"]) if r[0] == "split_angle"]
        assert got, f"no split_angle at kind {kind!r}"
        assert {row[2]["max_deg"] for row in got} == set(S._SPLIT_ANGLES), kind


def test_every_face_family_reaches_a_kind_that_used_to_forbid_it() -> None:
    """`oracle_face_grid.py`'s list of gates: split_continuity was `invalid` only, unify_same_domain `sliver` only,
    remove_small `sliver` only, split_closed `periodic` only."""
    got = ops_at("timeout", ["F1", "F2", "F3"])
    for op in ("split_angle", "split_continuity", "split_closed", "unify_same_domain", "remove_small"):
        assert op in got, f"{op} still not offered at a timeout site: {sorted(got)}"


# ---- lane C's finding: the body ops at a site that carries bodies ------------------------------

def test_the_body_ops_reach_any_site_naming_two_bodies_whatever_its_kind() -> None:
    """Lane C: overlapping bodies sat on a `self_overlap` site and det proposed no cut at all, so C11 had to apply
    them by hand."""
    for kind in ("self_overlap", "invalid", "proximity", "timeout"):
        got = rows_at(kind, ["B1", "B2"])
        modes = {row[2].get("mode") for row in got if row[0] == "conformalise"}
        assert any(str(m).startswith("cut_by:") for m in modes), f"no cut_by at kind {kind!r}: {modes}"
        assert any(row[0] == "fuse" for row in got), f"no fuse at kind {kind!r}"


def test_one_keeper_many_victims_is_proposed_when_a_site_names_more_than_two_bodies() -> None:
    """Lane C11: on dev 0051 one body overlapped three others and the cut that resolved layer 1 was `cut_by:B4` over
    ALL FOUR bodies, which no pair row can express."""
    got = rows_at("self_overlap", ["B1", "B2", "B3", "B4"])
    whole = [r for r in got if r[0] == "conformalise" and len(r[1]) == 4 and str(r[2].get("mode")).startswith("cut_by")]
    assert len(whole) == 4, [r[2] for r in whole]
    assert any(r[0] == "fuse" and len(r[1]) == 4 for r in got)


def test_a_whole_part_coarse_site_still_gets_no_body_rows() -> None:
    """C10 measured 11 no-op conformalise calls from exactly this: a coarse site lists every body and is no evidence
    that any two touch."""
    got = rows_at("invalid", ["B1", "B2"], coarse={"whole_part": True, "reason": "no member said where"})
    assert not [r for r in got if r[0] in ("conformalise", "fuse")]


# ---- the ordering and the cap ------------------------------------------------------------------

def test_the_kinds_own_families_come_first_and_the_rest_follow() -> None:
    order = S._family_order("overlap")
    assert order["body"] == 0, order
    assert order["face_split"] > order["body"]
    assert set(order) >= set(S._FAMILIES)


def test_the_cap_takes_a_fair_share_so_one_family_cannot_eat_it() -> None:
    """Lane A's newer finding: the sweep DID propose split_angle@60 and then dropped it under the cap. A family's
    first row is taken before any family's second."""
    def cand(op: str, n: int) -> S.Candidate:
        return S.Candidate(op, [], {"n": n}, "det", f"{op} {n}")

    many = [cand("conformalise", i) for i in range(10)] + [cand("split_angle", i) for i in range(3)]
    kept = S._fair_share(many, 4)
    assert len(kept) == 4
    assert [c.op for c in kept].count("split_angle") >= 1, [c.op for c in kept]
    # order inside a family is kept
    assert [c.params["n"] for c in kept if c.op == "conformalise"] == sorted(
        c.params["n"] for c in kept if c.op == "conformalise")
    assert S._fair_share(many, 99) == many        # under the cap nothing is reordered


def test_every_registry_op_has_a_family_so_the_fair_share_cannot_silently_bucket_one_alone() -> None:
    missing = sorted(op for op in REGISTRY if op not in S._OP_FAMILY)
    assert missing == [], f"ops with no family: {missing}"
