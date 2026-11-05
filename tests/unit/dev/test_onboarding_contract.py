# Responsibility: Verify make setup prepares a clone without configuration, and only the developer ever writes .env.
# Boundaries: no credential value reaches .env, an image or a tracked file; no test signs in or contacts Google.
from __future__ import annotations

import copy
import importlib.util
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SETUP = (REPO / "devtools" / "env" / "setup.sh").read_text()
MAKEFILE = (REPO / "Makefile").read_text()
COMPOSE_SRC = (REPO / "docker-compose.yml").read_text()
COMPOSE = yaml.safe_load(COMPOSE_SRC)
BASH = shutil.which("bash") or "/bin/bash"
GATE = REPO / "devtools" / "env" / "check_runtime_config.py"

#: The one service whose code reaches Google: run_mesh() is the only route to the Cloud Run
#: client, the engine runners are its only callers, and they run in the pipeline graph - which
#: the `simulation_jobs` queue executes. The API never runs the graph; cleanup, export and the
#: beat scheduler never mesh.
DISPATCHER = "worker"
CRED_PATH = "/gcp/adc.json"

#: Not a credential: the shape google-auth accepts for an authorized user, with values that
#: authenticate nothing. Nothing here transmits it.
ADC_SECRET = "NOT-A-REAL-REFRESH-TOKEN"
ADC_JSON = json.dumps({"type": "authorized_user", "client_id": "fixture.apps.googleusercontent.com",
                       "client_secret": "fixture-secret", "refresh_token": ADC_SECRET})

#: Every command setup is forbidden to run. Each is planted as a shim that RECORDS the call and
#: then fails, so a reintroduction cannot be silent: the marker names it and the run stops.
FORBIDDEN_COMMANDS = ("gcloud", "xdg-open", "open", "www-browser", "sensible-browser")


def _app_services() -> dict:
    return {n: s for n, s in COMPOSE["services"].items() if s.get("env_file")}


def _target_body(name: str) -> str:
    m = re.search(rf"^{name}:[^\n]*\n((?:\t.*\n|\s*\n)*)", MAKEFILE, re.M)
    return m.group(1) if m else ""


