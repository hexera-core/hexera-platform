# Responsibility: Turn the application's database URL into the synchronous one migrations run over.
# Boundaries: driver swap and explicit TLS posture.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SYNC_DRIVER = "postgresql+psycopg2"

# libpq sslmode values, split by whether they actually ENFORCE TLS. When the application policy
# says TLS is mandatory (DB_SSL_REQUIRED), only the enforcing set may survive - an explicit URL
# value must never be able to weaken a mandatory security policy, so the non-enforcing ones are
# rejected rather than silently upgraded or accepted.
_TLS_ENFORCING_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})
_NON_TLS_SSLMODES = frozenset({"disable", "allow", "prefer"})
_ALL_SSLMODES = _TLS_ENFORCING_SSLMODES | _NON_TLS_SSLMODES

# asyncpg spells the same policy with an `ssl` parameter. Its SECURITY MEANING is translated to
# the libpq equivalent before the key is removed - never dropped silently.
_ASYNCPG_SSL_ALIASES = {"true": "require", "1": "require", "yes": "require",
                        "false": "disable", "0": "disable", "no": "disable"}


class UnsupportedMigrationURL(ValueError):
    pass


def _one(value):
    return value[-1] if isinstance(value, (list, tuple)) else value


def _normalise_sslmode(raw) -> str:
    return str(_one(raw)).strip().lower()


def _asyncpg_ssl_to_sslmode(raw) -> str:
    v = _normalise_sslmode(raw)
    return _ASYNCPG_SSL_ALIASES.get(v, v)


def sync_migration_url(source: str | None = None, ssl_required: bool | None = None) -> str:
    from sqlalchemy.engine import make_url

    import meshpipeline.settings.providers as provcfg

    if source is None:
        source = provcfg.DATABASE_URL or provcfg.POSTGRES_DSN
    if ssl_required is None:
        ssl_required = provcfg.DB_SSL_REQUIRED

    try:
        url = make_url(source)
    except Exception as exc:  # noqa: BLE001 - surface a clear, actionable message
        raise UnsupportedMigrationURL(f"invalid database URL for migrations: {exc}") from exc
    if url.get_backend_name() != "postgresql":
        raise UnsupportedMigrationURL(
            f"unsupported database backend {url.get_backend_name()!r} for migrations - "
            "only PostgreSQL is supported")

    # async driver -> sync psycopg2. .set() rebuilds the URL from parsed components, so a literal
    # "+asyncpg" inside an (encoded) username/password is untouched - only the drivername changes.
    url = url.set(drivername=_SYNC_DRIVER)

    query = dict(url.query)
    sslmode = _normalise_sslmode(query["sslmode"]) if "sslmode" in query else None

    # asyncpg-only `ssl`: translate its SECURITY MEANING to the libpq equivalent, then drop the
    # key. A disagreement between the two spellings is a configuration error, not a coin toss.
    if "ssl" in query:
        translated = _asyncpg_ssl_to_sslmode(query.pop("ssl"))
        if translated not in _ALL_SSLMODES:
            raise UnsupportedMigrationURL(
                f"unrecognised ssl={translated!r} in the database URL - expected one of "
                f"{sorted(_ALL_SSLMODES)} (or true/false)")
        if sslmode is None:
            sslmode = translated
        elif sslmode != translated:
            raise UnsupportedMigrationURL(
                f"conflicting SSL settings in the database URL: sslmode={sslmode!r} vs "
                f"ssl={translated!r} - set exactly one, or make them agree")

    if sslmode is not None and sslmode not in _ALL_SSLMODES:
        raise UnsupportedMigrationURL(
            f"invalid sslmode={sslmode!r} in the database URL - expected one of {sorted(_ALL_SSLMODES)}")

    if ssl_required:
        # FAIL CLOSED: a mandatory policy is never satisfied by a mode that only *may* use TLS.
        if sslmode is None:
            sslmode = "require"
        elif sslmode in _NON_TLS_SSLMODES:
            raise UnsupportedMigrationURL(
                f"TLS is required for this database (DB_SSL_REQUIRED), but the URL requests "
                f"sslmode={sslmode!r}, which does not enforce TLS. Use sslmode=require, verify-ca "
                "or verify-full, or remove sslmode from the URL.")
        # otherwise it is already an enforcing mode - accept it exactly as given (never downgraded)

    if sslmode is not None:
        query["sslmode"] = sslmode
    else:
        query.pop("sslmode", None)
    url = url.set(query=query)

    # Report the TLS POSTURE accurately - and never the URL or credentials.
    mode = url.query.get("sslmode")
    if mode in _TLS_ENFORCING_SSLMODES:
        logger.info("migration DB connection: TLS enforced (sslmode=%s)", mode)
    elif mode:
        logger.warning("migration DB connection: TLS NOT enforced (sslmode=%s)", mode)
    return url.render_as_string(hide_password=False)
