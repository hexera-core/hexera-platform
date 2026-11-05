# Responsibility: Say what a tool call is called on a public page in safe mode.
# Boundaries: a closed label table: an unknown tool gets a generic label rather than leaking its real name.
from __future__ import annotations

from typing import Final

GENERIC: Final = "Performed application action"

#: internal tool name -> approved public activity label
SAFE_LABELS: Final[dict[str, str]] = {
    # Intake: settling what the user actually asked for
    "submit_requirements":         "Submitted requirements",
    "propose_engine_selection":    "Proposed a meshing approach",
    "confirm_engine_selection":    "Confirmed the meshing approach",
    "recommend_compatible_engines": "Checked compatible approaches",
    "preview_selected_admission":  "Validated configuration",
    # Builder: authoring and running the mesh
    "geometry_report":             "Inspected the geometry",
    "measure_scales":              "Measured the geometry",
    "configure_mesh":              "Set the meshing strategy",
    "write_file":                  "Generated file",
    "read_file":                   "Read a configuration file",
    "list_directory":              "Checked the workspace",
    "run_python":                  "Computed mesh sizing",
    "run_mesh":                    "Started mesh generation",
    "submit_mesh":                 "Submitted the mesh",
    # Reviewer: looking at what was built
    "set_camera_preset":           "Adjusted inspection view",
    "move_camera":                 "Adjusted inspection view",
    "rotate_camera":               "Adjusted inspection view",
    "zoom":                        "Adjusted inspection view",
    "zoom_to_region":              "Adjusted inspection view",
    "go_to_coordinates":           "Adjusted inspection view",
    "set_navigation_defaults":     "Adjusted inspection view",
    "reset_view":                  "Adjusted inspection view",
    "inspect_region":              "Inspected mesh region",
    "toggle_patch":                "Inspected mesh region",
    "submit_findings":             "Finalized review findings",
    # shared
    "web_search":                  "Checked reference material",
}

#: Deterministic (no model call) operations that still produce public activity.
#: They are application actions, so they are labelled the same way.
DETERMINISTIC_LABELS: Final[dict[str, str]] = {
    "author_configuration":  "Prepared mesh plan",
    "update_configuration":  "Updated configuration",
    "validate_configuration": "Generated mesh configuration",
    "deliver_mesh_result":   "Delivered mesh result",
    "run_native_mesher":     "Started mesh generation",
    "inspect_native_output": "Checked mesh output",
    "validate_mesh_output":  "Validated mesh output",
    "package_artifacts":     "Packaged mesh artifacts",
    "produce_inspection_image": "Produced inspection image",
    "inspect_image":         "Inspected image",
    "read_mesh_quality":     "Checked mesh quality",
}

_ALL: Final[dict[str, str]] = {**SAFE_LABELS, **DETERMINISTIC_LABELS}


def public_label(tool_name: str) -> str:
    if not tool_name:
        return GENERIC
    hit = _ALL.get(str(tool_name).strip())
    if hit:
        return hit
    # A tool the catalog does not know is an operator problem, not a user's.
    try:
        import logging
        logging.getLogger(__name__).warning(
            "public trace: tool %r has no safe label; publishing the generic one",
            str(tool_name)[:80])
    except Exception:
        pass
    return GENERIC


def known_tools() -> frozenset[str]:
    return frozenset(_ALL)
