# Responsibility: Decide whether this machine's configuration can run the stack, and name every fault at once.
# Owns: THE credential-path resolver - the one both this gate and the Compose mount are driven from.
# Boundaries: reads .env and the credential's shape; it prints no value, edits no file and reaches no network.
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV = ROOT / ".env"

# The one setting that names a file rather than carrying a value. It is checked as a file because
# an unreadable or truncated credential is a configuration fault the developer must fix, not
# something the stack can discover halfway through a mesh.
CREDENTIAL_FILE_SETTING = "GOOGLE_ADC_FILE"

# Where the credential lives, relative to the repository - the same on every machine, so no
# document, script or message has to know a username, a home directory or an operating system.
# The VALUE is declared once in the settings catalogue; this is only its shape.
CREDENTIAL_DIR = pathlib.Path("secrets") / "gcp"
CREDENTIAL_NAME = "application_default_credentials.json"
CANONICAL_DISPLAY = f"{CREDENTIAL_DIR.as_posix()}/{CREDENTIAL_NAME}"

PLACE_IT = ("Place your Google Application Default Credentials file at:\n"
            f"{CANONICAL_DISPLAY}")


def resolve(value: str) -> pathlib.Path:
    # Anchored at the REPOSITORY ROOT, never at whatever directory the caller happens to be in.
    # `make dev-up` hands the result to Compose, so the file this validates is the file that
    # gets mounted - there is no second interpretation anywhere.
    path = pathlib.Path(value.strip())
    return path if path.is_absolute() else (ROOT / path)


def canonical() -> pathlib.Path:
    return ROOT / CREDENTIAL_DIR / CREDENTIAL_NAME


def credential_problem(value: str) -> str | None:
    resolved = resolve(value)
    if resolved != canonical():
        return (f"{CREDENTIAL_FILE_SETTING} points somewhere else. Hexera keeps the credential in"
                f" one place on every machine, and only that place is supported")
    if not resolved.exists():
        return f"there is no credential at {CANONICAL_DISPLAY}"
    if resolved.is_symlink() or not resolved.is_file():
        # A symlink is refused rather than followed: the containment this location provides is
        # only real while the bytes are inside the directory that carries its protections.
        return f"{CANONICAL_DISPLAY} is not a regular file (a symlink is not supported here)"
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        return f"{CANONICAL_DISPLAY} cannot be read ({exc.__class__.__name__})"
    if not raw.strip():
        return f"{CANONICAL_DISPLAY} is empty"
    try:
        import google.auth
        google.auth.load_credentials_from_file(str(resolved))
    except Exception as exc:
        # The failure is named; the file's content never is.
        return f"{CANONICAL_DISPLAY} is not a usable credential ({exc.__class__.__name__})"
    return None


def problems() -> list[str]:
    from meshpipeline.settings.inventory import all_vars, required_names

    if not ENV.exists():
        return ["there is no .env - run: make setup"]

    defaults = {v.name: v.default for v in all_vars()}
    required = required_names()
    env_file = parse_env(ENV.read_text(encoding="utf-8"))
    # The documented precedence, unchanged: an exported variable beats .env, and .env beats the
    # value the code falls back to.
    values = {n: (os.environ.get(n) or env_file.get(n) or defaults.get(n, "")).strip()
              for n in required}

    found: list[str] = []
    if missing := [n for n in required if not values[n]]:
        found.append("these required settings are unset in .env: " + ", ".join(missing))
    if values.get(CREDENTIAL_FILE_SETTING):
        if fault := credential_problem(values[CREDENTIAL_FILE_SETTING]):
            found.append(fault)
    return found


def parse_env(text: str) -> dict[str, str]:
    # Compose's own env_file rules: a full-line comment is a comment, anything after the first
    # '=' is the value. Nothing is stripped from inside a value, so what is checked here is what
    # the containers will actually be given.
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv)[1:]
    if "--credential-path" in args:
        # The absolute host path Compose must bind. Machine output, printed nowhere else.
        print(canonical())
        return 0

    found = problems()
    if not found:
        return 0
    out = sys.stderr
    out.write("\n  The stack was not started - it is not configured to run.\n\n")
    for f in found:
        out.write(f"    - {f}\n")
    if any(CANONICAL_DISPLAY in f or CREDENTIAL_FILE_SETTING in f for f in found):
        # Verbatim and unindented: this exact wording is the contract an operator is told to
        # follow, and the path in it is the same on every machine.
        out.write("\n" + PLACE_IT + "\n")
        out.write("\n  Obtain it with:  gcloud auth application-default login\n")
        out.write("  then move the file that command names into place. Hexera never creates it.\n")
    out.write("\n  Fix the rest by editing .env - every supported setting is in .env.example.\n")
    out.write("  For a project with no mesh job yet, provision one first: make mesh-setup\n\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
