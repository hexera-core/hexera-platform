# Responsibility: Verify the JSON search format SearXNG must serve is configured in the repository, not in a volume.
# Boundaries: it reads the tracked settings file and compose wiring; the live smoke belongs to the deployment tier.
from __future__ import annotations

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[3]
SETTINGS = REPO / "deploy" / "searxng" / "settings.yml"
COMPOSE = REPO / "docker-compose.yml"


def _settings() -> dict:
    return yaml.safe_load(SETTINGS.read_text())


def _searxng_service() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["searxng"]


def test_the_tracked_settings_enable_the_json_format():
    # SearXNG ships `formats: [html]`, so `format=json` is answered with 403 and an HTML body -
    # which is exactly the request adapters/search/searxng.py makes. Without this override the
    # web_search tool is broken, and it was: the capability existed nowhere in the repository.
    formats = _settings()["search"]["formats"]
    assert "json" in formats, f"the JSON search format is not enabled: {formats}"
    assert "html" in formats, "the debug UI's html format was dropped"


def test_the_settings_are_mounted_over_the_generated_file():
    # The image writes its own settings.yml whenever the volume is empty, so an override that lives
    # only in the volume disappears with every reset. Mounting the tracked file is what survives one.
    mounts = _searxng_service()["volumes"]
    assert any("deploy/searxng/settings.yml:/etc/searxng/settings.yml" in m for m in mounts), (
        f"the tracked settings file is not mounted into the container: {mounts}")
    assert any(m.endswith(":ro") and "deploy/searxng/settings.yml" in m for m in mounts), (
        "the tracked settings file must be mounted read-only - the container must never rewrite it")


def test_no_secret_is_committed_in_the_settings_file():
    # SearXNG reads server.secret_key from SEARXNG_SECRET and refuses to start on its upstream
    # placeholder, so the file must carry neither.
    settings = _settings()
    assert "secret_key" not in (settings.get("server") or {}), (
        "a secret_key belongs in the environment, never in this tracked file")
    assert "ultrasecretkey" not in SETTINGS.read_text(), (
        "the upstream placeholder is refused by the server and must not be committed")


def test_compose_supplies_the_secret_from_the_environment():
    env = _searxng_service()["environment"]
    value = env["SEARXNG_SECRET"] if isinstance(env, dict) else next(
        v.split("=", 1)[1] for v in env if v.startswith("SEARXNG_SECRET="))
    assert value.startswith("${SEARXNG_SECRET"), (
        f"the secret must come from the environment so a deployment can replace it: {value!r}")
