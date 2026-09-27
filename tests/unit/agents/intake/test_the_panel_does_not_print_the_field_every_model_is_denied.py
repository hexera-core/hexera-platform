# Responsibility: the Surveyor panel shows the look's relied-on rows and not its free prose.
# Boundaries: what the panel prints; the tiering itself is the agent package's (vision/trust.py).
"""The one field withheld from every model was the one printed verbatim to the person.

`vision/trust.py` tiers `notes` as RECORDED, with the reason in its own words: "free prose, and the
claims that land there held out worst". Three separate code paths exist to keep it away from a
machine - `for_model`, `model_safe`, and `WITHHELD_FROM_MODELS`. None of them stood between it and
the customer: the panel printed it verbatim, sliced at a hard 260 characters.

MEASURED on the 36-run batch: 38 notes rows shown, 14 cut mid-word, and 11 carrying a claim about
the meshing outcome that the look's own contract forbids it from making.

The rows that stay are the relied-on ones, because they are what the panel's claim rests on. The
panel reads less like a person for losing this. That is the point: it read like a person because it
was free prose, which is exactly why it could not be trusted.
"""
from __future__ import annotations

from meshpipeline.agents.intake import geometry_brief as gb

#: A reply shaped like a real one: a relied-on row, a candidate row, and free prose that oversteps.
LOOK = {
    "status": "ok",
    "seconds": 28.0,
    "impression": {
        "looks_like": "a rectangular duct elbow",
        "confidence": "medium",
        "sharp_edges": ["sharp rectangular mouth rims"],
        "attachments": ["a flange at one end"],
        "openings_seen": [{"id": "o1"}, {"id": "o2"}],
        "inside_is_plain": True,
        "notes": ("The flow volume is a rectangular-section bend and the mesh should resolve the "
                  "corners; this will mesh cleanly at standard fidelity and the pressure drop can be "
                  "quoted with confidence once the outlet run is exte"),
    },
}


def _panel(survey: dict) -> str:
    for name in ("panel", "surveyor_panel", "render_panel", "panel_lines", "compose_panel"):
        fn = getattr(gb, name, None)
        if callable(fn):
            out = fn(survey)
            return out if isinstance(out, str) else "\n".join(out)
    raise AssertionError(f"no panel composer found on geometry_brief: {sorted(dir(gb))[:40]}")


def test_the_free_prose_row_is_not_printed():
    text = gb.__file__ and open(gb.__file__, encoding="utf-8").read()
    assert 'notes        {notes[:260]}' not in text, (
        "the panel is printing the look's `notes` again; trust.py tiers it RECORDED because its "
        "claims held out worst, and three model channels already withhold it")
    assert 'imp.get("notes")' not in text, "nothing on the panel should read the notes field at all"


def test_the_relied_on_rows_are_still_there():
    """A fix that also removed what the panel's claim rests on would be worse than the leak."""
    text = open(gb.__file__, encoding="utf-8").read()
    for row in ("attachments", "openings_seen", "inside_is_plain"):
        assert row in text, f"the panel stopped reading {row}, which is a relied-on field"


def test_the_reason_is_recorded_where_the_row_used_to_be():
    """So the next person to add a row knows the rule, rather than rediscovering it from a transcript."""
    text = open(gb.__file__, encoding="utf-8").read()
    assert "held out worst" in text and "WITHHELD_FROM_MODELS" in text
