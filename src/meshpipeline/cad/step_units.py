# Responsibility: Read the length unit a STEP file declares, from the file's own text: the SI unit
# (with its prefix, or none - plain metres) or the conversion-based unit (inches, a "MILLIMETRE"
# defined as 1 x mm, a "METRE" defined as 1 x m) that its geometric contexts assign.
# Boundaries: evidence, not a decision. It reads the text and nothing else - no OpenCASCADE - so a
# unit entity OCC would silently read as its metre fallback (a bad prefix, a missing name) is
# reported malformed here instead of being believed, and a well-formed plain metre is told apart
# from that fallback. It never scales geometry.
# Collaborates with: cad/unit_evidence.py, which turns what is read here into the evidence record.
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: The SI prefixes STEP spells, as powers of ten of the metre.
_PREFIX = {"EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9, "MEGA": 1e6, "KILO": 1e3,
           "HECTO": 1e2, "DECA": 1e1, "DECI": 1e-1, "CENTI": 1e-2, "MILLI": 1e-3, "MICRO": 1e-6,
           "NANO": 1e-9, "PICO": 1e-12, "FEMTO": 1e-15, "ATTO": 1e-18}
_NUM = rb"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_CONTEXT = re.compile(rb"GLOBAL_UNIT_ASSIGNED_CONTEXT\s*\(\s*\(([^)]*)\)\s*\)")
_REF = re.compile(rb"#(\d+)")
_SI = re.compile(rb"SI_UNIT\s*\(\s*([^,()]*?)\s*,\s*([^,()]*?)\s*\)")
_CONVERSION = re.compile(rb"CONVERSION_BASED_UNIT\s*\(\s*'((?:[^']|'')*)'\s*,\s*#(\d+)\s*\)")
#: The conversion's measure: typed (LENGTH_MEASURE(25.4), as most exporters write it) or bare
#: (25.4, as OpenCASCADE's own writer does), then the unit it is counted in.
_MEASURE = re.compile(rb"MEASURE_WITH_UNIT\s*\(\s*(?:[A-Z_]*MEASURE\s*\(\s*(" + _NUM + rb")\s*\)|("
                      + _NUM + rb"))\s*,\s*#(\d+)")
_ASSIGN = re.compile(rb"\s*=\s*")
#: How deep a conversion may chain (inch -> millimetre -> metre is two); a loop is malformed.
_MAX_DEPTH = 4


@dataclass(frozen=True)
class DeclaredLength:
    """One length unit a geometric context assigns: metres per file unit, and its name as the
    file spells it - or, when the entity does not parse, None and the reason."""

    metres: float | None
    name: str
    problem: str = ""


class _Instances:
    """The file's instances, found by number on demand: the unit entities are a handful among
    millions, so they are looked up, never indexed."""

    def __init__(self, data: bytes):
        self._data = data
        self._cache: dict[int, bytes | None] = {}

    def body(self, ref: int) -> bytes | None:
        if ref not in self._cache:
            self._cache[ref] = self._find(ref)
        return self._cache[ref]

    def _find(self, ref: int) -> bytes | None:
        # a plain substring search (memchr-fast even over tens of megabytes), then a check that
        # this is the instance's own name - "#12 =" at the start of an instance - and not a
        # reference to it or the head of "#123"
        data, needle, at = self._data, b"#" + str(ref).encode(), 0
        while True:
            at = data.find(needle, at)
            if at < 0:
                return None
            end = at + len(needle)
            before_ok = at == 0 or data[at - 1:at] in (b";", b" ", b"\n", b"\r", b"\t")
            m = _ASSIGN.match(data, end)
            if before_ok and m is not None:
                break
            at = end
        # the instance runs to the first semicolon outside a quoted string
        i, n, quoted = m.end(), len(data), False
        start = i
        while i < n:
            c = self._data[i]
            if c == 0x27:                         # a quote; '' inside a string is an escaped one
                quoted = not quoted
            elif c == 0x3B and not quoted:        # ;
                return self._data[start:i]
            i += 1
        return None


def _unit_metres(instances: _Instances, ref: int, depth: int = 0) -> DeclaredLength:
    body = instances.body(ref)
    if body is None:
        return DeclaredLength(None, "", f"unit #{ref} is missing")
    if depth > _MAX_DEPTH:
        return DeclaredLength(None, "", "the unit definitions refer to each other in a loop")
    conv = _CONVERSION.search(body)
    if conv is not None:
        name = conv.group(1).decode("latin-1").replace("''", "'")
        measure = instances.body(int(conv.group(2)))
        m = _MEASURE.search(measure or b"")
        if m is None:
            return DeclaredLength(None, name, f"the conversion of {name!r} states no length")
        base = _unit_metres(instances, int(m.group(3)), depth + 1)
        if base.metres is None:
            return DeclaredLength(None, name, base.problem)
        value = float(m.group(1) or m.group(2))
        if not value > 0:
            return DeclaredLength(None, name, f"the conversion of {name!r} is not a positive length")
        return DeclaredLength(value * base.metres, name)
    si = _SI.search(body)
    if si is None:
        return DeclaredLength(None, "", "the length unit is neither an SI nor a conversion-based unit")
    prefix, unit = si.group(1).strip(), si.group(2).strip()
    if unit != b".METRE.":
        return DeclaredLength(None, "", f"the SI length unit names {unit.decode('latin-1')!r}, not the metre")
    if prefix in (b"$", b""):
        return DeclaredLength(1.0, "metre")
    word = prefix.strip(b".").decode("latin-1")
    if not (prefix.startswith(b".") and prefix.endswith(b".")) or word not in _PREFIX:
        return DeclaredLength(None, "", f"the SI prefix {prefix.decode('latin-1')!r} is not one STEP defines")
    return DeclaredLength(_PREFIX[word], f"{word.lower()}metre")


def _length_unit_of_context(instances: _Instances, refs: list[int]) -> DeclaredLength | None:
    """The length unit among a context's assigned units: the one that says LENGTH_UNIT, else an
    SI unit of the metre. None when the context assigns no length unit at all."""
    fallback = None
    for ref in refs:
        body = instances.body(ref)
        if body is None:
            continue
        if b"LENGTH_UNIT" in body:
            return _unit_metres(instances, ref)
        if fallback is None and b".METRE." in body and b"SI_UNIT" in body:
            fallback = ref
    return _unit_metres(instances, fallback) if fallback is not None else None


def declared_lengths(path: str | Path) -> list[DeclaredLength]:
    """Every distinct length unit the file's geometric contexts assign, in order of first use.
    Empty when no context assigns one."""
    data = Path(path).read_bytes()
    instances = _Instances(data)
    seen: dict[tuple, DeclaredLength] = {}
    for ctx in _CONTEXT.finditer(data):
        refs = [int(r) for r in _REF.findall(ctx.group(1))]
        found = _length_unit_of_context(instances, refs)
        if found is None:
            continue
        key = (round(found.metres, 12) if found.metres is not None else None, found.problem)
        seen.setdefault(key, found)
    return list(seen.values())
