# Responsibility: Run the measurement path a multi-region part takes, and prove what it needs from the image.
# Boundaries: builds its meshes with trimesh and calls the agent's own `detect_regions`; no database, no file
#             fixture, no network. It is the RUNNING half of deploy/test_geometry_agent_distribution.py, which
#             can only read files.
#
# WHY IT EXISTS. `deploy/verify_install.py` imported the geometry agent and checked its subpackages and its
# package data, and an image that passed all of it could measure NOTHING. The reason is that nothing in the
# agent's import closure names the library the path needs: the agent casts rays, and trimesh reaches for an
# r-tree inside those calls, on the agent's behalf, for the intersector it falls back to without embree. So
# every check built on reading imports or reading the wheel's declarations said the image was complete.
#
# THERE ARE TWO SUCH CALLS AND ONLY ONE OF THEM IS ABOUT REGIONS. `facts.regions.detect_regions` casts rays
# for its nesting test and asks only once a part has two or more regions, and `facts.features.detect_features`
# casts rays through `facts.chords` on EVERY part. MEASURED in the built `api` image on the real parts in
# tests/fixtures/geometry, through the platform's own `geometry_measurement.measure_local_file`:
#
#   installed              plate_with_hole_2d.step (1 region)   cht_enclosing_2region.step (2 regions)
#   rtree + embreex        ok                                   ok
#   embreex only           ok                                   ok
#   rtree only             ok                                   ok
#   NEITHER                measurement_failed: no rtree         measurement_failed: no rtree
#
# so the image this fix replaced failed at the first upload of any shape, not at the first assembly. Either
# library alone is enough; requirements/runtime.txt says why both are pinned.
#
# The first tests below run those paths for real, so this environment is held to them too. The last two remove
# the libraries the way the image used to lack them and pin the failure, so the day trimesh stops needing one
# the comments in requirements/runtime.txt and requirements/constraints.txt are told they have gone stale
# rather than quietly becoming untrue.
from __future__ import annotations

import importlib.util
import sys

import pytest
from tests._surveyor_package import require

require("geometry_agent.facts.regions", needs="the nesting test every multi-region upload runs")
require("geometry_agent.facts.features", needs="the feature pass every upload runs, rays included")

import trimesh  # noqa: E402
from geometry_agent.facts.features import detect_features  # noqa: E402
from geometry_agent.facts.regions import detect_regions  # noqa: E402

#: The libraries trimesh imports inside the nesting call, in the order it prefers them. `embreex` gives the
#: embree intersector, which needs no r-tree; without it trimesh falls back to the pure-python intersector,
#: which builds an `rtree.index.Index` over the container's triangles. requirements/runtime.txt pins both.
RAY_BACKENDS = ("embreex", "rtree")

#: The nesting a box inside a box must produce, whichever backend ran.
NESTED = [("r1", None), ("r2", "r1")]


def _present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ImportError:
        return False


def _nesting(regions) -> list[tuple[str, str | None]]:
    return [(r.id, r.nested_in) for r in regions]


def _two_regions(tm):
    """A box with a smaller box inside it, built entirely by the trimesh module `tm`.

    ONE MODULE, DELIBERATELY. Mixing a reloaded trimesh with the original one produces meshes of the original
    `Trimesh` class, whose `.ray` property is the original `trimesh.ray` with embreex already resolved - so a
    test meaning to measure the fallback would measure the preferred backend and pass for the wrong reason.
    """
    return tm.util.concatenate([tm.creation.box(extents=(0.2, 0.2, 0.2)),
                                tm.creation.box(extents=(0.05, 0.05, 0.05))])


class _Hide:
    """Refuse some top-level modules for the length of a `with`, and hand back a freshly imported trimesh.

    trimesh decides which ray intersector exists at IMPORT time (`trimesh.ray.has_embree`), so hiding a
    library is only half of it: the package has to be imported again underneath the refusal, and put back
    afterwards so the rest of the session sees what it saw before.
    """

    def __init__(self, *names: str) -> None:
        self.names = set(names)
        self._touched = self.names | {"trimesh"}

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.names:
            raise ImportError(f"hidden by this test: {name}")
        return None

    def _drop(self) -> None:
        for name in [n for n in sys.modules if n.split(".")[0] in self._touched]:
            del sys.modules[name]

    def __enter__(self):
        self._saved = {n: m for n, m in sys.modules.items() if n.split(".")[0] in self._touched}
        self._drop()
        sys.meta_path.insert(0, self)
        return __import__("trimesh")

    def __exit__(self, *exc) -> None:
        sys.meta_path.remove(self)
        self._drop()
        sys.modules.update(self._saved)


