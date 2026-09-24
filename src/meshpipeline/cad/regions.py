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
           "planner_inputs_for_state", "reading_for_source",
           "regions_of", "stored_document_for_source",
           "surface_analysis_from_document", "what_the_measurement_said_about",
           "with_the_stages_that_did_not_run"]


# -------------------------------------------------------------------------------------------------
# THE READING A SESSION GETS
#
# Three situations used to arrive as one value. Intake's reader used to list the staging directory
# `api/v1/upload.py` empties at the end of every upload, so it returned an empty reading whether the
# file had no named regions, the directory was gone, or the read threw; `_geometry_facts` then turned
# every exception into `{}`; and `engines/base.py:430-434` only tests `surface_analysis is not None`,
# so `{}` passed the gate carrying nothing. A reader could not tell "measured and unremarkable" from
# "never measured", and a request for five named wall patches was refused on a count of zero that came
# from an empty directory rather than from anything anybody had looked at.
#
# The contract here, from docs/pipeline.md section 2.3:
#   None                      the measured phase did not run. Not an error, and the common case.
#   {"status": "..."} + keys  it ran. `status` always present, so no verdict is inferred from an
#                             absent key.
# -------------------------------------------------------------------------------------------------

#: What a reader is handed when nothing was measured and nothing could be read. It is `None`, and
#: this name exists so the intent is greppable rather than a bare literal at four call sites.
MEASURED_NOT_ATTEMPTED = None

