# Responsibility: Verify the catalogue accounts for every fixed setting the application reads.
# Boundaries: it measures the real source and the real generated surfaces; it asserts on no prose.
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
sys.path.insert(0, str(REPO / "devtools" / "quality"))

import config_inventory as ci  # noqa: E402

from meshpipeline.settings import inventory as cat  # noqa: E402


@pytest.fixture(scope="module")
def m() -> dict:
    return ci.measure()


# the fixed-name contract - the one this checkpoint establishes

def test_every_fixed_read_is_declared(m):
    assert m["fixed_undeclared"] == [], (
        "the application reads settings the catalogue does not declare: " + str(m["fixed_undeclared"]))


def test_no_direct_lookup_bypasses_the_loader(m):
    assert m["direct_bypasses"] == [], (
        "a module reads the environment without going through settings/env.py: "
        + ", ".join(f"{b['file']}:{b['line']}" for b in m["direct_bypasses"]))


def test_every_fixed_read_uses_an_approved_reader(m):
    assert m["approved_reader_reads"] == m["fixed_occurrences"]


def test_no_caller_default_disagrees_with_another_caller(m):
    assert m["caller_default_conflicts"] == {}


def test_no_call_site_default_disagrees_with_the_catalogue(m):
    assert m["catalogue_default_conflicts"] == [], (
        "a call-site fallback is a second, independently editable default: "
        + str(m["catalogue_default_conflicts"]))


def test_no_catalogue_entry_lacks_a_consumer(m):
    # An entry declared as read by the application but never read is either dead or misclassified;
    # one whose consumer is compose/make/sdk is expected not to appear in a scan of src/.
    assert m["unexplained_catalogue_entries"] == []


def test_live_and_removed_are_disjoint(m):
    assert m["live_removed_overlap"] == []


def test_the_fixed_name_certification_passes(m):
    assert ci.fixed_failures(m) == []


# full certification - no supported configuration exists outside the catalogue

def test_no_unsupported_dynamic_configuration_consumer_remains(m):
    assert m["unresolved_dynamic_consumers"] == [], (
        "an environment name is still built from data: "
        + ", ".join(f"{d['file']}:{d['line']}" for d in m["unresolved_dynamic_consumers"]))


def test_the_full_certification_passes(m):
    assert ci.full_failures(m) == []


