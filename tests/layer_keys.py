# Responsibility: Say which layer figures a reader can actually be shown, read off the producers rather than typed out.
# Boundaries: it names keys; it asserts nothing about labels, groups or values - the tests that import it do that.
"""WHY THIS IS NOT A LIST INSIDE A TEST.

Three guards held twelve literal key names between them - tests/unit/render's viewer-label check and
two browser checks in tests/ui. Commit 0248dcfb moved this engine's first layer floor off coverage
and onto `layer_kept_of_offered_pct`, published that figure and `layer_offered_pct` into the viewer
payload, and left all three lists at twelve. All three passed. The customer's "Delivered mesh" panel
drew both new figures in the catch-all "Other" group, labelled with their own key names spelled out:
"layer offered pct 97.23", "layer kept of offered pct 91.11" - measured in a real browser, on the
real plate's own layer log.

A guard against drift that has to be edited in step with the thing it guards is not a guard. So the
list is derived from the two authorities instead, and the next figure anyone publishes is covered
the moment it is published.
"""
from __future__ import annotations

#: THE FIVE THAT CANNOT BE DERIVED, because they do not come from `_LAYER_FIGURES`:
#: `finalize._near_wall` republishes the asked and measured layer counts under their `near_wall_`
#: names, and `finalize._layers_published` adds `layer_short` and its two tally keys beside the
#: figures rather than drawing them from that tuple. They are named here so the union below is a
#: superset of the twelve literals it replaces - the derivation is what catches the NEXT key, not a
#: licence to stop guarding what was already guarded.
BESIDE: tuple[str, ...] = ("near_wall_layers_asked", "near_wall_layers_measured",
                           "layer_short", "layer_floors_unjudged", "layer_floors_total")


def published_layer_keys() -> tuple[str, ...]:
    """Every layer figure a reader can be shown, from the producers plus `BESIDE`.

    TWO AUTHORITIES, INTERSECTED, because a figure has to clear both to reach a reader:
    `cfmesh_conjugate/finalize._LAYER_FIGURES` is what travels into the manifest, and
    `render/mesh_facts`' own metric lists are what survives into the viewer payload. Three of the
    carried names are deliberately NOT shown - `layer_seconds`, and `layers_asked`/`layers_in_mesh`,
    which are republished under their `near_wall_` names - so demanding them of the panel would be
    demanding a label for a key it never receives.
    """
    from meshpipeline.engines.cfmesh_conjugate import finalize as F
    from meshpipeline.render import mesh_facts as MF

    carried = set(F._LAYER_FIGURES)
    shown = set(MF._FLOAT_METRICS) | set(MF._INT_METRICS) | set(MF._TEXT_METRICS)
    derived = carried & shown
    assert derived, "neither authority named a single layer figure, so this guard measures nothing"
    return tuple(sorted(derived | set(BESIDE)))


__all__ = ["BESIDE", "published_layer_keys"]
