# Responsibility: Verify the verdict domain is exactly two members, with no sentinel and no overlap with execution.
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from meshpipeline.contracts import review_outcome
from meshpipeline.contracts.review_outcome import ReviewExecution, ReviewVerdict

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src" / "meshpipeline"
MODULE = Path(review_outcome.__file__)


def _imported_subpackages(path: Path) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module \
                and node.module.startswith("meshpipeline."):
            mods.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("meshpipeline."):
                    mods.add(a.name.split(".")[1])
    return mods


# placement and neutrality
def test_the_module_lives_in_the_neutral_contracts_layer():
    assert MODULE.parent.name == "contracts"
    assert not _imported_subpackages(MODULE), "the outcome vocabulary depends on nothing"


def test_it_is_named_for_the_pair_it_carries_beside_its_sibling():
    assert MODULE.name == "review_outcome.py"
    assert (MODULE.parent / "review_evidence.py").exists()
    assert {"ReviewVerdict", "ReviewExecution"} <= set(vars(review_outcome))


def _scan_sources(pattern: str, *, exclude: set[str] | None = None) -> list[str]:
    import re as _re
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                          "--", "src/", "tests/"],
                         cwd=REPO, capture_output=True, text=True, check=True)
    rx = _re.compile(pattern)
    skip = exclude or set()
    hits: list[str] = []
    for rel in out.stdout.splitlines():
        rel = rel.strip()
        if not rel or rel in skip or not (REPO / rel).is_file():
            continue
        for n, line in enumerate((REPO / rel).read_text(encoding="utf-8",
                                                        errors="replace").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{n}:{line.strip()}")
    return hits


def test_the_old_module_cannot_be_imported():
    with pytest.raises(ImportError):
        __import__("meshpipeline.contracts.verdict")


# the two domains
def test_the_verdict_domain_is_exactly_two_members():
    assert {m.value for m in ReviewVerdict} == {"passed", "failed"}


@pytest.mark.parametrize("sentinel", ["", "NONE", "INCONCLUSIVE", "inconclusive",
                                     "undecided", "unknown", "not_concluded", "not_run"])
def test_no_string_sentinel_lives_in_the_verdict_domain(sentinel):
    with pytest.raises(ValueError):
        ReviewVerdict(sentinel)


def test_the_execution_domain_carries_only_supported_states():
    assert {m.value for m in ReviewExecution} == {
        "not_reached", "completed", "failed_to_complete"}
    with pytest.raises(ValueError):
        ReviewExecution("not_applicable")      # no current workflow produces it


def test_execution_is_not_a_verdict_and_verdict_is_not_an_execution():
    assert set(ReviewVerdict.__members__) & set(ReviewExecution.__members__) == set()
