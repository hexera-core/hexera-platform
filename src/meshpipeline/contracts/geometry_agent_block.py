# Responsibility: Hand the mesh planner the measurement package's own typed block, or hand it nothing.
# Owns: the decision that there is a block to send, and the platform half of what it says.
# Boundaries: it reads and forwards; it computes no geometry number and recomputes none.
# Collaborates with: cad/regions.py for the stored document and engines/snappy/planner.py, which validates what arrives.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# WHY THIS MODULE DOES NO ARITHMETIC.
#
# Every number in the block is the measurement package's: `agent_forecast_cells` is
# `hexera.forecast(...)["cells_high"]`, `inlet_bore_m` is the bore the builder's own emulator sizes
# from, and the metres conversion is `value_file * coordinates.scale_to_metres` applied once, there.
# Spelling any of it a second time on this side of the boundary would be a second thing to keep in
# step across two independently pinned distributions, and the first divergence would be silent and
# in a customer's mesh.
#
# So the package assembles the block, stores it in the measurement document, and this reads it.
# What the platform adds is the failure contract - absence, staleness and a customer cap the package
# has no way to know - and nothing else.


def block_for_document(document: dict | None) -> dict | None:
    """The `metrics["geometry_agent"]` block for a stored measurement. None when there is none.

    None is the fail-open, and it is the answer to every one of: the setting is off, no measurement
    was attempted, the measurement failed, the row describes different bytes, the document predates
    the block, or the package that writes it is not installed in this image. In each case the mesh
    planner adds no key and composes exactly the dict it composes today.
    """
    if not isinstance(document, dict) or not document:
        return None
    stored = document.get("planner_block")
    if isinstance(stored, dict) and stored:
        # Written at measurement time by the package that owns the numbers. Preferred over anything
        # computed here, always.
        return stored
    return _from_package(document)


def _from_package(document: dict) -> dict | None:
    """Assemble the block now, for a document stored before the block was part of the schema.

    Still not arithmetic: this calls the package's own `planner_block`, which is the single
    definition. It is a separate path only because a row written last week has no `planner_block`
    key and re-measuring a file to add one would cost seconds for a value already derivable from
    what the row holds.

    An image without the distribution returns None here, which is the same answer as no measurement.
    """
    try:
        from geometry_agent.agent.hexera import planner_block
    except Exception as exc:                       # noqa: BLE001 - absence is an outcome
        logger.info("geometry agent block: the measurement package is not installed here (%s)", exc)
        return None
    try:
        return planner_block(document)
    except Exception as exc:                       # noqa: BLE001 - never worth a plan
        logger.warning("geometry agent block: could not be composed (%s)", exc)
        return None


def confirmed_cell_cap(block: dict | None) -> int | None:
    """The cell budget the CUSTOMER CONFIRMED, read off the survey the block carries. None otherwise.

    Read, not computed: `contract.deliver.survey_block` puts every answer the customer gave under
    `survey.confirmed`, each a mark carrying its own kind. Only a `confirmed` mark with a positive
    whole number counts. A budget the customer only wrote is `stated` and is not this; a default that
    stood never reaches `confirmed` at all, because the package refuses an assumed budget.
    """
    survey = (block or {}).get("survey") if isinstance(block, dict) else None
    if not isinstance(survey, dict):
        return None
    mark = (survey.get("confirmed") or {}).get("cell_budget")
    if not isinstance(mark, dict) or mark.get("kind") != "confirmed":
        return None
    value = mark.get("value")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


# WHERE THE STATEFUL HALF LIVES, and why it is not here.
#
# Reading the row for a run's geometry needs `cad/regions.py`, and `contracts/` may import nothing
# above itself - `tests/unit/hygiene/test_architecture_boundaries.py::test_contracts_are_neutral`
# forbids `cad` by name along with every other product package. So the read is
# `cad.regions.agent_block_for_state`, which already owns the row read, and this module stays what
# `contracts/` is for: a shape and a pure function over it.

__all__ = ["block_for_document", "confirmed_cell_cap"]
