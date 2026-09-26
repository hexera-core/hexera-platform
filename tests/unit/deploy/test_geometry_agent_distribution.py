# Responsibility: Prove the built image installs the geometry agent, and that what it installs is not stale.
# Boundaries: reads the repository's own build inputs; it builds no image and imports no agent module, so it
#             runs and REPORTS in every environment - including the one where the package is absent, which is
#             the environment the defect it guards hid in.
#
# THE DEFECT THIS EXISTS FOR. Nothing installed the geometry-agent distribution into any image: not
# requirements/runtime.txt, not requirements/dev.txt, not pyproject.toml (whose packages.find reads
# where=["src"], and the platform src holds only meshpipeline), not a Dockerfile line, not a deploy script.
# docker-compose.yml said so outright. Every entry point wraps its import in try/except and treats absence
# as an outcome to record, so nothing raised: no document, no survey, no plan, and a planner message byte
# for byte identical to the pre-Surveyor one.
#
# AND IT WAS INVISIBLE TO EVERY CHECK, because the tests that covered the Surveyor were
# pytest.importorskip("geometry_agent...") and skipped themselves in exactly the image where the package
# was missing. So every assertion below is made from FILES, never from an import: this test cannot be
# silenced by the absence of the thing it is about.
from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
VENDOR = REPO / "vendor" / "wheels"
PROVENANCE = VENDOR / "PROVENANCE.json"
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")


def _wheels() -> list[Path]:
    return sorted(VENDOR.glob("hexera_geometry_agent-*.whl"))


def _the_wheel() -> Path:
    found = _wheels()
    assert len(found) == 1, (
        f"expected exactly one vendored geometry-agent wheel in {VENDOR.relative_to(REPO)}, found "
        f"{[p.name for p in found]}. The Dockerfile's wheel stage asserts the same thing and would fail "
        f"the build; build one with `bash deploy/vendor_geometry_agent.sh /path/to/geometry_agent`.")
    return found[0]


