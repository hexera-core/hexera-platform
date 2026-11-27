# Responsibility: the delivered regions' boundary indexes are built concurrently, on threads.
# Boundaries: `native._index_regions` - the decisions it makes and the records it writes.
# Collaborates with: render/boundary_index, whose writers are stubbed so no mesh is needed.
"""The two regions were indexed one after the other: 107.5 s then 163.9 s on the delivered plate.

WHY THREADS, AND WHY THAT ANSWER MOVED. The ascii read is the whole cost of this pass. While it
went through `re.findall` the GIL made concurrency worthless - 1.04x on two threads - so the
written-down answer was "processes, decided". `render/boundary_index` no longer uses a regex and
`np.fromstring` releases the GIL, so re-measured on two files in the plate's own proportion
(1.3M and 1.7M records, 98.0 MiB):

    serial        1.21 s
    2 threads     0.61 s   1.99x
    2 processes   0.67 s   1.80x   (paying for the pickling)

Threads, and the earlier answer belonged to the earlier code.

WHAT MUST NOT CHANGE is every per-region decision and every per-region record: the skip ceiling,
the budget refusal, which mesh a region is, and the fact that a region that fails is NAMED rather
than absent.
"""
from __future__ import annotations

import threading
import time

import pytest

from meshpipeline.engines.cfmesh_conjugate import native as N


@pytest.fixture
def stub(monkeypatch):
    """Stand in for the writers, so this measures the orchestration and not a mesh."""
    seen: dict = {"threads": set(), "live": 0, "peak": 0, "order": []}
    lock = threading.Lock()

    def _meta(n_faces=1000):
        return {"n_faces": n_faces, "n_points": n_faces * 2,
                "grid": {"n_buckets": 7}, "bytes": {"a": 10, "b": 20}}

    def _write(polymesh, *, region, hold=0.25):
        with lock:
            seen["threads"].add(threading.current_thread().name)
            seen["live"] += 1
            seen["peak"] = max(seen["peak"], seen["live"])
            seen["order"].append(region)
        time.sleep(hold)
        with lock:
            seen["live"] -= 1
        return _meta()

    monkeypatch.setattr("meshpipeline.render.boundary_index.write_index",
                        lambda pm, *, region: _write(pm, region=region))
    monkeypatch.setattr("meshpipeline.render.boundary_index.write_index_decomposed",
                        lambda ws, *, region: _write(ws, region=region))
    monkeypatch.setattr("meshpipeline.render.boundary_index.index_is_decomposed",
                        lambda ws, pm: False)
    monkeypatch.setattr(N, "_patch_rows", lambda pm: [{"n_faces": 1000}])
    monkeypatch.setattr(N, "_boundary_quality", lambda pm, *, region, case=None: {"ok": True})
    return seen


def run(tmp_path, names=("fluid", "solid"), *, deadline=None):
    out: dict = {"regions": {}}
    regions = [(n, tmp_path / n / "polyMesh") for n in names]
    for _n, pm in regions:
        pm.mkdir(parents=True, exist_ok=True)
    N._index_regions(tmp_path, regions, out, deadline=deadline)
    return out


# ------------------------------------------------------------------ the concurrency

def test_both_regions_are_in_flight_at_the_same_time(tmp_path, stub):
    out = run(tmp_path)
    assert stub["peak"] == 2, "the two regions were still built one after the other"
    assert len(stub["threads"]) == 2
    assert set(out["regions"]) == {"fluid", "solid"}
    assert all(r["ok"] for r in out["regions"].values())


def test_the_pair_takes_about_as_long_as_one_of_them(tmp_path, stub):
    t = time.monotonic()
    run(tmp_path)
    elapsed = time.monotonic() - t
    # each stub holds for 0.25 s; serial would be 0.50
    assert elapsed < 0.42, f"elapsed {elapsed:.2f} s looks serial"


def test_a_single_region_does_not_start_a_pool(tmp_path, stub):
    """One region is the common case for every engine but this one; it should stay plain."""
    out = run(tmp_path, names=("fluid",))
    assert set(out["regions"]) == {"fluid"}
    assert stub["peak"] == 1
    assert stub["threads"] == {threading.current_thread().name}


# ------------------------------------------------------------------ every record survives

def test_each_region_gets_its_own_record_with_its_own_figures(tmp_path, stub):
    out = run(tmp_path)
    for name in ("fluid", "solid"):
        r = out["regions"][name]
        assert r["faces"] == 1000 and r["points"] == 2000
        assert r["buckets"] == 7 and r["bytes"] == 30
        assert r["decomposed"] is False
        assert isinstance(r["seconds"], float)
        assert r["quality"] == {"ok": True}


def test_a_region_over_the_skip_ceiling_is_named_and_not_built(tmp_path, stub, monkeypatch):
    monkeypatch.setattr(N, "_patch_rows",
                        lambda pm: [{"n_faces": N.INDEX_SKIP_ABOVE_FACES + 1}])
    out = run(tmp_path)
    for name in ("fluid", "solid"):
        assert out["regions"][name]["ok"] is False
        assert "over the" in out["regions"][name]["why"]
    assert stub["peak"] == 0, "a region over the ceiling was built anyway"


def test_a_region_the_budget_cannot_afford_is_named_and_not_started(tmp_path, stub):
    out = run(tmp_path, deadline=time.monotonic() - 1.0)   # no budget left at all
    for name in ("fluid", "solid"):
        assert out["regions"][name]["ok"] is False
        assert "budget" in out["regions"][name]["why"]
    assert stub["peak"] == 0


def test_the_budget_charges_each_region_the_whole_remainder_not_the_leftovers(tmp_path, stub):
    """BECAUSE THEY RUN TOGETHER the pair costs the slower of the two, not their sum - so
    charging the second region for the first one's time would refuse an index that fits."""
    import inspect

    src = inspect.getsource(N._index_regions)
    # the budget is read once per region from the same deadline, inside the decision loop,
    # and no region's price is subtracted from another's remainder
    assert src.count("deadline - time.monotonic()") == 1
    assert "want > left" in src


def test_a_writer_that_raises_names_its_region_and_leaves_the_other_alone(tmp_path, stub,
                                                                          monkeypatch):
    def _boom(pm, *, region):
        if region == "solid":
            raise RuntimeError("the sidecar could not be written")
        return {"n_faces": 5, "n_points": 10, "grid": {"n_buckets": 1}, "bytes": {"a": 1}}

    monkeypatch.setattr("meshpipeline.render.boundary_index.write_index", _boom)
    out = run(tmp_path)
    assert out["regions"]["fluid"]["ok"] is True
    assert out["regions"]["solid"]["ok"] is False
    assert "could not be written" in out["regions"]["solid"]["why"]


def test_no_region_at_all_is_not_an_error(tmp_path, stub):
    out: dict = {"regions": {}}
    N._index_regions(tmp_path, [], out, deadline=None)
    assert out == {"regions": {}}


# ------------------------------------------------------------------ the wiring

def test_the_decision_is_made_before_either_thread_starts():
    """`index_is_decomposed` must be one answer per region, settled outside the pool: two
    sidecars built from different answers would paint one mesh's numbers on another's faces."""
    import inspect

    src = inspect.getsource(N._index_regions)
    assert src.index("index_is_decomposed(ws, polymesh)") < src.index("ThreadPoolExecutor")
    assert src.index("todo.append") < src.index("ThreadPoolExecutor")
