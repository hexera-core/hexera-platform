# Responsibility: Verify every setting has one authority, and the environment template matches the catalogue exactly.
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from meshpipeline.settings import inventory

REPO = Path(__file__).parents[3]
ENV_RENDERED = inventory.render_env()
COMPOSE = (REPO / "docker-compose.yml").read_text()
REVIEWER_SETTINGS = (REPO / "src/meshpipeline/agents/reviewer/settings.py").read_text()

#: THE canonical Reviewer model. The reviewer is the VISUAL reviewer - it consumes rendered mesh
#: screenshots (agents/reviewer/visual.py: _publish.screenshot / initial_screenshot_b64) - so it
#: MUST be a vision-language model. Qwen3-VL is a VLM; the THINKING variant matches the documented
#: reviewer sampling params (settings.py: "Qwen3-VL THINKING params"). Kimi-K2.5 (text/agentic) and
#: the -Instruct variant were the stale disagreements.
CANONICAL_REVIEWER_MODEL = "Qwen/Qwen3-VL-235B-A22B-Thinking"


def _compose_default(key: str) -> str | None:
    m = re.search(rf"{re.escape(key)}:\s*\$\{{{re.escape(key)}:-([^}}]+)\}}", COMPOSE)
    return m.group(1).strip() if m else None


def _runtime_default(text: str, name: str) -> str | None:
    m = re.search(rf'{re.escape(name)}[^=]*=\s*optional_env\(\s*"[^"]+",\s*"([^"]+)"', text)
    return m.group(1) if m else None


# Reviewer model parity
def test_reviewer_model_agrees_across_every_source():
    sources = {
        "generated .env": inventory.default_of("REVIEWER_MODEL"),
        "runtime settings default": _runtime_default(REVIEWER_SETTINGS, "REVIEWER_MODEL"),
    }
    assert all(v is not None for v in sources.values()), f"a source is missing the key: {sources}"
    assert set(sources.values()) == {CANONICAL_REVIEWER_MODEL}, sources


def test_compose_restates_no_application_default():
    restated = sorted({
        m.group(1) for m in re.finditer(r"^\s*([A-Z0-9_]+):\s*\$\{[A-Z0-9_]+:-", COMPOSE, re.M)
        if m.group(1) in {v.name for v in inventory.all_vars()}
    })
    assert restated == [], f"docker-compose.yml restates a default the .env already carries: {restated}"


# MinIO bucket validity
def test_the_authoritative_bucket_default_is_mesh_artifacts():
    # The settings inventory is the authority; the generated .env must carry the same value, or a
    # fresh clone comes up pointing at a different bucket than the one the code defaults to.
    assert inventory.default_of("MINIO_BUCKET") == "mesh-artifacts"
    assert re.search(r"^MINIO_BUCKET=mesh-artifacts$", ENV_RENDERED, re.M), (
        "the generated .env does not carry the authoritative MINIO_BUCKET default")


def test_minio_bucket_default_is_valid_and_lowercase(monkeypatch):
    monkeypatch.delenv("MINIO_BUCKET", raising=False)
    import meshpipeline.settings.providers as prov
    p = importlib.reload(prov)
    assert p.MINIO_BUCKET == "mesh-artifacts" == p.MINIO_BUCKET.lower()


@pytest.mark.parametrize("bad", ["Invalid_Bucket", "", "   ", "ab", "x" * 64,
                                 "a..b", "Bad-Bucket", "1.2.3.4", "-abc", "abc-", "under_score"])
def test_invalid_bucket_names_fail_closed(bad):
    import meshpipeline.settings.providers as prov
    from meshpipeline.settings.env import ConfigurationError
    with pytest.raises(ConfigurationError):
        prov._valid_bucket_name(bad, var="MINIO_BUCKET")


@pytest.mark.parametrize("ok", ["mesh-artifacts", "my.bucket", "a1b", "mesh-artifacts-0"])
def test_valid_bucket_names_pass(ok):
    import meshpipeline.settings.providers as prov
    assert prov._valid_bucket_name(ok, var="MINIO_BUCKET") == ok


def test_startup_refuses_an_uppercase_bucket(monkeypatch):
    from meshpipeline.settings.env import ConfigurationError
    monkeypatch.setenv("MINIO_BUCKET", "Invalid_Bucket")
    import meshpipeline.settings.providers as prov
    with pytest.raises(ConfigurationError, match="lowercase"):
        importlib.reload(prov)
    monkeypatch.delenv("MINIO_BUCKET", raising=False)
    importlib.reload(prov)


def test_compose_declares_no_default_the_catalogue_already_owns():
    # A Compose ${VAR:-default} is a second authority: it wins silently when .env omits the key,
    # so the catalogue's value stops being the answer. The one exception is annotated in the file.
    from meshpipeline.settings.inventory import all_vars
    declared = {v.name for v in all_vars()}
    duplicated = sorted({n for n, _ in re.findall(r"\$\{([A-Z_]+):-([^}]*)\}", COMPOSE)}
                        & declared - {"GOOGLE_ADC_FILE"})
    assert duplicated == [], (
        f"these settings have a second default in docker-compose.yml: {duplicated}")


