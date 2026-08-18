# Responsibility: Verify every production progress publication names its own occurrence, scoped to the right thing.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_KINDS = ("note", "warn", "error", "stage", "attempt", "screenshot")
#: both spellings - a migrated publication carries the same occupancy identity
PUBLISHED = {*_KINDS, *(f"a{k}" for k in _KINDS)}
SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"

# The publisher's own definitions and internal fan-out - the site that RECEIVES op_id, not one
# that must supply it.
EXEMPT_FILES = {SRC / "adapters" / "event_stream" / "redis.py",
                SRC / "contracts" / "event_stream.py"}


def _publication_calls():
    for path in sorted(SRC.rglob("*.py")):
        if path in EXEMPT_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                                   # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in PUBLISHED:
                continue
            recv = node.func.value
            name = (recv.id if isinstance(recv, ast.Name)
                    else recv.attr if isinstance(recv, ast.Attribute) else "")
            # publisher handles are consistently named for the stream they write to
            if "pub" not in name.lower() and "publish" not in name.lower():
                continue
            yield path, node


def _has_op_id(call: ast.Call) -> bool:
    if any(kw.arg == "op_id" for kw in call.keywords):
        return True
    # `attempt(n, of)` carries the attempt number, which the publisher uses as the default
    return call.func.attr in ("attempt", "aattempt")


def test_every_production_progress_publication_names_its_occurrence():
    unkeyed = [f"{p.relative_to(SRC.parent.parent)}:{c.lineno} {c.func.attr}(...)"
               for p, c in _publication_calls() if not _has_op_id(c)]
    assert not unkeyed, (
        "these publications reach the replayable backlog without an occurrence identity, so a "
        "node re-run after a crash would show the user duplicate progress:\n  "
        + "\n  ".join(unkeyed))


def test_the_audit_can_actually_see_a_publication():
    found = list(_publication_calls())
    assert len(found) >= 25, f"only found {len(found)} publication call sites - walker is broken"


@pytest.mark.parametrize("expected_site", [
    ("engines/snappy/drivers.py", "pass-open"),          # inside an attempt loop
    ("pipeline/executor.py", "outcome"),                 # re-runs once per builder retry
    ("agents/builder/loop.py", "round"),                 # model narration, positional identity
    ("application/artifact_uploader.py", "delivery-failed"),  # run-scoped, fires at most once
])
def test_representative_producers_scope_identity_to_the_right_thing(expected_site):
    rel, token = expected_site
    assert token in (SRC / rel).read_text(encoding="utf-8"), (
        f"{rel} no longer names the '{token}' occurrence")
