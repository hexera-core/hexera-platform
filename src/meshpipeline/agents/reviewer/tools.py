# Responsibility: Expose the inspection operations the reviewer may perform.
# Boundaries: look-only: every tool observes the mesh, and none modifies anything.

REVIEWER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_camera_preset",
            "description": "Move camera to a named position: front, rear, top, bottom, left, right, iso",
            "parameters": {
                "type": "object",
                "properties": {
                    "preset": {
                        "type": "string",
                        "enum": ["front", "rear", "top", "bottom", "left", "right", "iso"],
                    }
                },
                "required": ["preset"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_camera",
            "description": (
                "Pan or dolly the camera. "
                "distance_mm is optional - omit to use the auto-scaled default (10% of domain extent). "
                "Use set_navigation_defaults() first if the default step is too coarse or too fine."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["left", "right", "up", "down", "forward", "back"],
                    },
                    "distance_mm": {
                        "type": "number",
                        "description": "Distance in mm. Omit to use the current default pan step.",
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rotate_camera",
            "description": "Rotate camera around yaw, pitch, or roll axis by given degrees.",
            "parameters": {
                "type": "object",
                "properties": {
                    "axis": {"type": "string", "enum": ["yaw", "pitch", "roll"]},
                    "degrees": {"type": "number"},
                },
                "required": ["axis", "degrees"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "zoom",
            "description": (
                "Zoom the camera. factor > 1 zooms in, factor < 1 zooms out. "
                "Omit factor to use the current default zoom step (1.5×). "
                "Use set_navigation_defaults() to change the default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "factor": {
                        "type": "number",
                        "description": "Zoom multiplier. Omit to use default (1.5×).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_navigation_defaults",
            "description": (
                "Override the default pan step and/or zoom factor for this review session. "
                "Call this when the auto-scaled defaults feel too coarse (e.g. jumping past the airfoil) "
                "or too fine (e.g. barely moving when inspecting the farfield). "
                "Returns the new active defaults so you can confirm the change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pan_step_mm": {
                        "type": "number",
                        "description": "New default pan distance in mm. Must be > 0.",
                    },
                    "zoom_step": {
                        "type": "number",
                        "description": "New default zoom factor per zoom() call (e.g. 1.5 = 1.5× in, 0.67 = 1.5× out). Must be > 0.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_to_coordinates",
            "description": (
                "Centre the camera on a world-space point and zoom to inspection scale. "
                "Use the patch centroid coordinates and span from your first message. "
                "Supply the optional preset to set the viewing direction in the same call - "
                "no separate set_camera_preset needed. Pan step is auto-recalibrated to "
                "10% of span after this call. "
                "Supply patch_name to automatically hide all other patches (e.g. domain walls) "
                "so the target surface is not occluded. Call reset_view() to restore all patches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "World X coordinate (mm)"},
                    "y": {"type": "number", "description": "World Y coordinate (mm)"},
                    "z": {"type": "number", "description": "World Z coordinate (mm)"},
                    "span": {"type": "number", "description": "Approximate size (mm) of the region to show - use the patch span from your first message"},
                    "preset": {
                        "type": "string",
                        "enum": ["front", "rear", "top", "bottom", "left", "right", "iso"],
                        "description": "Optional camera direction preset applied before centering. Use the hint from PATCH COORDINATES.",
                    },
                    "patch_name": {
                        "type": "string",
                        "description": "If provided, all other patches are hidden before navigation so this surface is unobstructed. Use when domain walls surround the target (e.g. patch_name='wall'). Call reset_view() afterwards.",
                    },
                },
                "required": ["x", "y", "z", "span"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "zoom_to_region",
            "description": (
                "Magnify a specific region of the current view to inspect individual mesh cells. "
                "The camera does not move - this is a render-resolution crop, not a camera operation. "
                "Specify where on screen to crop using normalised coordinates: "
                "screen_x=0.0 is left edge, 1.0 is right edge; screen_y=0.0 is top, 1.0 is bottom. "
                "The image is re-rendered at magnification× resolution and a 1280×960 crop is returned "
                "centred on that screen point - crisp cells, correct aspect ratio, no distortion. "
                "Use magnification to control detail level (4 = moderate, 8 = close-up cell inspection)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "screen_x": {
                        "type": "number",
                        "description": "Normalised horizontal position to crop (0.0=left, 0.5=centre, 1.0=right). Default 0.5.",
                    },
                    "screen_y": {
                        "type": "number",
                        "description": "Normalised vertical position to crop (0.0=top, 0.5=centre, 1.0=bottom). Default 0.5.",
                    },
                    "magnification": {
                        "type": "number",
                        "description": "Render resolution multiplier - renders at this multiple of base resolution then crops. Default 4.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_region",
            "description": (
                "Slice the VOLUME mesh at a named inspection region and return the cut as an image, so "
                "you can see INTERIOR cells - near-wall refinement / boundary layers and internal sizing "
                "- which the surface views cannot show. The available region names are listed in the "
                "INTERNAL SLICES section of your first message. Use this to confirm internal structure is "
                "actually present; cross-check exact counts/sizes against the mesh script."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region_name": {
                        "type": "string",
                        "description": "An inspection region name from the INTERNAL SLICES list (e.g. 'slice_y_centre', 'nearwall_z').",
                    },
                },
                "required": ["region_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_patch",
            "description": "Show or hide a named mesh patch to inspect underlying geometry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch_name": {
                        "type": "string",
                        "description": "Patch name from manifest: wall, inlet, outlet, farfield, symmetry, fluid",
                    },
                    "visible": {"type": "boolean"},
                },
                "required": ["patch_name", "visible"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_view",
            "description": "Return to default isometric view with all patches visible.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# The reviewer's tools are how it INSPECTS the mesh. Said to the person watching, so
# they can see the review actually happening instead of a spinner. This lives beside
# the declarations so a new tool cannot ship without a word for it (test-enforced).
ACTIONS: dict[str, str] = {
    "set_camera_preset":       "Framing the mesh",
    "move_camera":             "Moving the camera",
    "rotate_camera":           "Turning the mesh",
    "zoom":                    "Zooming in",
    "set_navigation_defaults": "Setting up the view",
    "go_to_coordinates":       "Moving to a location",
    "zoom_to_region":          "Zooming to a region",
    "inspect_region":          "Inspecting a region close up",
    "toggle_patch":            "Isolating a boundary",
    "reset_view":              "Resetting the view",
}


def action_for(tool_name: str) -> str:
    return ACTIONS.get(tool_name, "Inspecting")
