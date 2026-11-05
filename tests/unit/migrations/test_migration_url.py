# Responsibility: Verify the async DSN converts to a sync one with credentials, host and query preserved.
from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from meshpipeline.persistence.migration_url import UnsupportedMigrationURL, sync_migration_url


def _u(dsn, ssl=False):
    return make_url(sync_migration_url(dsn, ssl_required=ssl))


# URL parsing / driver substitution

def test_asyncpg_becomes_sync_psycopg2():
    u = _u("postgresql+asyncpg://u:p@h:5432/db")
    assert u.drivername == "postgresql+psycopg2"
    assert (u.username, u.password, u.host, u.port, u.database) == ("u", "p", "h", 5432, "db")


def test_plain_postgresql_stays_valid_sync():
    assert _u("postgresql://u:p@h/db").drivername == "postgresql+psycopg2"


def test_encoded_password_with_reserved_chars_is_preserved():
    # decoded password is  p@ss+word  (@ and + are reserved in userinfo)
    assert _u("postgresql+asyncpg://u:p%40ss%2Bword@h/db").password == "p@ss+word"


def test_literal_asyncpg_inside_credentials_is_not_altered():
    # A LITERAL '+asyncpg' in the userinfo is exactly what `dsn.replace('+asyncpg','')` corrupted
    # (password -> 'mypass', username -> 'myuser'); URL parsing keeps it. The %2B-encoded form,
    # which the naive replace happened NOT to touch, must also round-trip.
    assert _u("postgresql+asyncpg://user:my+asyncpgpass@h/db").password == "my+asyncpgpass"
    assert _u("postgresql+asyncpg://my+asyncpguser:p@h/db").username == "my+asyncpguser"
    assert _u("postgresql+asyncpg://user:my%2Basyncpgpass@h/db").password == "my+asyncpgpass"


def test_ipv6_host_is_preserved():
    u = _u("postgresql+asyncpg://u:p@[2001:db8::1]:5432/db")
    assert u.host == "2001:db8::1" and u.port == 5432


def test_neon_style_hostname_and_params_preserved():
    u = _u("postgresql://u:p@ep-cool.us-east-2.aws.neon.tech/neondb?sslmode=require", ssl=True)
    assert u.host.endswith("neon.tech") and u.query.get("sslmode") == "require"


def test_existing_non_ssl_query_param_survives():
    assert _u("postgresql+asyncpg://u:p@h/db?application_name=mesh").query.get("application_name") == "mesh"


def test_malformed_url_is_rejected_actionably():
    with pytest.raises(UnsupportedMigrationURL, match="invalid database URL"):
        sync_migration_url("://broken")


def test_unsupported_backend_is_rejected_actionably():
    with pytest.raises(UnsupportedMigrationURL, match="PostgreSQL"):
        sync_migration_url("mysql://u:p@h/db")


# TLS policy (Fix 2)

def test_tls_required_adds_sslmode_require_when_absent():
    assert _u("postgresql://u:p@ep.neon.tech/db", ssl=True).query.get("sslmode") == "require"


def test_local_development_has_no_sslmode():
    assert "sslmode" not in _u("postgresql+asyncpg://u:p@postgres/db", ssl=False).query


def test_explicit_stricter_sslmode_is_respected_not_downgraded():
    assert _u("postgresql://u:p@h/db?sslmode=verify-full", ssl=True).query.get("sslmode") == "verify-full"


def test_operator_disable_is_respected():
    assert _u("postgresql://u:p@h/db?sslmode=disable", ssl=False).query.get("sslmode") == "disable"


def test_asyncpg_only_ssl_param_is_dropped_and_sslmode_set():
    u = _u("postgresql://u:p@h/db?ssl=true", ssl=True)
    assert "ssl" not in u.query and u.query.get("sslmode") == "require"


# fail-closed TLS matrix: DB_SSL_REQUIRED must never be satisfied by a weaker mode

@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_tls_required_accepts_every_enforcing_sslmode_unchanged(mode):
    assert _u(f"postgresql://u:p@h/db?sslmode={mode}", ssl=True).query.get("sslmode") == mode


@pytest.mark.parametrize("mode", ["prefer", "allow", "disable"])
def test_tls_required_rejects_every_non_enforcing_sslmode(mode):
    with pytest.raises(UnsupportedMigrationURL, match="does not enforce TLS"):
        sync_migration_url(f"postgresql://u:p@h/db?sslmode={mode}", ssl_required=True)


