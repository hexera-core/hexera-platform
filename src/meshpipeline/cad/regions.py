# Responsibility: Report what an uploaded geometry is, from the stored measurement when there is one and from the bytes when there is not.
# Owns: the assembly/component read, the vocabulary for what was found, the per-upload cache, and the reading a session gets.
# Boundaries: it reads and describes; it tessellates nothing and decides no engine's capability.
# Collaborates with: cad/cad_tessellate.py, which writes the components this reports, and application/geometry_materializer.py for the durable bytes.
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


__all__ = ["CadRegions", "MEASURED_NOT_ATTEMPTED", "agent_block_for_state", "components_of",
           "reading_for_source",
           "regions_for_session", "regions_of", "stored_document_for_source",
           "surface_analysis_from_document"]


#: Keyed by (path, size, mtime) so a replaced upload is re-read rather than answered from a stale
#: entry. Reading a 25 MB STEP costs seconds; intake asks on every admission preview.
_CACHE: dict[tuple[str, int, int], CadRegions] = {}


def regions_for_session(session_id: str, jobs_dir) -> CadRegions:
    """The staged upload, read where the API used to leave it.

    THIS READS A DIRECTORY THAT IS EMPTY BY DESIGN. `api/v1/upload.py:253` unlinks the staging
    buffer at the end of every successful upload, because the durable copy is the object in
    storage; this lists that same directory. It has therefore returned an empty `CadRegions` on
    every job ever run, which is why the admission preview has always compared the customer's
    declaration against nothing and why `contracts/rationale.py` told them otherwise.

    It is kept, unchanged, for the one case it still answers: a file staged and not yet cleaned up,
    which is what an in-process test and a local run without object storage have. `reading_for_source`
    below is the reader that actually sees a customer's geometry.
    """
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


# -------------------------------------------------------------------------------------------------
# THE READING A SESSION GETS
#
# Three situations used to arrive as one value. `regions_for_session` returned an empty `CadRegions`
# whether the file had no named regions, the directory was gone, or the read threw; intake's
# `_geometry_facts` then turned every exception into `{}`; and `engines/base.py:430-434` only tests
# `surface_analysis is not None`, so `{}` passed the gate carrying nothing. A reader could not tell
# "measured and unremarkable" from "never measured".
#
# The contract here, from docs/pipeline.md section 2.3:
#   None                      the measured phase did not run. Not an error, and the common case.
#   {"status": "..."} + keys  it ran. `status` always present, so no verdict is inferred from an
#                             absent key.
# -------------------------------------------------------------------------------------------------

#: What a reader is handed when nothing was measured and nothing could be read. It is `None`, and
#: this name exists so the intent is greppable rather than a bare literal at four call sites.
MEASURED_NOT_ATTEMPTED = None

#: The five keys `engines/base.py` reads off `surface_analysis`, plus the `status` that makes the
#: three situations distinguishable. `region_count` and `region_names` at `base.py:172-178`,
#: `self_intersecting` at `:565`, `diag` and `thin_gap` at `:573-574`.
_PROJECTION_KEYS = ("region_names", "region_count", "region_source", "diag", "thin_gap")


def surface_analysis_from_document(document: Any) -> dict | None:
    """What the engines read, from a stored measurement document. None when nothing was measured.

    ONE TRANSLATION HAPPENS HERE AND IT IS LOAD-BEARING: `self_intersecting`.

    The measurement package does not test for self-intersection, and says so with the string
    `"unknown"` rather than `None`, because `None` reads as "not self-intersecting" to a dict `.get`.
    On this side of the boundary `"unknown"` is worse: `base.py:565` is
    `if ic.require_no_self_intersection and analysis.get("self_intersecting")`, a truthiness test, and
    a non-empty string is true. `engines/vmtk/spec.py:190` declares
    `require_no_self_intersection=True` and vmtk is `implemented=True`, so passing the word through
    would reject every vmtk upload with "[GEOMETRY_UNSUITABLE] the input surface self-intersects" on
    the strength of a measurement that never looked.

    So the key is OMITTED when nothing measured it, which is the only value `.get` reads as "no
    claim", and the package's own word is carried under `self_intersecting_state` where no gate reads
    it and no information is lost. The day the measurement learns to test for it, a real `True` or
    `False` comes through here unchanged.
    """
    if not isinstance(document, dict) or not document:
        return None
    from meshpipeline.contracts.geometry_measurement import STATUS_OK, projection_of

    projection = projection_of(document)
    if projection is None:
        return None
    if projection.get("status") != STATUS_OK:
        # It ran and could not finish. The dict carries `status` and `reason` and no measurements,
        # so every engine rule that needs a number sees no number rather than a wrong one.
        return {"status": projection.get("status"), "reason": str(projection.get("reason") or "")}
    out: dict = {"status": STATUS_OK}
    for key in _PROJECTION_KEYS:
        if key in projection:
            out[key] = projection[key]
    claimed = projection.get("self_intersecting")
    if isinstance(claimed, bool):
        out["self_intersecting"] = claimed
    else:
        out["self_intersecting_state"] = str(claimed or "unknown")
    out["units"] = projection.get("units") or "file"
    return out


