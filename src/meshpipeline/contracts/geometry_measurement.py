# Responsibility: Declare what a stored geometry measurement is, and refuse to read one that does not describe the bytes in hand.
# Owns: the document's status vocabulary, the sha guard and the projection accessor.
# Boundaries: shape and reading; producing a document is application/geometry_measurement.py.
# Collaborates with: persistence/repositories/geometry_measurement_repository.py and the engines' surface_analysis.
from __future__ import annotations

import logging
import re

from meshpipeline.errors import FailureClass

logger = logging.getLogger(__name__)

#: The document's own name. It is written into every stored document and checked on the way out, so
#: a row written by a different artefact cannot be read as this one.
MEASUREMENT_SCHEMA = "geometry_agent.measurement.v1"

#: THE THREE ANSWERS, and they are three. `ok` carries measurements. `refused` means the package
#: declined before opening the file - too large, or switched off - which is neither a broken file
#: nor a broken measurement. `measurement_failed` means it opened the file and could not finish.
#: A row always carries one of them. No row at all is a FOURTH answer, "never attempted", and it is
#: the absence of the row that says so - never a status string, and never an empty dict.
STATUS_OK = "ok"
STATUS_REFUSED = "refused"
STATUS_MEASUREMENT_FAILED = "measurement_failed"
STATUSES = (STATUS_OK, STATUS_REFUSED, STATUS_MEASUREMENT_FAILED)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# A CORRECTED FILE, MID-CONVERSATION. Settled here because this is the module a reader goes through.
#
# TODAY IT IS NOT EXPRESSIBLE, and that is a fact about the upload route rather than a choice made
# here: `api/v1/upload.py` calls `create_session` on every upload, and nothing anywhere rebinds
# `ChatSession.geometry_source_id`. So a customer who uploads a corrected file gets a NEW session
# with its own source and its own measurement; the old session keeps the file it was about. There
# is nothing to invalidate, and no measurement can be stale for the session that points at it.
#
# WHAT THIS MODULE GUARANTEES REGARDLESS is the read: a document is handed back only when its
# digest is the digest of the bytes the caller asked about. That guard is not decoration for a case
# that cannot happen - it is what makes rebinding safe on the day it becomes expressible, and it is
# the only thing standing between a customer and a port table measured off the file they replaced.
#
# WHEN REBINDING DOES BECOME EXPRESSIBLE, the shape is already fixed by what is immutable around it
# (`persistence/models.py`: an interpretation is "bound at APPROVAL, and immutable thereafter ... a
# correction is a new interpretation and a new job, never an edit of this"):
#   - before approval: rebind the session, and discard every pre-filled value that came from a
#     measurement while keeping every value the customer typed;
#   - after approval and before dispatch: invalidate the approval first, then as above;
#   - after dispatch: a new session, because the running job means what it meant.
# Nothing is mutated in place and no measurement row is deleted: the old rows stay as lineage, and
# each one still describes the bytes it was computed from.


class MeasurementMismatch(RuntimeError):
    """A stored measurement that does not describe the bytes it was asked about.

    This is `FailureClass.DATA_INTEGRITY` in the same sense the materializer already uses for an
    approved snapshot that no longer matches its row: we reached the data and what came back was
    wrong, it is blameless to the user, and repeating the read cannot produce a different answer.
    """

    failure_class = FailureClass.DATA_INTEGRITY


def document_for(row, *, sha256: str) -> dict | None:
    """The stored document, or None when there is nothing to read.

    None is "not attempted", and the caller must render that as the measured phase not having run
    rather than as a part with nothing unusual about it. A row that IS present always carries a
    `status`, so a reader never infers a verdict from an absent key.

    RAISES when the row describes different bytes. A measurement is a pure function of the file,
    so a document whose digest is not the digest in hand is a description of some other file and
    there is no honest way to use it: the alternative is a port table from a file the customer
    replaced, which is worse than no table.
    """
    if row is None:
        return None
    stored = str(getattr(row, "sha256", "") or "")
    if not _HEX64.match(stored) or stored != str(sha256 or "").lower():
        raise MeasurementMismatch(
            "the stored geometry measurement describes different bytes than the geometry in hand")
    document = getattr(row, "document", None)
    if not isinstance(document, dict) or not document:
        raise MeasurementMismatch("the stored geometry measurement carries no document")
    if document.get("schema") != MEASUREMENT_SCHEMA:
        raise MeasurementMismatch(
            f"the stored geometry measurement is {document.get('schema')!r}, not {MEASUREMENT_SCHEMA!r}")
    return document


def status_of(document: dict | None) -> str | None:
    """The document's own status, or None when there is no document."""
    if not isinstance(document, dict):
        return None
    status = str(document.get("status") or "")
    return status if status in STATUSES else STATUS_MEASUREMENT_FAILED


#: THE QUEUE SEAM, so nothing above this layer imports a broker. Bound in runtime/composition.py
#: exactly as the training export's is. With nothing bound - a process that composed no adapters, a
#: deployment with no worker - the enqueue is a logged no-op and the upload is unaffected, which is
#: the same answer as the measurement never having been attempted.
_enqueuer = None


def set_measurement_enqueuer(enqueuer) -> None:
    global _enqueuer
    _enqueuer = enqueuer


def enqueue_measurement(source_id: str, owner_id: str, *, purpose: str) -> bool:
    """Hand one measurement to a worker. True when something took it.

    Never raises. A broker that will not accept the task is a measurement that does not happen, and
    a measurement that does not happen is a conversation that runs exactly as it does today.
    """
    if _enqueuer is None:
        logger.info("geometry measurement: no enqueuer configured - source_id=%s", source_id)
        return False
    try:
        _enqueuer(source_id, owner_id, purpose=purpose)
        return True
    except Exception as exc:                       # noqa: BLE001 - fail open, never the upload
        logger.warning("geometry measurement: could not enqueue - source_id=%s: %s", source_id, exc)
        return False


def projection_of(document: dict | None) -> dict | None:
    """What an engine's admission reads off `surface_analysis`, or None when nothing was measured.

    `engines/base.py` reads five keys: `region_count`, `region_names`, `self_intersecting`, `diag`
    and `thin_gap`. The dict this returns always carries a `status`, because `base.py:430-434` only
    tests that `surface_analysis is not None` - so an empty dict passes the gate carrying nothing,
    and three different situations (never measured, measurement failed, measured and unremarkable)
    used to arrive as the same value.

    None means the measured phase did not run. A dict means it did, and `status` says how it went.
    """
    if not isinstance(document, dict):
        return None
    status = status_of(document)
    if status != STATUS_OK:
        return {"status": status, "reason": str(document.get("reason") or "")}
    projection = document.get("projection")
    if not isinstance(projection, dict):
        return {"status": STATUS_MEASUREMENT_FAILED,
                "reason": "the stored measurement carries no projection"}
    return {"status": STATUS_OK, **projection}
