# Responsibility: Recipe memory (design §5.10): remember the op sequences that took a DEV file to clean, keyed by the
#                 failure signature of the site each op addressed, and offer them back as candidates on similar sites.
# Owns: `signature` (site -> id-free key), `Recipe`/`Step` (id-free op patterns), `recipe_from_path` (a clean search's
#       winning path -> a recipe), `RecipeStore` (versioned JSON in the eval bucket, DEV-ONLY writes), `RecipeBook`
#       (a frozen snapshot: deterministic top-k retrieval and replay onto a new site's entities).
# Boundaries: advice only. Retrieval PROPOSES; the search evaluates every replayed candidate with the same verdict as
#             any other, so a wrong recipe costs one op and nothing else. No geometry, path or file name is stored -
#             only kinds, buckets, op names, relative parameters and the file's sha256 as a witness.
"""Recipe memory.

THE SIGNATURE carries no entity id, so it is stable when ids change (a renumbered face is the same site):
    kind x hard codes x which panel members fail x the neighbourhood's surface types
       x dist/h bucket x angular-span bucket x (open sites) smallest-loop-span / diagonal bucket.
Sizes are buckets of RATIOS (dist/h, span/diagonal), never millimetres, so a 2 mm gap on a bracket and on a casing
do not look alike.

A STEP is one op as a PATTERN: its targets as a ROLE relative to the site it addressed (`none`, `site_faces`,
`site_solids`, `site_entities`, `free_loop`:rank) and its millimetre parameters as fractions of the part diagonal.
A step whose targets or id-bearing parameters cannot be expressed as a role is kept (the AI still sees it) but is not
`replayable`.

WRITES ARE DEV-ONLY. `RecipeStore.record` refuses any split other than `dev` and the held-out wave type, so a
held-out or clean-control file can never teach the memory what it will later be graded on.

RETRIEVAL IS A SNAPSHOT. A shard loads the book once; recipes written during the wave are visible to the NEXT wave,
so one wave's results do not depend on which shard finished first. The snapshot's hash goes in the provenance.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

#: bumped by any change to the signature, the step pattern or the JSON layout; part of every object's path
RECIPE_SCHEMA = "crs-v4.recipe.1"
_ROOT = "recipes/v1"
#: the only split whose clean files may write a recipe (design §5.10; plan "Held-out split ... never used for recipe
#: memory"). The clean control is not dev either.
WRITABLE_SPLIT = "dev"
_FORBIDDEN_WAVE_TYPES = ("held_out",)
#: millimetre-valued parameters, stored as a fraction of the part diagonal and scaled back on replay
_MM_PARAMS = ("tol_mm", "max_width_mm", "fuzzy_mm", "tol_ladder", "small_width_mm")
#: parameters that carry entity ids; replayable only through a role (`_ID_PARAM_ROLES`)
_ID_PARAMS = ("face_pairs", "keep", "pairs", "fill_edges", "targets", "rebuild", "small")


class RecipeWriteRefused(PermissionError):
    """A recipe may only be learned from a clean file of the dev split."""


# --- the signature ------------------------------------------------------------------------------------------------


def _bucket_log2(x: float | None, lo: int = -6, hi: int = 4) -> str:
    if x is None or not math.isfinite(x) or x <= 0:
        return "none"
    return str(max(lo, min(hi, math.floor(math.log2(x)))))


def _bucket_span(deg: float | None) -> str:
    if deg is None or not math.isfinite(deg):
        return "none"
    for edge in (30, 90, 180):
        if deg <= edge:
            return f"<={edge}"
    return ">180"


def _bucket_loop(ratio: float | None) -> str:
    if ratio is None or not math.isfinite(ratio) or ratio <= 0:
        return "none"
    for edge in (0.01, 0.02, 0.05):
        if ratio < edge:
            return f"<{edge}"
    return ">=0.05"


def signature(site: Any) -> dict:
    """The id-free failure signature of `site` (a `types.Site`)."""
    g = site.graph or {}
    nodes = [n for n in g.get("nodes") or () if isinstance(n, dict)]
    spans = [float(n["angular_span_deg"]) for n in nodes if isinstance(n.get("angular_span_deg"), (int, float))]
    loops = [lp for lp in g.get("free_loops") or () if isinstance(lp, dict) and lp.get("span_mm") is not None]
    diag = float(g.get("diagonal_mm") or 0.0)
    loop_ratio = min(float(lp["span_mm"]) for lp in loops) / diag if loops and diag > 0 else None
    return {
        "kind": str(site.kind),
        "hard": sorted({str(c) for c in g.get("hard_codes") or ()}),
        "meshers": sorted({str(m) for m in site.meshers_failing or ()}),
        "surfaces": sorted({str(n["surface"]) for n in nodes if n.get("surface")}),
        "dist_h": _bucket_log2(site.dist_over_h),
        "span": _bucket_span(max(spans) if spans else None),
        "loop": _bucket_loop(loop_ratio),
    }


def signature_key(sig: dict) -> str:
    return hashlib.sha256(json.dumps(sig, sort_keys=True).encode()).hexdigest()[:16]


def _jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    return 1.0 if not sa and not sb else len(sa & sb) / len(sa | sb)


def _bucket_near(a: str, b: str, order: Sequence[str] | None = None) -> float:
    if a == b:
        return 1.0
    if order is not None and a in order and b in order and abs(order.index(a) - order.index(b)) == 1:
        return 0.5
    try:
        return 0.5 if abs(int(a) - int(b)) == 1 else 0.0
    except ValueError:
        return 0.0


_SPAN_ORDER = ("none", "<=30", "<=90", "<=180", ">180")
_LOOP_ORDER = ("none", "<0.01", "<0.02", "<0.05", ">=0.05")
#: the weights of `similarity`; kinds must match exactly or there is no similarity at all
WEIGHTS = {"meshers": 3.0, "hard": 2.0, "surfaces": 2.0, "dist_h": 1.0, "span": 1.0, "loop": 1.0}
MAX_SIMILARITY = sum(WEIGHTS.values())


def similarity(a: dict, b: dict) -> float | None:
    """How alike two signatures are, in [0, MAX_SIMILARITY]; None when the site kinds differ."""
    if a["kind"] != b["kind"]:
        return None
    return (WEIGHTS["meshers"] * _jaccard(a["meshers"], b["meshers"])
            + WEIGHTS["hard"] * _jaccard(a["hard"], b["hard"])
            + WEIGHTS["surfaces"] * _jaccard(a["surfaces"], b["surfaces"])
            + WEIGHTS["dist_h"] * _bucket_near(a["dist_h"], b["dist_h"])
            + WEIGHTS["span"] * _bucket_near(a["span"], b["span"], _SPAN_ORDER)
            + WEIGHTS["loop"] * _bucket_near(a["loop"], b["loop"], _LOOP_ORDER))


# --- steps and recipes ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    op: str
    #: `none`, `site_faces`, `site_solids`, `site_entities`, `free_loop`, or `other` (not replayable)
    role: str
    #: parameters with millimetres as fractions of the diagonal (`{"tol_mm": {"rel": 1e-4}}`) and id lists as roles
    params: dict
    #: the signature of the site this step addressed, at the state it was applied to
    site: dict
    replayable: bool
    #: for `free_loop`: the loop's rank by span among the site's loops (0 = smallest)
    loop_rank: int = 0


@dataclass(frozen=True)
class Recipe:
    steps: tuple[Step, ...]
    schema: str = RECIPE_SCHEMA

    @property
    def id(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json()["steps"], sort_keys=True).encode()).hexdigest()[:16]

    def to_json(self) -> dict:
        return {"schema": self.schema, "steps": [asdict(s) for s in self.steps]}

    @classmethod
    def from_json(cls, d: dict) -> Recipe:
        if d.get("schema") != RECIPE_SCHEMA:
            raise ValueError(f"recipe schema {d.get('schema')!r} is not {RECIPE_SCHEMA!r}")
        return cls(tuple(Step(**s) for s in d["steps"]), d["schema"])

    def summary(self) -> list[dict]:
        """What the model is shown: op, role and parameters per step; no ids."""
        return [{"op": s.op, "targets": s.role if s.role != "free_loop" else f"free_loop[{s.loop_rank}]",
                 "params": s.params} for s in self.steps]


def _loops_by_span(site: Any) -> list[list[str]]:
    loops = [lp for lp in (site.graph or {}).get("free_loops") or () if isinstance(lp, dict)]
    return [list(lp.get("edges") or []) for lp in sorted(loops, key=lambda lp: (float(lp.get("span_mm") or 0.0),
                                                                                 sorted(lp.get("edges") or [])))]


def _role_of(targets: Sequence[str], site: Any) -> tuple[str, int]:
    t = list(targets)
    if not t:
        return "none", 0
    for rank, loop in enumerate(_loops_by_span(site)):
        if set(t) == set(loop):
            return "free_loop", rank
    ents = set(site.entities or ())
    faces = {e for e in ents if e.startswith("F")}
    solids = {e for e in ents if e.startswith("B")}
    if set(t) == ents:
        return "site_entities", 0
    if faces and set(t) == faces:
        return "site_faces", 0
    if solids and set(t) == solids:
        return "site_solids", 0
    return "other", 0


def _relative_params(op: str, params: dict, diag: float | None) -> tuple[dict, bool]:
    """(the parameter pattern, replayable?)."""
    out: dict = {}
    ok = True
    for k, v in sorted((params or {}).items()):
        if k in _MM_PARAMS and v is not None:
            if not diag or diag <= 0:
                out[k], ok = v, False
            elif isinstance(v, list):
                out[k] = {"rel": [float(x) / diag for x in v]}
            else:
                out[k] = {"rel": float(v) / diag}
        elif k in _ID_PARAMS and v:
            out[k] = "<ids>"
            ok = ok and k == "face_pairs"            # face_pairs replays as the site's own face pairs
        elif k == "params" and isinstance(v, dict):  # run_stage
            inner, inner_ok = _relative_params(op, v, diag)
            out[k], ok = inner, ok and inner_ok
        else:
            out[k] = v
    return out, ok


def _ids_in(value: Any) -> set[str]:
    """Every entity-id-shaped string in an op's parameters (`face_pairs`, `fill_edges`, a stage's lists...)."""
    if isinstance(value, str):
        return {value} if value[:1] in ("F", "E", "B") and value[1:].isdigit() else set()
    if isinstance(value, dict):
        return set().union(*(_ids_in(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_ids_in(v) for v in value)) if value else set()
    return set()


def _site_for(via: dict, sites: Sequence[Any]) -> Any | None:
    """The site a step addressed: the one whose entities or loops its targets lie in; for a target-less step, the
    first site of a kind the op serves (`Op._site_kinds`), else the first site. Deterministic (sites by id)."""
    if not sites:
        return None
    ordered = sorted(sites, key=lambda s: (str(s.kind), str(s.id)))
    probe = set(via.get("targets") or ()) or _ids_in(via.get("params") or {})
    if probe:
        for s in ordered:
            pool = set(s.entities or ()) | {e for loop in _loops_by_span(s) for e in loop}
            if probe <= pool:
                return s
        for s in ordered:
            if probe & (set(s.entities or ()) | {e for loop in _loops_by_span(s) for e in loop}):
                return s
    from meshpipeline.cad.repair_v4.ops import REGISTRY
    kinds = getattr(REGISTRY.get(str(via.get("op"))), "_site_kinds", None) or ()
    return next((s for s in ordered if s.kind in kinds), ordered[0])


def recipe_from_path(path_steps: Sequence[dict], *, diagonal_mm: float | None) -> Recipe | None:
    """The recipe of a CLEAN search's winning path: `path_steps` is `SearchResult.path_steps` (each `{"via": the
    candidate, "sites": the parent state's sites}`). None when the path is empty (the start was already clean)."""
    steps: list[Step] = []
    for item in path_steps:
        via, sites = item["via"], item.get("sites") or []
        site = _site_for(via, sites)
        if site is None:
            continue
        role, rank = _role_of(via.get("targets") or (), site)
        params, params_ok = _relative_params(str(via["op"]), dict(via.get("params") or {}), diagonal_mm)
        steps.append(Step(op=str(via["op"]), role=role, params=params, site=signature(site),
                          replayable=role != "other" and params_ok, loop_rank=rank))
    return Recipe(tuple(steps)) if steps else None