#: The keys `engines/base.py` reads off `surface_analysis`, plus the `status` that makes the three
#: situations distinguishable. `region_count` and `region_names` at `base.py:172-178`,
#: `self_intersecting` at `:565`, `diag` and `thin_gap` at `:573-574`.
#:
#: `connected_components` IS HOW MANY REGIONS THE FILE HAS, where `region_count` is how many it NAMES, and
#: dropping it here is what made a correct refusal say something false. `region_count` is 1 for a file that
#: names none of its parts - the right answer to this platform's question, because the mesher writes each
#: patch under its region's name and an unnamed file can only ever deliver one - and `_wall_patch_cause` read
#: that 1 as the number of regions and told the customer "Your geometry is one region with no component
#: names". MEASURED on `tests/fixtures/geometry/cht_enclosing_2region.step`: two regions, one enclosing the
#: other, and every name on it a generated OpenCASCADE translator name the agent drops. So the decision was
#: right and the sentence was false about a part we had measured correctly.
_PROJECTION_KEYS = ("region_names", "region_count", "region_source", "connected_components", "diag", "thin_gap")


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
    engines want a handful of keys, and the conversation wants the opening table. Neither is derivable from
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

    `None` means the mesh planner adds no key and composes exactly the dict it composes without one.
    It is the answer to every failure: no measurement, a row for different bytes, a document written
    before the block existed in an image without the package, or a read that threw.
    """
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
        return with_the_stages_that_did_not_run(
            _checked((await _surveyed_block(ref, document)) or _with_the_look_state(block, document)), document)
    except Exception as exc:                       # noqa: BLE001 - a plan is never failed for this
        logger.warning("geometry agent block: unavailable for this run (%s) - the planner sees "
                       "exactly what it sees with no measurement", exc)
        return None


def _with_the_look_state(block: dict | None, document: dict) -> dict | None:
    """The measurement's OWN block, with what happened to the look said in it. Never raises.

    WHICH BLOCK THIS IS, and why it needed its own line. `_surveyed_block` returns None when no survey was
    stored for these bytes, and the planner is then handed `hexera.planner_block`'s block, composed from the
    document alone. That block carries `looked`, a boolean, so a look that FAILED and a look nobody took reach
    the builder as the same False beside the same empty `seen` - the same collapse that
    `geometry_survey.with_the_look_state` closes on the two paths that have a row. There is no row here, so
    the state is read from the document (`geometry_survey.look_state_of_document`), which can say `ok`,
    `failed` or `not_attempted` and deliberately never says `pending`: the queue's answer lives on the row.

    NEVER RAISES, and that is this path's rule rather than a shortcut. Everything from here down is the
    fail-open the planner gets when the geometry step did not run; a refusal of the row must cost the row and
    not the block, because a block with no sentence is where the builder already was and a missing block is
    worse. `_checked` then runs the whole contract over whatever comes back.
    """
    from meshpipeline.application import geometry_survey as gs

    try:
        return gs.with_the_look_state(block, gs.look_state_of_document(document))
    except Exception as exc:                       # noqa: BLE001 - a plan is never failed for this
        logger.warning("geometry agent block: the look's state is not in the block the builder reads (%s) - "
                       "it says `looked` and no more, which cannot tell a failed look from an absent one", exc)
        return block


def _checked(block: dict | None) -> dict | None:
    """The block, with its `survey` key checked against the whole contract and DROPPED if it breaks it.

    WHICH BLOCK THIS IS. When the geometry step did not run, the planner is handed
    `hexera.planner_block`'s own survey, and that block has the look's free-text findings added to it after the
    `SurveyHandoff` validator ran, so two of the six rules never saw them: no builder control, and no claim
    about how the mesh turns out. `geometry_survey.check_the_survey_block` runs all six.

    DROPPED AND NOT CUT. The planner reading no `survey` key is the state it was in before the survey existed
    and is this platform's fail-open everywhere else on this path; the block's own numbers are untouched. What
    must not happen is the builder acting on a setting the Surveyor named or a prediction it made, so the whole
    key goes and the reason is logged rather than a sentence being edited out of it.
    """
    if not isinstance(block, dict) or not isinstance(block.get("survey"), dict):
        return block
    from meshpipeline.application import geometry_survey as gs

    try:
        gs.check_the_survey_block(block["survey"])
    except gs.SurveyError as exc:
        logger.warning("geometry agent block: the survey is not handed to the planner (%s) - it reads the "
                       "measurement's own block, exactly as it does with no survey at all", exc)
        return {k: v for k, v in block.items() if k != "survey"}
    return block


async def planner_inputs_for_state(state) -> tuple[Any, dict | None, str]:
    """What the mesh planner is handed for a run: `(request_txt, typed block, why the step was not used)`.

    STEP 7 OF THE CHAIN: the geometry agent's write-up in front of the request and the block the
    package's `contract.deliver.builder_handoff` validated after the cut, carrying the survey, intake's
    write-up, the flow patches and the plan's envelope. Whenever that cannot be had (no plan, a failed
    step, a plan for other answers, a contract refusal, a read that threw) the answer is
    `state.get("request_txt", "")` and `agent_block_for_state(state)`, the two values every planner call
    site read before the step existed, and the third element says why. It never raises.

    THE THIRD VALUE IS THE FACT ON THE RECORD. An empty reason means the step was used; a reason means
    it was not, and says which failure it was. The caller logs it and writes it to the job's record, so
    a plan made without the geometry agent never looks like one made with it.
    """
    request_txt = (state or {}).get("request_txt", "")
    try:
        got = await _geometry_step_inputs(state, str(request_txt or ""))
        return got["request_txt"], got["typed"], ""
    except Exception as exc:                       # noqa: BLE001 - a plan is never failed for this
        from meshpipeline.application.geometry_step import StepRefused

        why = str(exc) if isinstance(exc, StepRefused) else f"{type(exc).__name__}: {exc}"
    return request_txt, await agent_block_for_state(state), why[:1000]


async def _geometry_step_inputs(state, request_txt: str) -> dict:
    """The step's handoff for this run, recorded in the job ledger once. Raises with the reason."""
    import asyncio

    from meshpipeline.application import geometry_step as gst
    from meshpipeline.application import geometry_survey as gs
    from meshpipeline.contracts.geometry_source import GeometrySourceRef

    payload = ((state or {}).get("geometry") or {}).get("ref")
    if not payload:
        raise gst.StepRefused("this run names no uploaded geometry")
    ref = GeometrySourceRef.from_payload(payload)
    document = await stored_document_for_source(ref)
    if document is None:
        raise gst.StepRefused("there is no stored measurement of these bytes")
    survey = await gs.load(str(ref.owner_id), str(ref.source_id), sha256=str(ref.sha256))
    if survey is None:
        raise gst.StepRefused("no survey was stored for this upload")
    handoff = await asyncio.to_thread(gst.builder_handoff, survey, document, request_txt=request_txt)
    # THE SAME SIX RULES ON THE STEP'S OWN BLOCK. `contract.deliver.check_builder_handoff` has already run, and
    # it runs `check_survey_block`, which is the same four rules with the same two missing. A refusal here is a
    # refused STEP, so `planner_inputs_for_state` falls back to the measurement's own block and writes the
    # reason to the job's record, which is the path a failed step already takes.
    try:
        gs.check_the_survey_block((handoff.get("typed") or {}).get("survey"))
    except gs.SurveyError as exc:
        raise gst.StepRefused(str(exc)) from exc
    recorded = gst.record_handover(survey, handoff)
    if recorded is not survey:
        await gs.save(str(ref.owner_id), str(ref.source_id), recorded)
    # THE SAME REFUSAL ON THE STEP'S OWN BLOCK. `deliver.builder_handoff` composes the typed block from the
    # same stored measurement, so a stage the clock took is missing from it in exactly the same way, and this
    # is the path the builder takes whenever the geometry agent planned the part, which is every job with a
    # survey to read. Applied after `check_builder_handoff`, which does not read `places_refused`, and after
    # `platform_drops`, which cannot move because this adds no key the planner's allowlist does not carry.
    typed = with_the_stages_that_did_not_run(handoff["typed"], document)
    return {"request_txt": gst.request_with_write_up(handoff, request_txt), "typed": typed}