def _gate_module():
    spec = importlib.util.spec_from_file_location("check_runtime_config", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A disposable clone the real setup.sh can COMPLETE in: a genuine minimal distribution, so the
# virtualenv, `pip install -e .` and the render-stack import check are really exercised. Only
# Docker is doubled, because a test may not touch this machine's Docker resources.


def _fixture_repo(root: Path, *, script: str | None = None) -> Path:
    (root / "devtools" / "env").mkdir(parents=True, exist_ok=True)
    (root / "requirements").mkdir(exist_ok=True)
    (root / "src" / "meshpipeline" / "sandbox").mkdir(parents=True, exist_ok=True)
    (root / "devtools" / "env" / "setup.sh").write_text(script if script is not None else SETUP)
    shutil.copy(REPO / ".env.example", root / ".env.example")   # the SHIPPED template
    # Every requirements file a real clone ships, derived from the tracked tree rather than
    # listed here - setup.sh now hands pip the constraints file, and a fixture missing it would
    # make this suite disagree with the clone it claims to simulate.
    for src in sorted((REPO / "requirements").glob("*.txt")):
        (root / "requirements" / src.name).write_text("")
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n'
        '[project]\nname = "meshpipeline"\nversion = "0.0.0"\n\n'
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n')
    # The REAL one, not an empty stand-in: it declares __version__, which is the product-version
    # authority setup reads to hand Compose its APP_VERSION. A clone that lacks it is not a clone
    # this suite can speak for, and an empty file here would make setup fail for a reason no
    # developer will ever meet.
    shutil.copy(REPO / "src" / "meshpipeline" / "__init__.py",
                root / "src" / "meshpipeline" / "__init__.py")
    (root / "src" / "meshpipeline" / "sandbox" / "__init__.py").write_text("")
    (root / "src" / "meshpipeline" / "sandbox" / "backend.py").write_text("")
    (root / "docker-compose.yml").write_text("services: {}\n")
    return root


def _doubles(bin_dir: Path, marker: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 0\n")     # no real daemon is touched
    for name in FORBIDDEN_COMMANDS:
        (bin_dir / name).write_text(f'#!/bin/sh\necho "{name} $@" >> "{marker}"\nexit 1\n')
    for name in ("docker", *FORBIDDEN_COMMANDS):
        (bin_dir / name).chmod(0o755)


class SetupRun:
    def __init__(self, proc: subprocess.CompletedProcess, root: Path, marker: Path):
        self.proc, self.root, self.marker = proc, root, marker

    @property
    def returncode(self) -> int:
        return self.proc.returncode

    @property
    def output(self) -> str:
        return self.proc.stdout + self.proc.stderr

    @property
    def forbidden_calls(self) -> list[str]:
        return self.marker.read_text().split() if self.marker.exists() else []


def _run_setup(root: Path) -> SetupRun:
    home = root.parent / "home"
    home.mkdir(exist_ok=True)
    bin_dir = root.parent / "bin"
    marker = root.parent / "forbidden_calls"
    _doubles(bin_dir, marker)
    env = {"PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin", "HOME": str(home),
           "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([BASH, str(root / "devtools" / "env" / "setup.sh")],
                          capture_output=True, text=True, env=env, cwd=root, timeout=900)
    return SetupRun(proc, root, marker)


@pytest.fixture(scope="module")
def fresh_clone(tmp_path_factory):
    # ONE completed run of the real script in a clone with no .env, no credential anywhere, and
    # every authentication command planted to fail if touched.
    base = tmp_path_factory.mktemp("fresh")
    return _run_setup(_fixture_repo(base / "repo"))


# make setup completes on a clone that is configured with nothing at all


def test_setup_succeeds_on_a_fresh_clone_with_no_env_no_credential_and_no_gcloud(fresh_clone):
    assert fresh_clone.returncode == 0, fresh_clone.output
    assert (fresh_clone.root / ".venv" / "bin" / "python").exists(), "no virtualenv was built"
    assert (fresh_clone.root / "data" / "jobs").is_dir(), "the bind-mounted directories are missing"


def test_setup_makes_the_credential_directory_and_never_the_credential(fresh_clone):
    holder = fresh_clone.root / "secrets" / "gcp"
    assert holder.is_dir(), "setup did not prepare the canonical credential directory"
    assert list(holder.iterdir()) == [], f"setup put something in it: {list(holder.iterdir())}"
    assert not (fresh_clone.root / CANONICAL_REL).exists(), "setup created a credential"


@pytest.mark.skipif(sys.platform != "linux", reason="permission bits are asserted on Linux only")
def test_the_credential_directory_is_owner_only(fresh_clone):
    for d in (fresh_clone.root / "secrets", fresh_clone.root / "secrets" / "gcp"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, f"{d} is {oct(d.stat().st_mode)}"


def test_the_created_env_is_byte_identical_to_the_template(fresh_clone):
    assert (fresh_clone.root / ".env").read_bytes() == (REPO / ".env.example").read_bytes(), \
        "setup put something in .env that the template does not have"


def _without_heredocs(script: str) -> str:
    # Drop every `<<WORD ... WORD` body: those lines are printed to the operator, never executed.
    out, skip_until = [], None
    for line in script.splitlines():
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        m = re.search(r"<<-?\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
        if m:
            skip_until = m.group(1)
            continue
        out.append(line)
    return "\n".join(out)


def test_setup_runs_no_authentication_command_and_opens_no_browser(fresh_clone):
    # THE INVARIANT: obtaining an identity is the developer's own act. Setup may install the CLI
    # that act needs - that is ordinary host tooling, and it asks first - but it must never run
    # gcloud, never authenticate, and never open a browser on someone's behalf. The check is the
    # command doubles: they record any invocation, and there must be none.
    assert fresh_clone.forbidden_calls == [], \
        f"setup invoked commands it must never invoke: {fresh_clone.forbidden_calls}"
    # The string guard below covers the EXECUTABLE script only. Setup's closing banner prints the
    # authentication steps a developer must take next, so the words necessarily appear in its
    # output; printing them is the opposite of running them. Heredoc bodies are stripped first so
    # the guard keeps meaning "setup never authenticates" instead of "setup never says the word".
    executable = _without_heredocs(SETUP)
    for forbidden in ("application-default", "browser", "SKIP_GCLOUD_LOGIN", "auth login"):
        assert forbidden not in executable, f"setup.sh still mentions {forbidden!r}"
    # gcloud may be named only to detect it or to install it - never to run it.
    for line in executable.splitlines():
        if "gcloud" not in line or line.lstrip().startswith("#"):
            continue
        assert ("have gcloud" in line or "google-cloud" in line or "cloud.google.com" in line
                or "cloud.google.gpg" in line or '"gcloud' in line or "gcloud-sdk" in line
                or "_install_gcloud" in line), (
            f"setup.sh appears to run gcloud: {line.strip()}")


def test_the_template_exposes_the_canonical_location_from_the_catalogue():
    from meshpipeline.settings.inventory import render_env
    template = (REPO / ".env.example").read_text()
    assert f"GOOGLE_ADC_FILE=./{CANONICAL_REL}\n" in template
    assert template == render_env(), "the template is not what the catalogue renders"


def test_setup_finishes_with_every_runtime_setting_still_blank(fresh_clone):
    from meshpipeline.settings.inventory import required_names

    env_text = "\n" + (fresh_clone.root / ".env").read_text()
    for name in required_names():
        if name == "GOOGLE_ADC_FILE":
            # the one required setting that ships a value: the location is Hexera's to choose,
            # the FILE is the developer's to provide
            assert f"\n{name}=./{CANONICAL_REL}\n" in env_text
            continue
        assert f"\n{name}=\n" in env_text, f"{name} is not blank in the created .env"
    assert fresh_clone.returncode == 0, "setup made its success conditional on a runtime value"


def test_repeated_setup_preserves_an_edited_env_byte_for_byte(tmp_path):
    root = _fixture_repo(tmp_path / "repo")
    assert _run_setup(root).returncode == 0

    sentinel = b"# EDITED BY THE DEVELOPER\nDEEPSEEK_API_KEY=sk-sentinel\nEXTRA_LINE=kept\n"
    (root / ".env").write_bytes(sentinel)

    second = _run_setup(root)
    assert second.returncode == 0, second.output
    assert (root / ".env").read_bytes() == sentinel, "a rerun rewrote the developer's .env"
    assert "left exactly as it is" in second.output


# the authentication modes are gone from the repository, not merely unused


@pytest.mark.parametrize("removed", ["SKIP_GCLOUD_LOGIN", "auth_decision", "adc_usable",
                                     "die_without_gcloud", "adopt_deployment"])
def test_the_removed_setup_machinery_exists_nowhere_in_the_tree(removed):
    hits = subprocess.run(["git", "grep", "-l", "-F", removed], cwd=REPO,
                          capture_output=True, text=True).stdout.split()
    # this module names them to check they are gone; nothing else may
    survivors = [h for h in hits if h != "tests/unit/dev/test_onboarding_contract.py"]
    assert survivors == [], f"{removed} survives in: {survivors}"


def test_no_file_declares_the_four_state_authentication_machine():
    found = subprocess.run(["git", "grep", "-lE", r"reuse \| skip \| login \| blocked"],
                           cwd=REPO, capture_output=True, text=True).stdout.strip()
    assert found == "", f"the decision machine survives in: {found}"


# make dev-up owns runtime validation, through one authority


def _gate(tmp_path, env_text: str, *, environ: dict[str, str] | None = None):
    # ROOT is redirected as well as ENV: the resolver anchors at the repository root, so a test
    # that wants a credential in place must own that root rather than write into this checkout.
    mod = _gate_module()
    (tmp_path / ".env").write_text(env_text)
    mod.ROOT = tmp_path
    mod.ENV = tmp_path / ".env"
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(environ or {})
        return mod, mod.problems()
    finally:
        os.environ.clear()
        os.environ.update(saved)


CANONICAL_REL = "secrets/gcp/application_default_credentials.json"


def _place_credential(root: Path, content: str = ADC_JSON) -> Path:
    adc = root / CANONICAL_REL
    adc.parent.mkdir(parents=True, exist_ok=True)
    adc.write_text(content)
    return adc


def _complete_env(tmp_path, *, credential: str | None = None, place: bool = True) -> str:
    from meshpipeline.settings.inventory import all_vars, required_names
    if place:
        _place_credential(tmp_path)
    values = {v.name: v.default for v in all_vars()}
    for name in required_names():
        values[name] = f"set-{name.lower()}"
    values["GOOGLE_ADC_FILE"] = credential if credential is not None else f"./{CANONICAL_REL}"
    return "".join(f"{k}={v}\n" for k, v in values.items())


def test_an_incomplete_env_reports_every_missing_setting_at_once(tmp_path):
    from meshpipeline.settings.inventory import all_vars, required_names
    defaults = {v.name: v.default for v in all_vars()}

    _, found = _gate(tmp_path, "DEEPSEEK_API_KEY=\nGCP_PROJECT_ID=\n")

    assert found, "an empty configuration was accepted"
    unset = next(f for f in found if "unset in .env" in f)
    for name in required_names():
        if defaults[name]:
            continue                    # a setting that ships a value can never be missing
        assert name in unset, f"{name} was not reported as missing"
    assert len([f for f in found if "unset in .env" in f]) == 1, \
        f"the missing settings were not reported together: {found}"
    # and the credential, which has a location but no file, is its own separate fault
    assert any(CANONICAL_REL in f for f in found), found


def test_the_gate_runs_before_any_application_container_starts():
    lines = [ln.strip() for ln in _target_body("dev-up").splitlines()
             if ln.strip() and not ln.strip().startswith("@#")]
    gate = next(i for i, ln in enumerate(lines) if "check_runtime_config.py" in ln)
    for i, line in enumerate(lines):
        if "docker compose" in line:
            assert i > gate, f"a container command runs before the gate: {line!r}"
    assert any("docker compose up" in ln for ln in lines), "dev-up no longer starts the stack"


def test_the_gate_names_no_setting_of_its_own():
    # The catalogue is the only source of WHAT is required; a name written into the gate would be
    # the duplicated list this replaced. GOOGLE_ADC_FILE is the exception it declares once,
    # because it is the only setting checked as a file rather than for presence.
    from meshpipeline.settings.inventory import required_names
    source = GATE.read_text()
    for name in required_names():
        if name == "GOOGLE_ADC_FILE":
            continue
        assert name not in source, f"the gate keeps its own copy of {name}"
    assert source.count('"GOOGLE_ADC_FILE"') == 1, "even that name is restated"
    # and the VALUE is the catalogue's, not a second copy living beside the resolver
    from meshpipeline.settings.inventory import all_vars
    default = next(v.default for v in all_vars() if v.name == "GOOGLE_ADC_FILE")
    assert default == f"./{CANONICAL_REL}", default
    assert default not in source, "the gate restates the catalogue's default value"


@pytest.mark.parametrize("kind", ["absent", "directory", "empty", "invalid", "symlink"])
def test_an_unusable_credential_stops_the_stack(tmp_path, kind):
    canonical = tmp_path / CANONICAL_REL
    canonical.parent.mkdir(parents=True)
    if kind == "directory":
        canonical.mkdir()
    elif kind == "empty":
        canonical.write_text("   \n")
    elif kind == "invalid":
        canonical.write_text('{"type":"nonsense"}')
    elif kind == "symlink":
        outside = tmp_path / "elsewhere.json"
        outside.write_text(ADC_JSON)
        canonical.symlink_to(outside)          # escapes the directory that carries the protections

    _, found = _gate(tmp_path, _complete_env(tmp_path, place=False))

    assert found, f"an {kind} credential was accepted"
    assert any(CANONICAL_REL in f for f in found), found
    assert ADC_SECRET not in " ".join(found), "the gate printed credential content"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file with no permission bits")
def test_an_unreadable_credential_stops_the_stack(tmp_path):
    adc = _place_credential(tmp_path)
    adc.chmod(0)
    try:
        _, found = _gate(tmp_path, _complete_env(tmp_path, place=False))
        assert found and any(CANONICAL_REL in f for f in found), found
    finally:
        adc.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_the_refusal_says_exactly_where_to_put_the_file(tmp_path, capsys):
    mod, _ = _gate(tmp_path, _complete_env(tmp_path, place=False))
    saved = dict(os.environ)
    os.environ.clear()
    try:
        assert mod.main([""]) == 1
    finally:
        os.environ.clear()
        os.environ.update(saved)

    err = capsys.readouterr().err
    assert ("Place your Google Application Default Credentials file at:\n"
            "secrets/gcp/application_default_credentials.json") in err, err


def test_no_host_specific_path_reaches_the_operator(tmp_path, capsys):
    mod, _ = _gate(tmp_path, _complete_env(tmp_path, place=False))
    saved = dict(os.environ)
    os.environ.clear()
    try:
        mod.main([""])
    finally:
        os.environ.clear()
        os.environ.update(saved)

    err = capsys.readouterr().err
    for host_specific in (str(tmp_path), "/home/", "/Users/", "%APPDATA%", "CLOUDSDK_CONFIG",
                          ".config/gcloud"):
        assert host_specific not in err, f"the refusal names {host_specific!r}"


def test_the_path_resolves_from_the_repository_root_whatever_the_caller_s_directory(tmp_path):
    # The same value, resolved from three different working directories, must name one file.
    (tmp_path / "somewhere" / "else").mkdir(parents=True)
    resolved = set()
    for cwd in (REPO, tmp_path, tmp_path / "somewhere" / "else"):
        r = subprocess.run([sys.executable, str(GATE), "--credential-path"],
                           capture_output=True, text=True, cwd=cwd)
        assert r.returncode == 0, r.stderr
        resolved.add(r.stdout.strip())
    assert len(resolved) == 1, f"the path depends on the caller's directory: {resolved}"
    assert resolved.pop() == str(REPO / CANONICAL_REL)


def test_an_arbitrary_external_credential_path_is_refused(tmp_path):
    outside = tmp_path / "somewhere-else.json"
    outside.write_text(ADC_JSON)
    _place_credential(tmp_path)                # even with a good file in the canonical place

    _, found = _gate(tmp_path, _complete_env(tmp_path, credential=str(outside), place=False))

    assert found, "an arbitrary absolute path was accepted"
    assert any("one place" in f for f in found), found


def test_a_complete_configuration_passes_the_gate_and_reaches_the_live_preflight(tmp_path):
    mod, found = _gate(tmp_path, _complete_env(tmp_path))
    assert found == [], f"a complete configuration was rejected: {found}"

    body = _target_body("dev-up")
    assert "devtools/env/preflight_cloudrun.py" in body, "the live preflight is gone"
    assert body.index("check_runtime_config.py") < body.index("preflight_cloudrun.py")
    assert mod.CREDENTIAL_FILE_SETTING == "GOOGLE_ADC_FILE"


def test_an_exported_variable_still_beats_the_file(tmp_path):
    from meshpipeline.settings.inventory import required_names
    _place_credential(tmp_path)
    # .env says something unusable for every setting; the environment says the right thing.
    hostile = "".join(f"{n}=\n" for n in required_names())
    exported = {n: f"exported-{n}" for n in required_names()}
    exported["GOOGLE_ADC_FILE"] = str(tmp_path / CANONICAL_REL)

    _, found = _gate(tmp_path, hostile, environ=exported)
    assert found == [], f"the documented precedence was lost: {found}"


# the host test tiers do not read the developer's configuration


@pytest.mark.parametrize("target", ["check", "check-fast", "test", "test-fast", "lint", "typecheck"])
def test_the_commit_gate_never_sources_the_developers_env(target):
    body = _target_body(target)
    assert ". ./.env" not in body and "source .env" not in body, \
        f"`make {target}` exports the developer's .env into the test process"


def test_the_settings_tier_passes_with_every_required_setting_hostile(tmp_path):
    # If any of these read the developer's file for its answer, an environment that contradicts
    # that file would change the result: .env can never override the process environment.
    from meshpipeline.settings.inventory import required_names
    hostile = dict.fromkeys(required_names(), "hostile-value-from-the-environment")
    env = {**os.environ, **hostile, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTEST_ADDOPTS": f"--basetemp={tmp_path}"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/unit/settings", "tests/unit/dev/test_developer_setup.py"],
                       capture_output=True, text=True, env=env, cwd=REPO, timeout=600)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]


# provisioning reports what it made; it edits nothing


def _provisioning_fixture(tmp_path) -> Path:
    (tmp_path / "deploy" / "output").mkdir(parents=True)
    (tmp_path / "deploy" / "output" / "deployment.json").write_text(json.dumps({
        "project": "proj-x", "region": "europe-west4",
        "service_account_key": ADC_SECRET,          # a decoy: it must never be printed
        "resources": {"mesh_job": {"name": "amp-mesh"},
                      "exchange_bucket": {"name": "proj-x-xfer"}}}))
    return tmp_path


def _run_printer(root: Path) -> subprocess.CompletedProcess:
    script = (REPO / "devtools" / "env" / "print_deployment_settings.py").read_text().replace(
        "ROOT = pathlib.Path(__file__).resolve().parents[2]", f'ROOT = pathlib.Path("{root}")')
    runner = root / "printer.py"
    runner.write_text(script)
    return subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, cwd=root)


def test_provisioning_prints_the_settings_and_leaves_env_untouched(tmp_path):
    root = _provisioning_fixture(tmp_path)
    before = b"DEEPSEEK_API_KEY=sk-mine\nGCP_PROJECT_ID=whatever-i-chose\n"
    (root / ".env").write_bytes(before)

    r = _run_printer(root)

    assert r.returncode == 0, r.stderr
    assert (root / ".env").read_bytes() == before, "provisioning edited the developer's .env"
    for expected in ("GCP_PROJECT_ID=proj-x", "GCP_REGION=europe-west4",
                     "CLOUDRUN_JOB=amp-mesh", "GCP_MESH_BUCKET=proj-x-xfer"):
        assert expected in r.stdout, f"the developer was not told {expected}"


def test_the_printed_settings_carry_nothing_secret(tmp_path):
    r = _run_printer(_provisioning_fixture(tmp_path))

    assert ADC_SECRET not in r.stdout + r.stderr, "provisioning printed a credential"
    assigned = [ln.strip().split("=")[0] for ln in r.stdout.splitlines() if "=" in ln]
    assert sorted(assigned) == ["CLOUDRUN_JOB", "GCP_MESH_BUCKET", "GCP_PROJECT_ID", "GCP_REGION"]


def test_only_setup_and_adopt_write_the_root_env():
    # TWO writers, each safe because of a rule the other depends on: setup CREATES .env only when
    # it is absent, and adopt FILLS only settings that are empty. A third writer - or either of
    # these losing its rule - would silently revert a value someone entered by hand.
    # --untracked so a writer added but not yet committed is caught by the same rule.
    hits = subprocess.run(
        ["git", "grep", "-nE", "--untracked",
         r"ENV(_FILE)?\.write_text|>>? *\"?\$\{ROOT\}/\.env|\.env\.example\" \"\$\{ROOT\}/\.env"],
        cwd=REPO, capture_output=True, text=True).stdout.splitlines()
    writers = sorted({h.split(":")[0] for h in hits if not h.startswith("tests/")})
    assert writers == ["devtools/env/adopt_executor.py", "devtools/env/setup.sh"], \
        "unexpected .env writer(s):\n  " + "\n  ".join(writers)


def _adopt_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "adopt_executor", REPO / "devtools" / "env" / "adopt_executor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_adopt_fills_empty_settings_and_never_overwrites_one_already_set(tmp_path):
    # The rule the writer test above depends on. Running adopt twice, or running it after someone
    # corrected a value by hand, must not revert what is there.
    mod = _adopt_module()
    env = tmp_path / ".env"
    env.write_text("GCP_PROJECT_ID=chosen-by-hand\nGCP_REGION=\nCLOUDRUN_JOB=\nGCP_MESH_BUCKET=\n")
    mod.ENV_FILE = env
    mod.apply({"GCP_PROJECT_ID": "discovered", "GCP_REGION": "us-central1",
               "CLOUDRUN_JOB": "job", "GCP_MESH_BUCKET": "bucket"})

    written = env.read_text()
    assert "GCP_PROJECT_ID=chosen-by-hand" in written, "adopt overwrote a value someone set"
    assert "discovered" not in written
    for filled in ("GCP_REGION=us-central1", "CLOUDRUN_JOB=job", "GCP_MESH_BUCKET=bucket"):
        assert filled in written, f"adopt did not fill the empty {filled.split('=')[0]}"


def test_adopt_creates_no_env_and_refuses_without_one(tmp_path, capsys):
    # It fills a file setup owns; it must never become a second way to bring one into existence.
    mod = _adopt_module()
    mod.ENV_FILE = tmp_path / ".env"
    with pytest.raises(SystemExit) as exit_info:
        mod.main()
    assert exit_info.value.code == 1
    assert "make setup" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists(), "adopt created a .env"


def test_provisioning_requires_gcloud_and_stops_before_creating_anything():
    body = _target_body("mesh-setup")
    prerequisites = body[:body.index("$(MAKE) mesh-image")]
    assert "command -v gcloud" in prerequisites, "provisioning does not check for the CLI first"
    assert "print-access-token" in prerequisites, \
        "provisioning does not verify the host is authenticated before mutating"
    assert "gcloud auth application-default login" in prerequisites, \
        "an unauthenticated host is not told the command to run"
    assert "read -r reply" in prerequisites, "provisioning does not require confirmation"
    assert "docker compose up" not in body, "provisioning starts the local stack"
    # it authenticates nobody: the only mention of a login is the instruction it prints
    assert body.count("application-default login") == 1
    assert "print_deployment_settings.py" in body and "write" not in body


# the credential reaches exactly one container, read-only, and nothing else


def _credential_holders(compose: dict) -> set[str]:
    return {n for n, s in compose["services"].items()
            if s.get("env_file") and any(CRED_PATH in v for v in s.get("volumes", []))}


def test_only_the_mesh_dispatcher_holds_a_credential_and_holds_it_read_only():
    holders = _credential_holders(COMPOSE)
    assert holders == {DISPATCHER}, f"credentials reach {holders}; only {DISPATCHER!r} calls Google"

    svc = COMPOSE["services"][DISPATCHER]
    (mount,) = [v for v in svc["volumes"] if CRED_PATH in v]
    assert mount.endswith(f":{CRED_PATH}:ro"), f"not read-only: {mount}"
    assert svc["environment"]["GOOGLE_APPLICATION_CREDENTIALS"] == CRED_PATH
    assert "simulation_jobs" in svc["command"], "the dispatcher no longer runs the pipeline queue"

    for name, other in _app_services().items():
        if name != DISPATCHER:
            assert "GOOGLE_APPLICATION_CREDENTIALS" not in (other.get("environment") or {}), (
                f"{name} is handed a credential path but never calls Google")


def test_no_credential_material_reaches_the_repository_an_image_or_a_tracked_file(fresh_clone):
    for token in ("refresh_token", "private_key", "BEGIN PRIVATE KEY", "client_secret"):
        assert token not in COMPOSE_SRC, f"docker-compose.yml carries {token!r}"
    dockerfile = (REPO / "Dockerfile").read_text()
    for token in ("adc.json", "application_default_credentials", "gcloud"):
        assert token not in dockerfile, f"the image build references {token!r}"
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO,
                             capture_output=True, text=True).stdout.split()
    assert ".env" not in tracked and not [f for f in tracked if f.endswith("adc.json")]

    for token in ("refresh_token", ADC_SECRET, "BEGIN PRIVATE KEY"):
        assert token not in fresh_clone.output, f"a real setup run logged {token!r}"


