#!/usr/bin/env python3
# Responsibility: Measure how the application actually reads configuration, and compare it to the catalogue.
# Owns: the AST scan, the classification of every read, and the two certification verdicts.
# Boundaries: it measures and reports; it changes nothing and imports no application module for its scan.

# Configuration inventory.
#
# The question this answers is "what does the code actually read, and is every one of those names
# declared?" - asked of the syntax tree rather than of a grep, because a comment that mentions
# `os.getenv` is not a read and a name built at runtime is not a name.
#
# Two verdicts, deliberately separate:
#
#   fixed   every literal-named read is declared, and every call-site fallback equals the
#           catalogue's default. This is the contract Batch 4a establishes and hygiene enforces.
#
#   full    additionally, no unsupported dynamic configuration consumer remains. This is STRICTER
#           and is expected to FAIL until the route / model-price / model-budget namespaces are
#           redesigned. Reporting that failure honestly is the point; reclassifying those three
#           sites as "approved" would turn a known gap into a false certificate.
#
#   python devtools/quality/config_inventory.py            # human report, exit 1 if `fixed` fails
#   python devtools/quality/config_inventory.py --json     # machine-readable, always exit 0
#   python devtools/quality/config_inventory.py --full     # exit 1 unless `full` passes
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = "src/meshpipeline"

#: The approved typed readers. A read through one of these is how configuration is supposed to
#: reach the application, because they are the only functions that consult the catalogue's rules.
APPROVED_READERS = ("optional_env", "bool_env")

#: Direct interpreter-level lookups. Legitimate ONLY inside the loader that implements the
#: approved readers; anywhere else they bypass the settings authority entirely.
DIRECT_FORMS = ("os.getenv", "os.environ.get", "os.environ[]")

#: THE loader. Its whole job is to read a name its caller supplies, so its own reads are
#: necessarily dynamic and necessarily direct. Exact path, not a directory: settings/ holds
#: policy.py and runtime.py too, and neither may bypass the loader.
LOADER = f"{SRC}/settings/env.py"

#: The one module allowed to inspect environment KEYS it did not write down, because what it is
#: looking for comes from the catalogue. Exact path, and the inspection must be the catalogue's own
#: retired_present() - which returns names and guidance, never a value, so it cannot become a
#: lookup. A read that resolves a value here is a bypass like any other.
REMOVED_ITERATOR = f"{SRC}/runtime/startup.py"
RETIRED_INSPECTION = "retired_present"


@dataclass
class Read:
    name: str | None
    file: str
    line: int
    reader: str
    default: str | None
    expr: str | None = None          # set when the name is not a literal

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None or k in ("name", "default")}


@dataclass
class Inventory:
    fixed: list[Read] = field(default_factory=list)
    dynamic: list[Read] = field(default_factory=list)


