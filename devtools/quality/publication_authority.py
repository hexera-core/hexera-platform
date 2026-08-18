# Responsibility: Resolve, for every production event publication, which authority its receiver actually is.
# Owns: the receiver-type resolution and the canonical identity of each publication site.
# Boundaries: it classifies; it enforces nothing. The fitness test decides what an answer means.
from __future__ import annotations

import ast
import json
import pathlib
from dataclasses import dataclass, field


def _production_package() -> pathlib.Path:
    # The package this scanner is about. A checkout has it at src/meshpipeline; an installed
    # deployment has the same modules under site-packages, and certification runs there - against
    # the wheel the image actually ships, with no source tree present to read instead. Asking the
    # import system answers both without a second layout rule.
    checkout = pathlib.Path(__file__).resolve().parents[2] / "src" / "meshpipeline"
    if checkout.is_dir():
        return checkout
    import importlib.util

    spec = importlib.util.find_spec("meshpipeline")
    if spec is not None and spec.origin:
        return pathlib.Path(spec.origin).resolve().parent
    raise RuntimeError(
        "no meshpipeline package to scan: neither src/meshpipeline in the checkout nor an "
        "installed distribution. The scanner reads the modules themselves - it cannot derive "
        "canonical ordinals from a manifest alone.")


SRC = _production_package()

#: The event vocabulary, derived from the typed contracts rather than restated here. `emit` is the
#: transport primitive and `publish_terminal` belongs to the terminal authority, so neither is a
#: semantic event; everything else a publisher declares is.
NOT_SEMANTIC = frozenset({"emit", "publish_terminal"})


