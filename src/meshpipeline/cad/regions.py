# Responsibility: Report the separately named regions a CAD file carries, and hand that same reading to whoever tessellates it.
# Owns: the assembly/component read, the vocabulary for what was found, and the per-upload cache.
# Boundaries: it reads and describes; it tessellates nothing and decides no engine's capability.
# Collaborates with: cad/cad_tessellate.py, which writes the components this reports.
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Strings a translator writes when the file named nothing. Kept deliberately narrow: "solid" and
#: "fluid" look generic but are the real part names in a conjugate-heat file, and discarding them
#: would report a file as structureless when it distinguishes its parts perfectly well.
_GENERATED_PREFIXES = ("open cascade", "opencascade", "step translator")


@dataclass(frozen=True)
class CadRegions:

    #: Component names in file order, empty when the file distinguishes none.
    names: tuple[str, ...] = ()
    #: How the structure was found: "assembly", "roots", "stl-solids", or "" when there is none.
    source: str = ""

    @property
    def count(self) -> int:
        return len(self.names)

    def as_facts(self) -> dict:
        return {"region_names": list(self.names), "region_count": self.count,
                "region_source": self.source}


def _meaningful(name: str) -> bool:
    n = (name or "").strip()
    if not n or len(n) < 2:
        return False
    low = n.lower()
    if any(low.startswith(p) for p in _GENERATED_PREFIXES):
        return False
    # A hash-like token carries no meaning even though it is unique: mixed case, no separator,
    # and long enough that no one typed it as a part name.
    return not (len(n) >= 12 and n.isalnum() and any(c.isdigit() for c in n)
                and any(c.isupper() for c in n) and any(c.islower() for c in n))


def _stl_regions(path: Path) -> CadRegions:
    from meshpipeline.cad.stl_io import read_stl_solids

    names = tuple(n for n in read_stl_solids(path) if _meaningful(n))
    return CadRegions(names=names, source="stl-solids" if len(set(names)) > 1 else "")


def components_of(path: Path):
    # THE read of a CAD file's component structure, and the only one. Both what a file offers and
    # what gets tessellated from it are decided here, so the count reported to a user and the
    # solids written for the mesher cannot disagree about what the file contains.
    #
    # The document is returned with the labels because it OWNS them: let it fall out of scope and
    # every label goes stale, GetShape_s answers null, and a file with regions silently reads as
    # having none. The caller holds it for as long as it uses what is returned.
    #
    # The XDE reader, not STEPControl_Reader: the plain reader's OneShape() returns geometry with
    # the assembly and its names already dropped, which is why a file that names its parts and one
    # that does not are indistinguishable downstream.
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("cad"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.ReadFile(str(path))
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def named(label) -> str:
        attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
            return str(attr.Get().ToExtString())
        return ""

    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    found: list[tuple] = []
    for i in range(1, roots.Length() + 1):
        root = roots.Value(i)
        subs = TDF_LabelSequence()
        tool.GetComponents_s(root, subs)
        if subs.Length():
            found.extend((subs.Value(j), named(subs.Value(j)))
                         for j in range(1, subs.Length() + 1))
        else:
            found.append((root, named(root)))
    return doc, tool, found, roots.Length()


def _cad_regions(path: Path) -> CadRegions:
    _doc, _tool, found, root_count = components_of(path)
    named = tuple(name for _label, name in found if _meaningful(name))
    # Distinct names are what makes regions separable; one name repeated is one region described
    # many times.
    if len(set(named)) > 1:
        return CadRegions(names=named, source="assembly" if root_count == 1 else "roots")
    return CadRegions()


def regions_of(path) -> CadRegions:
    # Never fatal: a file this cannot describe is reported as carrying no regions, which is what
    # the caller would otherwise have assumed anyway.
    p = Path(path)
    try:
        if p.suffix.lower() == ".stl":
            return _stl_regions(p)
        if p.suffix.lower() in (".step", ".stp", ".igs", ".iges"):
            return _cad_regions(p)
    except Exception as exc:
        logger.info("cad regions: %s could not be described (%s)", p.name, exc)
    return CadRegions()


__all__ = ["CadRegions", "components_of", "regions_for_session", "regions_of"]


#: Keyed by (path, size, mtime) so a replaced upload is re-read rather than answered from a stale
#: entry. Reading a 25 MB STEP costs seconds; intake asks on every admission preview.
_CACHE: dict[tuple[str, int, int], CadRegions] = {}


def regions_for_session(session_id: str, jobs_dir) -> CadRegions:
    # The staged upload, read where the API already wrote it. This is deliberately not a database
    # lookup: the bytes are on disk beside the session, so the facts need no schema of their own.
    from meshpipeline.contracts.intake_formats import format_for_suffix

    root = Path(jobs_dir) / str(session_id or "")
    if not root.is_dir():
        return CadRegions()
    staged = sorted(p for p in root.iterdir()
                    if p.is_file() and format_for_suffix(p.suffix.lower()))
    if not staged:
        return CadRegions()
    path = staged[0]
    stat = path.stat()
    key = (str(path), stat.st_size, int(stat.st_mtime))
    if key not in _CACHE:
        _CACHE[key] = regions_of(path)
    return _CACHE[key]