async def _surveyed_block(ref, document: dict) -> dict | None:
    """STEP 7 of the chain: the block composed for what the customer said, with the survey in it.

    None with no survey stored for these bytes, or when the package refuses the pair, and the caller
    then hands the planner the measurement's own block.

    The survey's block is preferred because it was composed FOR the customer: their purpose decided
    the representation and their budget is `customer_cell_cap`. If the look landed after the survey
    was last composed and the recomposition on the look worker did not happen, it is composed again
    here, in memory, from the same stored inputs, so the planner never gets the older of the two.

    WHATEVER THE LOOK DID, not only when it landed. This used to recompose on `ok` alone, so a look that
    FAILED after the survey was last composed never reached the row at all: the row still said
    `not_attempted`, and the block then told the builder no look had been taken of a part whose look had
    broken. A failed look is not an absent one and the builder is entitled to know which it was (audit item
    15), so any change in the look's status is composed in here.
    """
    from meshpipeline.application import geometry_survey as gs

    state = await gs.load(str(ref.owner_id), str(ref.source_id), sha256=str(ref.sha256))
    if state is None:
        return None
    stored_look = document.get("look")
    look = stored_look if isinstance(stored_look, dict) else {}
    status = str(look.get("status") or "")
    if status and status != str((state.get("composed_for") or {}).get("look_status") or ""):
        try:
            state = gs.recomposed(state, document)
        except gs.SurveyError as exc:
            logger.info("geometry agent block: the survey could not take the look in (%s)", exc)
    return gs.builder_block(state)


# -------------------------------------------------------------------------------------------------
# A STAGE OF THE MEASUREMENT THAT DID NOT RUN
#
# `facts.measure` runs on a 120-second wall-clock budget and skips its optional stages past it, saying so
# in `facts.warnings`, which `hexera.report_measured` copies to `document["warnings"]`. None of those lines
# reached the builder. The composed block carried 18 keys and `status: ok`, exactly as it does for a
# measurement that finished, and the stage's result was an ABSENT KEY with no refusal beside it.
#
# MEASURED, on a real part where the budget actually bites: tests/fixtures/geometry/cht_enclosing_2region.step
# at a 0.1-second budget against the same file at 120. The short measurement wrote nine warning lines, one of
# them "time budget of 0s exhausted after 0s: passage_ends skipped", and left `document["passage_ends"]`
# absent where the complete one left `[]`. Both composed a block with `places_refused` null and the same empty
# closed-end list, so a part whose closed ends were never measured and a part that has none reached the
# builder as the same block. That is a fact that lies, and the builder cannot tell it from a missing one.
# -------------------------------------------------------------------------------------------------

#: The stages a skipped measurement costs a PLACE, and the kind of place each one costs. Only these of the
#: stages a budget can take away produce places at all, and `passage_ends` is the one whose absence reads as a
#: measured zero: `hexera.planner_places` places a `closed_end` for every entry of `document["passage_ends"]`
#: and nothing at all when the key is not there.
#:
#: `document["passage_ends"]` IS THE MEASUREMENT'S OWN THREE-VALUED ANSWER, and that is what makes this
#: sayable honestly: a list when the stage ran and found ends, `[]` when it ran and found none, and ABSENT
#: when it did not run (`hexera.passage_ends_block`, whose docstring draws the same line). The platform reads
#: which of the three it is; it does not decide it.
PLACES_A_SKIPPED_STAGE_COSTS: dict[str, str] = {"passage_ends": "closed_end"}

#: What the block says when a stage did not run, in the platform's own words: a PLACE and a MEASUREMENT, no
#: builder setting and no claim about how the mesh turns out. `geometry_survey.what_the_surveyor_may_not_say`
#: is the check, and a test runs it over this constant.
STAGE_DID_NOT_RUN = ("the measurement's {stage} stage did not run on this part, so its {kind} places were "
                     "never measured: none is placed here, and an empty list is not a measurement that "
                     "there are none")