def _protocol_methods(class_name: str) -> set[str]:
    tree = ast.parse((SRC / "contracts" / "event_stream.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {n.name for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not n.name.startswith("_") and n.name not in NOT_SEMANTIC}
    raise AssertionError(f"{class_name} is not declared in contracts/event_stream.py")


def semantic_vocabulary() -> dict[str, str]:
    # {spelling seen in source: semantic event}. The async `a` prefix is normalized away, so
    # `note` and `anote` are one semantic method and a sync-to-async change is not a new site.
    plain = _protocol_methods("EventPublisher")
    gated = _protocol_methods("ExecutionEventPublisher")
    vocab = {m: m for m in plain}
    for g in gated:
        assert g.startswith("a") and g[1:] in plain, f"{g} has no plain counterpart"
        vocab[g] = g[1:]
    return vocab


VOCAB = semantic_vocabulary()

# authority categories
PLAIN = "EventPublisher"                    # the ungated contract
GATED = "ExecutionEventPublisher"           # the ownership-checked contract
ADAPTER = "InternalEmitter"                 # the adapter publishing to itself
TERMINAL = "TerminalAuthority"
MAINTENANCE = "MaintenanceAuthority"
OUTBOX = "OutboxAuthority"
INTAKE = "IntakeRequestPath"           # the API request path; no claim exists to check
FORWARDED = "CallerDetermined"              # a shared helper; its authority is its callers'
NOT_A_PUBLISHER = "NotAPublisher"
UNRESOLVED = "UNRESOLVED"

# lifecycle: WHEN a publication happens relative to an execution claim.
L_EXECUTION = "execution-owned"           # a claim is current; the publication must be gated
L_INTAKE = "intake/request-path"          # no claim exists yet
L_TERMINAL = "post-ownership-terminal"    # the claim is gone by construction
L_MAINTENANCE = "maintenance"             # runs with no claim at all
L_INTERNAL = "internal-emitter"           # the adapter talking to itself
L_NONE = "not-a-publisher"

#: Lifecycle for a PLAIN publication, keyed by the DEFINING MODULE of the construction root that
#: produced the object. The key is where the publisher is built, never the file a call sits in.
PLAIN_LIFECYCLE_BY_ROOT = {
    "application/pipeline_run.py": L_TERMINAL,
    "application/terminal_finalize.py": L_TERMINAL,
    "application/maintenance/cleanup.py": L_MAINTENANCE,
    "application/artifact_uploader.py": L_TERMINAL,
    "agents/intake/agent.py": L_INTAKE,
    "agents/intake/executor.py": L_INTAKE,
}


def lifecycle_of(authority: str, roots: tuple, path: str = "") -> str:
    if authority == GATED:
        return L_EXECUTION
    if authority == INTAKE:
        return L_INTAKE
    if authority == ADAPTER:
        return L_INTERNAL
    if authority == NOT_A_PUBLISHER:
        return L_NONE
    if authority != PLAIN:
        return ""
    # the module that BUILT the object decides, whichever of its roots we followed
    for where in [r.split("::")[0] for r in roots] + [path]:
        got = PLAIN_LIFECYCLE_BY_ROOT.get(where)
        if got:
            return got
    return ""

#: THE single committed catalogue of construction roots. Keyed by the callable that produces a
#: publisher, valued by the AUTHORITY it produces - never by what a caller chooses to name it.
#: A root that is not here is unresolved, and aliasing cannot bypass it because the key is the
#: resolved definition site, not the local spelling.
CONSTRUCTION_ROOTS = {
    ("meshpipeline.contracts.event_stream", "publisher"): PLAIN,
    ("meshpipeline.application.execution_publisher", "execution_publisher"): GATED,
    ("meshpipeline.application.execution_publisher", "OwnershipCheckedPublisher"): GATED,
    ("meshpipeline.adapters.event_stream.redis", "JobPublisher"): ADAPTER,
    ("meshpipeline.trace.sink", "PublicTraceSink"): INTAKE,
}

#: Types that are definitively not publishers. Resolved from what the receiver IS, so a variable
#: called `publish` that holds a Logger is excluded, and one called `pat` that holds a publisher
#: is not.
KNOWN_NON_PUBLISHER_CALLS = {
    ("logging", "getLogger"): "logging.Logger",
    ("re", "compile"): "re.Pattern",
}
KNOWN_NON_PUBLISHER_MODULES = {"re", "warnings", "logging", "json", "os", "time", "math"}


@dataclass
class Site:
    canonical: str
    path: str
    qualname: str
    method: str            # as spelled
    semantic: str          # `a` normalized
    lineno: int
    receiver: str
    authority: str
    receiver_type: str = ""
    root: str = ""
    detail: str = ""
    lifecycle: str = ""
    gated_required: bool = False
    roots: tuple = ()

    #: One syntactic site may serve several authorities; this is what makes each record unique.
    @property
    def context(self) -> str:
        return f"{self.canonical}|{self.authority}|{self.lifecycle}|{','.join(self.roots)}"


@dataclass
class ModuleFacts:
    path: str
    tree: ast.AST
    #: local name -> ("module", dotted) | ("root", authority) | ("nonpub", what)
    names: dict = field(default_factory=dict)


def _module_dotted(path: pathlib.Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
    return ".".join(rel.parts)


def _collect_names(tree: ast.AST, dotted: str) -> dict:
    # Imports and module-level assignments give every module-scope name a resolved meaning.
    names: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names[a.asname or a.name.split(".")[0]] = ("module", a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            base = node.module
            if node.level:                      # a relative import inside the package
                base = ".".join(dotted.split(".")[:-node.level] + [node.module])
            for a in node.names:
                key = (base, a.name)
                local = a.asname or a.name
                if key in CONSTRUCTION_ROOTS:
                    names[local] = ("root", CONSTRUCTION_ROOTS[key])
                else:
                    names[local] = ("module", f"{base}.{a.name}")
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call):
            what = _non_publisher_call(node.value, names)
            if what:
                names[node.targets[0].id] = ("nonpub", what)
    return names


def _non_publisher_call(call: ast.Call, names: dict) -> str:
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        mod = names.get(f.value.id)
        base = mod[1] if mod and mod[0] == "module" else f.value.id
        return KNOWN_NON_PUBLISHER_CALLS.get((base.split(".")[0], f.attr), "")
    return ""


#: Every module's facts, so a call into another module can be resolved to what it actually
#: returns instead of being guessed at from the local spelling.
_INDEX: dict[str, ModuleFacts] = {}


def _index() -> dict[str, ModuleFacts]:
    if not _INDEX:
        for path in sorted(SRC.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:                               # pragma: no cover
                continue
            dotted = _module_dotted(path)
            _INDEX[dotted] = ModuleFacts(path.relative_to(SRC).as_posix(), tree,
                                         _collect_names(tree, dotted))
    return _INDEX


class _Resolver:
    def __init__(self, facts: ModuleFacts) -> None:
        self.f = facts
        self.classes = {n.name: n for n in ast.walk(facts.tree) if isinstance(n, ast.ClassDef)}
        self._enclosing_class: ast.ClassDef | None = None

    # what a CALLABLE hands back, resolved where it is defined - the wrapper case: a module's own
    # `def _pub(job_id): return publisher(job_id)` carries the authority of what it returns.
    def _callable_authority(self, dotted: str, name: str, depth: int = 0) -> tuple[str, str] | None:
        if depth > 5:
            return None
        facts = _index().get(dotted)
        if facts is None:
            return None
        for node in ast.walk(facts.tree):
            if isinstance(node, ast.ClassDef) and node.name == name:
                bases = {ast.unparse(b) for b in node.bases}
                if any("LoggerAdapter" in b or "Protocol" in b for b in bases):
                    return (NOT_A_PUBLISHER, f"{dotted}.{name}")
                return (NOT_A_PUBLISHER, f"instance of {dotted}.{name}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                inner = _Resolver(facts)
                for ret in ast.walk(node):
                    if not (isinstance(ret, ast.Return) and ret.value is not None):
                        continue
                    val = ret.value
                    if isinstance(val, ast.Call):
                        got = inner._call_authority(val)
                        if got:
                            return got
                        if isinstance(val.func, ast.Name):
                            fwd = inner._resolve_named_callable(val.func.id, depth + 1)
                            if fwd:
                                return fwd
                if node.returns is not None:
                    ann = ast.unparse(node.returns)
                    got = self._authority_of_annotation(ann)
                    if got:
                        return (got, ann)
                    return (NOT_A_PUBLISHER, ann)
                return None
        return None

    def _resolve_named_callable(self, name: str, depth: int = 0) -> tuple[str, str] | None:
        known = self.f.names.get(name)
        if known and known[0] == "root":
            return (known[1], name)
        if known and known[0] == "module":
            dotted = known[1]
            return self._callable_authority(dotted.rsplit(".", 1)[0], dotted.rsplit(".", 1)[1],
                                            depth)
        # defined in this module
        for dotted, facts in _index().items():
            if facts.path == self.f.path:
                return self._callable_authority(dotted, name, depth)
        return None

    # the annotation of a name, if the enclosing function declares one
    @staticmethod
    def _param_annotation(fn, name: str) -> str | None:
        # `fn` is the enclosing function CHAIN, innermost first: a nested helper sees the
        # parameters of the function it is defined in.
        for f in (fn if isinstance(fn, list) else [fn] if fn else []):
            args = f.args
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                if a.arg == name:
                    return ast.unparse(a.annotation) if a.annotation else ""
        return None

    def _annotated_local(self, fn, name: str) -> tuple[str, str] | None:
        # `x: SomeContract = ...` states the authority outright
        scopes = list(fn) if isinstance(fn, list) else ([fn] if fn is not None else [])
        scopes.append(self.f.tree)
        for scope in scopes:
            for node in ast.walk(scope):
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                        and node.target.id == name and node.annotation is not None:
                    ann = ast.unparse(node.annotation)
                    got = self._authority_of_annotation(ann)
                    if got:
                        return (got, ann)
        return None

    def _assigned_authority(self, fn, name: str) -> tuple[str, str] | None:
        annotated = self._annotated_local(fn, name)
        if annotated:
            return annotated
        # `x = <construction root>(...)` anywhere in an enclosing function or the module
        scopes = list(fn) if isinstance(fn, list) else ([fn] if fn is not None else [])
        scopes.append(self.f.tree)
        for scope in scopes:
            for node in ast.walk(scope):
                if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                    continue
                tgt = node.targets[0]
                spelled = (tgt.id if isinstance(tgt, ast.Name)
                           else ast.unparse(tgt) if isinstance(tgt, ast.Attribute) else None)
                if spelled != name or not isinstance(node.value, ast.Call):
                    continue
                got = self._call_authority(node.value)
                if got:
                    return got
                what = _non_publisher_call(node.value, self.f.names)
                if what:
                    return (NOT_A_PUBLISHER, what)
                if isinstance(node.value.func, ast.Name):
                    fwd = self._resolve_named_callable(node.value.func.id)
                    if fwd:
                        return fwd
                if isinstance(node.value.func, ast.Attribute):
                    return (NOT_A_PUBLISHER, f"result of {ast.unparse(node.value)}")
        for scope in scopes:
            for node in ast.walk(scope):
                if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == name
                        and isinstance(node.value, (ast.Attribute, ast.Name, ast.BoolOp))):
                    continue
                auth, rtype, _root = self.resolve(node.value, fn, self._enclosing_class)
                if auth not in (UNRESOLVED,):
                    return (auth, rtype)
        return None

    def _call_authority(self, call: ast.Call) -> tuple[str, str] | None:
        f = call.func
        if isinstance(f, ast.Name):
            got = self.f.names.get(f.id)
            if got and got[0] == "root":
                return (got[1], f.id)
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            mod = self.f.names.get(f.value.id)
            if mod and mod[0] == "module":
                key = (mod[1], f.attr)
                if key in CONSTRUCTION_ROOTS:
                    return (CONSTRUCTION_ROOTS[key], f"{f.value.id}.{f.attr}")
        return None

    @staticmethod
    def _authority_of_annotation(ann: str) -> str | None:
        a = (ann or "").replace("'", "").replace('"', "")
        if "ExecutionEventPublisher" in a:
            return GATED
        if "EventPublisher" in a:
            return PLAIN
        return None

    def _field_annotation(self, cls: ast.ClassDef, attr: str) -> str | None:
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == attr \
                    and node.returns is not None:
                return ast.unparse(node.returns)
        for node in cls.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == attr:
                return ast.unparse(node.annotation)
        # constructor forwarding: `self.<attr> = <param>` picks up the parameter's annotation
        for node in ast.walk(cls):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for a in ast.walk(node):
                if isinstance(a, ast.Assign) and len(a.targets) == 1 \
                        and isinstance(a.targets[0], ast.Attribute) \
                        and a.targets[0].attr == attr and isinstance(a.value, ast.Name):
                    got = self._param_annotation(node, a.value.id)
                    if got:
                        return got
        return None

    # What every production caller actually passes for this parameter. One answer -> that is the
    # authority. Several -> the site is genuinely polymorphic and each context is recorded.
    def _from_callers(self, fn, param: str, depth: int = 0) -> tuple[str, str, str] | None:
        if depth > 4:
            return None
        owner = (fn[0] if isinstance(fn, list) and fn else fn)
        if owner is None:
            return None
        idx, kwonly = _param_position(owner, param)
        if idx is None and not kwonly:
            return None
        seen: dict[str, str] = {}
        for dotted, call, fnstack, _ccls in _callers_of(owner.name):
            arg = None
            for kw in call.keywords:
                if kw.arg == param:
                    arg = kw.value
            if arg is None and idx is not None and len(call.args) > idx:
                arg = call.args[idx]
            if arg is None:
                continue
            facts = _index().get(dotted)
            if facts is None:
                continue
            sub = _Resolver(facts)
            if isinstance(arg, ast.Name):
                as_callable = sub._resolve_named_callable(arg.id, depth + 1)
                if as_callable:
                    seen.setdefault(as_callable[0], as_callable[1])
                    continue
            auth, rtype, _r = sub.resolve(arg, fnstack, None)
            if auth == UNRESOLVED:
                continue
            if auth == FORWARDED and isinstance(arg, ast.Name):
                deeper = sub._from_callers(fnstack, arg.id, depth + 1)
                if deeper:
                    auth, rtype = deeper[0], deeper[1]
            seen.setdefault(auth, rtype)
        if not seen:
            return None
        if len(seen) == 1:
            auth, rtype = next(iter(seen.items()))
            return (auth, rtype, f"callers of {owner.name}()")
        if set(seen) <= {NOT_A_PUBLISHER}:
            return (NOT_A_PUBLISHER, "; ".join(seen.values()), "")
        return (FORWARDED, " | ".join(f"{a}:{t}" for a, t in seen.items()),
                f"callers of {owner.name}()")

    # EVERY authority a parameter is reachable under, with the roots that supply it. A helper
    # serving two lifecycles yields two determinate records, never one ambiguous verdict.
    def contexts_for_param(self, fn, param: str, depth: int = 0, seen_edges=None,
                           called_as: str = "") -> dict:
        seen_edges = seen_edges if seen_edges is not None else set()
        owner = (fn[0] if isinstance(fn, list) and fn else fn)
        if owner is None or depth > 6:
            return {}
        lookup = called_as or owner.name
        edge = (self.f.path, lookup, param)
        if edge in seen_edges:                 # a cyclic forwarding graph stops here, safely
            return {}
        seen_edges = seen_edges | {edge}
        idx, kwonly = _param_position(owner, param)
        if idx is None and not kwonly:
            return {}
        out: dict = {}

        def merge(auth, rtype, root_label):
            out.setdefault(auth, {"type": rtype, "roots": set()})
            out[auth]["roots"].add(root_label)

        for dotted, call, fnstack, ccls in _callers_of(lookup):
            arg = None
            for kw in call.keywords:
                if kw.arg == param:
                    arg = kw.value
            if arg is None and idx is not None and len(call.args) > idx:
                arg = call.args[idx]
            if arg is None:
                continue
            facts = _index().get(dotted)
            if facts is None:
                continue
            sub = _Resolver(facts)
            if isinstance(arg, ast.Name):
                as_callable = sub._resolve_named_callable(arg.id, depth + 1)
                if as_callable:
                    merge(as_callable[0], as_callable[1], f"{facts.path}::{arg.id}")
                    continue
            auth, rtype, root = sub.resolve(arg, fnstack, ccls)
            if auth == FORWARDED:
                forward = (arg.id if isinstance(arg, ast.Name)
                           else root if isinstance(arg, ast.Attribute) else "")
                deeper = (sub.contexts_for_param(fnstack, forward, depth + 1, seen_edges)
                          if forward else {})
                if not deeper and isinstance(arg, ast.Attribute) and ccls is not None:
                    deeper = sub.contexts_for_field(ccls, arg.attr, depth + 1)
                for a2, info in deeper.items():
                    for r in info["roots"]:
                        merge(a2, info["type"], r)
                continue
            if auth == UNRESOLVED:
                merge(UNRESOLVED, f"unclassifiable argument {ast.unparse(arg)!r}",
                      f"{facts.path}::{fnstack[0].name if fnstack else '<module>'}")
                continue
            merge(auth, rtype, f"{facts.path}::{root or rtype}")
        return out

    # `self.<field> = <param>`: which constructor parameter fills this field, and in which
    # function. A field is rarely named the same as the parameter that fills it.
    @staticmethod
    def _field_source(cls, attr: str):
        for node in ast.walk(cls):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for a in ast.walk(node):
                if isinstance(a, ast.Assign) and len(a.targets) == 1 \
                        and isinstance(a.targets[0], ast.Attribute) \
                        and a.targets[0].attr == attr and isinstance(a.value, ast.Name):
                    return node, a.value.id
        return None, ""

    # `self.<field>`: the contexts every construction of that class supplies for the field
    def contexts_for_field(self, cls, attr: str, depth: int = 0) -> dict:
        out: dict = {}
        holder, param = self._field_source(cls, attr)
        if holder is not None and param:
            called_as = cls.name if holder.name == "__init__" else holder.name
            for a2, info in self.contexts_for_param(
                    [holder], param, depth + 1, called_as=called_as).items():
                out.setdefault(a2, {"type": info["type"], "roots": set()})
                out[a2]["roots"] |= info["roots"]
        for dotted, call, fnstack, _cc in _callers_of(cls.name):
            facts = _index().get(dotted)
            if facts is None:
                continue
            sub = _Resolver(facts)
            for kw in call.keywords:
                if kw.arg != attr:
                    continue
                auth, rtype, root = sub.resolve(kw.value, fnstack, None)
                if auth == FORWARDED and isinstance(kw.value, ast.Name):
                    for a2, info in sub.contexts_for_param(
                            fnstack, kw.value.id, depth + 1).items():
                        out.setdefault(a2, {"type": info["type"], "roots": set()})
                        out[a2]["roots"] |= info["roots"]
                    continue
                if auth in (UNRESOLVED, FORWARDED):
                    continue
                out.setdefault(auth, {"type": rtype, "roots": set()})
                out[auth]["roots"].add(f"{facts.path}::{root or rtype}")
        return out

    def _loop_binding(self, fn, name: str) -> str:
        for scope in (list(fn) if isinstance(fn, list) else [fn] if fn else []):
            for node in ast.walk(scope):
                if not isinstance(node, (ast.For, ast.AsyncFor)):
                    continue
                targets = (node.target.elts if isinstance(node.target, ast.Tuple)
                           else [node.target])
                if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                    return f"element of {ast.unparse(node.iter)}"
        return ""

    # -> (authority, receiver_type, root) for a receiver expression
    def resolve(self, recv: ast.expr, fn, cls) -> tuple[str, str, str]:
        self._enclosing_class = cls
        # a construction root called inline: `publisher(job_id).note(...)`
        if isinstance(recv, ast.Call):
            got = self._call_authority(recv)
            if got:
                return (got[0], got[0], got[1])
            what = _non_publisher_call(recv, self.f.names)
            if what:
                return (NOT_A_PUBLISHER, what, "")
            if isinstance(recv.func, ast.Name):
                fwd = self._resolve_named_callable(recv.func.id)
                if fwd:
                    return (fwd[0], fwd[0], fwd[1])
                if self._param_annotation(fn, recv.func.id) is not None:
                    got = self._from_callers(fn, recv.func.id)
                    if got and got[0] != FORWARDED:
                        return got
                    return (FORWARDED, f"factory parameter {recv.func.id}", "caller-supplied")
            if isinstance(recv.func, ast.Attribute):
                return (NOT_A_PUBLISHER, f"result of {ast.unparse(recv)}", "")

        if isinstance(recv, ast.Name) and recv.id == "self" and cls is not None:
            if cls.name in {r[1] for r in CONSTRUCTION_ROOTS}:
                return (ADAPTER, f"self ({cls.name})", cls.name)
            return (NOT_A_PUBLISHER, f"self ({cls.name})", "")

        if isinstance(recv, ast.Name):
            known = self.f.names.get(recv.id)
            if known and known[0] == "module":
                base = known[1].split(".")[0]
                if base in KNOWN_NON_PUBLISHER_MODULES:
                    return (NOT_A_PUBLISHER, f"module {known[1]}", "")
                return (NOT_A_PUBLISHER, f"module-level helper {known[1]}", "")
            if known and known[0] == "nonpub":
                return (NOT_A_PUBLISHER, known[1], "")
            assigned = self._assigned_authority(fn, recv.id)
            if assigned:
                return (assigned[0], assigned[0], assigned[1])
            bound = self._loop_binding(fn, recv.id)
            if bound:
                return (NOT_A_PUBLISHER, bound, "")
            ann = self._param_annotation(fn, recv.id)
            if ann is not None:
                got = self._authority_of_annotation(ann)
                if got:
                    return (got, ann, "parameter")
                if ann == "":
                    got = self._from_callers(fn, recv.id)
                    if got:
                        return got
                    return (FORWARDED, "unannotated parameter", "")
                if ann.replace("'", "") in ("Any", "Any | None", "object"):
                    return (FORWARDED, ann, "")
                return (NOT_A_PUBLISHER, ann, "")
            return (UNRESOLVED, "", "")

        if isinstance(recv, ast.BoolOp):
            seen: dict[str, str] = {}
            for operand in recv.values:
                auth, rtype, _root = self.resolve(operand, fn, cls)
                if auth == NOT_A_PUBLISHER and rtype.startswith("result of "):
                    continue          # an opaque lookup: unknown, not a competing authority
                if auth not in (UNRESOLVED, FORWARDED):
                    seen.setdefault(auth, rtype)
            if len(seen) == 1:
                auth, rtype = next(iter(seen.items()))
                return (auth, rtype, "or-default")
            return (UNRESOLVED, "", "")

        if isinstance(recv, ast.Subscript):
            return (NOT_A_PUBLISHER, f"element of {ast.unparse(recv.value)}", "")

        if isinstance(recv, ast.Attribute):
            # `self.<field>` and `<obj>.<field>`
            if isinstance(recv.value, ast.Name) and recv.value.id == "self" and cls is not None:
                if recv.attr == "_inner":
                    return (ADAPTER, "the wrapped adapter", cls.name)
                ann = self._field_annotation(cls, recv.attr)
                if ann is not None:
                    got = self._authority_of_annotation(ann)
                    if got:
                        return (got, ann, f"{cls.name}.{recv.attr}")
                    if ann.replace("'", "") in ("Any", "Any | None", "object"):
                        return (FORWARDED, ann, f"{cls.name}.{recv.attr}")
                    return (NOT_A_PUBLISHER, ann, "")
                assigned = self._assigned_authority(None, ast.unparse(recv))
                if assigned:
                    return (assigned[0], assigned[0], assigned[1])
                for node in ast.walk(cls):
                    if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                            and isinstance(node.targets[0], ast.Attribute) \
                            and node.targets[0].attr == recv.attr \
                            and isinstance(node.value, ast.Name):
                        return (FORWARDED, f"{cls.name}.{recv.attr}", node.value.id)
                return (UNRESOLVED, "", "")
            # a field on another object: resolve that object's class, then its field
            base = recv.value
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) \
                    and base.value.id == "self" and cls is not None:
                owner_ann = self._field_annotation(cls, base.attr)
                owner = _all_classes().get((owner_ann or "").split("[")[0].split("|")[0].strip())
                if owner is not None:
                    fann = self._field_annotation(owner, recv.attr)
                    got = self._authority_of_annotation(fann or "")
                    if got:
                        return (got, fann or "", f"{owner.name}.{recv.attr}")
            if isinstance(base, ast.Name):
                ann = self._param_annotation(fn, base.id)
                owner = None
                if ann:
                    owner = self.classes.get(ann.split("[")[0].split("|")[0].strip())
                if owner is None and cls is not None:
                    fann = self._field_annotation(cls, base.id if base.id != "self" else "")
                if owner is None:
                    for other in _all_classes().values():
                        if self._field_annotation(other, recv.attr) is not None:
                            owner = other
                            break
                if owner is not None:
                    fann = self._field_annotation(owner, recv.attr)
                    got = self._authority_of_annotation(fann or "")
                    if got:
                        return (got, fann or "", f"{owner.name}.{recv.attr}")
                    if (fann or "").replace("'", "") in ("Any", "Any | None", "object"):
                        built = _from_constructions(owner.name, recv.attr)
                        if built:
                            return built
                        return (FORWARDED, fann or "", f"{owner.name}.{recv.attr}")
                known = self.f.names.get(base.id)
                if known and known[0] == "module":
                    return (NOT_A_PUBLISHER, f"module {known[1]}", "")
            return (UNRESOLVED, "", "")

        return (UNRESOLVED, "", "")


#: Every call expression in production, indexed by the callee's simple name, so an unannotated
#: parameter can be resolved from what its callers actually pass rather than left ambiguous.
_CALLS: dict[str, list[tuple[str, ast.Call, list]]] | None = None


def _call_index() -> dict:
    global _CALLS
    if _CALLS is None:
        _CALLS = {}

        def walk(dotted, node, fnstack, cls):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(dotted, child, [child, *fnstack], cls)
                    continue
                if isinstance(child, ast.ClassDef):
                    walk(dotted, child, fnstack, child)
                    continue
                if isinstance(child, ast.Call):
                    f = child.func
                    name = (f.id if isinstance(f, ast.Name)
                            else f.attr if isinstance(f, ast.Attribute) else "")
                    if name:
                        _CALLS.setdefault(name, []).append((dotted, child, fnstack, cls))
                walk(dotted, child, fnstack, cls)

        for dotted, facts in _index().items():
            walk(dotted, facts.tree, [], None)
    return _CALLS


# -> (positional index, is keyword-only) for a parameter of `fn`
# Every local spelling a callee is reached by, so `await _finalize_crash(...)` is found when
# looking for callers of `finalize_crash`.
def _callers_of(name: str) -> list:
    out = list(_call_index().get(name, []))
    for facts in _index().values():
        for local, what in facts.names.items():
            if what[0] == "module" and what[1].rsplit(".", 1)[-1] == name and local != name:
                out.extend(_call_index().get(local, []))
    return out


def _param_position(fn, name: str) -> tuple[int | None, bool]:
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    for i, a in enumerate(positional):
        if a.arg == name:
            return (i, False)
    for a in args.kwonlyargs:
        if a.arg == name:
            return (None, True)
    return (None, False)


# What every production construction of `Class(field=...)` actually puts in that field. One
# agreed answer makes the field determinate even when its annotation is permissive.
def _from_constructions(cls_name: str, field: str) -> tuple[str, str, str] | None:
    seen: dict[str, str] = {}
    for dotted, call, fnstack, _cc in _call_index().get(cls_name, []):
        facts = _index().get(dotted)
        if facts is None:
            continue
        for kw in call.keywords:
            if kw.arg != field:
                continue
            auth, rtype, _root = _Resolver(facts).resolve(kw.value, fnstack, _cc)
            if auth != UNRESOLVED:
                seen.setdefault(auth, rtype)
    if len(seen) == 1:
        auth, rtype = next(iter(seen.items()))
        return (auth, rtype, f"{cls_name}(...) constructions")
    return None


def _all_classes() -> dict:
    out: dict = {}
    for facts in _index().values():
        for n in ast.walk(facts.tree):
            if isinstance(n, ast.ClassDef):
                out.setdefault(n.name, n)
    return out


def _qualname(stack: list[str]) -> str:
    return ".".join(stack) if stack else "<module>"


def scan() -> list[Site]:
    sites: list[Site] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                                   # pragma: no cover
            continue
        facts = ModuleFacts(rel, tree, _collect_names(tree, _module_dotted(path)))
        resolver = _Resolver(facts)
        counters: dict[tuple[str, str], int] = {}

        def visit(node, stack, fn, cls, *, _r=resolver, _c=counters, _rel=rel):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    visit(child, [*stack, child.name], fn, child)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, [*stack, child.name], [child, *(fn or [])], cls)
                else:
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                            and child.func.attr in VOCAB:
                        spelled = child.func.attr
                        semantic = VOCAB[spelled]
                        auth, rtype, root = _r.resolve(child.func.value, fn, cls)
                        q = _qualname(stack)
                        key = (q, semantic)
                        _c[key] = _c.get(key, 0) + 1
                        canonical = f"{_rel}::{q}::{semantic}#{_c[key]}"
                        recv_src = ast.unparse(child.func.value)

                        # A POLYMORPHIC seam: one call, several reachable authorities. Each
                        # becomes its own determinate record rather than one ambiguous verdict.
                        expanded: dict = {}
                        if auth == FORWARDED:
                            node_recv = child.func.value
                            if isinstance(node_recv, ast.Name):
                                expanded = _r.contexts_for_param(fn, node_recv.id)
                            elif isinstance(node_recv, ast.Attribute) and cls is not None:
                                if isinstance(node_recv.value, ast.Name) \
                                        and node_recv.value.id == "self":
                                    expanded = _r.contexts_for_param(fn, root) if root else {}
                                    if not expanded:
                                        expanded = _r.contexts_for_field(cls, node_recv.attr)
                                else:
                                    expanded = _r.contexts_for_field(cls, node_recv.attr)
                        if expanded:
                            for a2 in sorted(expanded):
                                info = expanded[a2]
                                roots = tuple(sorted(info["roots"]))
                                sites.append(Site(
                                    canonical=canonical, path=_rel, qualname=q, method=spelled,
                                    semantic=semantic, lineno=child.lineno, receiver=recv_src,
                                    authority=a2, receiver_type=info["type"],
                                    root="|".join(roots),
                                    lifecycle=lifecycle_of(a2, roots, _rel),
                                    gated_required=(a2 == GATED), roots=roots))
                        else:
                            roots = (f"{_rel}::{root}",) if root else ()
                            sites.append(Site(
                                canonical=canonical, path=_rel, qualname=q, method=spelled,
                                semantic=semantic, lineno=child.lineno, receiver=recv_src,
                                authority=auth, receiver_type=rtype, root=root,
                                lifecycle=lifecycle_of(auth, roots, _rel),
                                gated_required=(auth == GATED), roots=roots))
                    visit(child, stack, fn, cls)

        visit(tree, [], [], None)
    return sites


#: The reviewed structural contract. The scanner discovers candidates on its own; this file is
#: the expected result, never an input that could hide one.
MANIFEST = pathlib.Path(__file__).resolve().parents[2] / "devtools" / "quality" \
    / "publication_authority_manifest.json"


class IncompleteScan(RuntimeError):
    pass


def completeness_problems(sites: list[Site]) -> list[str]:
    problems: list[str] = []
    seen: dict[str, int] = {}
    for s in sites:
        where = f"{s.path}::{s.qualname}::{s.semantic} (line {s.lineno}, recv {s.receiver!r})"
        if s.authority == UNRESOLVED:
            problems.append(f"unresolved receiver: {where}")
        if s.authority == FORWARDED:
            problems.append(f"caller-determined context: {where}")
        if not s.lifecycle:
            problems.append(f"no lifecycle: {where} authority={s.authority}")
        if s.authority != NOT_A_PUBLISHER and not s.roots:
            problems.append(f"no construction or caller root: {where}")
        if s.lifecycle == L_EXECUTION and s.authority != GATED:
            problems.append(f"execution-owned but publishes through {s.authority}: {where}")
        seen[s.context] = seen.get(s.context, 0) + 1
    problems.extend(f"duplicate context identity: {c}" for c, n in sorted(seen.items()) if n > 1)
    return problems


def record(s: Site) -> dict:
    # Stable keys, stable order, no line numbers, no timestamps, no absolute paths.
    return {
        "context": s.context,
        "path": s.path,
        "qualname": s.qualname,
        "semantic": s.semantic,
        "method": s.method,
        "ordinal": int(s.canonical.rsplit("#", 1)[1]),
        "receiver": s.receiver,
        "authority": s.authority,
        "lifecycle": s.lifecycle,
        "gated_required": s.gated_required,
        "roots": sorted(s.roots),
    }


def manifest(sites: list[Site]) -> list[dict]:
    rows = [record(s) for s in sites]
    rows.sort(key=lambda r: (r["path"], r["qualname"], r["semantic"], r["ordinal"],
                             r["authority"], r["lifecycle"]))
    return rows


def serialize(sites: list[Site]) -> str:
    return json.dumps(manifest(sites), indent=1, sort_keys=False, ensure_ascii=True) + "\n"


def regenerate() -> str:
    sites = scan()
    problems = completeness_problems(sites)
    if problems:
        raise IncompleteScan(
            "refusing to write a manifest from an incomplete scan:\n  "
            + "\n  ".join(problems))
    text = serialize(sites)
    MANIFEST.write_text(text)
    return text


def summary(sites: list[Site]) -> str:
    import collections
    by_a = collections.Counter(s.authority for s in sites)
    by_l = collections.Counter(s.lifecycle for s in sites)
    out = [f"syntactic candidates : {len({s.canonical for s in sites})}",
           f"expanded records     : {len(sites)}", "", "by authority"]
    out += [f"  {v:>4}  {k}" for k, v in sorted(by_a.items())]
    out += ["", "by lifecycle"]
    out += [f"  {v:>4}  {k}" for k, v in sorted(by_l.items())]
    return "\n".join(out)


# CHECK the tree against the committed manifest:
#     python devtools/quality/publication_authority.py check
# REGENERATE it after a reviewed source change (never automatic, never during tests):
#     python devtools/quality/publication_authority.py regenerate
# HUMAN-READABLE totals:
#     python devtools/quality/publication_authority.py summary
if __name__ == "__main__":                                    # pragma: no cover
    import sys
    what = sys.argv[1] if len(sys.argv) > 1 else "summary"
    found = scan()
    if what == "regenerate":
        regenerate()
        print(f"wrote {MANIFEST.relative_to(MANIFEST.parents[2])}: {len(found)} records")
    elif what == "check":
        bad = completeness_problems(found)
        drift = serialize(found) != MANIFEST.read_text()
        for b in bad:
            print(f"INCOMPLETE: {b}")
        if drift:
            print("DRIFT: the scan does not match the committed manifest; run the fitness test "
                  "for a per-record diff, then regenerate if the change is intended.")
        sys.exit(1 if (bad or drift) else 0)
    else:
        print(summary(found))