# --- replay ---------------------------------------------------------------------------------------------------------


def replay(step: Step, site: Any, *, diagonal_mm: float | None) -> tuple[list[str], dict] | None:
    """(targets, params) for `step` on a NEW site, or None when it cannot be expressed there."""
    if not step.replayable:
        return None
    ents = list(site.entities or ())
    if step.role == "none":
        targets: list[str] = []
    elif step.role == "free_loop":
        loops = _loops_by_span(site)
        if step.loop_rank >= len(loops):
            return None
        targets = loops[step.loop_rank]
    elif step.role == "site_faces":
        targets = [e for e in ents if e.startswith("F")]
    elif step.role == "site_solids":
        targets = [e for e in ents if e.startswith("B")]
    elif step.role == "site_entities":
        targets = ents
    else:
        return None
    if step.role != "none" and not targets:
        return None
    params = _absolute(step.params, site, diagonal_mm)
    return (targets, params) if params is not None else None


def _absolute(pattern: dict, site: Any, diag: float | None) -> dict | None:
    out: dict = {}
    for k, v in pattern.items():
        if isinstance(v, dict) and "rel" in v:
            if not diag or diag <= 0:
                return None
            out[k] = [r * diag for r in v["rel"]] if isinstance(v["rel"], list) else v["rel"] * diag
        elif v == "<ids>":
            if k != "face_pairs":
                return None
            faces = sorted(e for e in site.entities or () if e.startswith("F"))
            pairs = [[a, b] for i, a in enumerate(faces) for b in faces[i + 1:]][:16]
            if not pairs:
                return None
            out[k] = pairs
        elif k == "params" and isinstance(v, dict):
            inner = _absolute(v, site, diag)
            if inner is None:
                return None
            out[k] = inner
        else:
            out[k] = v
    return out