def test_the_full_certification_command_exits_zero():
    r = subprocess.run([sys.executable, str(REPO / "devtools/quality/config_inventory.py"), "--full"],
                       cwd=str(REPO), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FULL CERTIFICATION: PASS" in r.stdout


def test_the_fixed_command_exits_zero():
    r = subprocess.run([sys.executable, str(REPO / "devtools/quality/config_inventory.py")],
                       cwd=str(REPO), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FIXED-NAME CERTIFICATION: PASS" in r.stdout


def test_the_measurement_is_machine_readable_and_deterministic():
    def run():
        r = subprocess.run([sys.executable, str(REPO / "devtools/quality/config_inventory.py"), "--json"],
                           cwd=str(REPO), capture_output=True, text=True)
        assert r.returncode == 0
        return r.stdout
    assert json.loads(run())["fixed_undeclared"] == []
    assert run() == run()


# permitted dynamic mechanisms, both structural

def test_only_the_loader_reads_a_caller_supplied_name(m):
    # The count is not asserted - the loader may grow a reader. WHERE they live is the contract.
    files = {r["file"] for r in m["loader_reads"]}
    assert files == {"src/meshpipeline/settings/env.py"}
    assert m["loader_reads"], "the loader reads nothing - the classification is vacuous"


def test_the_retired_inspection_is_catalogue_derived_and_sanctioned(m):
    # Retired configuration is now recognised by handing the environment to the catalogue's own
    # retired_present(), which returns names and guidance and never resolves a value.
    insp = m["retired_inspections"]
    assert insp, "no retired-configuration inspection found - removed settings would be ignored"
    assert all(i["sanctioned"] for i in insp), f"unsanctioned inspection: {insp}"
    assert {i["file"] for i in insp} == {"src/meshpipeline/runtime/startup.py"}


# exposure metadata explains every omission from the template

def test_the_template_is_exactly_the_template_exposed_entries():
    rendered = cat.render_env()
    emitted = {ln.split("=", 1)[0] for ln in rendered.splitlines()
               if ln and not ln.startswith("#") and "=" in ln}
    assert emitted == {v.name for v in cat.template_vars()}


def test_nothing_is_omitted_from_the_template_without_saying_why():
    for v in cat.all_vars():
        assert v.exposure in cat.EXPOSURES
        if v.exposure != "template":
            assert v.help or v.consumer != "app" or v.exposure == "internal", (
                f"{v.name} is kept out of the template with no stated reason")


def test_internal_entries_are_absent_from_the_template():
    rendered = cat.render_env()
    for v in cat.all_vars():
        if v.exposure == "internal":
            assert f"\n{v.name}=" not in rendered, f"{v.name} is internal but was rendered"


def test_external_entries_are_absent_from_the_template():
    rendered = cat.render_env()
    for v in cat.all_vars():
        if v.exposure == "external":
            assert f"\n{v.name}=" not in rendered, f"{v.name} is external but was rendered"


def test_secrets_render_only_safe_placeholders():
    rendered = cat.render_env()
    for v in cat.all_vars():
        if not (v.secret and v.exposure == "template"):
            continue
        line = next((ln for ln in rendered.splitlines() if ln.startswith(f"{v.name}=")), None)
        assert line is not None, f"{v.name} is a template secret but is not in the template"
        value = line.split("=", 1)[1]
        # A local-stack seed (minio's own published default, the dev postgres password) is not a
        # credential; anything that looks like a real one must never be generated.
        assert not any(t in value.lower() for t in ("sk-", "key-", "tvly-", "http", "@", "bearer")), (
            f"{v.name} renders something credential-shaped: {value!r}")
        assert len(value) < 32, f"{v.name} renders a long literal that looks like a real secret"


def test_a_secret_falls_back_to_empty_in_code_even_when_the_template_seeds_one():
    for v in cat.all_vars():
        if v.secret and v.runtime_default is not None:
            assert v.runtime_default == "", (
                f"{v.name} declares a non-empty code fallback for a secret")


def test_generation_is_deterministic_and_free_of_duplicates():
    once, twice = cat.render_env(), cat.render_env()
    assert once == twice
    names = [ln.split("=", 1)[0] for ln in once.splitlines() if ln and not ln.startswith("#") and "=" in ln]
    assert len(names) == len(set(names))


def test_no_machine_specific_path_reaches_the_template():
    # "Machine-specific" is the test, not "absolute": OPENFOAM_BASHRC names a fixed location
    # inside the mesh image and is the same on every machine. What must never be generated is a
    # value that only makes sense on the machine that rendered it - a checkout path or a home
    # directory - which is exactly what a cwd-derived default produced before this batch.
    home = str(Path.home())
    for ln in cat.render_env().splitlines():
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        value = ln.split("=", 1)[1]
        assert str(REPO) not in value, f"a checkout path reached the template: {ln}"
        assert home not in value, f"a home directory reached the template: {ln}"
        assert "/tmp/" not in value, f"a scratch path reached the template: {ln}"


def test_the_data_layout_defaults_are_relative():
    # These are the ones resolved against the working directory, which is what lets one value be
    # correct both in a checkout and at WORKDIR /srv in the image.
    for name in ("DATA_ROOT", "WORKSPACE_BASE", "STATIC_DIR"):
        default = cat.get(name).default
        assert default.startswith("./"), f"{name} default {default!r} is not working-directory relative"
    for name in ("JOBS_DIR", "CORPUS_DIR"):
        assert cat.get(name).derived, f"{name} should derive from DATA_ROOT rather than restate it"


def test_a_derived_default_names_only_declared_settings():
    names = {v.name for v in cat.all_vars()}
    for v in cat.all_vars():
        if not v.derived:
            continue
        for token in v.derived.split("<")[1:]:
            assert token.split(">")[0] in names, f"{v.name} derives from an undeclared setting"


def test_a_derived_default_follows_the_setting_it_derives_from():
    got = cat.resolve_default("JOBS_DIR", lambda n: {"DATA_ROOT": "/somewhere/else"}[n])
    assert got == "/somewhere/else/jobs"
    got = cat.resolve_default("CORPUS_DIR", lambda n: {"DATA_ROOT": "/somewhere/else"}[n])
    assert got == "/somewhere/else/corpus"


# the removed-name authority

def test_the_catalogue_is_the_only_removed_name_authority():
    # The retired data-layout tuple used to live in settings/runtime.py. Nothing outside the
    # catalogue may keep a second list, so no production module may name those strings itself.
    retired = ("OUTPUTS_BASE", "UPLOADS_BASE", "UPLOAD_STAGING_ROOT",
               "RUNTIME_DATA_ROOT", "RUNTIME_SAMPLES_DIR")
    for name in retired:
        assert name in cat.REMOVED, f"{name} lost its refusal when the tuple was merged"
    # A comment may DISCUSS the retired names (settings/runtime.py explains why they moved); only
    # a string literal can be a second list, so the scan reads literals from the parsed tree.
    import ast
    src = subprocess.run(["git", "ls-files", "src/meshpipeline"], cwd=str(REPO),
                         capture_output=True, text=True, check=True).stdout.split()
    for rel in [f for f in src if f.endswith(".py")]:
        if rel.endswith("settings/inventory.py"):
            continue                       # the authority itself
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        clash = sorted(set(retired) & literals)
        assert clash == [], f"{rel} keeps a second copy of the retired name(s) {clash}"


def test_the_generated_roster_in_the_documentation_is_current():
    doc = (REPO / "docs/reference/configuration.md").read_text(encoding="utf-8")
    block = cat.render_reference()
    start = doc.find(cat.REFERENCE_BEGIN)
    end = doc.find(cat.REFERENCE_END)
    assert start != -1 and end != -1, "the generated settings roster is missing from the document"
    assert doc[start:end + len(cat.REFERENCE_END)] == block, (
        "the settings roster is stale - regenerate with "
        "`python -m meshpipeline.settings.inventory --reference`")


# STATIC_DIR: one relative default that is correct in both places it runs

def _resolved(cwd: Path, **env) -> dict:
    src = str(REPO / "src")
    code = (f"import sys;sys.path.insert(0,{src!r});"
            "import meshpipeline.settings.runtime as r;"
            "import json;print(json.dumps({'static':r.STATIC_DIR,'data':str(r.DATA_ROOT),"
            "'jobs':str(r.JOBS_DIR),'ws':str(r.WORKSPACE_BASE)}))")
    base = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "POSTGRES_PASSWORD": "x", "ENV": "dev"}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env={**base, **env},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout)