def _imported_subpackages() -> set[str]:
    """Every `geometry_agent.<subpackage>` the application imports, read out of src/ rather than listed.

    A hand-kept list is the same blind spot again: it would say the wheel is complete on the day a new
    import is added. This derives the answer from the imports themselves.
    """
    found: set[str] = set()
    # IMPORT STATEMENTS ONLY, not every mention of the name: two schema string constants read
    # "geometry_agent.measurement.v1" and "geometry_agent.planner_block.v1", and neither is a module.
    # `from geometry_agent import hexera`-style imports are covered by the second pattern.
    dotted = re.compile(r"^\s*(?:from|import)\s+geometry_agent\.([A-Za-z_][A-Za-z0-9_]*)", re.M)
    bare = re.compile(r"^\s*from\s+geometry_agent\s+import\s+([A-Za-z_][A-Za-z0-9_, ]*)", re.M)
    dynamic = re.compile(r"import_module\(\s*[\"']geometry_agent\.([A-Za-z_][A-Za-z0-9_]*)")
    for path in (REPO / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found |= set(dotted.findall(text))
        found |= set(dynamic.findall(text))
        for group in bare.findall(text):
            found |= {name.strip() for name in group.split(",") if name.strip()}
    assert found, "no geometry_agent import found in src/ at all - this test is checking nothing"
    return found


# ---------------------------------------------------------------- the image installs it

def test_the_wheel_stage_collects_the_vendored_geometry_wheel_into_dist():
    # /dist is what both runtime blocks install from, so collecting it here is what puts the package in
    # the api, pipeline AND mesh images without a fourth place to keep in step.
    assert "COPY vendor/wheels/ /vendor/wheels/" in DOCKERFILE, (
        "the Dockerfile's `wheel` stage does not copy vendor/wheels/ into the build")
    assert "cp /vendor/wheels/hexera_geometry_agent-*.whl /dist/" in DOCKERFILE, (
        "the vendored geometry wheel is copied nowhere - /dist is what the runtime stages install from")


def test_the_build_refuses_to_proceed_without_exactly_one_geometry_wheel():
    # The whole failure mode was an image that quietly lacked the package. A build that finds no wheel,
    # or two, must stop rather than produce one.
    assert 'if [ "$count" != "1" ]' in DOCKERFILE and "exit 1" in DOCKERFILE, (
        "the Dockerfile does not assert exactly one vendored geometry wheel before copying it")


def test_every_runtime_target_installs_everything_in_dist():
    # `pip install --no-deps /tmp/*.whl` is the line that installs BOTH distributions. There are two of
    # these blocks - runtime-base (api, pipeline) and mesh - and both must be glob installs, because a
    # block that named meshpipeline's wheel explicitly would silently skip the geometry one.
    installs = DOCKERFILE.count("pip install --no-cache-dir --no-deps /tmp/*.whl")
    assert installs == 2, (
        f"expected the two runtime blocks (runtime-base and mesh) to install every wheel in /tmp, found "
        f"{installs} such lines - a stage that does not is a target with no Surveyor")


def test_the_build_verifies_the_geometry_agent_resolves_before_the_image_is_finished():
    verify = (REPO / "deploy" / "verify_install.py").read_text(encoding="utf-8")
    assert "import geometry_agent" in verify, (
        "deploy/verify_install.py does not import the geometry agent, so a stage that failed to install "
        "it still builds - which is exactly how the defect survived")
    assert "GEOMETRY_SUBPACKAGES" in verify and "GEOMETRY_DATA" in verify, (
        "deploy/verify_install.py checks that the agent imports but not that it is COMPLETE; a stale "
        "wheel imports fine and fails at a customer's upload")
    assert DOCKERFILE.count("python /tmp/verify_install.py") == 2, (
        "verify_install.py does not run in both runtime blocks")


def test_the_environment_the_wheel_installs_into_carries_what_the_agent_imports():
    # The wheel is installed with --no-deps, the same as meshpipeline's, so requirements/runtime.txt is
    # what has to carry its imports. trimesh was the only one missing.
    runtime = (REPO / "requirements" / "runtime.txt").read_text(encoding="utf-8")
    assert re.search(r"^trimesh==", runtime, re.M), (
        "requirements/runtime.txt does not pin trimesh, which geometry_agent.facts imports at module "
        "level - the wheel installs but the first measurement raises ImportError")


#: What trimesh reaches for ON THE AGENT'S BEHALF, with the path and what its absence costs. Neither is a
#: Requires-Dist of the agent's wheel and the agent never writes either import, so reading the agent's own
#: declarations - which is what the two requirements comments used to do - says the image is complete when it
#: is not. MEASURED in the built `api` image on the real parts in tests/fixtures/geometry, through the
#: platform's own `measure_local_file`: with both uninstalled, one region and two regions both come back
#: `measurement_failed` with `No module named 'rtree'` in the reason. Either one alone is enough.
TRIMESH_REACHES_FOR = {
    "rtree": "the r-tree trimesh builds for its fallback ray intersector "
             "(ray.intersects_any -> triangles_tree -> util.bounds_tree), which is the path EVERY part "
             "takes when embree is not loaded: `facts.features.detect_features` casts rays through "
             "`facts.chords` on all of them and `facts.regions.detect_regions` casts more on a part with "
             "two or more regions",
    "embreex": "the embree intersector trimesh prefers, which is what the agent's own environment was "
               "measured on: the same nesting test costs 0.41 s on embree against 20.05 s on the rtree "
               "fallback at 209,408 faces, and the gap grows with face count",
}


@pytest.mark.parametrize("pin", sorted(TRIMESH_REACHES_FOR))
def test_the_environment_carries_what_trimesh_reaches_for_on_the_agents_behalf(pin):
    runtime = (REPO / "requirements" / "runtime.txt").read_text(encoding="utf-8")
    constraints = (REPO / "requirements" / "constraints.txt").read_text(encoding="utf-8")
    assert re.search(rf"^{pin}==", runtime, re.M), (
        f"requirements/runtime.txt does not pin {pin}: {TRIMESH_REACHES_FOR[pin]}")
    assert re.search(rf"^{pin}==", constraints, re.M), (
        f"requirements/constraints.txt does not pin {pin}, so a rebuild resolves it freely while every "
        f"other distribution in the image is locked")


@pytest.mark.parametrize("pin", sorted(TRIMESH_REACHES_FOR))
def test_the_wheel_declares_neither_of_them_which_is_why_reading_it_was_not_enough(pin):
    """The premise of the two pins above, asserted rather than asserted-in-prose.

    Both requirements files used to say these were "declared by the agent". They are not, and that is the
    whole reason a dependency check built on the agent's declarations could not see the gap. The day the agent
    declares one, this test fails and those comments have to be rewritten - which is the point: a note that
    lies is the same defect one layer up.
    """
    with zipfile.ZipFile(_the_wheel()) as z:
        name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        declared = {re.split(r"[\s;<>=!~\[]", line.split(":", 1)[1].strip())[0].lower()
                    for line in z.read(name).decode("utf-8").splitlines()
                    if line.startswith("Requires-Dist:")}
    assert declared, "the wheel declares no dependencies at all - this test is checking nothing"
    assert pin not in declared, (
        f"the agent's wheel now declares {pin}, so the comments in requirements/runtime.txt and "
        f"requirements/constraints.txt saying it does NOT are stale - rewrite them")


def test_the_build_runs_the_path_a_multi_region_part_takes_rather_than_only_importing_it():
    """The gate that catches the next dependency hiding behind trimesh.

    Importing `geometry_agent` proves the wheel is there. It proved exactly that while the image could not
    measure a part with two regions at all, because the import closure of `facts.regions` does not name rtree
    - trimesh reaches for it, inside the call. So the check has to RUN the call.
    """
    verify = (REPO / "deploy" / "verify_install.py").read_text(encoding="utf-8")
    assert "detect_regions" in verify, (
        "deploy/verify_install.py does not run the nesting test, so an image that imports the agent but "
        "cannot measure a multi-region part still builds - and the first customer with a CHT assembly, a "
        "body inside a body or a sealed bubble finds out instead")
    assert verify.count("_measure_the_nesting(") == 3, (
        "deploy/verify_install.py does not run the nesting test TWICE (one definition, two calls): the "
        "measurement has two ray backends, and a run on the preferred one alone passes in an image whose "
        "fallback raises ImportError the first time embree cannot load")
    for token in ('_GEOMETRY_RAY_FALLBACK = "embreex"', "sys.meta_path.insert"):
        assert token in verify, (
            f"deploy/verify_install.py no longer hides {token!r} to reach the fallback backend, so the second "
            f"run measures the same path as the first")
    assert DOCKERFILE.count("python /tmp/verify_install.py") == 2, (
        "the multi-region run does not reach both runtime blocks")


# ---------------------------------------------------------------- what it installs is not stale

def test_the_vendored_wheel_matches_its_recorded_provenance():
    wheel = _the_wheel()
    doc = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert doc["wheel"] == wheel.name, (
        f"PROVENANCE.json describes {doc['wheel']} but the vendored wheel is {wheel.name} - one of them "
        f"was replaced without the other")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert digest == doc["sha256"], (
        "the vendored wheel's bytes do not match the sha256 PROVENANCE.json records, so nothing here "
        "describes the wheel the image would install. Rebuild it with deploy/vendor_geometry_agent.sh.")


def test_the_wheel_names_the_agent_commit_it_was_built_from():
    # A static 0.1.0 makes two wheels built six months apart indistinguishable, which is how
    # dist/hexera-geometry-agent.tar.gz went stale unnoticed. The local version segment puts the commit
    # where `importlib.metadata.version` inside a running container can read it.
    doc = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", doc["agent_commit"]), (
        f"PROVENANCE.json records no usable agent commit: {doc['agent_commit']!r}")
    assert doc["agent_commit"].startswith(doc["agent_commit_short"])
    assert f"+g{doc['agent_commit_short']}" in doc["version"], (
        f"the wheel's version {doc['version']!r} does not carry the agent commit")
    assert doc["version"] in _the_wheel().name.replace("_", "+", 0) or \
        doc["agent_commit_short"] in _the_wheel().name, (
        f"the wheel filename {_the_wheel().name} does not carry the commit its provenance records")


