# Responsibility: Run the measurement path a multi-region part takes, and prove what it needs from the image.
# Boundaries: builds its meshes with trimesh and calls the agent's own `detect_regions`; no database, no file
#             fixture, no network. It is the RUNNING half of deploy/test_geometry_agent_distribution.py, which
#             can only read files.
#
# WHY IT EXISTS. `deploy/verify_install.py` imported the geometry agent and checked its subpackages and its
# package data, and an image that passed all of it could not measure a part with TWO REGIONS. The reason is
# that nothing in the agent's import closure names the library the path needs: `facts.regions.detect_regions`
# casts rays for its nesting test, and trimesh reaches for an r-tree inside that call, on the agent's behalf.
# So every check built on reading imports or reading the wheel's declarations said the image was complete.
#
# MEASURED, in the built `api` image, with rtree and embreex uninstalled:
#   one region : [('r1', None)]
#   two regions: FAILED ModuleNotFoundError No module named 'rtree'
# and with them installed, on the same image:
#   two regions: [('r1', None), ('r2', 'r1')]      backend trimesh.ray.ray_pyembree
#
# The first test below runs that path for real, so this environment is held to it too. The last one removes
# the libraries the way the image used to lack them and pins the failure, so the day trimesh stops needing one
# the comments in requirements/runtime.txt and requirements/constraints.txt are told they have gone stale
# rather than quietly becoming untrue.
from __future__ import annotations

import importlib.util
import sys

import pytest
from tests._surveyor_package import require

require("geometry_agent.facts.regions", needs="the nesting test every multi-region upload runs")

import trimesh  # noqa: E402
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
        f"{backend} is not importable on this interpreter. requirements/runtime.txt pins it for the nesting "
        f"test every multi-region upload runs; re-run the environment setup, or `pip install -c "
        f"requirements/constraints.txt {backend}`.")


@pytest.mark.parametrize("backend", RAY_BACKENDS)
def test_each_ray_backend_on_its_own_measures_the_same_nesting(backend):
    """Either library alone is enough, and the answer does not depend on which one ran.

    This is what keeps both pins live rather than one of them being insurance nobody exercises: the preferred
    backend is measured by the first test, and the fallback is measured here by hiding the preferred one.
    """
    with _Hide(*(n for n in RAY_BACKENDS if n != backend)) as fresh:
        assert _nesting(detect_regions(_two_regions(fresh))) == NESTED, (
            f"the nesting test answers differently with only {backend} available")


def test_with_neither_of_them_a_one_region_part_still_measures_and_a_two_region_part_does_not():
    """The image's actual defect, reproduced. This is the shape that made it invisible: nothing raised at
    import, nothing raised on a simple part, and the first assembly failed at a customer's upload.

    If trimesh ever stops needing an r-tree here, this test fails - and that is the right outcome, not a
    nuisance: the pins and the notes that justify them in requirements/runtime.txt and
    requirements/constraints.txt would have gone stale, and a note that lies is the defect one layer up.
    """
    with _Hide(*RAY_BACKENDS) as fresh:
        one = fresh.creation.box(extents=(0.2, 0.2, 0.2))
        assert _nesting(detect_regions(one)) == [("r1", None)], (
            "a single-region part needs no ray test, so this is the part the broken image measured fine")
        with pytest.raises(ImportError) as raised:
            detect_regions(_two_regions(fresh))
        assert "rtree" in str(raised.value), (
            f"a part with two regions failed for another reason ({raised.value}); the nesting test no longer "
            f"needs an r-tree and the requirements comments that say it does are now wrong")
