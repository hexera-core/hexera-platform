"""Task 49: prove, through run_arm's OWN path (real GCS markers, real probe gate, real flags), that the lowered
ledger cap and the per-file ceiling bind. Calls NO model: settles are fabricated at a fixed cost."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/w/devtools/validation/v4_eval")
import run_arm
bucket = "dev-crsv3-transfer-224734058693"
args = run_arm.parse_args(["--arms", "search_ai", "--split", "dev", "--h-mm", "auto", "--wave-type", "normal",
    "--wave-id", sys.argv[1], "--n-shards", "1", "--ledger-path", tempfile.mkdtemp() + "/log.jsonl",
    "--marker-bucket", bucket, "--model-usd-cap", "5", "--file-usd-cap", "0.5", "--ops", "24"])
markers, backend = run_arm.make_marker_store(bucket)
print("backend", backend, "probe_problem:", run_arm.check_marker_binding(markers, backend, bucket))
ledger = run_arm.build_ledger(args, markers)
print("slice cap", ledger.cap_usd)
ledger.cap_to(args.model_usd_cap)
print("cap after flag", ledger.cap_usd)
arm = run_arm.effective_arm(run_arm.evalspec.ARMS["search_ai"], args)
print("arm ops", arm.budget.ops, "file ceiling", arm.budget.model_usd)
n_ok = 0
for i in range(40):
    a = ledger.admit(f"f{i}", 0.40, file_ceiling_usd=arm.budget.model_usd)
    if not a.ok:
        print("REFUSED file", i, a.reason); break
    ledger.settle(a.reservation, file_id=f"f{i}", job_id="cap", provider="x", model="x", input_tokens=0,
                  cached_input_tokens=0, output_tokens=0, cost_usd=0.40)
    n_ok += 1
print("admitted", n_ok, "spent", ledger.spent_usd())
a = ledger.admit("g", 0.30, file_ceiling_usd=arm.budget.model_usd)
ledger.release(a.reservation) if a.ok else None
a = ledger.admit("f0", 0.20, file_ceiling_usd=arm.budget.model_usd)
print("same-file over 0.50 ->", a.ok, a.reason)
assert n_ok == 12 and ledger.spent_usd() <= 5.0 and not a.ok
print("CAP BINDS")
