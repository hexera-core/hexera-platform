# Responsibility: Keep the admin stage's drift check aware of the settings the stage itself binds.
# Owns: the DECLARED_ENV_NAMES assertions over create-admin-service.sh.
# Boundaries: read-only inspection of the script; it calls no cloud and deploys nothing.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
ADMIN = REPO / "deploy" / "gcp" / "scripts" / "create-admin-service.sh"
CONSOLE = REPO / "deploy" / "gcp" / "scripts" / "create-console-service.sh"

#: Every runtime variable the admin stage binds with --set-secrets. Each is on the service exactly
#: as much as one set with --set-env-vars, so each must be declared or the drift check reports it.
SECRET_RUNTIME_VARS = (
    "GOOGLE_CLIENT_SECRET",
    "OUTREACH_DB_PASSWORD",
    "ANTHROPIC_API_KEY",
    "APOLLO_API_KEY",
    "VERIFIER_API_KEY",
)


def test_the_secret_loop_declares_the_names_it_binds():
    """THE BUG THIS FILE EXISTS FOR.

    The drift check compares the live service's env names against DECLARED_ENV_NAMES and refuses to
    drop anything it does not find. The secret loop bound five names onto the service and declared
    none of them, so on the NEXT run those five read back as settings "this deployment does not
    declare" - and the stage refused every service it had ever successfully deployed. prod-admin
    could not be rolled at all, and the error pointed at ADMIN_ENV_PRUNE as though discarding a
    working credential were the remedy.
    """
    text = ADMIN.read_text(encoding="utf-8")
    loop = text[text.index("ADMIN_SECRET_BINDINGS=()"):text.index("# REFUSE A KNOWN-ABSENT")]
    assert 'DECLARED_ENV_NAMES+=("${runtime_var}")' in loop, (
        "the admin secret loop binds runtime variables without declaring them, so the drift check "
        "below reports settings this stage bound itself and refuses to redeploy")


def test_declared_names_are_not_reset_after_the_secret_loop():
    """Order is the contract. Appending in the secret loop achieves nothing if the array is
    re-initialised further down - which is exactly how this failed: `DECLARED_ENV_NAMES=()` sat
    beside ADMIN_ENV_PAIRS, after the secret names had already been collected, and discarded them.
    """
    text = ADMIN.read_text(encoding="utf-8")
    init_positions = [m.start() for m in re.finditer(r"^DECLARED_ENV_NAMES=\(\)", text, re.M)]
    assert len(init_positions) == 1, (
        f"DECLARED_ENV_NAMES is initialised {len(init_positions)} times; a second initialisation "
        f"discards every name collected before it")
    # The loop that ACCUMULATES, not the array declarations above it: the initialisation has to
    # precede the first `+=`, and both `+=` sites have to follow it.
    first_append = text.index('DECLARED_ENV_NAMES+=("${runtime_var}")')
    second_append = text.index('DECLARED_ENV_NAMES+=("${pair%%=*}")')
    assert init_positions[0] < first_append < second_append, (
        "DECLARED_ENV_NAMES must be initialised before the secret loop appends to it, so the "
        "secret-backed names and the plain settings accumulate into one list")


def test_every_secret_backed_runtime_var_reaches_the_declared_list():
    """The five names are declared by the loop rather than listed, so this asserts the loop's INPUT
    still contains each one - a pair quietly dropped from it would bind nothing and declare nothing,
    which looks identical to working until the setting is missing at runtime."""
    text = ADMIN.read_text(encoding="utf-8")
    loop = text[text.index("ADMIN_SECRET_BINDINGS=()"):text.index("# REFUSE A KNOWN-ABSENT")]
    for runtime_var in SECRET_RUNTIME_VARS:
        assert f'"{runtime_var}:' in loop, (
            f"{runtime_var} is no longer resolved by the admin secret loop, so it is neither bound "
            f"nor declared")


def test_the_admin_stage_matches_the_console_stage_it_was_modelled_on():
    """create-console-service.sh has always declared its secret-backed names in the same loop that
    binds them. The two stages carry the same contract; the admin one simply omitted this line. If
    the console stage ever stops doing it, this file's premise is wrong and should be revisited
    rather than silently diverging."""
    console = CONSOLE.read_text(encoding="utf-8")
    assert 'DECLARED_ENV_NAMES+=("${runtime_var}")' in console, (
        "the console stage no longer declares its secret-backed names - the pattern this file "
        "asserts for the admin stage no longer has a reference implementation")