def test_gcloud_is_in_no_image_and_no_runtime_dependency():
    for rel in ("Dockerfile", "requirements/runtime.txt", "requirements/dev.txt"):
        assert "gcloud" not in (REPO / rel).read_text(), f"{rel} carries the CLI"
    assert "google-auth" in (REPO / "requirements" / "runtime.txt").read_text(), \
        "the images need the Python client they use instead of the CLI"


# the credential directory is contained by git, docker, and the distribution


def _ignore_rules(name: str) -> list[str]:
    return [ln.strip() for ln in (REPO / name).read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


@pytest.mark.parametrize("ignore_file", [".gitignore", ".dockerignore"])
def test_the_credential_directory_is_excluded_root_anchored(ignore_file):
    rules = _ignore_rules(ignore_file)
    assert "/secrets/" in rules, f"{ignore_file} does not exclude /secrets/"
    assert "secrets/" not in rules, (
        f"{ignore_file} also carries the UNANCHORED rule, which matches any path ending in the "
        "same word and hides which one is load-bearing")


def test_git_cannot_stage_anything_under_the_credential_directory(tmp_path):
    # A real repository, with this repository's own .gitignore, driven by an ordinary `git add -A`.
    repo = tmp_path / "clone"
    (repo / "secrets" / "gcp").mkdir(parents=True)
    shutil.copy(REPO / ".gitignore", repo / ".gitignore")
    (repo / CANONICAL_REL).write_text(ADC_JSON)
    (repo / "secrets" / "stray-note.txt").write_text("also contained")
    (repo / "ordinary.txt").write_text("tracked normally")
    for cmd in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)

    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo,
                            capture_output=True, text=True).stdout.split()

    assert "ordinary.txt" in staged, "the fixture repository staged nothing at all"
    assert not [f for f in staged if f.startswith("secrets/")], \
        f"git add -A staged the credential directory: {staged}"
    assert ADC_SECRET not in subprocess.run(["git", "diff", "--cached"], cwd=repo,
                                            capture_output=True, text=True).stdout


