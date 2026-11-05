# Responsibility: Declare the geometry check's switches - whether an upload is scouted at all,
# and how long the picture-naming step may take.
# Boundaries: settings only; read by the upload endpoint (enqueue), the geometry API (serve) and
# the worker task (run). Nothing here decides what the check says.
from __future__ import annotations

from meshpipeline.settings.env import bool_env, optional_env

#: OFF unless a deployment turns it on. When on, every CAD upload is scouted on the worker right
#: away and the console shows the labelled picture to confirm before the intake asks for it.
GEOMETRY_CHECK_ENABLED: bool = bool_env("GEOMETRY_CHECK_ENABLED", "false")

#: Seconds the vision naming step may spend before the check ships with the code's own names.
#: The pictures are few and the answer is short; a minute is generous.
GEOMETRY_CHECK_VISION_TIMEOUT_S: int = int(optional_env("GEOMETRY_CHECK_VISION_TIMEOUT_S", "60"))