async def reading_for_source(ref, *, sha256: str = "") -> dict | None:
    """What the uploaded geometry is, for the session that owns `ref`. Never raises.

    THE STORED REPORT FIRST. It is the only reading that costs nothing: the file was measured once,
    at upload, against these exact bytes, and the row is keyed to their digest.

    THE OBJECT STORE SECOND. When there is no row - the measurement was off when this file landed,
    or the worker has not reached it yet - the bytes are still there for thirty days and
    `application/geometry_materializer.fetch_verified_bytes` is a verified route to them. Reading the
    component structure off a downloaded STEP is a few seconds and it is what the old reader was
    trying to do before the staging buffer was deleted out from under it.

    NOTHING THIRD. `None` means the measured phase did not run, and every caller renders that as the
    absence it is. A measurement that describes different bytes is the one condition that is not an
    absence: `contracts/geometry_measurement.document_for` raises `MeasurementMismatch` for it, and
    that is caught here and reported as not attempted, because a port table measured off a file the
    customer replaced is worse than no table.
    """
    if ref is None:
        return MEASURED_NOT_ATTEMPTED
    digest = str(sha256 or getattr(ref, "sha256", "") or "")
    document = await _stored_document(ref, digest)
    if document is not None:
        analysis = surface_analysis_from_document(document)
        if analysis is not None:
            return analysis
    return await _analysis_from_object_store(ref)


async def stored_document_for_source(ref, *, sha256: str = "") -> dict | None:
    """The whole stored measurement document, or None. Never raises.

    Separate from `reading_for_source` because two different readers want two different things: the
    engines want five keys, and the conversation wants the opening table. Neither is derivable from
    the other, and both come from one row.
    """
    if ref is None:
        return None
    return await _stored_document(ref, str(sha256 or getattr(ref, "sha256", "") or ""))


async def agent_block_for_state(state) -> dict | None:
    """The mesh planner's typed block for the geometry a run's state already names. Never raises.

    It lives here, beside the row read it needs, because `contracts/` may import nothing above
    itself (`tests/unit/hygiene/test_architecture_boundaries.py::test_contracts_are_neutral`) and
    the shape of the block is a contract while reading a row is not.

    `None` means the mesh planner adds no key and composes exactly the dict it composes today. It is
    the answer to every failure: the setting off, no measurement, a row for different bytes, a
    document written before the block existed in an image without the package, or a read that threw.
    """
    import meshpipeline.settings.policy as polcfg

    if not polcfg.GEOMETRY_REPORT_READERS_ENABLED:
        return None
    try:
        from meshpipeline.contracts.geometry_agent_block import block_for_document
        from meshpipeline.contracts.geometry_source import GeometrySourceRef

        payload = ((state or {}).get("geometry") or {}).get("ref")
        if not payload:
            return None
        ref = GeometrySourceRef.from_payload(payload)
        document = await stored_document_for_source(ref)
        block = block_for_document(document)
        if block is None or document is None:
            return None
        return (await _surveyed_block(ref, document)) or block
    except Exception as exc:                       # noqa: BLE001 - a plan is never failed for this
        logger.warning("geometry agent block: unavailable for this run (%s) - the planner sees "
                       "exactly what it sees with no measurement", exc)
        return None