def test_no_dockerfile_stage_can_copy_the_credential_directory():
    dockerfile = (REPO / "Dockerfile").read_text()
    copies = [ln.split()[1] for ln in dockerfile.splitlines()
              if ln.startswith("COPY ") and not ln.startswith("COPY --from")]
    assert copies, "no COPY lines were found to check"
    for src in copies:
        assert not src.startswith("secrets"), f"a stage copies {src}"
    # and even a future `COPY . .` cannot reach it, because the context excludes the directory
    assert "/secrets/" in _ignore_rules(".dockerignore")


def test_the_distribution_cannot_carry_the_credential_directory():
    # setuptools packages what it FINDS under src/. secrets/ is at the repository root, so no
    # wheel or sdist can contain it, and there is no MANIFEST.in that could add it back.
    pyproject = (REPO / "pyproject.toml").read_text()
    assert 'where = ["src"]' in pyproject, "the packaging root changed; recheck containment"
    assert not (REPO / "MANIFEST.in").exists(), \
        "a MANIFEST.in can add root files to an sdist - it must be reviewed if introduced"
    package_data = pyproject[pyproject.index("[tool.setuptools.package-data]"):]
    assert "secrets" not in package_data.split("[tool.ruff]")[0]


def test_no_test_or_diagnostic_reads_the_real_credential():
    # Nothing may open the canonical path in this checkout: a diagnostic that reads it can print
    # it. The gate opens the path its resolver returns, which the tests redirect.
    hits = subprocess.run(["git", "grep", "-n", "-F", CANONICAL_REL], cwd=REPO,
                          capture_output=True, text=True).stdout.splitlines()
    for hit in hits:
        path, _, text = hit.partition(":")
        assert "read_text" not in text and "read_bytes" not in text and "open(" not in text, \
            f"{hit} reads the credential by its literal path"