@pytest.mark.parametrize("mode", ["prefer", "allow", "disable", "require", "verify-full"])
def test_tls_not_required_preserves_an_explicit_sslmode(mode):
    assert _u(f"postgresql://u:p@h/db?sslmode={mode}", ssl=False).query.get("sslmode") == mode


def test_an_invalid_sslmode_is_rejected():
    with pytest.raises(UnsupportedMigrationURL, match="invalid sslmode"):
        sync_migration_url("postgresql://u:p@h/db?sslmode=banana", ssl_required=False)


# asyncpg `ssl` parameter: meaning translated, then the key removed

@pytest.mark.parametrize("ssl_value,expected", [
    ("require", "require"), ("true", "require"), ("1", "require"),
    ("verify-ca", "verify-ca"), ("verify-full", "verify-full"),
])
def test_asyncpg_ssl_is_translated_to_the_equivalent_sslmode(ssl_value, expected):
    u = _u(f"postgresql://u:p@h/db?ssl={ssl_value}", ssl=True)
    assert u.query.get("sslmode") == expected and "ssl" not in u.query


@pytest.mark.parametrize("ssl_value", ["false", "disable", "0"])
def test_asyncpg_ssl_disable_is_rejected_when_tls_is_required(ssl_value):
    with pytest.raises(UnsupportedMigrationURL, match="does not enforce TLS"):
        sync_migration_url(f"postgresql://u:p@h/db?ssl={ssl_value}", ssl_required=True)


def test_asyncpg_ssl_disable_is_translated_when_tls_is_not_required():
    u = _u("postgresql://u:p@h/db?ssl=false", ssl=False)
    assert u.query.get("sslmode") == "disable" and "ssl" not in u.query


def test_unrecognised_ssl_value_is_rejected():
    with pytest.raises(UnsupportedMigrationURL, match="unrecognised ssl"):
        sync_migration_url("postgresql://u:p@h/db?ssl=maybe", ssl_required=False)


# conflicting spellings are a configuration error, never a silent choice

@pytest.mark.parametrize("ssl_value,sslmode", [
    ("require", "verify-full"), ("true", "disable"), ("false", "require"), ("verify-ca", "require"),
])
def test_conflicting_ssl_and_sslmode_are_rejected(ssl_value, sslmode):
    with pytest.raises(UnsupportedMigrationURL, match="conflicting SSL settings"):
        sync_migration_url(f"postgresql://u:p@h/db?ssl={ssl_value}&sslmode={sslmode}", ssl_required=False)


def test_agreeing_ssl_and_sslmode_are_accepted():
    u = _u("postgresql://u:p@h/db?ssl=require&sslmode=require", ssl=True)
    assert u.query.get("sslmode") == "require" and "ssl" not in u.query


def test_tls_required_can_never_yield_a_non_enforcing_sslmode():
    inputs = [
        "postgresql://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",
        *[f"postgresql://u:p@h/db?sslmode={m}" for m in
          ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")],
        *[f"postgresql://u:p@h/db?ssl={v}" for v in
          ("true", "false", "0", "1", "require", "disable", "verify-ca", "verify-full")],
        "postgresql://u:p@h/db?ssl=true&sslmode=disable",
    ]
    produced = []
    for dsn in inputs:
        try:
            produced.append(make_url(sync_migration_url(dsn, ssl_required=True)).query.get("sslmode"))
        except UnsupportedMigrationURL:
            continue                      # rejected - also an acceptable fail-closed outcome
    assert produced, "expected at least some accepted inputs"
    assert all(m in {"require", "verify-ca", "verify-full"} for m in produced), produced


def test_the_url_string_is_never_returned_with_a_logged_password(caplog):
    with caplog.at_level("INFO"):
        sync_migration_url("postgresql://u:sup3rsecret@ep.neon.tech/db", ssl_required=True)
    assert "sup3rsecret" not in caplog.text
    assert "TLS enforced" in caplog.text and "sslmode=require" in caplog.text


def test_a_non_enforcing_sslmode_is_reported_as_not_enforced(caplog):
    with caplog.at_level("INFO"):
        sync_migration_url("postgresql://u:p@h/db?sslmode=disable", ssl_required=False)
    assert "TLS NOT enforced" in caplog.text
    assert "TLS enforced" not in caplog.text.replace("TLS NOT enforced", "")
