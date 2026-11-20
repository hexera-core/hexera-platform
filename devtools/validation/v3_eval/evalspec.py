#!/usr/bin/env python3
# Responsibility: The CRS v3 verdict runs (T19, T29) as data: which arms exist, what environment
#                 each one runs under, which machines may carry them, and the refusals that keep
#                 two arms comparable (one sample, one commit, one image).
# Owns: ARMS, KINDS, the machine-family rule, the settings preflight, the cross-arm checks and the
#       T29 acceptance rule.
# Boundaries: PURE. No gcloud, no docker, no CAD, no import of the repair stack: the launcher, the
#             in-container runner and the collector all load this file, and the unit tests hold
#             every refusal here without a cloud or a kernel.
"""The v3 verdict runs, stated once.

Every arm runs `devtools/validation/repair_corpus.py` over the SAME frozen sample with the SAME
harness flags; arms differ only by environment. The env is checked against the settings module
the image actually ships (`preflight`), because a flag the build does not read is a flag that
silently did nothing - and an arm that silently equals another arm is the one result this
comparison cannot survive.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

# --------------------------------------------------------------------------- the frozen sample

MANIFEST = "devtools/validation/corpora/mesh-failure-real.json"
CLEAN_MANIFEST = "devtools/validation/corpora/clean-control-100.json"
SAMPLE_SIZE = 120
SEED = 20261120
CLEAN_SIZE = 100

#: The two input sets an arm is run over. `failing` is the stratified sample of the 9,956 real
#: mesh failures; `clean` is the 100-part clean control, which must come out untouched.
KINDS = ("failing", "clean")

# --------------------------------------------------------------------------- the posture

#: THE EVERYTHING-ON SET (T6's posture). Every name is CAD_REPAIR_-prefixed in the environment.
EVERYTHING_ON: dict[str, str] = {
    "CAD_REPAIR_ENABLED": "true",
    "CAD_REPAIR_PACKAGE_REVIEW": "live",
    "CAD_REPAIR_TOLERANT_PROBE": "live",
    "CAD_REPAIR_MESH_PROBE": "live",
    "CAD_REPAIR_REGION_LOOP": "live",
    "CAD_REPAIR_AUTO_ACCEPT": "live",
    "CAD_REPAIR_MEMORY": "live",
    "CAD_REPAIR_BUDGET_ATTEMPTS": "live",
    "CAD_REPAIR_REBUILD_FACE": "true",
    "CAD_REPAIR_FIX_SMALL_FACES": "true",
    "CAD_REPAIR_FRAGMENT_BODIES": "true",
    "CAD_REPAIR_FILL_PLANAR_HOLES": "true",
    "CAD_REPAIR_FILL_NONPLANAR_HOLES": "true",
    "CAD_REPAIR_SPLIT_CLOSED_FACES": "true",
    "CAD_REPAIR_CLOSE_WIRE_GAPS": "true",
    "CAD_REPAIR_HEAL_SELF_INTERSECTIONS": "true",
    "OMP_NUM_THREADS": "1",
}

#: The model the reviewer, the investigator and the tournament's model sources call. They all go
#: through `call_repair_review_model`, which routes on INTAKE_ROUTE (agents/intake/settings.py ->
#: settings/inventory.py: INTAKE_PROVIDER, INTAKE_MODEL). The key arrives from Secret Manager on
#: the VM, never through here.
MODEL_ENV: dict[str, str] = {"INTAKE_PROVIDER": "openai", "INTAKE_MODEL": "gpt-5.6-luna"}
MODEL_SECRET = "openai-api-key"
MODEL_KEY_ENV = "OPENAI_API_KEY"

#: The harness flags every arm shares. `--goal benchmark` is the T29 plan's; `--mesh-probe
#: --force-repair` is the coarse gmsh probe verdict (tier 2) T6 was scored on.
HARNESS_FLAGS: tuple[str, ...] = ("--mesh-probe", "--force-repair", "--goal", "benchmark")


def _arm(investigator: str, tournament: str, k: str | None = None) -> dict[str, str]:
    env = {**EVERYTHING_ON, **MODEL_ENV, "CAD_REPAIR_INVESTIGATOR": investigator,
           "CAD_REPAIR_TOURNAMENT": tournament}
    if k is not None:
        env["CAD_REPAIR_TOURNAMENT_K"] = k
    return env


#: THE ARMS. `v3-full` is BOTH T19's "tournament on" arm AND T29's v3 arm: same sample, same commit,
#: same environment, so running it once serves both comparisons.
ARMS: dict[str, dict] = {
    "deterministic": {
        "short": "det", "env": _arm("off", "off"), "investigator": "off",
        "used_by": ("T29",),
        "about": "everything-on, investigator and tournament off (T6's posture, Linux image)",
    },
    "v3-no-tournament": {
        "short": "v3nt", "env": _arm("live", "off"), "investigator": "live",
        "used_by": ("T19",),
        "about": "everything-on, investigator live, tournament off",
    },
    "v3-full": {
        "short": "v3full", "env": _arm("live", "live", "3"), "investigator": "live",
        "used_by": ("T19", "T29"),
        "about": "everything-on, investigator live, tournament live with K=3",
    },
}

#: Which arm pairs each verdict compares: (baseline, treatment).
COMPARISONS = {"T19": ("v3-no-tournament", "v3-full"), "T29": ("deterministic", "v3-full")}


def arm(name: str) -> dict:
    if name not in ARMS:
        raise ValueError(f"unknown arm {name!r}; the arms are {', '.join(ARMS)}")
    return ARMS[name]


def env_file_text(name: str) -> str:
    """The arm's environment as a docker --env-file body. Carries no credential."""
    return "".join(f"{k}={v}\n" for k, v in sorted(arm(name)["env"].items()))