def test_the_rendered_compose_carries_the_catalogue_value():
    from tests._compose_support import product_version, render

    from meshpipeline.settings.inventory import all_vars
    bucket = next(v.default for v in all_vars() if v.name == "MINIO_BUCKET")
    out = render(env={"MINIO_BUCKET": bucket, "GOOGLE_ADC_FILE": "/dev/null",
                      "APP_VERSION": product_version()})
    assert out.returncode == 0, f"the tracked compose file did not render:\n{out.stderr}"
    assert bucket in out.stdout


def test_compose_reads_the_env_file_rather_than_interpolating_each_key():
    import yaml
    compose = yaml.safe_load(COMPOSE)
    app_services = [n for n, sv in compose["services"].items()
                    if (sv.get("environment") or {}).get("DATA_ROOT") == "/srv/data"]
    assert app_services, "no application service found"
    for name in app_services:
        assert compose["services"][name].get("env_file") == [".env"], (
            f"{name} must read the developer's .env as a file")

    catalogued = {v.name for v in inventory.all_vars()}
    for name in app_services:
        for key, value in (compose["services"][name].get("environment") or {}).items():
            if key in catalogued and isinstance(value, str) and value == "":
                raise AssertionError(
                    f"{name} forwards {key} as an empty value, which overrides the code default")


# THE environment-file authority: one file a developer edits, one tracked template it comes from


def _tracked() -> set[str]:
    import subprocess
    return set(subprocess.run(["git", "ls-files"], cwd=REPO,
                              capture_output=True, text=True).stdout.split())


def _ignored(rel: str) -> bool:
    import subprocess
    return subprocess.run(["git", "check-ignore", "-q", rel],
                          cwd=REPO).returncode == 0


def test_the_example_is_exactly_what_the_catalogue_renders():
    # Not "contains the same keys": byte-for-byte, so a settings change that is not regenerated
    # is a failure here rather than a template that quietly drifts from the code.
    assert (REPO / ".env.example").read_text(encoding="utf-8") == inventory.render_env()


def test_the_template_is_tracked_and_the_configuration_is_ignored():
    tracked = _tracked()
    assert ".env.example" in tracked, "the template a fresh clone needs is not tracked"
    assert ".env" not in tracked, "a developer's configuration is tracked"
    assert _ignored(".env"), ".env is not gitignored"
    assert not _ignored(".env.example"), ".env.example is ignored and would not reach a clone"


def test_only_the_known_environment_templates_are_tracked():
    # ONE TEMPLATE PER RUNTIME A DEVELOPER ACTUALLY CONFIGURES, and no others. There are two
    # because there are two runtimes: the product (Python, `.env.example`, generated from
    # settings/inventory.py and checked byte-for-byte above) and the browser console (Node, its
    # own process, its own settings, none of which the inventory knows about). A single file
    # could not be generated from one authority or copied to one place.
    #
    # The rule still bites: it is an exact set, so a THIRD template - the failure this test was
    # written for, where a stray `env.example` appears and a fresh clone cannot tell which file
    # to copy - fails here.
    known = [".env.example", "apps/console/.env.example"]
    templates = sorted(f for f in _tracked()
                       if f.endswith((".env.example", "env_example.txt", "env.example")))
    assert templates == sorted(known), (
        f"tracked environment templates are not the known set: {templates}")


def test_no_retired_environment_path_is_referenced_anywhere():
    import subprocess
    retired = ("env_example.txt", "deploy/gcp/env.example", "deploy/gcp/env ", "deploy/gcp/env\"",
               "deploy/gcp/env`", "deploy/gcp/env'")
    hits = []
    for token in retired:
        out = subprocess.run(["git", "grep", "-nF", "--", token], cwd=REPO,
                             capture_output=True, text=True).stdout.strip()
        if out:
            hits.extend(f"{token}: {ln}" for ln in out.splitlines()
                        if not ln.startswith("tests/unit/settings/test_config_parity.py"))
    assert hits == [], "a retired environment path is still referenced:\n  " + "\n  ".join(hits)


def test_host_and_compose_read_the_same_root_env():
    import yaml
    compose = yaml.safe_load(COMPOSE)
    app = {n: s for n, s in compose["services"].items() if s.get("env_file")}
    assert app, "no service reads an env_file"
    for name, svc in app.items():
        assert svc["env_file"] == [".env"], f"{name} reads {svc['env_file']}, not the root .env"
    # and the host loader resolves that same file, from the package rather than the cwd
    env_src = (REPO / "src/meshpipeline/settings/env.py").read_text()
    assert 'parents[3] / ".env"' in env_src, (
        "the host loader no longer resolves the repository-root .env by absolute path")