# mutation controls: each guarantee must be one a real change would break


def _env_slice(root: Path) -> str:
    step = SETUP[SETUP.index('step "Local configuration (.env)"'):
                 SETUP.index("# 3. Python virtualenv")]
    return ('set -e\nstep() { echo "STEP $*"; }\nok() { echo "OK $*"; }\n'
            'warn() { echo "WARN $*"; }\ndie() { echo "DIE $*"; exit 1; }\n'
            f'ROOT="{root}"\n' + step)


def test_reintroducing_automatic_env_mutation_would_be_caught(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy(REPO / ".env.example", root / ".env.example")
    sentinel = b"DEEPSEEK_API_KEY=sk-sentinel\n"
    (root / ".env").write_bytes(sentinel)

    mutated = _env_slice(root) + '\nprintf "GOOGLE_ADC_FILE=/somewhere\\n" >> "${ROOT}/.env"\n'
    r = subprocess.run([BASH, "-c", mutated], capture_output=True, text=True, cwd=tmp_path)

    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / ".env").read_bytes() != sentinel, \
        "a script appending to .env went undetected - the preservation claim proves nothing"


def test_reintroducing_setup_time_authentication_would_be_caught(tmp_path):
    root = _fixture_repo(tmp_path / "repo", script=SETUP.replace(
        'step "Local configuration (.env)"',
        'step "Local configuration (.env)"\ngcloud auth application-default login || true', 1))

    calls = _run_setup(root).forbidden_calls

    assert calls, "a setup that runs gcloud went undetected - the no-authentication claim is empty"
    assert "application-default" in " ".join(calls), calls