def harness_argv(name: str, kind: str, *, shard: str, inputs: str, out: str,
                 code_root: str) -> list[str]:
    """repair_corpus.py's argv for one shard of one arm. The failing kind selects the sample from
    the manifest by repair_corpus's own sampler; the clean kind is a directory the runner filled
    with this shard's files (repair_corpus's directory mode, as T6's clean control ran)."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; the kinds are {', '.join(KINDS)}")
    spec = arm(name)
    argv: list[str] = []
    if kind == "failing":
        argv += ["--manifest", f"{code_root}/{MANIFEST}", "--roots", inputs,
                 "--sample", str(SAMPLE_SIZE), "--seed", str(SEED), "--shard", shard]
    else:
        argv += [inputs]
    argv += list(HARNESS_FLAGS)
    if spec["investigator"] != "off":
        argv += ["--investigator", spec["investigator"],
                 "--transcripts", f"{out}/transcripts.jsonl"]
    # --bundle streams one row per file into bundle/rows.jsonl AS IT IS PRODUCED (the VM syncs it
    # to GCS), and --json writes the full rows at the end for the collector.
    argv += ["--json", f"{out}/rows.json", "--bundle", f"{out}/bundle",
             "--track", "mesh-failure-real" if kind == "failing" else "clean-control"]
    return argv


# --------------------------------------------------------------------------- machines

#: Families an eval VM may use. N2 is NOT here: its regional quota is shared with the product
#: fleets and was exhausted by an earlier run. c2d and c3 have their own quotas in us-central1.
ALLOWED_FAMILIES = ("c2d", "c3", "c3d", "c2")
DEFAULT_MACHINE = "c2d-highcpu-4"


def validate_machine_type(machine_type: str) -> str:
    """The machine type, or ValueError naming why not. Only the allowed families pass."""
    text = str(machine_type or "").strip().lower()
    family = text.split("-", 1)[0]
    if family in ("n2", "n2d"):
        raise ValueError(
            f"{machine_type!r} is in the {family} family, whose regional CPU quota is shared with "
            f"the product fleets and was exhausted before; use one of: "
            f"{', '.join(ALLOWED_FAMILIES)} (e.g. {DEFAULT_MACHINE})")
    if family not in ALLOWED_FAMILIES or text.count("-") < 2:
        raise ValueError(f"{machine_type!r} is not an allowed eval machine type; use one of the "
                         f"families {', '.join(ALLOWED_FAMILIES)} (e.g. {DEFAULT_MACHINE})")
    return text


def vcpus_of(machine_type: str) -> int:
    """vCPUs from a predefined type name (`c2d-highcpu-4` -> 4)."""
    try:
        return int(machine_type.rsplit("-", 1)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"cannot read a vCPU count off {machine_type!r}") from exc


def parse_shard(spec: str) -> tuple[int, int]:
    """`I/N` -> (I, N), refusing what repair_corpus.shard_of would refuse."""
    index, _, count = str(spec).partition("/")
    try:
        i, n = int(index), int(count)
    except ValueError as exc:
        raise ValueError(f"shard wants I/N, not {spec!r}") from exc
    if n < 1 or not 1 <= i <= n:
        raise ValueError(f"shard wants I/N with 1 <= I <= N, not {spec!r}")
    return i, n


# --------------------------------------------------------------------------- preflight

#: What the CODE reads when a posture attribute is absent from the settings module: the getattr
#: fallback in tournament.py / memory.py. Absent and requested == fallback is harmless; absent and
#: requested != fallback means the flag would do NOTHING.
CODE_FALLBACKS = {"CAD_REPAIR_TOURNAMENT": "off", "CAD_REPAIR_TOURNAMENT_K": "1",
                  "CAD_REPAIR_MEMORY": "off"}


def _norm(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip().lower()


def preflight(settings: Mapping[str, object], env: Mapping[str, str]) -> list[str]:
    """Problems with running `env` against a build whose settings module holds `settings`
    (name -> effective value, e.g. `vars(meshpipeline.settings.cad_repair)`). Empty means every
    CAD_REPAIR_ flag in the arm is read by the build AND took the requested value.

    A flag the module does not define is reported as INERT unless the arm asks for exactly what
    the code falls back to - which is how a "tournament on" arm that is secretly tournament-off is
    caught before it costs a fleet.
    """
    problems: list[str] = []
    for name, wanted in sorted(env.items()):
        if not name.startswith("CAD_REPAIR_"):
            continue
        if name not in settings:
            fallback = CODE_FALLBACKS.get(name)
            if fallback is not None and _norm(wanted) == fallback:
                continue
            problems.append(
                f"INERT {name}={wanted}: this build's settings/cad_repair.py defines no {name}, so "
                f"the code reads {fallback if fallback is not None else 'nothing'} and the flag "
                f"does nothing")
            continue
        got = _norm(settings[name])
        if got != _norm(wanted):
            problems.append(f"MISMATCH {name}: asked {wanted!r}, the build read {settings[name]!r} "
                            f"(fail-closed posture or a typo)")
    return problems


# --------------------------------------------------------------------------- cross-arm checks

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sample_sha256(path: Path) -> str:
    """The sample's identity: sha256 of the sample.json BYTES repair_corpus.write_bundle_sample
    wrote. Two arms share a sample exactly when these agree."""
    return sha256_bytes(Path(path).read_bytes())


def sample_body(entries: Iterable[Mapping], *, seed: int) -> bytes:
    """The bytes repair_corpus.write_bundle_sample would write for these entries - same keys, same
    sort, same json.dumps arguments - so a sample can be fingerprinted without writing a file.
    A test pins this against the real writer, so the two cannot drift."""
    body = {"seed": int(seed),
            "files": [{"name": e["name"], "sha256": e["sha256"]}
                      for e in sorted(entries, key=lambda e: e["sha256"])]}
    return json.dumps(body, indent=2, sort_keys=True).encode("utf-8")


def check_same(provenances: Iterable[Mapping], key: str, *, what: str) -> str:
    """The one value every provenance states under `key`, or ValueError listing the split."""
    seen: dict[str, list[str]] = {}
    for prov in provenances:
        value = str(prov.get(key) or "")
        if not value:
            raise ValueError(f"{prov.get('label', '?')} states no {what} ({key})")
        seen.setdefault(value, []).append(str(prov.get("label", "?")))
    if not seen:
        raise ValueError(f"no runs to compare for {what}")
    if len(seen) != 1:
        detail = "; ".join(f"{v[:16]}: {', '.join(labels)}" for v, labels in sorted(seen.items()))
        raise ValueError(f"the runs do not share one {what}: {detail}")
    return next(iter(seen))


def check_comparable(provenances: list[Mapping]) -> dict:
    """Refuse a set of shard provenances that cannot be compared: mixed commits, mixed images, or -
    within a kind - mixed samples. Returns {commit, image, samples: {kind: sha256}}."""
    if not provenances:
        raise ValueError("no shard provenance to check")
    commit = check_same(provenances, "commit", what="commit")
    image = check_same(provenances, "image", what="image digest")
    samples = {}
    for kind in sorted({str(p.get("kind")) for p in provenances}):
        samples[kind] = check_same([p for p in provenances if p.get("kind") == kind],
                                   "sample_sha256", what=f"{kind} sample")
    return {"commit": commit, "image": image, "samples": samples}


# --------------------------------------------------------------------------- the verdicts

RATIO = 4


def t29_verdict(det: Mapping, v3: Mapping, *, clean_untouched: bool,
                cost_per_fixed_part: float | None) -> dict:
    """The T29 acceptance rule over two meshing summaries (repair_corpus.meshing_summary).

    v3 newly_meshed >= 4x deterministic newly_meshed, newly_broken == 0 in both, the clean control
    untouched, and the cost per fixed part printed. If the deterministic arm newly meshed NOTHING
    the ratio is degenerate (anything >= 0): the verdict is then judged on v3's absolute count,
    which is stated plainly, and the 4x goal is reported as undefined - never as met.
    """
    d, v = int(det.get("newly_meshed") or 0), int(v3.get("newly_meshed") or 0)
    broken = int(det.get("newly_broken") or 0) + int(v3.get("newly_broken") or 0)
    out: dict = {"deterministic_newly_meshed": d, "v3_newly_meshed": v,
                 "newly_broken_total": broken, "clean_untouched": bool(clean_untouched),
                 "cost_per_fixed_part_usd": cost_per_fixed_part,
                 "cost_printed": cost_per_fixed_part is not None}
    if d == 0:
        out["ratio"] = None
        out["ratio_rule"] = (f"4x goal: undefined (deterministic ceiling 0); judged on v3's "
                             f"absolute newly_meshed = {v}")
        ratio_ok = v > 0
    else:
        out["ratio"] = round(v / d, 3)
        out["ratio_rule"] = f"v3 {v} vs {RATIO}x deterministic {d} = {RATIO * d}"
        ratio_ok = v >= RATIO * d
    out["ratio_ok"] = ratio_ok
    out["accepted"] = bool(ratio_ok and broken == 0 and clean_untouched
                           and cost_per_fixed_part is not None)
    return out


def rule_of_three_upper(n: int) -> float:
    """95% upper bound on a rate after n trials with zero events (3/n)."""
    return 3.0 / n if n > 0 else 1.0
