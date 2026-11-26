# Responsibility: Run ONE CRS v5 repair as its own process - the product's way of running v5 from inside a Celery
#                 prefork child, which may not start the kernel pool itself (a daemonic process cannot have
#                 children, and spawn will not pickle the billiard child's authkey).
# Owns: the child's command line and its result file.
# Boundaries: composition (`install_adapters`, so the model router, the stores and the gs:// port are the worker's
#             own), then `repair_v5.engine.repair_in_process`. Started by `repair_v5.engine.repair_file`; never by a
#             person.
"""python -m meshpipeline.runtime.crs_v5_child SOURCE DEST RESULT_JSON SECONDS_LEFT CHOICE [AUTO_ACCEPT_MM]"""

from __future__ import annotations

import json
import sys
import time


def main(argv: list[str]) -> int:
    source, dest, result_path, seconds_left, choice = argv[:5]
    auto_accept = float(argv[5]) if len(argv) > 5 and argv[5] not in ("", "none") else None
    deadline = time.monotonic() + float(seconds_left)
    out: dict
    code = 0
    try:
        from meshpipeline.runtime.composition import install_adapters

        install_adapters()
        from meshpipeline.cad.repair_v5.engine import repair_in_process

        run = repair_in_process(source, dest, deadline_s=deadline, choice=choice, auto_accept_mm=auto_accept)
        out = {"delivered": run.delivered, "result": run.result.to_dict(), "trace": run.trace,
               "progress": run.progress}
    except BaseException as exc:  # noqa: BLE001 - the parent reads the failure from the file and runs v3
        out = {"error": f"{type(exc).__name__}: {exc}"[:2000],
               "unavailable": type(exc).__name__ == "V5Unavailable"}
        code = 3
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, default=str)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
