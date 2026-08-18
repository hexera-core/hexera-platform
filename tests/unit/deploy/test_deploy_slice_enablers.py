# Responsibility: Verify the hosted database URL normalises and TLS defaults on, and the mesh launcher uses ADC.
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from meshpipeline.settings.env import normalize_database_url

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


# 1. DATABASE_URL normalisation
def test_neon_url_normalizes_to_asyncpg_and_strips_ssl_params():
    dsn, ssl = normalize_database_url(
        "postgresql://u:p@ep-x.aws.neon.tech/appdb?sslmode=require&channel_binding=require")
    assert dsn == "postgresql+asyncpg://u:p@ep-x.aws.neon.tech/appdb"
    assert "sslmode" not in dsn and "channel_binding" not in dsn
    assert ssl is True


def test_postgres_scheme_and_extra_params_preserved():
    dsn, ssl = normalize_database_url(
        "postgres://u:p@h:5432/db?application_name=meshapp&sslmode=require")
    assert dsn.startswith("postgresql+asyncpg://u:p@h:5432/db?")
    assert "application_name=meshapp" in dsn      # non-ssl params kept
    assert "sslmode" not in dsn
    assert ssl is True


def test_sslmode_disable_opts_out_of_tls():
    _, ssl = normalize_database_url("postgresql://u:p@localhost/db?sslmode=disable")
    assert ssl is False


def test_no_query_defaults_to_tls_required():
    dsn, ssl = normalize_database_url("postgresql://u:p@ep.neon.tech/db")
    assert dsn == "postgresql+asyncpg://u:p@ep.neon.tech/db"
    assert ssl is True


# 2. the mesh launcher uses ADC, not a key file
def test_mesh_launcher_authenticates_via_adc_not_key_file():
    src = (APP / "adapters" / "mesh_execution" / "cloud_run_client.py").read_text()
    assert "google.auth.default" in src, "mesh launcher must use ADC (runtime identity)"
    assert "from_service_account_file" not in src, (
        "mesh launcher still parses a SA key file - Cloud Run has no key, use ADC")


# 4. production validate() accepts DATABASE_URL in place of POSTGRES_PASSWORD
# With a managed-Postgres URL (Neon) the password lives IN the URL and POSTGRES_PASSWORD
# is empty - production validate() must not reject that (it would crash the API on boot).
def _validate_boots(extra: dict, tmp: Path) -> tuple[bool, str]:
    env = {
        "DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x",
        "ENV": "production", "MESH_API_KEY": "k", "USER_TOKEN_SECRET": "s",
        "CORS_ORIGINS": "https://app.example.com",
        # both data roots, or startup.validate() probes CWD-relative ./data in the checkout
        "WORKSPACE_BASE": str(tmp / "ws"),
        "DATA_ROOT": str(tmp / "data"), "CORPUS_DIR": str(tmp / "data" / "corpus"),
        # Blanked, not omitted: the process environment outranks the developer's .env, so the
        # subprocess sees the unconfigured deployment this test is about.
        "POSTGRES_PASSWORD": "", "DATABASE_URL": "",
        "PATH": os.environ.get("PATH", ""),
    }
    env.update(extra)
    code = (""
            "import meshpipeline.runtime.startup as startupcfg; startupcfg.validate()")
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    return p.returncode == 0, p.stderr


def test_production_validate_accepts_database_url_without_postgres_password(tmp_path):
    ok, err = _validate_boots(
        {"DATABASE_URL": "postgresql://u:p@ep.neon.tech/db?sslmode=require"}, tmp_path)
    assert ok, f"validate() rejected DATABASE_URL despite empty POSTGRES_PASSWORD: {err[-500:]}"


def test_production_validate_requires_a_database(tmp_path):
    ok, err = _validate_boots({}, tmp_path)     # neither DATABASE_URL nor POSTGRES_PASSWORD
    assert not ok and "DATABASE_URL" in err
