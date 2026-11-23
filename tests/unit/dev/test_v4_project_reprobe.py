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


pr = _load("project_reprobe")
ev = _load("evalspec")
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
