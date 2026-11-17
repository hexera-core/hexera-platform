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


#: The most of a STEP file read for its assembly tree (the usage occurrences are near the end).
TREE_BYTES = 64 * 1024 * 1024
_ENTITY = re.compile(r"#(\d+)\s*=\s*(PRODUCT|PRODUCT_DEFINITION_FORMATION(?:_WITH_SPECIFIED_SOURCE)?|"
                     r"PRODUCT_DEFINITION|NEXT_ASSEMBLY_USAGE_OCCURRENCE)\s*\((.*?)\)\s*;", re.IGNORECASE | re.S)
_REFS = re.compile(r"#(\d+)")


def _quoted(args: str) -> list[str]:
    return [s.replace("''", "'") for s in re.findall(r"'((?:[^']|'')*)'", args)]


def assembly_root_name(text: str) -> str | None:
    """THE ASSEMBLY'S OWN NAME. A STEP assembly lists every part as a PRODUCT, often the parts before
    the assembly that holds them, so the first PRODUCT named the whole upload after one part of it
    ("Housing Mount ..." for a 10-part assembly). The root is the product no usage occurrence places
    inside another. Its name when it has a real one; "" when the root is unnamed (the file's own name
    is then the better one); None when the file is no assembly (no usage occurrence)."""
    products: dict[str, str] = {}
    formation_of: dict[str, str] = {}
    definition_of: dict[str, str] = {}
    relating: list[str] = []
    related: set[str] = set()
    for m in _ENTITY.finditer(text):
        eid, kind, args = m.group(1), m.group(2).upper(), m.group(3)
        refs = _REFS.findall(args)
        if kind == "PRODUCT":
            names = _quoted(args)
            # the first string, as step_product_name reads a single part; the name field when that is a placeholder
            products[eid] = next((n for n in names[:2] if n.strip() and not is_generic(_tidy(n))), "")
        elif kind.startswith("PRODUCT_DEFINITION_FORMATION"):
            if refs:
                formation_of[eid] = refs[0]
        elif kind == "PRODUCT_DEFINITION":
            if refs:
                definition_of[eid] = refs[0]
        elif len(refs) >= 2:      # NEXT_ASSEMBLY_USAGE_OCCURRENCE(id, name, description, relating, related, ...)
            relating.append(refs[0])
            related.add(refs[1])
    if not relating:
        return None
    for pd in dict.fromkeys(relating):
        if pd in related:
            continue
        name = _tidy(products.get(formation_of.get(definition_of.get(pd, ""), ""), ""))
        if name and not is_generic(name):
            return name
    return ""


def step_product_name(path: Path | str) -> str:
    """The STEP file's own name for what it holds: an assembly's root product (assembly_root_name), else
    the first PRODUCT entity's name that is not a placeholder; "" if none (or an unnamed assembly)."""
    try:
        with open(path, "rb") as fh:
            whole = fh.read(TREE_BYTES).decode("latin-1", errors="replace")
    except OSError:
        return ""
    root = assembly_root_name(whole)
    if root is not None:
        return root
    head = whole[:HEAD_BYTES]
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