def test_a_host_checkout_resolves_the_repository_ui():
    # The defect this replaces: /srv/ui does not exist on a host, so the API logged a warning and
    # served no UI at all. The relative default finds the checkout's ui/ instead.
    got = _resolved(REPO)
    assert got["static"] == str((REPO / "ui").resolve())
    assert (Path(got["static"]) / "index.html").is_file(), "the resolved host UI has no index.html"


def test_the_container_layout_resolves_the_installed_ui(tmp_path):
    # The image sets WORKDIR /srv and copies the client to /srv/ui, so the SAME relative default
    # resolves to the installed directory there. Simulated by a working directory that has ui/.
    (tmp_path / "ui").mkdir()
    got = _resolved(tmp_path)
    assert got["static"] == str(tmp_path / "ui")
    assert got["data"] == str(tmp_path / "data")
    assert got["ws"] == str(tmp_path / "workspaces")


def test_an_explicit_override_still_wins(tmp_path):
    got = _resolved(tmp_path, STATIC_DIR="/custom/ui")
    assert got["static"] == "/custom/ui"


def test_the_dockerfile_layout_agrees_with_the_resolved_container_path():
    # Where the image actually puts the client, checked against the value the relative default
    # produces under that WORKDIR - so the two cannot drift apart silently.
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "WORKDIR /srv" in dockerfile
    assert "COPY ui/ ./ui/" in dockerfile
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./ui:/srv/ui:ro" in compose, "compose no longer mounts the client where the image expects it"


def test_a_missing_ui_directory_disables_the_route_without_crashing(tmp_path):
    # The established behaviour for a configured directory that is not there: the app still serves
    # its API and says so, rather than refusing to boot or pretending to have a UI.
    src = str(REPO / "src")
    code = (f"import sys;sys.path.insert(0,{src!r});"
            "import meshpipeline.api.app as a;"
            "print('MOUNTED' if any(getattr(r,'path','')=='/ui' for r in a.app.routes) else 'DISABLED')")
    base = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "POSTGRES_PASSWORD": "x", "ENV": "dev"}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                       env={**base, "STATIC_DIR": str(tmp_path / "absent")},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
    assert "DISABLED" in r.stdout
    assert "not found" in r.stderr or "DISABLED" in r.stdout


def test_the_ui_route_is_mounted_when_the_directory_exists():
    src = str(REPO / "src")
    code = (f"import sys;sys.path.insert(0,{src!r});"
            "import meshpipeline.api.app as a;"
            "print('MOUNTED' if any(getattr(r,'path','')=='/ui' for r in a.app.routes) else 'DISABLED')")
    base = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "POSTGRES_PASSWORD": "x", "ENV": "dev"}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=base,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
    assert "MOUNTED" in r.stdout, "a host checkout no longer serves the UI"