def _os_aliases(tree: ast.AST) -> set[str]:
    # `import os as _os` is still os. A scanner that only knows the name "os" is one rename away
    # from being blind, and settings-adjacent code really does alias it.
    names = {"os"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "os" and a.asname:
                    names.add(a.asname)
    return names


def _literal(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _string_literal(expr: str) -> str | None:
    try:
        node = ast.parse(expr, mode="eval").body
    except SyntaxError:
        return None
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _tracked_sources() -> list[str]:
    out = subprocess.run(["git", "ls-files", SRC], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout.split()
    return [rel for rel in out if rel.endswith(".py")]


def _iterates_removed_catalogue(tree: ast.AST, node: ast.AST) -> bool:
    # Structural, not positional: the read must sit inside a `for ... in <...>REMOVED` loop whose
    # iterable names the catalogue. A loop over a locally written tuple does not match, which is
    # exactly the second-authority pattern this batch removed from settings/runtime.py.
    for parent in ast.walk(tree):
        if not isinstance(parent, (ast.For, ast.comprehension, ast.GeneratorExp, ast.ListComp)):
            continue
        iters = [parent.iter] if isinstance(parent, (ast.For, ast.comprehension)) else \
                [g.iter for g in parent.generators]
        body = ([parent] if not isinstance(parent, ast.For) else parent.body)
        if not any(node is d for b in body for d in ast.walk(b)):
            continue
        for it in iters:
            if "REMOVED" in ast.unparse(it):
                return True
    return False


def scan() -> Inventory:
    inv = Inventory()
    for rel in _tracked_sources():
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        aliases = _os_aliases(tree)
        env_bases = {f"{a}.environ" for a in aliases}
        for n in ast.walk(tree):
            found: list[Read] = []
            if isinstance(n, ast.Call):
                fname = n.func.id if isinstance(n.func, ast.Name) else (
                    n.func.attr if isinstance(n.func, ast.Attribute) else None)
                if fname in APPROVED_READERS and n.args:
                    found.append(Read(_literal(n.args[0]), rel, n.lineno, fname,
                                      ast.unparse(n.args[1]) if len(n.args) > 1 else None,
                                      None if _literal(n.args[0]) else ast.unparse(n.args[0])))
                elif isinstance(n.func, ast.Attribute) and n.func.attr in ("getenv", "get"):
                    base = ast.unparse(n.func.value)
                    ok = (n.func.attr == "getenv" and base in aliases) or \
                         (n.func.attr == "get" and base in env_bases)
                    if ok and n.args:
                        form = "os.getenv" if n.func.attr == "getenv" else "os.environ.get"
                        found.append(Read(_literal(n.args[0]), rel, n.lineno, form,
                                          ast.unparse(n.args[1]) if len(n.args) > 1 else None,
                                          None if _literal(n.args[0]) else ast.unparse(n.args[0])))
            elif isinstance(n, ast.Subscript) and ast.unparse(n.value) in env_bases:
                # A WRITE (os.environ["X"] = ...) is not a read; ast.Subscript in a Store context
                # is the assignment target, so context is what separates them.
                if isinstance(n.ctx, ast.Load):
                    found.append(Read(_literal(n.slice), rel, n.lineno, "os.environ[]", None,
                                      None if _literal(n.slice) else ast.unparse(n.slice)))
            for r in found:
                if r.name is None and _iterates_removed_catalogue(tree, n):
                    r.reader += " (over inventory.REMOVED)"
                (inv.fixed if r.name else inv.dynamic).append(r)
    return inv


def _retired_inspections() -> list[dict]:
    # A call to the catalogue's retired_present(<environ>) inside the sanctioned module. It is not
    # an environment READ - no value is resolved - so it appears in no read classification; it is
    # reported separately so the permitted mechanism is visible rather than merely absent.
    out = []
    for rel in _tracked_sources():
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call)
                    and ((isinstance(n.func, ast.Attribute) and n.func.attr == RETIRED_INSPECTION)
                         or (isinstance(n.func, ast.Name) and n.func.id == RETIRED_INSPECTION))):
                arg = ast.unparse(n.args[0]) if n.args else ""
                out.append({"file": rel, "line": n.lineno, "argument": arg,
                            "sanctioned": rel == REMOVED_ITERATOR and "environ" in arg})
    return out


def measure() -> dict:
    sys.path.insert(0, str(REPO / "src"))
    from meshpipeline.settings import inventory as cat

    inv = scan()
    declared = {v.name: v for v in cat.all_vars()}
    fixed_names = sorted({r.name for r in inv.fixed})

    undeclared = [n for n in fixed_names if n not in declared]
    bypasses = [r for r in inv.fixed if r.reader in DIRECT_FORMS and r.file != LOADER]

    loader_reads = [r for r in inv.dynamic if r.file == LOADER]
    removed_iteration = [r for r in inv.dynamic
                         if "over inventory.REMOVED" in r.reader and r.file == REMOVED_ITERATOR]
    unresolved = [r for r in inv.dynamic if r not in loader_reads and r not in removed_iteration]

    # caller-vs-caller: the same name read with two different fallbacks
    per_name: dict[str, set[str]] = {}
    for r in inv.fixed:
        if r.default is not None:
            per_name.setdefault(r.name, set()).add(r.default)
    caller_conflicts = {k: sorted(v) for k, v in per_name.items() if len(v) > 1}

    # catalogue-vs-runtime: a call-site fallback that is not what the catalogue declares
    catalogue_conflicts = []
    for r in inv.fixed:
        var = declared.get(r.name)
        if var is None or r.default is None:
            continue
        # A COMPUTED fallback - str(DATA_ROOT / "jobs"), another optional_env(...) - is not a
        # literal to compare; the catalogue models those as derived defaults instead.
        literal = _string_literal(r.default)
        if literal is None:
            continue
        if var.derived:
            continue                       # resolved by settings/runtime.py, not a literal here
        expected = var.code_default()
        if literal != expected:
            catalogue_conflicts.append({"name": r.name, "file": r.file, "line": r.line,
                                        "call_site": literal, "catalogue": expected})

    # An entry may be consumed without any literal naming it: settings/routes.py asks the
    # catalogue for a declared EnvVar and the loader reads that entry's own name. Two structural
    # sources of that, both derived from the catalogue rather than listed by hand:
    #   - every name the declared route matrix produces, which route_from_catalogue reads in full
    #   - every literal handed to inventory.get(...) in production source
    route_consumed = {cat.route_setting_name(row[1], suffix)
                      for row in cat.ROUTE_MATRIX for suffix, _, _ in cat.ROUTE_SUFFIXES}
    mediated = set()
    for rel in _tracked_sources():
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get" and "inventory" in ast.unparse(n.func.value)
                    and n.args):
                lit = _literal(n.args[0])
                if lit:
                    mediated.add(lit)
    consumed = set(fixed_names) | route_consumed | mediated
    unexplained = [v.name for v in cat.all_vars()
                   if v.name not in consumed and v.consumer == "app"]
    overlap = sorted(set(declared) & set(cat.REMOVED))

    by_file: dict[str, int] = {}
    by_reader: dict[str, int] = {}
    for r in inv.fixed:
        by_file[r.file] = by_file.get(r.file, 0) + 1
        by_reader[r.reader] = by_reader.get(r.reader, 0) + 1

    return {
        "declaration_mediated_reads": sorted(route_consumed | mediated),
        "fixed_occurrences": len(inv.fixed),
        "fixed_names": len(fixed_names),
        "fixed_undeclared": undeclared,
        "approved_reader_reads": sum(1 for r in inv.fixed if r.reader in APPROVED_READERS),
        "direct_bypasses": [r.as_dict() for r in bypasses],
        "dynamic_reads": len(inv.dynamic),
        "loader_reads": [r.as_dict() for r in loader_reads],
        "removed_catalogue_iteration": [r.as_dict() for r in removed_iteration],
        "retired_inspections": _retired_inspections(),
        "unresolved_dynamic_consumers": [r.as_dict() for r in unresolved],
        "caller_default_conflicts": caller_conflicts,
        "catalogue_default_conflicts": catalogue_conflicts,
        "unexplained_catalogue_entries": unexplained,
        "live_removed_overlap": overlap,
        "declared_total": len(declared),
        "declared_template": len(cat.template_vars()),
        "declared_internal": sum(1 for v in cat.all_vars() if v.exposure == "internal"),
        "declared_external": sum(1 for v in cat.all_vars() if v.exposure == "external"),
        "removed_total": len(cat.REMOVED),
        "by_file": dict(sorted(by_file.items())),
        "by_reader": dict(sorted(by_reader.items())),
    }


