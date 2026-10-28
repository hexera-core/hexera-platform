# Responsibility: Read an ASCII OpenFOAM labelList (a polyMesh's owner or neighbour) into an array, whatever its size.
# Boundaries: a reader - it judges nothing; None when the file is not a list it can trust.
# Collaborates with: engines/snappy/solvability.py and engines/cfmesh/solvability.py (the trial solve and the lean region count).
"""AN OPENFOAM LABEL LIST, READ IN PIECES.

The lists were parsed as one string with numpy's text parser. A 107 M-cell cfMesh mesh's neighbour
file is 2.9 GB, past 2^31 characters, and the one-string parse came back with a -1 in it - the
lean region count then raised (scipy: "negative axis 1 index: -1") where it should have counted
(lab, 28 Oct, job 6cb2d606's mesh). So the list is parsed in pieces cut at line ends, and what
comes back is checked against the count the file states: the right number of labels, none
negative - else None, and the caller skips the measure as it does for a binary mesh.
"""
from __future__ import annotations

import logging
import re
import warnings
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: How much of the list is parsed at once: well under the 2^31 characters the one-string parse broke at.
PIECE_BYTES = 64 * 1024 * 1024
_COUNT = re.compile(rb"(?:^|\n)\s*(\d+)\s*\n\s*\(")
#: The header and the count are within this much of the start.
_HEAD = 1024 * 1024


def read_foam_labels(path: Path) -> np.ndarray | None:
    """The labels of the ASCII labelList at `path` (int64), or None: unreadable, binary, or not
    the number of non-negative labels it says it holds."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    head = data[:4096].decode("ascii", errors="replace")
    if "format" in head and "binary" in head.split("FoamFile", 1)[-1][:400]:
        return None  # binary polyMesh - parser only handles ASCII
    m = _COUNT.search(data[:_HEAD])
    if not m:
        return None
    count, start = int(m.group(1)), m.end()
    end = data.find(b")", start)
    if end < 0:
        return None
    out = np.empty(count, dtype=np.int64)
    at, pos = 0, start
    try:
        while pos < end:
            stop = min(end, pos + PIECE_BYTES)
            if stop < end:
                cut = data.rfind(b"\n", pos, stop)
                stop = cut if cut > pos else stop
            with warnings.catch_warnings():
                # a piece it cannot read to its end comes back short: the count check below says so
                warnings.simplefilter("ignore", DeprecationWarning)
                part = np.fromstring(data[pos:stop].decode("ascii", errors="replace"), dtype=np.int64, sep=" ")
            if at + len(part) > count:
                raise ValueError("more labels than the list states")
            out[at:at + len(part)] = part
            at += len(part)
            pos = stop
    except (ValueError, OverflowError) as exc:
        logger.warning("labelList %s unreadable (%s)", Path(path).name, exc)
        return None
    if at != count or (count and int(out.min()) < 0):
        logger.warning("labelList %s: %d labels read of %d stated, or a negative one - not used",
                       Path(path).name, at, count)
        return None
    return out


__all__ = ["PIECE_BYTES", "read_foam_labels"]