def test_a_part_with_two_regions_measures_and_its_nesting_is_found():
    """The path itself, run on whatever backend this interpreter has.

    One region is not enough: `detect_regions` returns before the nesting test on a single-region part
    (`if len(out) < 2: return out`), which is exactly why the image's gap was invisible for so long.
    """
    one = trimesh.creation.box(extents=(0.2, 0.2, 0.2))
    assert _nesting(detect_regions(one)) == [("r1", None)]
    assert _nesting(detect_regions(_two_regions(trimesh))) == NESTED


@pytest.mark.parametrize("backend", RAY_BACKENDS)
def test_the_environment_carries_this_ray_backend(backend):
    """Both pins, required, in any environment that runs this suite. NOT skipped where one is absent.

    requirements/runtime.txt pins both and every installer resolves that file, so an environment without one
    is an environment that did not install what the product declares. rtree is what the path needs to work at
    all; embreex is what it needs to work in seconds (0.41 s against 20.05 s at 209,408 faces). A skip here
    would be this suite declining to answer about the thing the image shipped without, which is how the
    original defect survived every check.
    """
    assert _present(backend), (
        # The constraints path stays on ONE line with its `-c`: `check_dependency_drift._install_commands`
        # scans the tracked tree line by line, so a wrap between the flag and its argument made this
        # sentence look like an installer that constrains with `"` and turned
        # `test_no_second_constraint_authority` red on a file that installs nothing.
        f"{backend} is not importable on this interpreter. requirements/runtime.txt pins it for the nesting "
        f"test every multi-region upload runs; re-run the environment setup, or run pip install with "
        f"-c requirements/constraints.txt {backend}.")


@pytest.mark.parametrize("backend", RAY_BACKENDS)
def test_each_ray_backend_on_its_own_measures_the_same_nesting(backend):
    """Either library alone is enough, and the answer does not depend on which one ran.

    This is what keeps both pins live rather than one of them being insurance nobody exercises: the preferred
    backend is measured by the first test, and the fallback is measured here by hiding the preferred one.
    """
    with _Hide(*(n for n in RAY_BACKENDS if n != backend)) as fresh:
        assert _nesting(detect_regions(_two_regions(fresh))) == NESTED, (
            f"the nesting test answers differently with only {backend} available")


def test_with_neither_of_them_the_nesting_test_is_the_call_that_raises():
    """The nesting test, reproduced with neither library. It is the call `deploy/verify_install.py` runs.

    `detect_regions` alone returns before the ray test on a single-region part (`if len(out) < 2: return
    out`), and that early return is what made the gap look narrower than it is - measured on this call alone,
    a simple part passes. The next test is the other half: the agent casts rays on a simple part too, just
    not from here.

    If trimesh ever stops needing an r-tree here, this test fails - and that is the right outcome, not a
    nuisance: the pins and the notes that justify them in requirements/runtime.txt and
    requirements/constraints.txt would have gone stale, and a note that lies is the defect one layer up.
    """
    with _Hide(*RAY_BACKENDS) as fresh:
        one = fresh.creation.box(extents=(0.2, 0.2, 0.2))
        assert _nesting(detect_regions(one)) == [("r1", None)], (
            "the nesting test returns before the rays on one region, which is why measuring this call alone "
            "said a simple part was fine")
        with pytest.raises(ImportError) as raised:
            detect_regions(_two_regions(fresh))
        assert "rtree" in str(raised.value), (
            f"a part with two regions failed for another reason ({raised.value}); the nesting test no longer "
            f"needs an r-tree and the requirements comments that say it does are now wrong")


def test_with_neither_of_them_even_a_one_region_part_cannot_be_MEASURED():
    """THE BLAST RADIUS, and the reason the note in requirements/runtime.txt is about every upload.

    Reading `detect_regions` says a single-region part is safe without an r-tree. Running the MEASUREMENT
    says it is not: `facts.features.detect_features` casts rays through `facts.chords` on every part, one
    region or forty, and trimesh reaches for the same r-tree inside that call. MEASURED in the built `api`
    image on the real parts in tests/fixtures/geometry through the platform's own `measure_local_file`: with
    neither library, `plate_with_hole_2d.step` (one region) and `cht_enclosing_2region.step` (two) both come
    back `measurement_failed` with `No module named 'rtree'` in the reason.

    A box is enough to hold that here, and it keeps this test hermetic - no CAD reader, no fixture file. If
    this ever stops raising, the reach really is confined to multi-region parts and three comments plus the
    header of this file have to be narrowed back.
    """
    with _Hide(*RAY_BACKENDS) as fresh:
        with pytest.raises(ImportError) as raised:
            detect_features(fresh.creation.box(extents=(0.2, 0.1, 0.05)))
    assert "rtree" in str(raised.value), (
        f"the feature pass over a single-region part failed for another reason ({raised.value}); if it no "
        f"longer reaches for an r-tree, the measured table in requirements/runtime.txt is now wrong")