def test_a_release_wheel_was_not_built_from_a_dirty_agent_checkout():
    doc = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert doc["agent_checkout_dirty"] is False, (
        "the vendored wheel was built from an agent checkout with uncommitted changes, so the commit it "
        "records does not describe it. Rebuild from a clean checkout.")


def test_the_vendored_wheel_carries_every_subpackage_the_application_imports():
    # THE staleness test. The abandoned dist/hexera-geometry-agent.tar.gz carried agent, cli, evaluate,
    # facts, render, synthetic and testing - and none of ask, chain, contract, learn, reconcile or
    # vision, every one of which src/meshpipeline imports today. A wheel like that installs without
    # complaint and fails at the first real job.
    names = set(zipfile.ZipFile(_the_wheel()).namelist())
    present = {n.split("/")[1] for n in names if n.startswith("geometry_agent/") and n.count("/") > 1}
    present |= {n.split("/")[1].removesuffix(".py") for n in names
                if n.startswith("geometry_agent/") and n.endswith(".py") and n.count("/") == 1}
    missing = sorted(_imported_subpackages() - present)
    assert not missing, (
        f"src/meshpipeline imports geometry_agent.{{{', '.join(missing)}}}, and the vendored wheel does "
        f"not carry {'them' if len(missing) > 1 else 'it'}. The wheel is stale - rebuild it from an "
        f"agent checkout that has them.")


