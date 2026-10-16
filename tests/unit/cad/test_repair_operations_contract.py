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