async def _surveyed_block(ref, document: dict) -> dict | None:
    """STEP 7 of the chain: the block composed for what the customer said, with the survey in it.

    None with the survey off, with no survey stored for these bytes, or when the package refuses the
    pair, and the caller then hands the planner the measurement's own block, exactly as before. The
    gate is read first, so with it off nothing is imported and no row is read.

    The survey's block is preferred because it was composed FOR the customer: their purpose decided
    the representation and their budget is `customer_cell_cap`. If the look landed after the survey
    was last composed and the recomposition on the look worker did not happen, it is composed again
    here, in memory, from the same stored inputs, so the planner never gets the older of the two.
    """
    import meshpipeline.settings.policy as polcfg

    if not polcfg.GEOMETRY_SURVEY_ENABLED:
        return None
    from meshpipeline.application import geometry_survey as gs

    if not gs.survey_enabled():
        return None
    state = await gs.load(str(ref.owner_id), str(ref.source_id), sha256=str(ref.sha256))
    if state is None:
        return None
    stored_look = document.get("look")
    look = stored_look if isinstance(stored_look, dict) else {}
    if look.get("status") == "ok" and (state.get("composed_for") or {}).get("look_status") != "ok":
        try:
            state = gs.recomposed(state, document)
        except gs.SurveyError as exc:
            logger.info("geometry agent block: the survey could not take the look in (%s)", exc)
    return gs.builder_block(state)


async def _stored_document(ref, digest: str) -> dict | None:
    from meshpipeline.contracts.geometry_measurement import MeasurementMismatch, document_for

    try:
        import uuid as _uuid

        from meshpipeline.persistence.repositories.geometry_measurement_repository import (
            GeometryMeasurementRepository,
        )
        from meshpipeline.persistence.session import get_db

        source_uuid = _uuid.UUID(str(getattr(ref, "source_id", "") or ""))
        async with get_db() as db:
            row = await GeometryMeasurementRepository().for_source(
                db, owner_id=str(getattr(ref, "owner_id", "") or ""),
                geometry_source_id=source_uuid)
        return document_for(row, sha256=digest)
    except MeasurementMismatch as exc:
        # DATA INTEGRITY, and the honest answer is to describe nothing rather than the wrong file.
        logger.warning("geometry reading: the stored measurement does not describe these bytes - "
                       "source_id=%s: %s", getattr(ref, "source_id", ""), exc)
        return None
    except Exception as exc:                      # noqa: BLE001 - a reading is never worth a turn
        logger.info("geometry reading: no stored measurement available - source_id=%s (%s)",
                    getattr(ref, "source_id", ""), exc)
        return None


async def _analysis_from_object_store(ref) -> dict | None:
    """The components, read off the durable bytes. The fallback the old reader could not take.

    It answers a narrower question than the measurement - which parts a file names, and nothing
    about openings, thickness or size - so the dict it returns carries only the keys it actually
    measured. No engine rule is fed a number this did not compute.
    """
    import asyncio
    import tempfile

    def _read() -> dict | None:
        from meshpipeline.application.geometry_materializer import fetch_verified_bytes

        with tempfile.TemporaryDirectory(prefix="geometry-reading-") as workspace:
            local = fetch_verified_bytes(ref, workspace=workspace,
                                         job_id=f"reading:{getattr(ref, 'source_id', '')}")
            regions = regions_of(local)
        from meshpipeline.contracts.geometry_measurement import STATUS_OK
        return {"status": STATUS_OK, "region_names": list(regions.names),
                "region_count": regions.count, "region_source": regions.source,
                #: Not measured here, and the key is absent rather than false: see
                #: `surface_analysis_from_document`. `diag` and `thin_gap` are absent for the same
                #: reason, so `geometry_unsuitable` computes no ratio from numbers nobody produced.
                "self_intersecting_state": "unknown", "units": "file",
                "reading_source": "object_store"}

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:                      # noqa: BLE001 - fail open, always
        logger.info("geometry reading: the durable bytes could not be described - source_id=%s (%s)",
                    getattr(ref, "source_id", ""), exc)
        return MEASURED_NOT_ATTEMPTED
