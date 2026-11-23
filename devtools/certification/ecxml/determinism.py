#!/usr/bin/env python3
"""Does the ECXML path give the same answer twice, and does it refuse in words?

For every file in the lab, three stages, each run TWICE in the same process and compared as a
canonical JSON blob:

  read     ecxml.read_ecxml      -> the model: every object, box, material, power
  place    ecxml_place.place     -> the placement: precedence resolved, regions decided
  plan     snapgrid grid plan    -> the grid: planes, cell counts, region cell counts

Three outcomes per stage, and only one of them is a failure:

  SAME     both runs agreed, to the byte of their canonical form        pass
  REFUSED  both runs raised the same message from the product's own     pass
           error types (EcxmlError, FidelityError, OverBudget)
  DIFFER   the two runs disagreed                                      FAIL
  CRASH    something that is not one of the product's error types       FAIL

A second process is run separately by the caller with --hash so cross-process determinism is
checked too: hash equality between two interpreters catches anything seeded by id(), PYTHONHASHSEED
or dict insertion order that happens to be stable within one process.
"""
from __future__ import annotations

import os
import pathlib as _pl

#: The repository this lab is part of, and the lab directory beside it. HEXERA_ECXML_LAB moves the
#: lab; the default sits under the repository so two checkouts do not share one.
REPO = _pl.Path(os.environ.get("HEXERA_REPO") or
                _pl.Path(__file__).resolve().parents[3])
LAB_ROOT = _pl.Path(os.environ.get("HEXERA_ECXML_LAB") or (REPO / ".ecxml_lab"))

import argparse
import hashlib
import json
import pathlib
import sys
import traceback

ROOT = REPO
sys.path.insert(0, str(REPO / "src"))

LAB = LAB_ROOT / "models"

from meshpipeline.cad.ingest.ecxml import EcxmlError, read_ecxml  # noqa: E402
from meshpipeline.cad.ingest.ecxml_build import FidelityError  # noqa: E402

#: The errors the product raises on purpose. Anything else is a crash.
EXPECTED = (EcxmlError, FidelityError)
try:
    from meshpipeline.engines.snapgrid.grid import OverBudget
    EXPECTED = EXPECTED + (OverBudget,)
except Exception:  # pragma: no cover - import shape is checked by the run itself
    pass
try:
    from meshpipeline.cad.ingest.ecxml_build import ModelTooLargeError
    EXPECTED = EXPECTED + (ModelTooLargeError,)
except Exception:
    pass


def canon(obj) -> str:
    """A canonical JSON form: keys sorted, floats to 17 significant digits (exact for binary64),
    and anything that is not JSON reduced to its repr so a change in it still shows up."""
    def go(x):
        if isinstance(x, float):
            return f"{x:.17g}"
        if isinstance(x, (int, str, bool)) or x is None:
            return x
        if isinstance(x, dict):
            return {str(k): go(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
        if isinstance(x, (list, tuple)):
            return [go(v) for v in x]
        if hasattr(x, "as_dict"):
            return go(x.as_dict())
        if hasattr(x, "__dict__"):
            return go({k: v for k, v in vars(x).items() if not k.startswith("_")})
        return repr(x)
    return json.dumps(go(obj), sort_keys=True, separators=(",", ":"))


# -- the three stages -----------------------------------------------------------------------------
def stage_read(path: pathlib.Path):
    m = read_ecxml(path)
    return {"name": m.name, "producer": m.producer,
            "domain": m.domain, "materials": m.materials,
            "n_objects": len(m.objects), "n_active": len(m.active_objects),
            "objects": [{"kind": o.kind, "name": o.name, "path": list(o.path),
                         "location": o.location, "size": o.size, "order": o.order,
                         "noisy": o.noisy, "props": o.props,
                         "material": getattr(o, "material", None),
                         "power": getattr(o, "power", None),
                         "plane": getattr(o, "plane", None),
                         "active": o.active} for o in m.objects]}


def stage_place(path: pathlib.Path):
    from meshpipeline.cad.ingest.ecxml_place import place
    p = place(read_ecxml(path))
    return p


def stage_plan(path: pathlib.Path):
    from meshpipeline.cad.ingest.ecxml_place import place
    from meshpipeline.engines.snapgrid.grid import GridPlan, build_grid
    pl = place(read_ecxml(path))
    plan = GridPlan()
    g = build_grid(pl, plan)
    return g


STAGES = {"read": stage_read, "place": stage_place, "plan": stage_plan}


def run_one(fn, path):
    """Return ('ok', canon) or ('refused', message) or ('crash', traceback-tail)."""
    try:
        return "ok", canon(fn(path))
    except EXPECTED as exc:
        return "refused", f"{type(exc).__name__}: {exc}"
    except Exception:
        return "crash", traceback.format_exc(limit=4).strip().splitlines()[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hash", action="store_true",
                    help="print one hash per file+stage, for comparing two interpreters")
    ap.add_argument("--only", default=None)
    a = ap.parse_args()

    files = sorted(LAB.rglob("*.ecxml"))
    if a.only:
        files = [f for f in files if a.only in str(f)]

    if a.hash:
        for f in files:
            for sname, fn in STAGES.items():
                kind, body = run_one(fn, f)
                h = hashlib.sha256(body.encode()).hexdigest()[:16]
                print(f"{f.parent.name}/{f.stem}\t{sname}\t{kind}\t{h}")
        return 0

    width = max(len(f.stem) for f in files) + 2
    bad = 0
    counts = {"SAME": 0, "REFUSED": 0, "DIFFER": 0, "CRASH": 0}
    print(f"{'group':9s}{'file':{width}s}{'read':>22s}{'place':>22s}{'plan':>22s}")
    for f in files:
        cells = []
        for sname, fn in STAGES.items():
            k1, b1 = run_one(fn, f)
            k2, b2 = run_one(fn, f)
            if k1 == "crash" or k2 == "crash":
                verdict, note = "CRASH", b1 if k1 == "crash" else b2
            elif k1 != k2 or b1 != b2:
                verdict, note = "DIFFER", ""
            elif k1 == "refused":
                verdict, note = "REFUSED", b1.split(":")[0]
            else:
                verdict, note = "SAME", hashlib.sha256(b1.encode()).hexdigest()[:10]
            counts[verdict] += 1
            if verdict in ("DIFFER", "CRASH"):
                bad += 1
            cells.append(f"{verdict:<8s}{note[:13]:>13s}")
        print(f"{f.parent.name:9s}{f.stem:{width}s}" + "".join(f"{c:>22s}" for c in cells))

    print(f"\nSAME {counts['SAME']}   REFUSED {counts['REFUSED']}   "
          f"DIFFER {counts['DIFFER']}   CRASH {counts['CRASH']}")

    # every hostile file must be refused at the read stage, or the sandbox claim is empty
    unrefused = []
    for f in files:
        if f.parent.name != "hostile":
            continue
        k, _ = run_one(stage_read, f)
        if k != "refused":
            unrefused.append(f.stem)
    if unrefused:
        print("HOSTILE FILES NOT REFUSED AT READ: " + ", ".join(unrefused))
    return 1 if (bad or unrefused) else 0


if __name__ == "__main__":
    raise SystemExit(main())