# every removed name refuses, from the one authority

@pytest.mark.parametrize("name", sorted(cat.REMOVED))
def test_every_removed_name_refuses_startup(name):
    src = str(REPO / "src")
    code = (f"import sys;sys.path.insert(0,{src!r});"
            "from meshpipeline.runtime import startup;startup.validate();print('STARTED')")
    base = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "POSTGRES_PASSWORD": "x", "ENV": "dev"}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env={**base, name: "anything"},
                       capture_output=True, text=True)
    assert r.returncode != 0, f"{name} is removed but the process started"
    assert "STARTED" not in r.stdout, f"{name} was refused only after startup completed"
    assert name in r.stderr, f"the refusal for {name} does not name it"
    assert cat.REMOVED[name][:40] in r.stderr, f"the refusal for {name} gives no actionable reason"
    assert "anything" not in r.stderr, f"the refusal for {name} echoed the configured value"


# every declared route setting is explicit, and reaches exactly its own typed field

ROUTE_FIELD = {
    "PROVIDER": lambda r: r.primary.provider,
    "MODEL": lambda r: r.primary.model,
    "ACCOUNT": lambda r: r.primary.account,
    "TIMEOUT": lambda r: r.timeout_s,
    "MAX_ATTEMPTS": lambda r: r.retry.max_attempts,
    "BACKOFF_BASE": lambda r: r.retry.backoff_base_s,
    "BACKOFF_MAX": lambda r: r.retry.backoff_max_s,
    "CONCURRENCY_BUDGET": lambda r: r.concurrency_budget,
    "QUEUE_DEADLINE": lambda r: r.queue_deadline_s,
}


def test_the_route_matrix_is_fully_declared():
    names = {v.name for v in cat.all_vars()}
    expected = {cat.route_setting_name(row[1], suffix)
                for row in cat.ROUTE_MATRIX for suffix, _, _ in cat.ROUTE_SUFFIXES}
    assert len(expected) == len(cat.ROUTE_MATRIX) * len(cat.ROUTE_SUFFIXES)
    missing = sorted(expected - names)
    assert missing == [], f"declared route settings missing from the catalogue: {missing}"


def test_every_route_setting_reaches_the_template():
    rendered = cat.render_env()
    for row in cat.ROUTE_MATRIX:
        for suffix, _, _ in cat.ROUTE_SUFFIXES:
            name = cat.route_setting_name(row[1], suffix)
            assert f"\n{name}=" in rendered, f"{name} is supported but absent from .env.example"


def _routes(**env) -> dict:
    src = str(REPO / "src")
    code = (f"import sys;sys.path.insert(0,{src!r});import json;"
            "from meshpipeline.adapters.model_inference.routes import all_routes;"
            "print(json.dumps({k:{'provider':v.primary.provider,'model':v.primary.model,"
            "'account':v.primary.account,'timeout':v.timeout_s,'attempts':v.retry.max_attempts,"
            "'bo_base':v.retry.backoff_base_s,'bo_max':v.retry.backoff_max_s,"
            "'budget':v.concurrency_budget,'queue':v.queue_deadline_s} for k,v in all_routes().items()}))")
    base = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "POSTGRES_PASSWORD": "x", "ENV": "dev"}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env={**base, **env},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-600:]
    return json.loads(r.stdout)


def test_route_defaults_match_the_declared_matrix():
    got = _routes()
    for role, prefix, provider, model, timeout, attempts, bo_b, bo_m, budget, queue in cat.ROUTE_MATRIX:
        r = got[role]
        assert (r["provider"], r["model"], r["account"]) == (provider, model, "default")
        assert (r["timeout"], r["attempts"]) == (float(timeout), int(attempts))
        assert (r["bo_base"], r["bo_max"]) == (float(bo_b), float(bo_m))
        assert (r["budget"], r["queue"]) == (int(budget), float(queue))