#: What is said when the measurement wrote nothing about it. A document stored before the stage existed also
#: carries the key absent, and which of the two it was is not on the record, so neither is claimed.
STAGE_SAID_NOTHING = ("the measurement did not say why, so whether the clock took it or the row predates the "
                      "stage is not on the record")


def what_the_measurement_said_about(document: Any, stage: str) -> list[str]:
    """The measurement's own warning lines that NAME this stage, verbatim and whole.

    It matches the stage's own IDENTIFIER and nothing else. `facts.measure` writes `"...: {stage} skipped"`,
    `"{stage} not measured: ..."` and, in its overrun line, `"the record is missing ... {stage}"`, with
    `stage` its own name in every one. So the join is on a name both sides own: no sentence is parsed for
    meaning here and none is rewritten, which is the only way this platform may carry another distribution's
    words to a builder.
    """
    if not isinstance(document, dict):
        return []
    return [str(w) for w in (document.get("warnings") or []) if stage in str(w)]


def with_the_stages_that_did_not_run(block: dict | None, document: Any) -> dict | None:
    """The block, with a refusal row for every place a stage of the measurement never measured.

    IT GOES IN `places_refused` AND NOT IN A KEY OF ITS OWN. `engines/snappy/planner.py` holds an allowlist of
    the keys a block may put in front of a model (`GEOMETRY_AGENT_BLOCK_KEYS`), so a key this module invented
    would be dropped there in silence, which is the same failure as the one being fixed one boundary further
    on. `places_refused` is on that list, the planner's own note already tells a model how to read it ("Treat
    those spots as unknown, not as ordinary"), and a stage that did not run is exactly a place the measurement
    will not state.

    THE PLATFORM'S SENTENCE IS KEPT WHATEVER HAPPENS TO THE QUOTE. The measurement's own lines ride inside the
    row so the reason travels, and they are checked against the two rules the Surveyor's own block is checked
    against before they go; a quote that breaks either one, or a check that cannot run at all because the
    package is not in this image, leaves the platform's sentence on its own rather than losing the refusal.
    """
    if not block or not isinstance(block, dict) or not isinstance(document, dict):
        # An empty block is not a block: `block_for_document` returns None rather than `{}` and the planner
        # refuses a dict that does not name itself, so there is nothing here for a refusal to be read beside.
        return block
    rows = [r for r in (block.get("places_refused") or []) if isinstance(r, dict)]
    added = []
    for stage, kind in PLACES_A_SKIPPED_STAGE_COSTS.items():
        if document.get(stage) is not None:
            continue                               # it ran: `[]` is a measured answer and says so
        if any(str(r.get("kind")) == kind for r in rows):
            # THE PACKAGE ALREADY REFUSED THIS KIND OF PLACE and its reason is its own. One refusal is what
            # the builder acts on - treat those spots as unknown - and a second row saying the same thing in
            # this platform's words adds nothing it can act on. `hexera.planner_places` writes exactly such a
            # row when the representation forbids placing a closed end, though only where there were measured
            # ends to place, so today the two cannot both arise; this is what keeps that true.
            continue
        added.append({"kind": kind, "why": _why_a_stage_did_not_run(document, stage, kind)})
    if not added:
        return block
    return {**block, "places_refused": [*rows, *added]}


def _why_a_stage_did_not_run(document: dict, stage: str, kind: str) -> str:
    mine = STAGE_DID_NOT_RUN.format(stage=stage.replace("_", " "), kind=kind.replace("_", " "))
    said = what_the_measurement_said_about(document, stage)
    if not said:
        return f"{mine}. {STAGE_SAID_NOTHING}"
    try:
        from meshpipeline.application import geometry_survey as gs

        broken = gs.what_the_surveyor_may_not_say(said)
    except Exception as exc:                       # noqa: BLE001 - the refusal is worth more than the quote
        logger.info("geometry agent block: the measurement's own words could not be checked (%s); the "
                    "refusal travels without them", exc)
        return f"{mine}. {STAGE_SAID_NOTHING}"
    if broken:
        logger.warning("geometry agent block: the measurement's own words about %s are not handed over: %s",
                       stage, broken)
        return f"{mine}. {STAGE_SAID_NOTHING}"
    return f"{mine}. The measurement said: " + "; ".join(said)


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