def test_the_build_time_subpackage_gate_covers_what_the_application_actually_imports():
    # deploy/verify_install.py's list is what FAILS THE BUILD, so a list that has drifted below the real
    # imports is a gate that passes an incomplete wheel. Parsed textually because that file is
    # deliberately stdlib-only and is not importable from here without a built meshpipeline.
    verify = (REPO / "deploy" / "verify_install.py").read_text(encoding="utf-8")
    block = re.search(r"GEOMETRY_SUBPACKAGES\s*=\s*\(([^)]*)\)", verify)
    assert block, "GEOMETRY_SUBPACKAGES is not declared as a tuple literal in deploy/verify_install.py"
    gated = set(re.findall(r'"([^"]+)"', block.group(1)))
    names = set(zipfile.ZipFile(_the_wheel()).namelist())
    real_subpackages = {n.split("/")[1] for n in names
                        if n.startswith("geometry_agent/") and n.endswith("/__init__.py")}
    uncovered = sorted((_imported_subpackages() & real_subpackages) - gated)
    assert not uncovered, (
        f"the application imports geometry_agent.{{{', '.join(uncovered)}}} but the build-time gate in "
        f"deploy/verify_install.py does not check for {'them' if len(uncovered) > 1 else 'it'}, so an "
        f"image missing {'them' if len(uncovered) > 1 else 'it'} would still build")


#: A `Path(__file__)`-rooted expression that ends in a data file's NAME: `with_name("x.json")`, `.parent / "x"`,
#: `.parents[1] / "learn" / "x"`. Every table the agent reads out of its own installed tree is written this way,
#: and that is not a coincidence - it is the only shape that works from inside a wheel, which is the point
#: `learn/calibration.py` makes at length about `store.CALIB_DIR` not existing in the container.
_FILE_ROOTED = re.compile(
    r"Path\(\s*__file__\s*\)((?:\s*\.\s*resolve\(\s*\)|\s*\.\s*parent\b|\s*\.\s*parents\s*\[\s*\d+\s*\])*)"
    r"(?:\s*\.\s*with_name\(\s*[\"']([^\"']+)[\"']\s*\)|((?:\s*/\s*[\"'][^\"']+[\"'])+))")