def fixed_failures(m: dict) -> list[str]:
    out = []
    if m["fixed_undeclared"]:
        out.append(f"{len(m['fixed_undeclared'])} undeclared fixed read(s): {m['fixed_undeclared']}")
    if m["direct_bypasses"]:
        out.append(f"{len(m['direct_bypasses'])} direct lookup(s) outside the loader: "
                   + ", ".join(f"{b['file']}:{b['line']}" for b in m["direct_bypasses"]))
    if m["caller_default_conflicts"]:
        out.append(f"caller defaults disagree: {m['caller_default_conflicts']}")
    if m["catalogue_default_conflicts"]:
        out.append("call-site fallback differs from the catalogue: "
                   + ", ".join(f"{c['name']} ({c['call_site']!r} vs {c['catalogue']!r})"
                               for c in m["catalogue_default_conflicts"]))
    if m["unexplained_catalogue_entries"]:
        out.append("declared, consumer=app, but never read: "
                   f"{m['unexplained_catalogue_entries']}")
    if m["live_removed_overlap"]:
        out.append(f"declared live AND removed: {m['live_removed_overlap']}")
    return out


def full_failures(m: dict) -> list[str]:
    out = fixed_failures(m)
    for insp in m.get("retired_inspections", []):
        if not insp["sanctioned"]:
            out.append(f"retired-configuration inspection outside the sanctioned module or not "
                       f"over the environment: {insp['file']}:{insp['line']}")
    if m["unresolved_dynamic_consumers"]:
        out.append(f"{len(m['unresolved_dynamic_consumers'])} unsupported dynamic configuration "
                   "consumer(s): " + ", ".join(f"{d['file']}:{d['line']} {d['reader']}({d['expr']})"
                                               for d in m["unresolved_dynamic_consumers"]))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="configuration inventory and certification")
    ap.add_argument("--json", action="store_true", help="machine-readable; always exit 0")
    ap.add_argument("--full", action="store_true", help="require the FULL certification")
    args = ap.parse_args(argv[1:])
    m = measure()
    if args.json:
        json.dump(m, sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    print(f"fixed reads          : {m['fixed_occurrences']} occurrences, {m['fixed_names']} names")
    print(f"  via approved reader: {m['approved_reader_reads']}")
    print(f"  undeclared         : {len(m['fixed_undeclared'])}")
    print(f"  direct bypasses    : {len(m['direct_bypasses'])}")
    print(f"dynamic reads        : {m['dynamic_reads']}")
    print(f"  loader (settings/env.py)          : {len(m['loader_reads'])}")
    print(f"  catalogue REMOVED iteration       : {len(m['removed_catalogue_iteration'])}")
    print(f"  UNRESOLVED consumers              : {len(m['unresolved_dynamic_consumers'])}")
    for d in m["unresolved_dynamic_consumers"]:
        print(f"      {d['file']}:{d['line']} {d['reader']}({d['expr']})")
    print(f"catalogue            : {m['declared_total']} entries "
          f"({m['declared_template']} template, {m['declared_internal']} internal, "
          f"{m['declared_external']} external), {m['removed_total']} removed")
    print(f"default conflicts    : caller-vs-caller={len(m['caller_default_conflicts'])} "
          f"catalogue-vs-runtime={len(m['catalogue_default_conflicts'])}")
    failures = full_failures(m) if args.full else fixed_failures(m)
    label = "FULL" if args.full else "FIXED-NAME"
    if failures:
        print(f"\n{label} CERTIFICATION: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\n{label} CERTIFICATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
