# Responsibility: Name an uploaded part for the stage - the product name a STEP file gives its part, else the uploaded file's own name - so every stage (flow and FEA alike) shows what the part is called even when no model named it.
# Boundaries: pure over the upload's bytes and its original file name; no model, no store. Generic names a CAD exporter fills in ("Part1", "Body", "geometry") are passed over.
# Collaborates with: application/geometry_check.py (records it with the scout's facts; the proposal falls back to it when the pictures name no part).
"""THE PART'S NAME, FROM THE FILE.

A STEP file names its part in its PRODUCT entity (the name the CAD tool gave the part); a mesh or
CAD file of any other kind - and a STEP whose product name is an exporter's placeholder - is named
after the uploaded file itself: "bracket_v2-final.STEP" reads "bracket v2 final".
"""
from __future__ import annotations

import re
from pathlib import Path

#: The most of a STEP file read for its product name (the PRODUCT entity is near the top).
HEAD_BYTES = 4 * 1024 * 1024
#: The longest name shown.
MAX_CHARS = 80
_PRODUCT = re.compile(r"PRODUCT\s*\(\s*'((?:[^']|'')*)'", re.IGNORECASE)
#: Names an exporter gives a part nobody named: no better than nothing.
_GENERIC = re.compile(r"^(part|body|solid|product|assembly|assem|asm|model|shape|component|geometry|"
                      r"open cascade step translator.*|unnamed|untitled|noname|default|none|n/?a|"
                      r"[-_ ]*)\s*[-_ #]*\d*$", re.IGNORECASE)
_STEP_SUFFIXES = (".step", ".stp")


def is_generic(name: str) -> bool:
    return bool(_GENERIC.match(str(name or "").strip()))


def step_product_name(path: Path | str) -> str:
    """The first PRODUCT entity's name in a STEP file that is not a placeholder; "" if none."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES).decode("latin-1", errors="replace")
    except OSError:
        return ""
    for m in _PRODUCT.finditer(head):
        name = _tidy(m.group(1).replace("''", "'"))
        if name and not is_generic(name):
            return name
    return ""


def file_stem_name(original_filename: str) -> str:
    """The uploaded file's own name, read as words: no folder, no extension, separators as spaces."""
    stem = Path(str(original_filename or "").replace("\\", "/")).name
    for _ in range(2):        # "part.step.gz", "part.stl.zip"
        stem = Path(stem).stem if Path(stem).suffix else stem
    name = _tidy(re.sub(r"[_\-.+]+", " ", stem))
    return "" if is_generic(name) else name


def part_name(path: Path | str | None, original_filename: str = "") -> str:
    """The part's name for the stage: a STEP file's own product name, else the file's name."""
    if path is not None and str(path).lower().endswith(_STEP_SUFFIXES):
        named = step_product_name(path)
        if named:
            return named
    return file_stem_name(original_filename)


def _tidy(text: str) -> str:
    return " ".join(str(text).split())[:MAX_CHARS].strip()


__all__ = ["file_stem_name", "is_generic", "part_name", "step_product_name"]