_SEGMENT = re.compile(r"/\s*[\"']([^\"']+)[\"']")
_UPWARDS = re.compile(r"\.\s*parents\s*\[\s*(\d+)\s*\]|\.\s*(parent)\b")


def _data_files_the_wheels_own_modules_read() -> dict[str, list[str]]:
    """Every non-.py file the wheel's OWN modules resolve against `__file__`, and which module reads each.

    WHY THIS IS NOT THE DECLARATION AGAIN. The declaration says what was MEANT to ship; this says what the
    shipped code will open. They come from different places - a pyproject table recorded at vendoring time
    against the .py files actually in the zip - so a file that is declared and not packaged, or packaged under
    the wrong path, shows up as a difference rather than as agreement with itself.

    THE FAILURE IT GUARDS is silent in the worst way. `learn/engine_cell_correction.json` was declared in the
    agent's pyproject and named in neither this file's list nor the vendoring script's, so nothing checked it:
    with the table absent, `hexera.correction_info` finds no row, falls through to the older per-representation
    table which is unfitted inside the container, and the forecast reverts to raw with no error anywhere.
    """
    out: dict[str, list[str]] = {}
    with zipfile.ZipFile(_the_wheel()) as z:
        for name in sorted(z.namelist()):
            if not (name.startswith("geometry_agent/") and name.endswith(".py")):
                continue
            text = z.read(name).decode("utf-8")
            for m in _FILE_ROOTED.finditer(text):
                base = name
                for up in _UPWARDS.finditer(m.group(1) or ""):
                    # `Path(__file__).parent` is the module's directory; `parents[k]` is k+1 levels up from
                    # the file itself, which is why these two count differently.
                    for _ in range(int(up.group(1)) + 1 if up.group(1) else 1):
                        base = posixpath.dirname(base)
                if m.group(2):
                    base, segments = posixpath.dirname(base), [m.group(2)]
                else:
                    segments = _SEGMENT.findall(m.group(3) or "")
                if not segments or segments[-1].endswith(".py") or "." not in segments[-1]:
                    continue
                out.setdefault(posixpath.normpath(posixpath.join(base, *segments)), []).append(name)
    assert out, "no data file read was found in the wheel's own modules at all - this test is checking nothing"
    return out


def _declared_package_data() -> set[str]:
    """The agent's own `[tool.setuptools.package-data]` declaration, as `deploy/vendor_geometry_agent.sh`
    recorded it, resolved to wheel paths.

    NOT A LIST KEPT HERE. It used to be three names in this file and the same three in the vendoring script, one
    hand-kept list in two places with a subset test between them: the day the agent declared a fourth
    (`learn/engine_cell_correction.json`) both said the wheel was complete, and `PROVENANCE.json` still records
    `package_data` as three while its own `data_files` shows four. Entries may be globs, so a glob is expanded
    against the wheel rather than compared to it.
    """
    doc = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    declared = [str(g).strip() for g in (doc.get("package_data") or []) if str(g).strip()]
    assert declared, (
        "PROVENANCE.json records no package-data declaration, so nothing here knows what the agent meant to "
        "ship. Re-vendor with deploy/vendor_geometry_agent.sh.")
    names = set(zipfile.ZipFile(_the_wheel()).namelist())
    out: set[str] = set()
    for glob in declared:
        rel = f"geometry_agent/{glob.lstrip('/')}"
        if any(c in glob for c in "*?["):
            out |= {n for n in names if fnmatch.fnmatch(n, rel)} or {rel}
        else:
            out.add(rel)
    return out