# --- the store ------------------------------------------------------------------------------------------------------


class ObjectBackend(Protocol):
    def list(self, prefix: str) -> list[str]: ...

    def get(self, key: str) -> str | None: ...

    def put_if_absent(self, key: str, text: str) -> bool: ...


class DirBackend:
    """A directory of JSON files (local runs and tests)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*.json"))

    def get(self, key: str) -> str | None:
        p = self.root / key
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def put_if_absent(self, key: str, text: str) -> bool:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(p, "x", encoding="utf-8") as fh:
                fh.write(text)
        except FileExistsError:
            return False
        return True


class GcsBackend:
    """Objects under `gs://bucket/prefix` (the eval bucket). `bucket` is injectable for tests."""

    def __init__(self, uri: str, *, bucket: Any = None) -> None:
        from meshpipeline.contracts.gcs_buckets import split_gs

        self._bucket_name, prefix = split_gs(uri)
        self._prefix = prefix.strip("/")
        self._bucket = bucket

    def _b(self) -> Any:
        if self._bucket is None:
            from meshpipeline.contracts import gcs_buckets
            self._bucket = gcs_buckets.bucket(self._bucket_name)
        return self._bucket

    def _full(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def list(self, prefix: str) -> list[str]:
        cut = len(self._full(""))
        return sorted(b.name[cut:] for b in self._b().list_blobs(prefix=self._full(prefix))
                      if b.name.endswith(".json"))

    def get(self, key: str) -> str | None:
        blob = self._b().blob(self._full(key))
        return blob.download_as_text() if blob.exists() else None

    def put_if_absent(self, key: str, text: str) -> bool:
        from meshpipeline.contracts import gcs_buckets
        try:
            self._b().blob(self._full(key)).upload_from_string(text, content_type="application/json",
                                                               if_generation_match=0)
        except gcs_buckets.precondition_failed():
            return False
        return True


def backend_for(uri: str) -> ObjectBackend:
    if not uri or not uri.strip():
        raise ValueError("recipe memory needs a location: CAD_REPAIR_V4_RECIPES_URI (gs://bucket/prefix or a dir)")
    return GcsBackend(uri) if uri.startswith("gs://") else DirBackend(uri)


class RecipeStore:
    """Recipes as versioned JSON: `recipes/v1/<signature key of step 1>/<recipe id>/<file sha256>.json`, one object per
    (recipe, witnessing file), written create-if-absent so shards never overwrite each other."""

    def __init__(self, backend: ObjectBackend) -> None:
        self.backend = backend

    def record(self, recipe: Recipe, *, split: str, wave_type: str | None, file_sha256: str,
               provenance: dict | None = None) -> str:
        """Store `recipe` as witnessed by a clean file. REFUSED unless `split` is the dev split and the wave is not
        the held-out wave. Returns the object key."""
        if split != WRITABLE_SPLIT:
            raise RecipeWriteRefused(f"recipes are learned from the {WRITABLE_SPLIT!r} split only; refusing a "
                                     f"{split!r} file (held-out and clean-control files never write)")
        if wave_type in _FORBIDDEN_WAVE_TYPES:
            raise RecipeWriteRefused(f"a {wave_type!r} wave never writes recipes")
        if len(file_sha256) != 64 or any(c not in "0123456789abcdef" for c in file_sha256):
            raise ValueError("file_sha256 must be a sha256 hex digest (no names or paths are stored)")
        key = f"{_ROOT}/{signature_key(recipe.steps[0].site)}/{recipe.id}/{file_sha256}.json"
        body = {**recipe.to_json(), "witness": {"file_sha256": file_sha256, "split": split,
                                                 **{k: v for k, v in (provenance or {}).items()
                                                    if k in ("wave_id", "arm", "commit") and v}}}
        self.backend.put_if_absent(key, json.dumps(body, sort_keys=True))
        return key

    def load(self) -> RecipeBook:
        recipes: dict[str, Recipe] = {}
        witnesses: dict[str, set[str]] = {}
        for key in self.backend.list(_ROOT + "/"):
            text = self.backend.get(key)
            if text is None:
                continue
            try:
                d = json.loads(text)
                r = Recipe.from_json(d)
            except (ValueError, TypeError, KeyError):
                continue                     # another schema, or a torn object: not this book's
            recipes[r.id] = r
            witnesses.setdefault(r.id, set()).add(str(d.get("witness", {}).get("file_sha256", key)))
        return RecipeBook(recipes, {rid: len(w) for rid, w in witnesses.items()})


# --- the book: retrieval ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    recipe_id: str
    step_index: int
    step: Step
    score: float
    support: int


@dataclass
class RecipeBook:
    """A frozen snapshot of the store. Retrieval is a pure function of the snapshot and the query."""

    recipes: dict[str, Recipe] = field(default_factory=dict)
    support: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._entries = [(rid, i, s) for rid in sorted(self.recipes) for i, s in enumerate(self.recipes[rid].steps)]

    def __len__(self) -> int:
        return len(self.recipes)

    @property
    def snapshot(self) -> str:
        """A hash of the book's content, for the shard's provenance."""
        return hashlib.sha256(json.dumps(sorted(self.recipes), sort_keys=True).encode()).hexdigest()[:16]

    def lookup(self, site: Any, k: int = 3, *, min_score: float = 0.5 * MAX_SIMILARITY) -> list[Hit]:
        """The top-k steps that worked on sites like `site`: ranked by similarity, then support (how many clean files
        witnessed the recipe), then recipe id and step index - so equal inputs always give equal output."""
        q = signature(site)
        hits: list[Hit] = []
        for rid, i, step in self._entries:
            score = similarity(q, step.site)
            if score is None or score < min_score:
                continue
            hits.append(Hit(rid, i, step, round(score, 6), self.support.get(rid, 1)))
        hits.sort(key=lambda h: (-h.score, -h.support, h.recipe_id, h.step_index))
        out, seen = [], set()
        for h in hits:                       # one hit per distinct step pattern
            pat = json.dumps([h.step.op, h.step.role, h.step.loop_rank, h.step.params], sort_keys=True)
            if pat in seen:
                continue
            seen.add(pat)
            out.append(h)
            if len(out) == k:
                break
        return out

    def advice(self, sites: Iterable[Any], k: int = 3) -> list[dict]:
        """What the model is shown: per site, the recipes whose steps matched it, as id-free sequences."""
        out = []
        for site in sites:
            hits = self.lookup(site, k)
            if not hits:
                continue
            out.append({"site": site.id, "similar_recipes": [
                {"recipe": h.recipe_id, "similarity": round(h.score / MAX_SIMILARITY, 3), "clean_files": h.support,
                 "matched_step": h.step_index, "sequence": self.recipes[h.recipe_id].summary()} for h in hits]})
        return out


class RecipeMemory:
    """What a shard holds: the book it READS (a snapshot) and, on the dev split only, the store it WRITES to.

    `for_shard` builds it from the flag and the location. A shard of any other split, or the held-out wave, gets a
    read-only memory (`store` None): it retrieves what dev learned and can never write. `RecipeStore.record` refuses
    those splits as well, so the rule holds even if a caller builds a writer by hand."""

    def __init__(self, book: RecipeBook, store: RecipeStore | None, *, split: str, wave_type: str | None,
                 provenance: dict | None = None) -> None:
        self.book, self.store = book, store
        self.split, self.wave_type = split, wave_type
        self.provenance = dict(provenance or {})
        self.counts = {"written": 0, "write_errors": 0, "clean_without_recipe": 0}

    @classmethod
    def for_shard(cls, uri: str, *, split: str, wave_type: str | None, provenance: dict | None = None,
                  backend: ObjectBackend | None = None) -> RecipeMemory:
        store = RecipeStore(backend if backend is not None else backend_for(uri))
        writable = split == WRITABLE_SPLIT and wave_type not in _FORBIDDEN_WAVE_TYPES
        return cls(store.load(), store if writable else None, split=split, wave_type=wave_type,
                   provenance=provenance)

    def describe(self) -> dict:
        return {"enabled": True, "snapshot": self.book.snapshot, "recipes": len(self.book),
                "writes": self.store is not None, "split": self.split, **self.counts}

    def learn(self, result: Any, *, file_sha256: str, diagonal_mm: float | None) -> str | None:
        """Store the recipe of a CLEAN search on a dev file; the object key, or None (read-only memory, not clean,
        no ops on the path, or a storage error - counted, never raised: a write is a by-product of a measured file,
        not part of its measurement). A refused split DOES raise: that would be a wiring defect."""
        if self.store is None or not getattr(result, "clean", False):
            return None
        diag = diagonal_mm or next((float(st.graph.get("diagonal_mm")) for item in result.path_steps
                                    for st in item.get("sites") or () if (st.graph or {}).get("diagonal_mm")), None)
        recipe = recipe_from_path(result.path_steps, diagonal_mm=diag)
        if recipe is None:
            self.counts["clean_without_recipe"] += 1
            return None
        try:
            key = self.store.record(recipe, split=self.split, wave_type=self.wave_type, file_sha256=file_sha256,
                                    provenance=self.provenance)
        except RecipeWriteRefused:
            raise
        except Exception:  # noqa: BLE001 - the eval bucket being unwell must not change a file's row
            self.counts["write_errors"] += 1
            return None
        self.counts["written"] += 1
        return key


__all__ = ["Hit", "RecipeMemory", "MAX_SIMILARITY", "RECIPE_SCHEMA", "Recipe", "RecipeBook", "RecipeStore", "RecipeWriteRefused",
           "Step", "WRITABLE_SPLIT", "backend_for", "recipe_from_path", "replay", "signature", "signature_key",
           "similarity"]
