# Responsibility: Read whether the user's words ask for the part to be meshed as a SHELL or a SOLID
# (SHSTAGE-1): the fixed word table first (application/purpose_map.mesh_as_of), and the model only for
# words the table cannot read alone - a paraphrase ("plate elements on the middle of the walls"),
# both kinds at once, a word taken back ("no tets") - its answer checked against the allowed values.
# Boundaries: reads words, stores nothing. The model is asked through the neutral contract, once, with
# a time limit; no answer, or an answer outside {shell, solid, none}, is "nothing said" - never a guess.
# Collaborates with: application/purpose_map.py (the table), application/geometry_check.py (the FEA
# naming step asks it once and stores the reading), application/shell_check.py (the stage reads it).
from __future__ import annotations

import asyncio
import json
import logging
import re

from meshpipeline.application import purpose_map as pm

logger = logging.getLogger(__name__)

#: Words that MAY say how to mesh the part without being in the table: only these go to the model,
#: so a plain "stress in my bracket" never costs a model call.
_HINT = re.compile(r"\b(element|elements|mesh|meshed|plate|plates|sheet|sheets|thin|surface|membrane|"
                   r"dimension\w*|2 ?d|3 ?d|shell\w*|solid\w*|tet\w*|hex\w*|brick\w*|mid\w*|quad\w*|"
                   r"tri\w*|volume\w*|nastran|femap|abaqus|ansys|calculix)\b", re.IGNORECASE)
#: The model's time limit, seconds: the stage never waits on it.
MODEL_TIMEOUT_S = 30.0
#: What the model may answer. "none" is "nothing said".
ALLOWED = (pm.MESH_SHELL, pm.MESH_SOLID, "none")
_SYSTEM = (
    "You read one thing in an engineer's words about a structural (FEA) mesh: do they ask for the part "
    "to be meshed as a SHELL (plate or 2D elements on the mid-surface: CQUAD4, S4, SHELL181) or as a "
    "SOLID (3D elements through the volume: tetrahedra, hexahedra, C3D10, CTETRA)? Answer with a JSON "
    "object and nothing else: {\"mesh_as\": \"shell\"} or {\"mesh_as\": \"solid\"} or {\"mesh_as\": "
    "\"none\"}. Answer \"none\" when the words do not plainly ask for one of the two, or ask for both.")


def needs_model(text: str, table: pm.MeshAsReading) -> bool:
    """Whether the words go to the model: the table left them unread (both kinds, a word taken back)
    or said nothing while a word that might say it is there."""
    if table.value:
        return False
    return table.unsure or bool(_HINT.search(str(text or "")))


def answer_of(content) -> str:
    """The model's reply, checked against the allowed values: "shell", "solid" or "" - any other
    reply (prose, another word, a malformed object) is "nothing said"."""
    raw = str(content or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    value = ""
    if m:
        try:
            got = json.loads(m.group(0))
            value = str(got.get("mesh_as") or "") if isinstance(got, dict) else ""
        except (TypeError, ValueError):
            value = ""
    elif raw.lower().strip(".\"' ") in ALLOWED:
        value = raw
    return pm.mesh_as_answer(value)


async def _ask_model(text: str, *, session_id: str, owner_id: str) -> str:
    from meshpipeline.contracts import model_inference as llm_router

    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"The engineer's words:\n{str(text)[:2000]}"}]
    result = await llm_router.call_intake_model(messages, job_id=session_id, user_id=owner_id, name="MeshAs")
    return answer_of(getattr(result, "assistant_text", ""))


async def read_mesh_as(text: str, *, session_id: str = "", owner_id: str = "",
                       timeout_s: float = MODEL_TIMEOUT_S) -> pm.MeshAsReading:
    """SHSTAGE-1: the table first; the model only for words it cannot read; the model's answer only
    when it is one of the allowed values. Never raises."""
    table = pm.mesh_as_of(text)
    if not needs_model(text, table):
        return table
    try:
        value = await asyncio.wait_for(_ask_model(text, session_id=session_id, owner_id=owner_id),
                                       timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - provider weather: nothing said, the stage recommends
        logger.warning("mesh as: the model could not read the words (%s)", type(exc).__name__)
        return pm.MeshAsReading(why=f"{table.why}; the model did not answer")
    if not value:
        return pm.MeshAsReading(why=f"{table.why}; the model read nothing said")
    return pm.MeshAsReading(value=value, by="model", why=f"{table.why}; the model read {value}")


def read_mesh_as_sync(text: str, *, session_id: str = "", owner_id: str = "") -> pm.MeshAsReading:
    """read_mesh_as from a worker thread (the naming step runs outside any event loop)."""
    table = pm.mesh_as_of(text)
    if not needs_model(text, table):
        return table
    try:
        return asyncio.run(read_mesh_as(text, session_id=session_id, owner_id=owner_id))
    except Exception as exc:  # noqa: BLE001 - never a failed naming over this
        logger.warning("mesh as: not read (%s)", type(exc).__name__)
        return pm.MeshAsReading(why=f"{table.why}; not read")


def as_record(reading: pm.MeshAsReading) -> dict:
    """The reading as the check stores it (facts["mesh_as_words"])."""
    return {"value": reading.value, "by": reading.by, "why": reading.why[:300]}


__all__ = ["ALLOWED", "MODEL_TIMEOUT_S", "answer_of", "as_record", "needs_model", "read_mesh_as",
           "read_mesh_as_sync"]
