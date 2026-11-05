# Responsibility: Read configuration from the environment, with typed accessors and clear refusals.
# Boundaries: reading and coercion; it decides no policy.
from __future__ import annotations

import os
from pathlib import Path

# The developer's local configuration is the repository root .env, four levels up from here
# (settings/ -> meshpipeline/ -> src/ -> root). An installed distribution has no checkout there,
# so the file is absent and the process environment is the only source. override=False keeps the
# precedence: process environment, then .env, then the defaults below.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parents[3] / ".env", override=False)
except ImportError:
    pass


class ConfigurationError(RuntimeError):
    pass


# The package root (src/meshpipeline/) - anchor for prompts/workspaces/upload staging.
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
PROMPTS_DIR = PROJECT_ROOT / "prompts"


def optional_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


_BOOL_TRUE = frozenset({"true", "1", "yes", "on"})
_BOOL_FALSE = frozenset({"false", "0", "no", "off"})


def bool_env(name: str, default: str) -> bool:
    # A blank value is "not configured", not a malformed boolean: an env file that carries the
    # key with nothing after the '=' is how a developer leaves a setting alone.
    raw = (os.environ.get(name) or "").strip().lower() or default.strip().lower()
    if raw in _BOOL_TRUE:
        return True
    if raw in _BOOL_FALSE:
        return False
    raise ConfigurationError(
        f"Environment variable '{name}' must be a boolean - one of true/1/yes/on or "
        f"false/0/no/off - got {os.environ.get(name)!r}.")


def declared_value(var) -> str:
    # THE loader reading a DECLARED entry's own name. This is the one sanctioned way to resolve a
    # setting whose name is not written at the call site, and it is deliberately not a generic
    # lookup: it takes a catalogue entry, not a string, so there is nothing to construct a name
    # from. A caller that does not already hold an EnvVar cannot reach the environment through it.
    return os.environ.get(var.name, var.code_default())


def load_prompt(path: Path) -> str:
    if not path.exists():
        raise ConfigurationError(
            f"Required prompt file not found: {path}\nCreate this file with the appropriate "
            "prompt content before starting the server.")
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ConfigurationError(
            f"Required prompt file is empty: {path}\nAdd the appropriate prompt content to "
            "this file before starting the server.")
    return content


def normalize_database_url(url: str) -> tuple[str, bool]:
    from urllib.parse import parse_qsl, urlsplit, urlunsplit

    u = urlsplit(url)
    libpq_ssl = {"sslmode", "channel_binding", "ssl", "sslrootcert", "sslcert", "sslkey"}
    kept = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in libpq_ssl]
    ssl_vals = {v.lower() for k, v in parse_qsl(u.query) if k in ("sslmode", "ssl")}
    ssl_required = ssl_vals != {"disable"}
    dsn = urlunsplit(("postgresql+asyncpg", u.netloc, u.path,
                      "&".join(f"{k}={v}" for k, v in kept), u.fragment))
    return dsn, ssl_required
