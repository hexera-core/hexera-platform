# Responsibility: The MESHABILITY PASS's operation registry - which repair operations make a part
#                 mesh in gmsh, which mesh defects each one claims, and in what order they are tried.
# Owns: the `MeshabilityOp` record, the registry, and the claim order. Nothing else.
# Boundaries: no kernel and no shapes. It names operations and answers "who claims this defect";
#             applying one is the pass's job (region_loop), proving it is the session's
#             (`try_package`), and every operation keeps the operations.py contract.
# Collaborates with: cad/repair/region_loop.py (the pass that walks this registry before any
#                    author), cad/repair/operations.py (the RepairOp contract every entry builds),
#                    each provider module in `PROVIDERS` (which registers its own entries).
"""The meshability pass's operation registry.

THE CONTRACT, for a lane adding an operation. Register a `MeshabilityOp` from your own module and
add that module to `PROVIDERS`. The entry promises:

  - `claims(defect)` is PURE and CHEAP: it reads the defect's code, entity and `measurements`
    (e.g. `located_by`, `clash_kind`, `clash_gap_mm`, `stage`) and touches no kernel. True means
    "this operation is built for exactly this finding on this face".
  - `build(rung)` returns a `RepairOp` (operations.py) for retry rung 0..rungs-1, finer or more
    aggressive as the rung rises. The op returns a NEW shape, never mutates its input, DECLARES
    its measured area/volume change (`AREA_DELTA`/`VOLUME_DELTA`, rounded away from zero), and
    DECLINES with a reason rather than raising. Any kernel algorithm that can run long (distance,
    boolean, section, fix) runs in a bounded fresh interpreter, never the parent, never a fork.
  - `enabled()` reads the op's own consent flag; off, the entry is invisible.

What the pass guarantees in return: each application is ONE package through the ordinary
`session.try_package` - predicate (the claimed fingerprint cleared), caps against the upload,
solid count, validity, the reviewer per posture, the delivered-STEP keep rule and the delivered
file re-read and BRepChecked in the upload's unit. Every face's rung 0 is tried before any face's
rung 1; a decline spends the entry for that face; the pass has a bounded share of the run clock.
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: THE MODULES THAT REGISTER MESHABILITY OPERATIONS, imported (once, in this order) the first time
#: the registry is read. A lane adds its module here; registration therefore never depends on who
#: happened to import what first.
PROVIDERS: tuple[str, ...] = (
    "meshpipeline.cad.repair.gap_split",
)


@dataclass(frozen=True, slots=True)
class MeshabilityOp:
    """One registered meshability operation. See the module docstring for the contract."""

    #: The operation's catalogue name (`RepairOp.name`): the ledger, rows and reviewer key.
    name: str
    #: Is this mesh finding exactly what the operation is built for? Pure, no kernel.
    claims: Callable[[object], bool]
    #: The operation at retry rung `rung` (0-based, < `rungs`).
    build: Callable[[int], object]
    #: The operation's consent flag, read at call time.
    enabled: Callable[[], bool]
    #: How many rungs `build` accepts. 1 = no retry.
    rungs: int = 1
    #: Lower runs first when two entries claim one defect.
    priority: int = 100
    #: The owning lane, for the report.
    lane: str = ""


_REGISTRY: dict[str, MeshabilityOp] = {}
_loaded = False


def register(entry: MeshabilityOp) -> MeshabilityOp:
    """Add `entry`. Re-registering the SAME entry is a no-op; a different one under a taken name
    raises, because two operations answering to one ledger key would corrupt the run history."""
    if entry.rungs < 1:
        raise ValueError(f"{entry.name}: rungs must be at least 1")
    held = _REGISTRY.get(entry.name)
    if held is not None and held != entry:
        raise ValueError(f"meshability op {entry.name!r} is already registered differently")
    _REGISTRY[entry.name] = entry
    return entry


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for module in PROVIDERS:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - one broken provider must not hide the others
            logger.warning("meshability: provider %s did not load (%s)", module, exc)


def registered(*, enabled_only: bool = True) -> tuple[MeshabilityOp, ...]:
    """Every registered entry, in claim order (priority, then name)."""
    _load()
    entries = (e for e in _REGISTRY.values() if not enabled_only or _on(e))
    return tuple(sorted(entries, key=lambda e: (e.priority, e.name)))


def _on(entry: MeshabilityOp) -> bool:
    try:
        return bool(entry.enabled())
    except Exception:  # noqa: BLE001 - an unreadable flag is off
        return False


def claimant(defect, *, rung_of: Mapping[str, int] | None = None,
             spent: Callable[[str, str], bool] | None = None) -> tuple[MeshabilityOp, int] | None:
    """The first enabled entry that claims `defect` and is not spent on it, with the rung to try.

    `rung_of[name]` is how many times that entry already failed on this defect's face (0 if
    never), and `spent(name, fingerprint)` says whether it is used up there (declined, or every
    rung tried). Both default to "nothing tried yet"."""
    fingerprint = str(getattr(defect, "fingerprint", "") or "")
    for entry in registered():
        try:
            if not entry.claims(defect):
                continue
        except Exception as exc:  # noqa: BLE001 - a claim that cannot be read is no claim
            logger.debug("meshability: %s could not read a claim (%s)", entry.name, exc)
            continue
        if spent is not None and spent(entry.name, fingerprint):
            continue
        rung = int((rung_of or {}).get(entry.name, 0))
        if rung >= entry.rungs:
            continue
        return entry, rung
    return None
