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
                        # the parameter keeps its old name; the mesh is in metres, and so is this
                        "description": "Distance in the mesh's unit (metres - the delivered mesh is always "
                                       "in metres, whatever the key's name says). Omit to use the current "
                                       "default pan step.",
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
                        "description": "New default pan distance in the mesh's unit (metres, whatever the "
                                       "key's name says). Must be > 0.",
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
                    "x": {"type": "number", "description": "World X coordinate, in metres (the mesh's unit)"},
                    "y": {"type": "number", "description": "World Y coordinate, in metres (the mesh's unit)"},
                    "z": {"type": "number", "description": "World Z coordinate, in metres (the mesh's unit)"},
                    "span": {"type": "number", "description": "Approximate size of the region to show, in metres (the mesh's unit) - use the patch span from your first message"},
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
                "See the inside of the mesh at a named inspection region, as an image, so you can judge "
                "INTERIOR cells - near-wall refinement / boundary layers and internal sizing - which the "
                "surface views cannot show. The available region names are listed in your first message. "
                "TWO KINDS OF MESH ANSWER THIS, and your first message says which one this is. Under "
                "INTERNAL SLICES the volume is here and this slices it live. Under INTERNAL CUTS the "
                "interior was too large to bring here, so the cut was taken where the mesh was built and "
                "you are handed ONE PICTURE OF ONE PLANE at a fixed window: the camera does not move in "
                "it and anything outside its window is not in it. Every frame is returned with the window "
                "it covers and how many pixels across a cell is in it - judge only what that frame can "
                "actually resolve, and say which frame a finding came from."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region_name": {
                        "type": "string",
                        "description": "An inspection region name from your first message (e.g. 'across_the_flow', 'nearwall_z').",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["whole", "zoom"],
                        "description": (
                            "Which frame of a PREPARED cut to be shown (INTERNAL CUTS only). Omit it and "
                            "you get 'whole', the ENTIRE cut plane - that is the region's own question, "
                            "and a channel is under a pixel wide in it, so it cannot be counted in and "
                            "does NOT discharge the region. 'zoom' is a few millimetres of that same "
                            "plane, drawn fine enough for cells, and is the only frame an inspection can "
                            "be judged on - ask for it by name. Each frame is returned with what was "
                            "MEASURED in the picture, including how many cells were actually resolved in "
                            "it; a frame measured to contain no cells cannot support a cell count no "
                            "matter how fine its stated scale is. ALL OF THAT IS ABOUT A PREPARED "
                            "CUT. A live-sliced mesh has no frames at all: there the scope is ignored, "
                            "the one plane you are handed IS the inspection, and the reply says so."
                        ),
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
            "description": (
                "Isolate a named boundary patch - hide the ones around it, or show it alone - and "
                "return the view as an image. THIS IS THE ONLY TOOL THAT PRODUCES EVIDENCE OF A "
                "PATCH. Judging how the mesh sits on the wall requires it: a slice through the "
                "interior shows cells, not the boundary surface, and findings about the wall that "
                "cite only slices are refused as ungrounded no matter how many you take. Hide the "
                "far-field to see the body, then isolate the wall patch itself. The patch names "
                "for THIS mesh are listed in your first message - use those, not the ones in the "
                "parameter example."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patch_name": {
                        "type": "string",
                        "description": "A patch name from THIS mesh, as listed in your first message. The names are per-job - a wing case names its wall 'airfoil', not 'wall'.",
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
