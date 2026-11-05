# Responsibility: Read an ASCII OpenFOAM labelList (a polyMesh's owner or neighbour) into an array, whatever its size.
# Boundaries: a reader - it judges nothing; None when the file is not a list it can trust.
# Collaborates with: engines/snappy/solvability.py and engines/cfmesh/solvability.py (the trial solve and the lean region count).
"""AN OPENFOAM LABEL LIST, READ IN PIECES AND CHECKED.

On the lab (28 Oct), the worker's lean region count for job 6cb2d606's 107 M-cell cfMesh mesh raised
(scipy: "negative axis 1 index: -1") where it should have counted. cfMesh writes the neighbour list
the length of ALL faces - 323,413,728 entries - with -1 for each boundary face after the internal
ones, and the reader took the -1s as cells. A neighbour list is now cut at its first -1 (the
internal faces always come first; a -1 anywhere before the last internal face means the list is not
one we can trust). The list is also parsed in 64 MB pieces cut at line ends, never as one string of
gigabytes, and is used only when it holds the number of labels it states - else None, and the
caller skips the measure as it does for a binary mesh.
"""
from __future__ import annotations

import logging
import re
import warnings
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: How much of the list is parsed at once.
PIECE_BYTES = 64 * 1024 * 1024
_COUNT = re.compile(rb"(?:^|\n)\s*(\d+)\s*\n\s*\(")
#: The header and the count are within this much of the start.
_HEAD = 1024 * 1024


def read_foam_labels(path: Path, *, neighbour: bool | None = None) -> np.ndarray | None:
    """The labels of the ASCII labelList at `path` (int64), or None: unreadable, binary, not the
    number of labels it says it holds, or with a negative label where none may be. `neighbour`
    (default: the file is named neighbour): the list is cut at the boundary faces' -1 padding."""
    p = Path(path)
    is_neighbour = p.name == "neighbour" if neighbour is None else neighbour
    try:
        data = p.read_bytes()
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
            logger.warning("labelList %s: more labels than the %d it states - not used", p.name, count)
            return None
        out[at:at + len(part)] = part
        at += len(part)
        pos = stop
    del data
    if at != count:
        logger.warning("labelList %s: %d labels read of %d stated - not used", p.name, at, count)
        return None
    negative = np.flatnonzero(out < 0)
    if not len(negative):
        return out
    first = int(negative[0])
    if is_neighbour and bool((out[first:] < 0).all()):
        return out[:first]           # the boundary faces' padding: the internal faces are the list
    logger.warning("labelList %s: a negative label among the cells (at %d) - not used", p.name, first)
    return None


__all__ = ["PIECE_BYTES", "read_foam_labels"]
