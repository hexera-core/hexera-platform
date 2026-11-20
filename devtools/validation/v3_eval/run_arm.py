#!/usr/bin/env python3
# Responsibility: Run ONE shard of ONE arm of the CRS v3 verdict inside the image, and say exactly
#                 what ran: refuse a drifted environment, a flag the build does not read, a missing
#                 fTetWild or a sample that is not the staged one; then hand off to repair_corpus.
# Owns: the preflight, the model-router/telemetry installation for a harness run, the clean shard's
#       input directory, and PROVENANCE.json.
# Boundaries: it measures nothing itself - repair_corpus.main() runs the files and writes the rows.
#             No secret is printed or written: the key is only checked for presence.
"""One shard of one arm, in the container (launched by vm_startup.sh).

    python run_arm.py --arm v3-full --kind failing --shard 3/10 --inputs /eval/inputs \\
        --out /eval/out --code-root /eval/code --sample-json /eval/sample.json \\
        --commit <40-hex> --image <digest ref>

    python run_arm.py --arm v3-full --kind failing --preflight-only     # checks only, no CAD
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import platform
import socket
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE_ROOT_DEFAULT = HERE.parents[2]


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evalspec = _load("evalspec", HERE / "evalspec.py")


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TelemetryFile:
    """Every model call's telemetry record (role, model, tokens, estimated cost, status - never a
    prompt or a reply) appended to a JSONL file, so the arm's spend is measured, not estimated."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0
        self.cost = 0.0

    def record(self, call) -> None:
        rec = call.as_record()
        self.calls += 1
        self.cost += float(rec.get("estimated_cost_usd") or 0.0)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, default=str) + "\n")


def env_drift(arm_env: dict[str, str]) -> list[str]:
    return [f"{k}: arm wants {v!r}, the process has {os.environ.get(k)!r}"
            for k, v in sorted(arm_env.items()) if os.environ.get(k) != v]


def settings_problems(arm_env: dict[str, str], accept: set[str]) -> tuple[list[str], dict]:
    import meshpipeline.settings.cad_repair as rcfg

    settings = {k: v for k, v in vars(rcfg).items() if k.isupper()}
    problems = [p for p in evalspec.preflight(settings, arm_env)
                if not any(f" {name}=" in p or f" {name}:" in p for name in accept)]
    return problems, settings


def ftetwild() -> str | None:
    try:
        from meshpipeline.engines import fea_tetwild
        found = fea_tetwild.binary()
        return str(found) if found else None
    except Exception:  # noqa: BLE001 - absent engine module reads as absent binary
        return None


def install_model(out: Path) -> TelemetryFile:
    """The real router, an in-process capacity controller and a telemetry file: what
    runtime/composition.py binds in a worker, minus Redis. repair_corpus only installs a router for
    --planner, so without this every investigation ends `no_router` and every review has no model."""
    from meshpipeline.adapters.model_capacity.local import LocalCapacityController
    from meshpipeline.adapters.model_inference import router as real
    from meshpipeline.contracts import inference_telemetry, model_capacity, model_inference

    model_capacity.set_capacity_controller(LocalCapacityController())
    model_inference.set_model_router(real)
    sink = TelemetryFile(out / "model-calls.jsonl")
    inference_telemetry.set_inference_telemetry(sink)
    return sink


def failing_sample_sha(code_root: Path) -> tuple[str, int]:
    rc = _load("repair_corpus", code_root / "devtools/validation/repair_corpus.py")
    probe = rc._load_sibling("mesh_probe")
    manifest = probe.load_manifest(code_root / evalspec.MANIFEST)
    entries = rc.stratified_sample(manifest, evalspec.SAMPLE_SIZE, seed=evalspec.SEED)
    return evalspec.sha256_bytes(evalspec.sample_body(entries, seed=evalspec.SEED)), len(entries)