def test_dropping_one_setting_from_the_central_validation_would_be_caught(tmp_path, monkeypatch):
    mod = _gate_module()
    mod.ENV = tmp_path / ".env"
    mod.ENV.write_text("")

    import meshpipeline.settings.inventory as inv
    full = inv.required_names()
    monkeypatch.setattr(inv, "required_names", lambda: [n for n in full if n != "GCP_MESH_BUCKET"])
    monkeypatch.delenv("GCP_MESH_BUCKET", raising=False)

    assert "GCP_MESH_BUCKET" not in " ".join(mod.problems()), \
        "the gate reported a setting the catalogue no longer requires - it keeps its own list"


def test_mounting_the_credential_into_another_service_would_be_caught():
    mutated = copy.deepcopy(COMPOSE)
    mutated["services"]["api"].setdefault("volumes", []).append(
        f"${{GOOGLE_ADC_FILE}}:{CRED_PATH}:ro")

    assert _credential_holders(mutated) != {DISPATCHER}, \
        "a credential mounted into the api went undetected"


def test_removing_the_git_rule_would_be_caught(tmp_path):
    repo = tmp_path / "clone"
    (repo / "secrets" / "gcp").mkdir(parents=True)
    mutated = "\n".join(ln for ln in (REPO / ".gitignore").read_text().splitlines()
                        if ln.strip() != "/secrets/")
    (repo / ".gitignore").write_text(mutated)
    (repo / CANONICAL_REL).write_text(ADC_JSON)
    for cmd in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)

    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo,
                            capture_output=True, text=True).stdout.split()
    assert CANONICAL_REL in staged, \
        "a .gitignore without /secrets/ still contained the credential - the rule proves nothing"


