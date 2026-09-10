# Responsibility: Verify every status write is compare-and-set, and no engine name is branched on outside engines.
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).parents[3]
SRC = REPO / "src" / "meshpipeline"


def _files(*pkgs: str) -> list[Path]:
    roots = [SRC / p for p in pkgs] if pkgs else [SRC]
    return [p for root in roots for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _rel(p: Path) -> str:
    return p.relative_to(SRC).as_posix()


# 1. no unrestricted terminal-status writer

# Named repository bypasses that must never exist: an UNCONDITIONAL `UPDATE ... SET status=`
# reachable from application code lets a stale worker overwrite a terminal result last-writer-wins.
# set_status was the original; admin_force_status was a zero-caller "operator recovery" variant
# removed for the same reason (out-of-band recovery is a DBA action, not a callable method).
_FORBIDDEN_STATUS_BYPASSES = ("set_status", "admin_force_status")


def test_no_unrestricted_status_bypass_method_exists():
    offenders = []
    for p in _files():
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _FORBIDDEN_STATUS_BYPASSES:
                offenders.append(f"{_rel(p)}:{node.lineno} def {node.name}")
            elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_STATUS_BYPASSES:
                offenders.append(f"{_rel(p)}:{node.lineno} .{node.attr}")
    assert not offenders, (
        f"an unrestricted status bypass is back - status writes must go through the CAS "
        f"transition: {offenders}")


def _has_status_in_predicate(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "in_"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "status"):
            return True
    return False


def _writes_status(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "values" and any(kw.arg == "status" for kw in node.keywords):
                return True
            if node.func.attr == "_timestamps_for":
                return True
    return False


def test_every_status_writing_update_is_cas_guarded():
    offenders = []
    for p in _files():
        for fn in ast.walk(ast.parse(p.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _writes_status(fn) and not _has_status_in_predicate(fn):
                    offenders.append(f"{_rel(p)}:{fn.lineno} {fn.name}")
    assert not offenders, (
        "status-writing UPDATE(s) without a compare-and-set status predicate - an unconditional "
        f"status write is an escape hatch that can overwrite a terminal result: {offenders}")


# 2. terminal states are never an ordinary transition source

def test_terminal_states_are_never_a_legal_transition_source():
    from meshpipeline.persistence.job_state import LEGAL_SOURCES, TERMINAL_STATES
    for target, sources in LEGAL_SOURCES.items():
        assert not (sources & TERMINAL_STATES), (
            f"target {target} lists a TERMINAL source {sources & TERMINAL_STATES}: an ordinary "
            "transition could reopen a finished job")


# 3. no engine-policy conditional outside engines/

def _engine_names() -> set[str]:
    from meshpipeline.engines.registry import all_engine_names
    return set(all_engine_names())


# Files OUTSIDE engines/ permitted to COMPARE against an engine name, each with a reason. A branch
# on engine identity is engine POLICY and belongs on the EngineSpec - this list stays near-empty.
_ENGINE_COMPARE_ALLOWED: dict[str, str] = {}


def _engine_name_compares(tree: ast.AST, names: set[str]) -> list[int]:
    hits: list[int] = []

    def _consts(n: ast.AST) -> list[str]:
        out: list[str] = []
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, (ast.Tuple, ast.List, ast.Set)):
            for e in n.elts:
                out += _consts(e)
        return out

    for node in ast.walk(tree):
        operands: list[ast.AST] = []
        if isinstance(node, ast.Compare):            # engine == "snappy" / engine in ("snappy",...)
            operands = [node.left, *node.comparators]
        elif isinstance(node, ast.match_case) and isinstance(node.pattern, ast.MatchValue):
            operands = [node.pattern.value]          # case "snappy":
        if any(c in names for op in operands for c in _consts(op)):
            hits.append(getattr(node, "lineno", 0))
    return hits


def test_no_engine_name_conditional_outside_engines():
    names = _engine_names()
    offenders = {}
    for p in _files():
        rel = _rel(p)
        if rel.startswith("engines/") or rel in _ENGINE_COMPARE_ALLOWED:
            continue
        lines = _engine_name_compares(ast.parse(p.read_text()), names)
        if lines:
            offenders[rel] = lines
    assert not offenders, (
        "engine-name conditional(s) outside engines/ - move the policy onto the EngineSpec or, if "
        f"legitimate, all-list with a reason in _ENGINE_COMPARE_ALLOWED: {offenders}")


# 4. job-mutating API routes require auth

# Mutating routes allowed to carry no `owner_dep`, each keyed as `<path>:<function>` with its
# reason. This list stays at one entry: `owner_dep` proves an ALREADY-established identity, so the
# only route that can legitimately lack it is the one whose job is to establish identity in the
# first place. Every other POST/PATCH/PUT/DELETE acts for a caller who is already known.
_UNAUTHENTICATED_ROUTE_ALLOWED: dict[str, str] = {
    "api/auth.py:create_session": (
        "POST /auth/session IS the identity-establishing endpoint: it exchanges an Identity "
        "Platform ID token for this API's identity, so there is no established owner for it to "
        "depend on - requiring one would make signing in require being signed in. It is not "
        "ungated: it checks MESH_API_KEY before anything else and then refuses any token that "
        "does not verify. test_the_identity_establishing_route_is_still_gated below keeps that "
        "true, so this exception cannot quietly become an open door."),
}


def test_job_mutating_routes_require_authentication():
    mutating = {"post", "patch", "put", "delete"}
    offenders = []
    for p in _files("api"):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_mutating = any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and d.func.attr in mutating
                and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
                for d in node.decorator_list)
            if not is_mutating:
                continue
            # auth = a parameter defaulted to Depends(owner_dep)
            has_owner_dep = any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "Depends"
                and d.args and isinstance(d.args[0], ast.Name) and d.args[0].id == "owner_dep"
                for d in node.args.defaults + node.args.kw_defaults if d is not None)
            if not has_owner_dep and f"{_rel(p)}:{node.name}" not in _UNAUTHENTICATED_ROUTE_ALLOWED:
                offenders.append(f"{_rel(p)}:{node.lineno} {node.name}")
    assert not offenders, (
        "mutating route(s) without owner_dep authentication - if one of these ESTABLISHES "
        "identity rather than assuming it, declare it in _UNAUTHENTICATED_ROUTE_ALLOWED with "
        f"the reason: {offenders}")


def test_the_identity_establishing_route_is_still_gated():
    # The one route excused from owner_dep is excused because it has no owner yet, NOT because
    # it is open. Its gate is the internal API key, checked before the token is even looked at.
    src = (SRC / "api" / "auth.py").read_text()
    assert "api/auth.py:create_session" in _UNAUTHENTICATED_ROUTE_ALLOWED
    assert "polcfg.MESH_API_KEY" in src and "compare_digest" in src, (
        "POST /auth/session no longer gates on MESH_API_KEY. It is exempt from owner_dep because "
        "it establishes identity; without this gate that exemption is simply an unauthenticated "
        "mutating endpoint.")


# 5. deploy manifests: bounded retries + immutable image refs

_MANIFESTS = REPO / "deploy" / "gcp" / "cloud-run"






def test_the_mesh_job_manifest_declares_exactly_zero_retries():
    import re
    job = (_MANIFESTS / "mesh-job.yaml").read_text()
    m = re.search(r"maxRetries:\s*([0-9]+)", job)
    assert m, "the mesh job manifest declares no integer maxRetries (unbounded)"
    assert int(m.group(1)) == 0, (
        f"maxRetries is {m.group(1)}, not 0 - add a worker epoch/lease to the terminal transition "
        "before raising this.")


def test_the_mesh_manifest_image_is_not_a_floating_tag():
    text = (_MANIFESTS / "mesh-job.yaml").read_text()
    assert "${" in text and "IMAGE" in text, "mesh-job.yaml: image is not a rendered variable"
    assert ":latest" not in text, "mesh-job.yaml: pins a floating :latest tag (not immutable)"


def _manifest_env_int(manifest: str, name: str) -> int:
    import re
    m = re.search(rf"name:\s*{re.escape(name)}\s*\n\s*value:\s*\"?(\d+)\"?", manifest)
    assert m, f"env {name} not found (as an integer) in the manifest"
    return int(m.group(1))




# 6. hosted config cannot create an unsafe DB budget silently
