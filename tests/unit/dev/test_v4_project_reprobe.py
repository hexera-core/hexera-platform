"""The projector must cost the wave the launcher actually runs: the same hash partition and VM packing."""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

V4 = Path(__file__).resolve().parents[3] / "devtools" / "validation" / "v4_eval"


def _load(name: str, where: Path = V4, file: str | None = None):
    spec = importlib.util.spec_from_file_location(name, where / f"{file or name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pr = _load("project_reprobe_under_test", V4, "project_reprobe")
ev = _load("v4_evalspec_under_test", V4, "evalspec")
v3 = _load("v3_evalspec_for_pack", V4.parent / "v3_eval", "evalspec")  # the launcher packs with v3's


def _sha(i: int) -> str:
    return hashlib.sha256(str(i).encode()).hexdigest()


def test_hash_partition_is_the_launchers() -> None:
    for i in range(200):
        h = _sha(i)
        assert ev.shard_of(h, pr.shard_of(h, 96) + 1, 96)


def test_vm_packing_is_the_launchers() -> None:
    units = [f"reprobe:pool:{i}/96" for i in range(1, 97)]
    vms = pr.pack_vms(96)
    assert len(vms) == 12 and sorted(i for v in vms for i in v) == list(range(96))
    assert [[int(u.split(":")[2].split("/")[0]) - 1 for u in v] for v in v3.pack(units, 8)] == vms


def test_lpt_balances_better_than_any_shard_holding_two_giants() -> None:
    costs = {_sha(i): c for i, c in enumerate([100, 90, 10, 10, 10, 10, 10, 10, 10, 10])}
    assign = pr.lpt(costs, 2)
    loads = [sum(c for h, c in costs.items() if assign[h] == s) for s in (0, 1)]
    assert assign[_sha(0)] != assign[_sha(1)] and max(loads) == 140


def test_the_simulated_cap_is_the_waves_own_wall_not_a_second_copy(tmp_path, capsys) -> None:
    """The projection prices the wave, so the wall it caps simulated files at must BE the wall the wave runs
    (evalspec.DEFAULT_FILE_TIMEOUT_S). A retyped default would price the old wall after the next raise."""
    import json

    rows = [{"size": 10_000 * (i + 1), "wall_s": 5.0 * (i + 1), "cpu_s": 5.0 * (i + 1),
             "throttled_s": 0.0, "mem_peak": 1 << 20, "excluded": None} for i in range(12)]
    measured = tmp_path / "files.jsonl"
    measured.write_text("".join(json.dumps(r) + "\n" for r in rows))
    sizes = tmp_path / "sizes.json"
    sizes.write_text(json.dumps({_sha(i): 100_000 for i in range(50)}))

    seen: list[float] = []
    real = pr.simulate
    pr.simulate = lambda *a, **k: (seen.append(a[5]), real(*a, **k))[1]
    try:
        assert pr.main(["--measured", str(measured), "--sizes", str(sizes), "--shards", "4", "--reps", "2"]) == 0
    finally:
        pr.simulate = real
    capsys.readouterr()
    assert seen and set(seen) == {float(ev.DEFAULT_FILE_TIMEOUT_S)}


def test_the_projector_loads_v4s_evalspec_even_when_v3_owns_the_bare_name() -> None:
    """v3_eval's modules register THEIR evalspec in sys.modules under the bare name `evalspec`, so
    `import evalspec` here would pick up whichever reached sys.modules first. The projector loads v4's by
    path; this is the regression test for the order-dependent failure that caused."""
    import importlib.util
    import sys

    v3_evalspec = _load("evalspec", V4.parent / "v3_eval", "evalspec")  # exactly what v3_eval/run_arm.py does
    assert sys.modules["evalspec"] is v3_evalspec
    assert not hasattr(v3_evalspec, "DEFAULT_FILE_TIMEOUT_S")

    spec = importlib.util.spec_from_file_location("project_reprobe_reloaded", V4 / "project_reprobe.py")
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fresh
    spec.loader.exec_module(fresh)
    assert fresh._evalspec.DEFAULT_FILE_TIMEOUT_S == ev.DEFAULT_FILE_TIMEOUT_S