def test_removing_the_docker_rule_would_be_caught():
    mutated = [ln for ln in _ignore_rules(".dockerignore") if ln != "/secrets/"]
    assert "/secrets/" not in mutated, "the mutation did not remove the rule"
    # the containment claim is exactly this rule's presence; without it there is nothing to assert
    assert "/secrets/" in _ignore_rules(".dockerignore"), "the shipped rule is missing"


def test_resolving_against_the_callers_directory_would_be_caught(tmp_path):
    mod = _gate_module()
    mod.ROOT = tmp_path
    correct = mod.resolve(f"./{CANONICAL_REL}")

    mod.resolve = lambda value: pathlib.Path(value)     # the mutation: cwd-relative
    mutated = mod.resolve(f"./{CANONICAL_REL}")

    assert mutated != correct, "a cwd-relative resolver produced the same path - nothing is proven"
    assert not mutated.is_absolute(), mutated


# An optional host-tool install must never be able to abort setup


def test_the_gcloud_install_cannot_abort_setup():
    # THE INVARIANT: installing the CLI is optional, so it may not decide whether the rest of
    # setup happens. It runs in a subshell with `set +e` and every failure returns; the caller
    # warns and continues. A `die` here would let a package-manager problem this script does not
    # own stop .env and the virtualenv from being made at all.
    body = SETUP[SETUP.index("_install_gcloud() ("):SETUP.index("if have gcloud;")]
    assert "set +e" in body, "the install runs under the script's errexit"
    assert "die " not in body, "a failed optional install kills setup"
    call = SETUP[SETUP.index("if have gcloud;"):]
    assert "if _install_gcloud && have gcloud; then" in call
    assert "setup continues without it" in call


