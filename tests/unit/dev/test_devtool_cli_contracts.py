# Responsibility: Hold the devtool CLIs to their declared exit codes and help surface.
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
TIMEOUT_S = 60

# The Make help surfaces are operator-facing COMMANDS, so the contract is what they RENDER, not
# what the recipe happens to contain. `make help` once advertised `up` and `down`, which had never
# been targets; reading the Makefile source could not see that, because the strings were there.
ADVERTISED_RE = re.compile(r"^\s{2,}([a-z][a-z0-9-]*)\s{2,}\S")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

#: tool -> the argv that must produce a help surface without touching anything.
HELP_TOOLS = {
    "devtools/release/record.py": ["--help"],
}

#: (tool, argv, expected exit code, a fragment the operator must see)
USAGE_MATRIX = [
    ("devtools/release/record.py", ["--help"], 0, "usage"),
]


def _run(tool: str, argv: list[str], *, cwd: Path, env: dict | None = None):
    import os

    return subprocess.run([sys.executable, str(REPO / tool), *argv], cwd=cwd,
                          capture_output=True, text=True, timeout=TIMEOUT_S,
                          env={**os.environ, **(env or {})})


@pytest.mark.parametrize("tool,argv,code,fragment", USAGE_MATRIX,
                         ids=[f"{Path(t).stem}{argv}" for t, argv, _c, _f in USAGE_MATRIX])
def test_the_cli_exits_with_the_declared_code_and_says_why(tool, argv, code, fragment):
    r = _run(tool, argv, cwd=REPO)
    assert r.returncode == code, (
        f"{tool} {argv} exited {r.returncode}, expected {code}\n{r.stdout}\n{r.stderr}")
    assert fragment.lower() in (r.stdout + r.stderr).lower(), (
        f"{tool} {argv} printed nothing an operator could act on:\n{r.stdout}\n{r.stderr}")


@pytest.mark.parametrize("tool", sorted(HELP_TOOLS))
def test_help_never_opens_a_connection_or_hangs(tool):
    r = _run(tool, HELP_TOOLS[tool], cwd=REPO,
             env={"REDIS_URL": "redis://127.0.0.1:1/0", "DATABASE_URL": ""})
    assert r.returncode == 0, f"{tool} help exited {r.returncode}: {r.stderr[:400]}"
    assert r.stdout.strip(), f"{tool} help printed nothing"


@pytest.mark.parametrize("tool", sorted(HELP_TOOLS))
def test_help_works_from_a_directory_that_is_not_the_repository_root(tool, tmp_path):
    r = _run(tool, HELP_TOOLS[tool], cwd=tmp_path,
             env={"REDIS_URL": "redis://127.0.0.1:1/0"})
    assert r.returncode == 0, f"{tool} depends on the caller's working directory: {r.stderr[:300]}"


@pytest.mark.parametrize("tool", sorted(HELP_TOOLS))
def test_help_writes_no_file_anywhere(tool, tmp_path):
    before = set(tmp_path.rglob("*"))
    _run(tool, HELP_TOOLS[tool], cwd=tmp_path, env={"REDIS_URL": "redis://127.0.0.1:1/0"})
    assert set(tmp_path.rglob("*")) == before, f"{tool} --help wrote into the working directory"


def test_the_matrix_covers_every_tool_that_declares_a_help_surface():
    assert set(HELP_TOOLS) == {
        "devtools/release/record.py",
    }
    covered = {t for t, _a, _c, _f in USAGE_MATRIX}
    assert set(HELP_TOOLS) <= covered, f"a help tool has no usage rows: {set(HELP_TOOLS) - covered}"
    assert len(USAGE_MATRIX) == 1


# make help: every command it advertises must be one make can actually run

# `floor` guards against a scan that silently reads nothing; the synthetic control below declares
# its own, because a two-target Makefile is a legitimate subject there and not a broken read.
def make_targets(cwd: Path, floor: int = 10) -> set[str]:
    # make's own database, not a regex over the source: a target defined by a pattern, an include
    # or a variable is still a target, and a string that merely looks like one is not.
    out = subprocess.run(["make", "-rpn"], cwd=cwd, capture_output=True, text=True).stdout
    found = {m.group(1) for m in re.finditer(r"^([a-zA-Z][a-zA-Z0-9_.-]*):(?!=)", out, re.M)}
    assert len(found) > floor, f"only {len(found)} targets in the Make database for {cwd}"
    return found


def rendered_help(target: str, cwd: Path) -> str:
    r = subprocess.run(["make", target], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"make {target} failed in {cwd}: {r.stderr[:200]}"
    return ANSI_RE.sub("", r.stdout)


def advertised(text: str) -> set[str]:
    return {m.group(1) for line in text.split("\n") if (m := ADVERTISED_RE.match(line))}


def unresolved_advertisements(text: str, targets: set[str]) -> list[str]:
    return sorted(advertised(text) - targets)


@pytest.mark.parametrize("surface", ["help", "help-all"])
def test_every_advertised_help_command_is_a_real_target(surface):
    targets = make_targets(REPO)
    text = rendered_help(surface, REPO)
    names = advertised(text)
    assert names, f"make {surface} advertised nothing - the scan subject is empty"
    assert unresolved_advertisements(text, targets) == [], (
        f"make {surface} advertises commands that do not exist: "
        f"{unresolved_advertisements(text, targets)}")


def test_the_deployment_help_surface_advertises_only_real_targets():
    gcp = REPO / "deploy" / "gcp"
    assert unresolved_advertisements(rendered_help("help", gcp), make_targets(gcp)) == []


def test_a_help_surface_advertising_a_missing_target_is_rejected(tmp_path):
    # The whole pipeline - render, parse, compare - driven against a deliberately defective
    # surface, in a throwaway directory so no mutation reaches the worktree.
    (tmp_path / "Makefile").write_text(
        "real-target:\n\t@true\n\n"
        "help:\n"
        "\t@printf '  %-16s %s\\n' real-target \"exists\" ghost-target \"does not exist\"\n")
    targets = make_targets(tmp_path, floor=1)
    assert "real-target" in targets and "ghost-target" not in targets
    text = rendered_help("help", tmp_path)
    assert advertised(text) == {"real-target", "ghost-target"}
    assert unresolved_advertisements(text, targets) == ["ghost-target"]
