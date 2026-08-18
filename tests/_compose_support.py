# Responsibility: Render the TRACKED compose file hermetically, so every tier asks the same question.
# Boundaries: rendering only; it starts nothing and never reads the developer's own .env.
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def product_version() -> str:
    init = (REPO / "src/meshpipeline/__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.M).group(1)


@dataclass(frozen=True)
class Rendered:
    returncode: int
    stdout: str
    stderr: str


# Run `docker compose config` on the tracked file in a throwaway project directory.
#
# The tracked file declares `env_file: .env`, so rendering it inside the checkout requires a
# configured developer machine: on one it answers the question, on another it fails for a reason
# that has nothing to do with what was being asked. Three suites hid that behind a skip that
# reported "docker compose is unavailable" even where Docker was working perfectly.
#
# Here the file is rendered against the tracked `.env.example` in a temporary directory:
# placeholders only, no developer value, no credential, removed with the directory. Pass
# `with_env_file=False` to render WITHOUT that file, which is how a caller asks what Compose does
# when a required value has no source at all.
def render(*, project: str | None = None, env: dict[str, str] | None = None,
           with_env_file: bool = True) -> Rendered:
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        shutil.copy(REPO / "docker-compose.yml", project_dir / "docker-compose.yml")
        if with_env_file:
            shutil.copy(REPO / ".env.example", project_dir / ".env")

        argv = ["docker", "compose", "--project-directory", str(project_dir)]
        if project:
            argv += ["-p", project]
        argv.append("config")

        # A deliberately minimal environment: the renderer must not inherit whatever the developer
        # happens to export, or the answer changes per machine again.
        base = {"PATH": os.environ.get("PATH", ""), "HOME": str(project_dir)}
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300,
                              env={**base, **(env or {})})
        return Rendered(done.returncode, done.stdout, done.stderr)
