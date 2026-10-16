# Responsibility: Verify every repair operation declares what it answers and how narrow it is, and that a mapping built from no history is honest about being geometric.
from __future__ import annotations

from meshpipeline.cad.repair.contracts import DefectCode
from meshpipeline.cad.repair.identity import EntityMapping
from meshpipeline.cad.repair.operations import OPERATIONS, OpOutcome

# The kernel work is proved in the native tier. What is checkable without a kernel is the
# CATALOGUE: a planner chooses between operations by reading `answers` and `scope`, so an
# operation that declares neither - or declares a defect nothing produces - is a planner bug
# waiting to happen.


def test_every_operation_has_a_name_and_a_declared_defect_set():
    assert OPERATIONS, "the catalogue is empty"
    for op in OPERATIONS:
        assert op.name and isinstance(op.name, str)
        assert isinstance(op.answers, frozenset)
        assert all(isinstance(code, DefectCode) for code in op.answers)


def test_no_two_operations_share_a_name():
    # The op name is the key in the ineffective-pair ledger and in the training log. Two
    # operations sharing one would make a rolled-back pair ban the wrong operation.
    names = [op.name for op in OPERATIONS]
    assert len(names) == len(set(names))


def test_every_operation_declares_a_scope_so_the_planner_can_prefer_the_narrow_one():
    for op in OPERATIONS:
        assert isinstance(op.scope, int) and op.scope > 0


def test_the_whole_shape_fallback_is_the_widest_scope_in_the_catalogue():
    # `fix_shape_global` is the last resort, and the planner expresses that by ordering on scope.
    # If anything were wider, the planner would reach for it first.
    widest = max(OPERATIONS, key=lambda op: op.scope)
    assert widest.name == "fix_shape_global"


def test_the_whole_shape_fallback_takes_no_targets():
    # It is the operation that exists so the loop is never WORSE than today's single pass, and
    # today's pass aims at nothing.
    glob = next(op for op in OPERATIONS if op.name == "fix_shape_global")
    assert glob.targets_for(object()) == ()


def test_an_outcome_defaults_to_an_empty_geometric_mapping():
    # An operation that offers no history must not have one inferred for it. "Geometric" is the
    # honest default, and it is what makes the verdict's basis meaningful.
    outcome = OpOutcome(shape=object())
    assert outcome.mapping == EntityMapping.empty()
    assert outcome.mapping.basis == "geometric"
    assert outcome.added == () and outcome.evidence == {}


# THE SCOPED OPERATIONS' DECLARATIONS


def test_each_scoped_operation_is_narrower_than_the_whole_shape_fallback():
    glob = next(op for op in OPERATIONS if op.name == "fix_shape_global")
    for op in OPERATIONS:
        if op.name != glob.name:
            assert op.scope < glob.scope, f"{op.name} is not narrower than the fallback"


def test_the_small_edge_operation_claims_the_small_and_degenerate_edge_defects():
    op = next(o for o in OPERATIONS if o.name == "fix_small_edges")
    assert DefectCode.small_edge in op.answers
    assert DefectCode.degenerate_edge in op.answers
    # NOT small_face. A sliver face is a question about the customer's intent - a legitimate
    # chamfer looks identical - and it routes to a person. An operation that claimed it would let
    # the planner defeature without anybody deciding to.
    assert DefectCode.small_face not in op.answers


def test_an_operation_asked_about_a_defect_it_does_not_claim_offers_no_targets():
    from meshpipeline.cad.repair.contracts import DefectSeverity
    from meshpipeline.cad.repair.localize import EntityDefect

    op = next(o for o in OPERATIONS if o.name == "fix_small_edges")
    wrong = EntityDefect(code=DefectCode.self_intersection, severity=DefectSeverity.error,
                         entity_type="face", entity_index=4, message="x")
    assert op.targets_for(wrong) == ()


def test_an_operation_offers_no_targets_for_a_defect_it_cannot_locate():
    # An unlocated defect has no box, so no fingerprint, so nothing to aim at. The planner reports
    # skipping it; the operation must not invent a target from an index alone.
    from meshpipeline.cad.repair.contracts import DefectSeverity
    from meshpipeline.cad.repair.localize import EntityDefect

    op = next(o for o in OPERATIONS if o.name == "fix_small_edges")
    unlocated = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.warning,
                             entity_type="edge", entity_index=0, message="x")
    assert op.targets_for(unlocated) == ()
