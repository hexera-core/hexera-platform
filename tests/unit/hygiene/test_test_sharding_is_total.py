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


def _unit_lane() -> dict:
    lane = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["unit"]
    assert "strategy" in lane, (
        "the unit lane is no longer sharded - delete this test rather than leaving it asserting "
        "a partition nothing uses")
    return lane


def _matrix_shards() -> list[int]:
    return _unit_lane()["strategy"]["matrix"]["shard"]


def _declared_shard_count() -> int:
    """The `SHARDS=` the lane passes to make, which is the number the partition is actually cut at."""
    body = yaml.dump(_unit_lane()["steps"])
    found = set(re.findall(r"SHARDS=(\d+)", body))
    assert found, "the unit lane passes no SHARDS= argument - it is not running a shard of anything"
    assert len(found) == 1, f"the unit lane passes more than one shard count: {sorted(found)}"
    return int(found.pop())


def test_the_matrix_and_the_shard_count_agree():
    shards = _matrix_shards()
    assert shards == list(range(1, len(shards) + 1)), (
        f"the shard matrix must be 1..N with no gaps, got {shards} - a gap is a shard of the "
        f"partition that no runner executes")
    assert _declared_shard_count() == len(shards), (
        f"the matrix runs {len(shards)} shards but the lane cuts the partition into "
        f"{_declared_shard_count()} - the difference is tests that no shard runs")


def test_every_tracked_unit_test_file_is_in_exactly_one_shard():
    module = _shard_tests()
    files = module.tracked_test_files("tests/unit")
    assert files, "git ls-files found no unit test files - the scan subject is empty"

    bins = module.partition(files, _declared_shard_count())
    assigned = [path for shard in bins for path in shard]

    missing = sorted(set(files) - set(assigned))
    assert missing == [], (
        f"{len(missing)} tracked unit test file(s) are in no shard and would never run: "
        f"{missing[:10]}")
    duplicated = sorted({p for p in assigned if assigned.count(p) > 1})
    assert duplicated == [], f"file(s) in more than one shard, doubling their cost: {duplicated}"
    assert len(assigned) == len(files)


def test_no_shard_is_empty():
    # An empty shard is the vacuous pass this whole file is about: `pytest` handed no files exits 0
    # having collected nothing, and the lane goes green.
    module = _shard_tests()
    bins = module.partition(module.tracked_test_files("tests/unit"), _declared_shard_count())
    empty = [i + 1 for i, shard in enumerate(bins) if not shard]
    assert empty == [], f"shard(s) {empty} have no files - a green lane that ran nothing"


def test_the_partition_is_stable_for_a_fixed_input():
    # Two runs of the same tree must cut the same way, or a rerun of one shard reruns different
    # tests than the shard that failed.
    module = _shard_tests()
    files = module.tracked_test_files("tests/unit")
    n = _declared_shard_count()
    assert module.partition(files, n) == module.partition(files, n)


@pytest.mark.parametrize("n", [1, 2, 3, 5, 7, 16])
def test_the_partition_is_total_at_other_shard_counts_too(n):
    # The property is the algorithm's, not this particular matrix's, so changing the matrix does
    # not need a new proof - only the agreement asserted above.
    module = _shard_tests()
    files = module.tracked_test_files("tests/unit")
    assigned = [p for shard in module.partition(files, n) for p in shard]
    assert sorted(assigned) == files
