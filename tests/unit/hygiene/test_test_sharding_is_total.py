# Responsibility: Prove a sharded test lane still runs every tracked test file.
# Owns: the totality, disjointness and non-vacuity assertions, checked at the shard count CI uses.
# Boundaries: it reads the workflow and the tracked tree; it collects nothing and runs no tests.

# WHY THIS TEST EXISTS. Sharding trades one long lane for several short ones, and it pays for that
# with a failure mode nothing else in this repository has: a file assigned to no shard is not red,
# it is ABSENT. The suite still passes, the gate is still green, and the only evidence is a test
# count nobody reads. Every other guard here catches something that goes wrong loudly.
#
# So the shard count is not trusted to a comment. It is read out of .github/workflows/ci.yml - the
# matrix that actually runs, and the `SHARDS=` argument the lane actually passes - and the
# partition is asserted total at THAT number. Raising the matrix to six without the two agreeing
# fails here rather than silently running five sixths of the tier.
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
CI = REPO / ".github/workflows/ci.yml"

#: The sharded lanes, and the tree each one cuts up. Adding a lane here is what puts it under
#: every assertion below; a sharded lane that is NOT here is the one case this file cannot see.
SHARDED_LANES = {"unit": "tests/unit", "integration": "tests/integration"}


def _shard_tests():
    """Load devtools/quality/shard_tests.py by path.

    By path rather than by import: devtools is a tools directory, not an installed package, and a
    test that depends on it being on sys.path passes or fails for reasons that have nothing to do
    with sharding.
    """
    path = REPO / "devtools/quality/shard_tests.py"
    assert path.is_file(), f"{path} is missing - the lane it drives cannot be checked"
    spec = importlib.util.spec_from_file_location("shard_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lane(job: str) -> dict:
    lane = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"][job]
    assert "strategy" in lane, (
        f"the {job} lane is no longer sharded - drop it from SHARDED_LANES rather than leaving "
        f"this file asserting a partition nothing uses")
    return lane


def _matrix_shards(job: str) -> list[int]:
    return _lane(job)["strategy"]["matrix"]["shard"]


def _declared_shard_count(job: str) -> int:
    """The `SHARDS=` the lane passes to make, which is the number the partition is actually cut at."""
    body = yaml.dump(_lane(job)["steps"])
    found = set(re.findall(r"SHARDS=(\d+)", body))
    assert found, f"the {job} lane passes no SHARDS= argument - it runs a shard of nothing"
    assert len(found) == 1, f"the {job} lane passes more than one shard count: {sorted(found)}"
    return int(found.pop())


@pytest.mark.parametrize("job", sorted(SHARDED_LANES))
def test_the_matrix_and_the_shard_count_agree(job):
    shards = _matrix_shards(job)
    assert shards == list(range(1, len(shards) + 1)), (
        f"the shard matrix must be 1..N with no gaps, got {shards} - a gap is a shard of the "
        f"partition that no runner executes")
    assert _declared_shard_count(job) == len(shards), (
        f"the {job} matrix runs {len(shards)} shards but the lane cuts the partition into "
        f"{_declared_shard_count(job)} - the difference is tests that no shard runs")


@pytest.mark.parametrize("job,tree", sorted(SHARDED_LANES.items()))
def test_every_tracked_test_file_is_in_exactly_one_shard(job, tree):
    module = _shard_tests()
    files = module.tracked_test_files(tree)
    assert files, f"git ls-files found no test files under {tree} - the scan subject is empty"

    bins = module.partition(files, _declared_shard_count(job))
    assigned = [path for shard in bins for path in shard]

    missing = sorted(set(files) - set(assigned))
    assert missing == [], (
        f"{len(missing)} tracked {tree} file(s) are in no shard and would never run: "
        f"{missing[:10]}")
    duplicated = sorted({p for p in assigned if assigned.count(p) > 1})
    assert duplicated == [], f"file(s) in more than one shard, doubling their cost: {duplicated}"
    assert len(assigned) == len(files)


@pytest.mark.parametrize("job,tree", sorted(SHARDED_LANES.items()))
def test_no_shard_is_empty(job, tree):
    # An empty shard is the vacuous pass this whole file is about: `pytest` handed no files exits 0
    # having collected nothing, and the lane goes green.
    module = _shard_tests()
    bins = module.partition(module.tracked_test_files(tree), _declared_shard_count(job))
    empty = [i + 1 for i, shard in enumerate(bins) if not shard]
    assert empty == [], f"shard(s) {empty} have no files - a green lane that ran nothing"


@pytest.mark.parametrize("job,tree", sorted(SHARDED_LANES.items()))
def test_the_partition_is_stable_for_a_fixed_input(job, tree):
    # Two runs of the same tree must cut the same way, or a rerun of one shard reruns different
    # tests than the shard that failed.
    module = _shard_tests()
    files = module.tracked_test_files(tree)
    n = _declared_shard_count(job)
    assert module.partition(files, n) == module.partition(files, n)


@pytest.mark.parametrize("n", [1, 2, 3, 5, 7, 16])
def test_the_partition_is_total_at_other_shard_counts_too(n):
    # The property is the algorithm's, not this particular matrix's, so changing the matrix does
    # not need a new proof - only the agreement asserted above.
    module = _shard_tests()
    files = module.tracked_test_files("tests/unit")
    assigned = [p for shard in module.partition(files, n) for p in shard]
    assert sorted(assigned) == files


def test_the_integration_union_pass_exists_and_the_gate_waits_on_it():
    """Sharding the integration tier MOVED its non-vacuity guard; it must not have removed it.

    A shard cannot assert the 300-test floor or the three named real-PostgreSQL guarantees - split
    four ways it holds neither - so `run_in_container.sh` runs its guard with --partial and the
    whole-suite claim is made once, over the union of every shard's report, by `integration-tier`.
    That claim is only worth anything if a red one can fail the run, so the gate has to wait on it
    and has to read its result. Both are asserted here, because the way this guard would be lost
    is not somebody deleting it - it is somebody dropping one line from ci-gate.
    """
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    assert "integration-tier" in jobs, (
        "the integration union pass is gone - with the shards running --partial, nothing is left "
        "asserting that the dependency-backed tier ran at all")
    body = yaml.dump(jobs["integration-tier"]["steps"])
    assert "assert_integration_coverage.py" in body, (
        "integration-tier no longer runs the coverage guard")

    gate = jobs["ci-gate"]
    assert "integration-tier" in gate["needs"], "ci-gate does not wait for the integration union pass"
    assert "integration-tier=${{ needs.integration-tier.result }}" in yaml.dump(gate["steps"]), (
        "ci-gate waits for the union pass but never reads its result, so a vacuous integration "
        "tier would still post a green required check")