def test_the_vendored_wheel_carries_the_data_the_package_reads():
    # A wheel built without a package-data declaration ships .py files only. identity_tests.json is read
    # with .read_text(), so its absence is a FileNotFoundError the first time a plan is built - at a
    # customer's upload, not at build.
    #
    # THE LIST IS DERIVED, from two independent places: what the agent DECLARED (its pyproject, recorded in
    # PROVENANCE.json at vendoring time) and what the shipped modules will actually OPEN. A hand-kept list was
    # the same blind spot this file's header is about, one layer down - it said the wheel was complete on the
    # day a fourth table was added to the declaration and to nothing else.
    #
    # NOT PARAMETRISED over the derived list, deliberately. Deriving it needs the wheel, and a parameter list
    # computed at collection turns a missing wheel into a collection error for this whole module - which is
    # exactly the "it cannot be silenced by the absence of the thing it is about" property at the head of this
    # file, lost. Every check here happens inside the test, and every missing file is reported at once.
    readers = _data_files_the_wheels_own_modules_read()
    required = sorted(_declared_package_data() | set(readers))
    names = set(zipfile.ZipFile(_the_wheel()).namelist())
    missing = [rel for rel in required if rel not in names]
    why = {rel: ("read by " + ", ".join(readers[rel])) if readers.get(rel)
           else "declared as package data by the agent" for rel in missing}
    assert not missing, (
        "the vendored wheel does not carry "
        + "; ".join(f"{rel} ({why[rel]})" for rel in missing)
        + ". The installed package cannot read its own table: rebuild with deploy/vendor_geometry_agent.sh "
          "from an agent checkout that declares and contains it.")


def test_the_shipped_correction_table_is_one_the_reader_will_actually_apply():
    """The basis gate, checked against the table BESIDE the reader that gates on it, both out of the wheel.

    `learn/calibration.ENGINE_BASIS` names the estimator the table was fitted against, and
    `hexera.EMULATOR_BASIS_WORDS` is what `hexera._engine_correction` checks it for before multiplying anything.
    Refit the table against a different estimator without changing that sentence and the check cannot fire;
    change the sentence so that no admitted word survives and the whole table is applied as 1.0 while every
    field still reports a multiplier, an n and an interval as though they were in force. Neither of those is
    visible from inside the container, so it is asserted here, where both files are in one zip.
    """
    rel = "geometry_agent/learn/engine_cell_correction.json"
    with zipfile.ZipFile(_the_wheel()) as z:
        names = set(z.namelist())
        assert rel in names, f"the vendored wheel carries no {rel}"
        table = json.loads(z.read(rel).decode("utf-8"))
        hexera = z.read("geometry_agent/agent/hexera.py").decode("utf-8")
    block = re.search(r"EMULATOR_BASIS_WORDS\s*=\s*\(([^)]*)\)", hexera)
    assert block, "EMULATOR_BASIS_WORDS is not a tuple literal in the wheel's geometry_agent/agent/hexera.py"
    admitted = set(re.findall(r'"([^"]+)"', block.group(1)))
    assert admitted, "EMULATOR_BASIS_WORDS is empty in the wheel, so the basis gate admits nothing"
    basis = str(table.get("basis") or "")
    assert any(word in basis for word in admitted), (
        f"the shipped table's basis {basis[:90]!r} contains none of {sorted(admitted)}, so "
        f"hexera._engine_correction will report its multiplier and apply 1.0. Either the table was refit "
        f"against a different estimator, or the gate's words changed - fix whichever is wrong, but the "
        f"forecast must not quietly revert to raw while the block still prints a factor.")
    for key, row in (table.get("by_engine") or {}).items():
        assert row.get("why"), f"{key} in the shipped table says nothing about its verdict"
        if not row.get("applied"):
            assert row.get("multiplier") == 1.0, f"{key} is not applied and still carries {row.get('multiplier')}"


def test_the_wheel_ships_no_test_or_eval_tree():
    # An image gets the library, not the agent's own suite or its corpus. This also catches a wheel built
    # by something other than the vendoring script, which is what would ship them.
    names = zipfile.ZipFile(_the_wheel()).namelist()
    stowaways = [n for n in names if n.startswith(("tests/", "eval/", "docs/"))]
    assert not stowaways, f"the vendored wheel ships {len(stowaways)} files it should not: {stowaways[:5]}"


