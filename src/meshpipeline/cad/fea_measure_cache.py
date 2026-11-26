# Responsibility: Keep an FEA measurement of a solid FILE, keyed on the file's digest (and everything else the
# measurement reads), so measuring the same solid again - a job run again, another pass's plan, the same part
# planned twice - reads the stored record instead of ray-tracing its walls and re-running the Boolean checks.
# Boundaries: a store of JSON records in the object store, keyed by content. It never fails a measurement: a
# record it cannot read or write is a miss, and the caller measures as before. It holds the RECORD only;
# the labelled triangle surface the plan sizes on is cheap and is tessellated again from the same file.
# Collaborates with: engines/fea_driver.measure_staged (the plan's measurement), cad/fea_measure (VERSION).
"""THE FEA MEASUREMENT CACHE.

Shared dev, 1 Nov: an IGES landing gear's FEA plan spent 23 minutes "Planning the mesh" on one core, and
measured again from nothing on every run of the job. Offline, the plan's measurement of that staged solid took
1090 s - 1054 s of it the suspected-defect Boolean checks, run 87 times on 11 solids. Those are now run once a
solid and on every core (cad/suspect_defects), and the record is kept here under

    fea_measure_cache/<sha256 of the staged file>-<what else it read>.json

so the next measurement of the same solid is a read.

Keyed on the bytes of the file that was measured, the measurement's VERSION, the ray budget, the unit it was
read in and a digest of the code that measures it (CODE_FILES): a record made by older rules is never served.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: The modules whose rules make the record: a change to any of them is a new key.
CODE_FILES = ("fea_measure.py", "fea_features.py", "fold_back.py", "suspect_defects.py", "validity_brep.py")
PREFIX = "fea_measure_cache"


def _code_stamp() -> str:
    here = Path(__file__).parent
    h = hashlib.sha256()
    for name in CODE_FILES:
        try:
            h.update((here / name).read_bytes())
        except OSError:
            h.update(name.encode())
    return h.hexdigest()[:16]


def file_digest(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


#: A STEP file's lines that change every time the SAME solid is written (gmsh / OpenCASCADE's writer): the HEADER's
#: write time (FILE_NAME) and the writer's per-process count in the product's name ("Open CASCADE STEP translator 7.8
#: 2"). Measured 26 Nov on the knuckle staged twice: those three lines are the only difference, and they made every
#: staging a new key - the plan step's measurement was never read back by the run (33 s of suspected-defect checks
#: and the measurement again, every run: the 85-90 s "measure + plan" of feabench_lab/BENCH.md).
_STEP_TRANSLATOR = re.compile(rb"(Open CASCADE STEP translator [0-9.]+) \d+")


def solid_digest(path: Path | str) -> str:
    """The digest of the SOLID in the file at `path`: its bytes, but for a STEP file without what changes each time
    the same solid is written (its HEADER section and the writer's count, _STEP_TRANSLATOR) - so two stagings of
    one part share one key. Any other file: its bytes (file_digest)."""
    p = Path(path)
    if p.suffix.lower() not in (".step", ".stp"):
        return file_digest(p)
    data = p.read_bytes()
    at = data.find(b"\nDATA;")
    body = data[at:] if at >= 0 else data
    return hashlib.sha256(_STEP_TRANSLATOR.sub(rb"\1", body)).hexdigest()


def key_for(path: Path | str, **reads) -> str:
    """The cache key of a measurement of the solid in the file at `path` (solid_digest) under `reads` (unit, ray
    budget, ...)."""
    from meshpipeline.cad.fea_measure import VERSION

    extra = hashlib.sha256(json.dumps({"v": VERSION, "code": _code_stamp(), **reads}, sort_keys=True,
                                      default=str).encode()).hexdigest()[:16]
    return f"{PREFIX}/{solid_digest(path)}-{extra}.json"


def load(key: str) -> dict | None:
    """The stored entry ({"record": ..., plus what the caller kept with it}), or None."""
    try:
        from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store

        try:
            got = json.loads(get_object_store().get_bytes(object_key=key))
        except ObjectNotFound:
            return None
        return got if isinstance(got, dict) and isinstance(got.get("record"), dict) else None
    except Exception as exc:  # noqa: BLE001 - a miss
        logger.info("fea measure cache: not read (%s)", type(exc).__name__)
        return None


def save(key: str, entry: dict) -> None:
    try:
        from meshpipeline.contracts.object_storage import get_object_store

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(entry, fh, default=_plain)
            tmp = Path(fh.name)
        try:
            get_object_store().upload_file(local_path=tmp, object_key=key, content_type="application/json")
        finally:
            tmp.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - measured again next time
        logger.info("fea measure cache: not written (%s)", type(exc).__name__)


def _plain(v):
    try:
        import numpy as np

        if isinstance(v, np.generic):
            return v.item()
        if isinstance(v, np.ndarray):
            return v.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(v)


__all__ = ["file_digest", "key_for", "load", "save", "solid_digest"]