def clean_shard_dir(code_root: Path, inputs: Path, shard: str, into: Path) -> list[dict]:
    """This shard's clean parts, linked by their basenames into `into` (repair_corpus's directory
    mode reads them from there). Partitioned by repair_corpus.shard_of over the sha256-sorted
    manifest, exactly as a failing sample is."""
    rc = _load("repair_corpus", code_root / "devtools/validation/repair_corpus.py")
    manifest = json.loads((code_root / evalspec.CLEAN_MANIFEST).read_text())
    mine = rc.shard_of(manifest["files"], shard)
    into.mkdir(parents=True, exist_ok=True)
    for entry in mine:
        src = inputs / entry["sha256"] / Path(entry["name"]).name
        if not src.is_file():
            raise FileNotFoundError(f"clean part {entry['name']} is not staged at {src}")
        link = into / Path(entry["name"]).name
        if not link.exists():
            link.symlink_to(src)
    return mine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", required=True, choices=sorted(evalspec.ARMS))
    parser.add_argument("--kind", required=True, choices=evalspec.KINDS)
    parser.add_argument("--shard", default="1/1")
    parser.add_argument("--inputs", default="/eval/inputs")
    parser.add_argument("--out", default="/eval/out")
    parser.add_argument("--code-root", default=str(CODE_ROOT_DEFAULT))
    parser.add_argument("--sample-json", default="",
                        help="the STAGED sample.json; its sha256 must equal the sampler's")
    parser.add_argument("--commit", default="",
                        help="the code commit: --code-root holds its src/ and devtools/validation")
    parser.add_argument("--image-commit", default="",
                        help="the commit the image was built from (toolchain only)")
    parser.add_argument("--image", default="")
    parser.add_argument("--machine-type", default="")
    parser.add_argument("--accept-inert", action="append", default=[],
                        help="run although this flag is inert in the build (recorded)")
    parser.add_argument("--allow-no-ftetwild", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)

    code_root = Path(args.code_root)
    out = Path(args.out)
    spec = evalspec.arm(args.arm)
    accept = set(args.accept_inert)
    started, t0 = _now(), time.monotonic()
    prov: dict = {
        "label": f"{args.arm}/{args.kind}/{args.shard}", "arm": args.arm, "kind": args.kind,
        "shard": args.shard, "commit": args.commit, "image_commit": args.image_commit,
        "image": args.image,
        "machine_type": args.machine_type, "host": socket.gethostname(),
        "hardware_class": f"{platform.system().lower()}-{platform.machine().lower()}",
        "arm_env": dict(sorted(spec["env"].items())), "accepted_inert": sorted(accept),
        "model_role": spec["model_role"],
        "known_noops": {k: v for k, v in evalspec.KNOWN_NOOPS.items() if k in spec["env"]},
        "harness_flags": list(evalspec.HARNESS_FLAGS), "started_at": started,
    }
    problems: list[str] = []
    # THE CODE UNDER TEST IS THE ARCHIVE'S: meshpipeline must import from <code-root>/src, not
    # from the image's installed wheel (PYTHONPATH=/eval/code/src on the VM).
    import meshpipeline

    prov["meshpipeline_from"] = str(Path(meshpipeline.__file__).resolve())
    if args.commit and not prov["meshpipeline_from"].startswith(
            str((code_root / "src").resolve())):
        problems.append(f"CODE: meshpipeline imports from {prov['meshpipeline_from']}, not "
                        f"{code_root / 'src'} - the image's wheel would be measured instead of "
                        f"{args.commit[:12]}")
    drift = env_drift(spec["env"])
    if drift:
        problems += [f"ENV {d}" for d in drift]
    settings_issues, settings = settings_problems(spec["env"], accept)
    problems += settings_issues
    prov["ftetwild"] = ftetwild()
    if prov["ftetwild"] is None and spec["env"].get("CAD_REPAIR_TOLERANT_PROBE") == "live" \
            and not args.allow_no_ftetwild:
        problems.append("NO FTETWILD: CAD_REPAIR_TOLERANT_PROBE=live but this image has no "
                        "fTetWild, so tier 2b would read `unavailable` (use the mesh image)")
    if not os.environ.get(evalspec.MODEL_KEY_ENV):
        problems.append(f"NO MODEL KEY: {evalspec.MODEL_KEY_ENV} is not set (the package reviewer "
                        f"and the investigator would have no model)")
    if args.kind == "failing":
        sha, n = failing_sample_sha(code_root)
        prov["sample_sha256"], prov["sample_size"] = sha, n
        if args.sample_json:
            staged = evalspec.sample_sha256(Path(args.sample_json))
            prov["staged_sample_sha256"] = staged
            if staged != sha:
                problems.append(f"SAMPLE: the staged sample.json is {staged[:16]}, the sampler at "
                                f"this commit draws {sha[:16]} - the arms would not share it")
    else:
        clean = (code_root / evalspec.CLEAN_MANIFEST).read_bytes()
        prov["sample_sha256"] = evalspec.sha256_bytes(clean)
        prov["sample_size"] = len(json.loads(clean)["files"])

    for line in problems:
        print(f"PREFLIGHT {line}", file=sys.stderr)
    if args.preflight_only:
        print(json.dumps({"arm": args.arm, "problems": problems,
                          "ftetwild": prov["ftetwild"],
                          "sample_sha256": prov.get("sample_sha256")}, indent=1))
        return 1 if problems else 0

    out.mkdir(parents=True, exist_ok=True)
    code = 3
    try:
        if problems:
            prov["refused"] = problems
            return code
        sink = install_model(out)
        rc = _load("repair_corpus", code_root / "devtools/validation/repair_corpus.py")
        prov["operations_catalogue"] = rc.operation_catalogue()
        inputs = Path(args.inputs)
        if args.kind == "clean":
            prov["clean_files"] = [e["name"] for e in
                                   clean_shard_dir(code_root, inputs, args.shard,
                                                   out / "clean-inputs")]
            inputs = out / "clean-inputs"
        argv_rc = evalspec.harness_argv(args.arm, args.kind, shard=args.shard, inputs=str(inputs),
                                        out=str(out), code_root=str(code_root))
        prov["repair_corpus_argv"] = argv_rc
        prov["settings_seen"] = {k: str(v) for k, v in sorted(settings.items())
                                 if k.startswith(("CAD_REPAIR_", "CAD_INSPECT_"))}
        sys.argv = ["repair_corpus.py", *argv_rc]
        code = int(rc.main() or 0)
        prov["model_calls"], prov["model_cost_usd"] = sink.calls, round(sink.cost, 4)
        return code
    except Exception:  # noqa: BLE001 - the provenance must still be written
        prov["error"] = traceback.format_exc()[-4000:]
        code = 4
        return code
    finally:
        prov.update(ended_at=_now(), wall_seconds=round(time.monotonic() - t0, 1), exit_code=code)
        (out / "PROVENANCE.json").write_text(json.dumps(prov, indent=1, sort_keys=True,
                                                        default=str) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