#: THE CALLS THIS APPLICATION MAKES INTO THE AGENT THAT A SUBPACKAGE LIST CANNOT COVER, as
#: `(module path inside the wheel, what the application names, the keywords it passes)`.
#:
#: WHY THIS IS NOT THE SUBPACKAGE GATE AGAIN. `test_the_vendored_wheel_carries_every_subpackage_the_application
#: _imports` asks whether `geometry_agent.reconcile` is in the wheel at all. It was, on the day
#: `application/geometry_survey` started calling `reconcile.joint.for_job`, and it would have been on the day
#: somebody passed that function a keyword only the agent's master has. The wheel is built from ONE recorded agent
#: commit, and every entry point here wraps its agent call in try/except, so a `TypeError: unexpected keyword
#: argument` is a step that is skipped in silence on every job with one warning line behind it. That is the
#: failure shape this file exists for, one level further in: not an absent module, an absent argument.
#:
#: Checked against the WHEEL and never against an import, for the reason at the head of this file: on a
#: developer's machine `geometry_agent` comes off PYTHONPATH from a checkout that is ahead of the wheel, which is
#: the one environment where this cannot be allowed to pass.
AGENT_CALLS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("geometry_agent/reconcile/joint.py", "for_job", ("case", "path", "mesh")),
    ("geometry_agent/agent/hexera.py", "report_measured", ("look", "fluid_side", "inlet_ids")),
    ("geometry_agent/ask/intake.py", "ask_intake", ("declared", "brief", "asked_before", "alerts",
                                                    "reconciliation")),
    #: `application/geometry_survey.dispatch_refusals` reads the customer's answer to the ONE question that
    #: decides whether the job runs through this. A wheel without it cannot tell "send it back" from "try it
    #: anyway", so that gate passes every submission - which is where this platform was before the gate existed.
    ("geometry_agent/ask/say.py", "dispatch_of", ()),
)


@pytest.mark.parametrize("rel,name,keywords", AGENT_CALLS)
def test_the_vendored_wheel_carries_the_calls_this_application_makes(rel, name, keywords):
    import ast
    with zipfile.ZipFile(_the_wheel()) as z:
        assert rel in set(z.namelist()), f"the vendored wheel does not carry {rel}"
        tree = ast.parse(z.read(rel).decode("utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name), None)
    assert fn is not None, (
        f"{rel} in the vendored wheel defines no {name}; src/meshpipeline calls it. The wheel is stale - "
        f"rebuild it with deploy/vendor_geometry_agent.sh from an agent checkout that has it.")
    args = fn.args
    accepted = {a.arg for a in (*args.args, *args.posonlyargs, *args.kwonlyargs)}
    takes_kwargs = args.kwarg is not None
    missing = sorted(k for k in keywords if k not in accepted)
    assert takes_kwargs or not missing, (
        f"src/meshpipeline calls {name}({', '.join(k + '=' for k in missing)}...) and the {name} in the "
        f"vendored wheel does not accept {'them' if len(missing) > 1 else 'it'}. Against this wheel that call is "
        f"a TypeError the caller swallows, so the step is skipped on every job. Rebuild the wheel with "
        f"deploy/vendor_geometry_agent.sh.")


def test_the_recorded_agent_commit_is_one_this_checkout_can_name():
    """A wheel whose provenance names a commit nobody can look up cannot be checked against anything.

    NOT that it is the newest commit: this repository does not own the agent's branch and a wheel one commit
    behind is a normal, deliberate state. What it may not be is unidentifiable.
    """
    recorded = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", str(recorded.get("agent_commit") or "")), recorded.get("agent_commit")
    assert str(recorded.get("agent_commit", "")).startswith(str(recorded.get("agent_commit_short", "x")))
    assert recorded.get("agent_checkout_dirty") is False, (
        "the vendored wheel was built from a dirty agent checkout, so the commit it records does not describe it")
