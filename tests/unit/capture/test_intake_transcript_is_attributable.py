# Responsibility: Verify the intake transcript is captured under a bound tenant, not dropped.
# Boundaries: where the write sits relative to the claim; WHO the tenant is belongs to the fence.
from __future__ import annotations

import ast
import inspect

WRITE = "_emit_intake_events"
CLAIM = "claim_delivery"


def _function(name: str, module) -> ast.AST:
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone; this guard is watching the wrong name")


def _lines_calling(node, name: str) -> list[int]:
    # EVERY occurrence, not the last one. Keying by call name would collapse a second write into
    # the first, which is precisely the case worth catching: one write correctly placed after the
    # claim says nothing about another sitting before it.
    return sorted(c.lineno for c in ast.walk(node) if isinstance(c, ast.Call)
                  and getattr(c.func, "id", getattr(c.func, "attr", "")) == name)


def test_every_transcript_write_sits_after_the_claim():
    # A capture write with no tenant bound is DROPPED, and dropping is a warning rather than a
    # failure, so a write placed before the claim costs the corpus the whole intake conversation
    # while the run itself looks healthy. Claiming is what binds the tenant.
    from meshpipeline.application import pipeline_run

    body = _function("_run_async", pipeline_run)
    writes = _lines_calling(body, WRITE)
    claims = _lines_calling(body, CLAIM)
    assert writes, f"{WRITE} is not called inside _run_async at all"
    assert claims, f"{CLAIM} moved; this guard can no longer order against it"
    early = [ln for ln in writes if ln < claims[0]]
    assert early == [], (
        f"{WRITE} is called at line(s) {early}, before the claim at {claims[0]} that binds the "
        "tenant those records file under, so every one of them is dropped")


def test_dispatch_writes_no_transcript_of_its_own():
    # Dispatch has no tenant it is ALLOWED to file under: the owner it was handed came from the
    # payload, and the fence deliberately reads the job row instead, because filing one tenant's
    # capture content under another's id is a disclosure rather than a bookkeeping error.
    from meshpipeline.application import pipeline_run

    assert _lines_calling(_function("run_pipeline", pipeline_run), WRITE) == [], \
        "dispatch writes the intake transcript before any tenant is bound"
