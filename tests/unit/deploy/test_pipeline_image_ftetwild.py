# Responsibility: Verify the pipeline (worker) image carries fTetWild where engines/fea_tetwild.binary()
#                 looks for it, because the repair ladder's tier 2b tolerant probe runs on the worker.
# Boundaries: it reads the Dockerfile and fea_tetwild's constants; it builds nothing (the build itself
#             runs `fTetWild --help`, and tests/native exercises the binary).
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
DOCKERFILE = REPO / "Dockerfile"
FEA_TETWILD = REPO / "src" / "meshpipeline" / "engines" / "fea_tetwild.py"

# GMP for fTetWild's exact rationals, libgomp for geogram: the two system libraries it links
# dynamically (docs/reference/third-party.md).
RUNTIME_LIBS = ("libgmp10", "libgomp1")


def _stage(name: str) -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"^FROM ", text, flags=re.M)]
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[s:end]
        if re.match(rf"^FROM \S+ AS {re.escape(name)}\s*$", block.splitlines()[0]):
            return block
    raise AssertionError(f"no Dockerfile stage named {name!r}")


def _code(block: str) -> str:
    return "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))


def _bin_path() -> str:
    m = re.search(r'^BIN_PATH = "([^"]+)"$', FEA_TETWILD.read_text(encoding="utf-8"), flags=re.M)
    assert m, "engines/fea_tetwild.py no longer declares BIN_PATH"
    return m.group(1)


def _assert_carries_ftetwild(stage: str) -> None:
    code = _code(_stage(stage))
    assert "COPY --from=ftetwild-build /opt/ftetwild /opt/ftetwild" in code, (
        f"the {stage} image does not copy the fTetWild build")
    assert _bin_path() == "/opt/ftetwild/bin/fTetWild", (
        "fea_tetwild.BIN_PATH moved; the Dockerfile copies /opt/ftetwild")
    assert "ln -s /opt/ftetwild/bin/fTetWild /usr/local/bin/fTetWild" in code, (
        f"fTetWild is not on PATH in the {stage} image, where fea_tetwild.binary() looks first")
    for lib in RUNTIME_LIBS:
        assert re.search(rf"apt-get install[^\n]*\b{re.escape(lib)}\b", code), (
            f"the {stage} image does not install {lib}, which fTetWild links")
    assert "fTetWild --help" in code, f"the {stage} build does not prove fTetWild runs"


def test_mesh_toolchain_carries_ftetwild():
    _assert_carries_ftetwild("mesh-toolchain")


def test_pipeline_worker_carries_ftetwild_for_the_tolerant_probe():
    """Tier 2b (ladder._tolerant -> tolerant_probe -> fea_tetwild.binary()) runs on the worker; a
    pipeline image without the binary answers 'not installed' and never routes the overlap family."""
    _assert_carries_ftetwild("pipeline")


def test_api_does_not_carry_ftetwild():
    """The API never runs the ladder; neither it nor the base it shares with pipeline grows a mesher."""
    for stage in ("api", "runtime-base"):
        assert "ftetwild" not in _code(_stage(stage)).lower(), f"{stage} should not carry fTetWild"