def test_setup_refuses_to_run_apt_when_packages_are_pending_configuration():
    # apt would try to finish somebody else's half-installed packages inside our transaction, so
    # their failure becomes ours. The check must come before any apt call.
    body = SETUP[SETUP.index("_install_gcloud() ("):SETUP.index("if have gcloud;")]
    guard = body.index("dpkg -l")
    assert guard < body.index("apt-get"), "apt runs before the pending-configuration check"
    assert "will not run apt" in body
    assert "sdk.cloud.google.com" in body, "no apt-free alternative is offered"


def test_setup_installs_only_the_prerequisites_that_are_absent():
    # An apt transaction is never free: it also finishes whatever else is half-installed on the
    # machine. Installing packages that are already present buys nothing and takes that risk for
    # no reason, so absence is checked per package first.
    body = SETUP[SETUP.index("_install_gcloud() ("):SETUP.index("if have gcloud;")]
    assert "dpkg-query -W" in body, "prerequisites are installed without checking for them first"
    assert "install -y -qq ${missing}" in body


def test_the_pending_configuration_detector_matches_dpkg_state():
    # `dpkg -l` marks a package needing configuration with a desired/status pair whose second
    # character is not `i` - iU (unpacked), iF (half-configured). The awk must select exactly those.
    sample = ("ii  good-package   1.0  amd64  fine\n"
              "iU  unpacked-pkg   1.0  amd64  needs configuring\n"
              "iF  halfconf-pkg   1.0  amd64  failed configuring\n")
    out = subprocess.run(["awk", '$1 ~ /^i[^i]/ {print $2}'], input=sample,
                         capture_output=True, text=True).stdout.split()
    assert out == ["unpacked-pkg", "halfconf-pkg"], out