@pytest.mark.parametrize("suffix", sorted(ROUTE_FIELD))
def test_an_override_reaches_only_its_own_route_and_field(suffix):
    # One route is overridden; every other route AND every other field must be untouched.
    role, prefix = cat.ROUTE_MATRIX[1][0], cat.ROUTE_MATRIX[1][1]      # builder
    value = {"PROVIDER": "deepseek", "MODEL": "sentinel-model", "ACCOUNT": "sentinel",
             "TIMEOUT": "77.5", "MAX_ATTEMPTS": "9", "BACKOFF_BASE": "3.5",
             "BACKOFF_MAX": "44.5", "CONCURRENCY_BUDGET": "13", "QUEUE_DEADLINE": "6.5"}[suffix]
    baseline = _routes()
    got = _routes(**{cat.route_setting_name(prefix, suffix): value})
    key = {"PROVIDER": "provider", "MODEL": "model", "ACCOUNT": "account", "TIMEOUT": "timeout",
           "MAX_ATTEMPTS": "attempts", "BACKOFF_BASE": "bo_base", "BACKOFF_MAX": "bo_max",
           "CONCURRENCY_BUDGET": "budget", "QUEUE_DEADLINE": "queue"}[suffix]
    expected = value if suffix in ("PROVIDER", "MODEL", "ACCOUNT") else (
        int(value) if suffix in ("MAX_ATTEMPTS", "CONCURRENCY_BUDGET") else float(value))
    assert got[role][key] == expected, f"{prefix}_{suffix} did not reach {key}"
    for other_field in set(baseline[role]) - {key}:
        assert got[role][other_field] == baseline[role][other_field], (
            f"{prefix}_{suffix} also changed {other_field}")
    for other_role in set(baseline) - {role}:
        assert got[other_role] == baseline[other_role], (
            f"{prefix}_{suffix} leaked into the {other_role} route")


def test_an_unknown_route_cannot_be_constructed():
    from meshpipeline.settings.routes import UnknownRoute, route_from_catalogue
    with pytest.raises(UnknownRoute, match="not a declared model route"):
        route_from_catalogue("no_such_role", circuit_group="x")


def test_an_unknown_route_setting_cannot_be_requested():
    from meshpipeline.settings import routes as R
    with pytest.raises(R.UnknownRoute, match="not a declared route setting"):
        R._value("builder", "NOT_A_SUFFIX")


def test_a_non_numeric_route_value_refuses():
    src = str(REPO / "src")
    code = (f"import sys;sys.path.insert(0,{src!r});"
            "from meshpipeline.adapters.model_inference.routes import all_routes;all_routes()")
    base = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "POSTGRES_PASSWORD": "x",
            "ENV": "dev", "BUILDER_TIMEOUT": "not-a-number"}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=base,
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "BUILDER_TIMEOUT" in r.stderr and "not a number" in r.stderr


def test_the_certifier_itself_still_rejects_each_former_violation_class():
    # A clean tree cannot tell a working certifier from a weakened one - there is nothing for it
    # to catch. So the certifier's own logic is exercised against synthetic measurements, one per
    # class that Batch 4b eliminated. Without this, deleting the dynamic-consumer check would pass.
    clean = ci.measure()
    assert ci.full_failures(clean) == [], "the real tree is not clean; fix that before reading this"

    def with_(**over):
        m = dict(clean)
        m.update(over)
        return m

    cases = {
        "unresolved dynamic consumer": with_(unresolved_dynamic_consumers=[
            {"file": "src/meshpipeline/settings/routes.py", "line": 1,
             "reader": "optional_env", "expr": "f'{p}_{s}'", "name": None, "default": None}]),
        "undeclared fixed read": with_(fixed_undeclared=["SOME_NEW_KNOB"]),
        "direct bypass": with_(direct_bypasses=[
            {"file": "src/meshpipeline/settings/routes.py", "line": 2,
             "reader": "os.getenv", "name": "X", "default": None}]),
        "catalogue/runtime default conflict": with_(catalogue_default_conflicts=[
            {"name": "X", "file": "f", "line": 1, "call_site": "a", "catalogue": "b"}]),
        "caller/caller default conflict": with_(caller_default_conflicts={"X": ["1", "2"]}),
        "unexplained catalogue entry": with_(unexplained_catalogue_entries=["X"]),
        "live/removed overlap": with_(live_removed_overlap=["X"]),
    }
    for label, m in cases.items():
        assert ci.full_failures(m), f"the certifier accepts a {label} - it is not load-bearing"


def test_a_plausible_unsupported_dynamic_price_name_is_never_read(monkeypatch):
    # The namespace is gone, so a variable shaped like the old one must have no effect at all.
    from meshpipeline.adapters.inference_telemetry import pricing
    monkeypatch.setenv("MODEL_PRICE_DEEPSEEK_DEEPSEEK_V4_PRO", "9,9,9")
    monkeypatch.delenv("MODEL_PRICE_OVERRIDES", raising=False)
    assert pricing.price_for("deepseek", "deepseek-v4-pro") == (0.435, 0.87, 0.003625)
