# Responsibility: Keep every argument the deploy passes to gcloud an argument gcloud still accepts.
# Owns: the stale-flag checker's reading of shell, and the three sites where a stale argument was found.
# Boundaries: the parser tests read strings; the surface test reads `gcloud --help` and nothing else.

# WHY THIS FILE EXISTS. gcloud removes things, and a removed flag is not an error in a shell script.
# Three were found in one sweep, and each had been dead for an unknown number of releases:
#
#   --min-ready            on the fleet roll     - ABORTED v0.1.5 at stage 18 of 19 (fixed already)
#   --limit                on `storage ls`       - failed into 2>/dev/null, so a FULL exchange
#                                                  bucket read as empty and was deleted unasked
#   compute autoscalers    in the queue publisher- the whole command group is gone, so the live
#                                                  autoscaler was never read and every deploy
#                                                  overwrote the console's sizing with env defaults
#
# Only the first one announced itself. The other two failed into `|| true` and a fallback, which is
# the more dangerous shape: the deploy stayed green and did the wrong thing quietly. The checker
# under test asks the INSTALLED gcloud about every flag in deploy/, so the next removal is found by
# a gate rather than by a release.
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"
sys.path.insert(0, str(REPO / "devtools" / "quality"))

import check_gcloud_flags as checker  # noqa: E402


def _calls(tmp_path: Path, body: str):
    script = tmp_path / "fragment.sh"
    script.write_text(body, encoding="utf-8")
    return checker.invocations(script)


# ── the parser: what it reads as an invocation, and what it refuses to ──────────────────────────


def test_a_plain_invocation_is_read_with_its_flags(tmp_path):
    (call,) = _calls(tmp_path, 'gcloud storage ls "gs://b/**" --limit 1\n')
    assert call.command == ("storage", "ls")
    assert call.flags == {"--limit"}


def test_flags_split_across_line_continuations_are_read(tmp_path):
    (call,) = _calls(tmp_path, 'gcloud run deploy "${SVC}" \\\n  --region eu \\\n  --cpu 2\n')
    assert call.flags == {"--region", "--cpu"}


def test_a_gcloud_inside_a_command_substitution_is_read(tmp_path):
    """The bucket probe that hid a removed flag is written `$(gcloud storage ls ...)` inside a
    test, so a parser that only reads line-leading commands would never have seen it."""
    (call,) = _calls(tmp_path, 'if [ -n "$(gcloud storage ls gs://b --limit 1)" ]; then :; fi\n')
    assert call.command == ("storage", "ls")
    assert call.flags == {"--limit"}


def test_flags_in_an_expanded_array_belong_to_the_command_that_expands_it(tmp_path):
    """`gc run deploy "${deploy_args[@]}"` passes thirty flags that are nowhere near the call."""
    (call,) = _calls(
        tmp_path,
        'deploy_args=(\n  --image "${IMG}"\n  --cpu 2\n)\ngc run deploy "${SVC}" "${deploy_args[@]}"\n',
    )
    assert {"--image", "--cpu"} <= call.flags


def test_prose_in_a_multi_line_message_is_not_an_invocation(tmp_path):
    """run-migrations.sh prints `gcloud beta run jobs executions logs read <execution>` as ADVICE,
    on its own line inside a multi-line string. Reading it as a call reports flag errors against
    something no script runs - and a gate that reports those gets switched off."""
    assert (
        _calls(
            tmp_path,
            'die "the job failed. Read what it refused:\n'
            "     gcloud beta run jobs executions logs read <exec> --region eu\"\n",
        )
        == []
    )


def test_a_flag_inside_a_quoted_argument_is_not_a_gcloud_flag(tmp_path):
    """`--args "outreach-worker.js,--once"` passes `--once` to node, not to gcloud."""
    (call,) = _calls(tmp_path, 'gcloud run jobs create j --args "worker.js,--once" --region eu\n')
    assert call.flags == {"--args", "--region"}


def test_a_heredoc_body_is_not_shell(tmp_path):
    assert _calls(tmp_path, "python3 - <<'PY'\ngcloud compute nonsense --made-up\nPY\n") == []


def test_a_commented_out_invocation_is_not_read(tmp_path):
    assert _calls(tmp_path, "# gcloud compute nonsense --made-up\n") == []


# ── the three sites a stale argument was found at ───────────────────────────────────────────────


def test_the_bucket_emptiness_probe_does_not_use_limit():
    """`gcloud storage ls` has no `--limit`. With it, the call aborted into `2>/dev/null`, the probe
    saw no output, and the 'this bucket is NOT empty - delete it?' prompt never ran: a bucket full
    of meshes somebody was waiting on was removed without the question."""
    text = (SCRIPTS / "mesh-destroy.sh").read_text(encoding="utf-8")
    probe = next(line for line in text.split("\n") if "storage ls" in line and "BUCKET" in line)
    assert "--limit" not in probe, (
        "the bucket probe passes --limit, which gcloud rejects - the probe then reports every "
        "bucket as empty and the confirmation prompt is skipped")
    assert "head" in probe, "the probe no longer reads anything; every bucket would look empty"


def test_the_publisher_reads_the_live_autoscaler_off_the_group():
    """`gcloud compute autoscalers` is gone from every track. Reading it returned nothing, the
    fallbacks supplied this deployment's env values, and `set-autoscaling` - which replaces the
    whole policy - wrote them over the floor, ceiling and cooldown the admin console owns."""
    text = (SCRIPTS / "create-queue-depth-publisher.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.split("\n") if not line.strip().startswith("#"))
    assert "compute autoscalers" not in code, (
        "the publisher is back on `gcloud compute autoscalers`, which no longer exists - the live "
        "sizing reads empty and the deploy overwrites the console's numbers")
    assert "autoscaler.autoscalingPolicy.minNumReplicas" in code, (
        "the live sizing is not read at all now - set-autoscaling would apply the deployment's "
        "values over the console's on every run")


def test_the_printed_cloud_sql_remediation_is_a_command_that_runs():
    """Advice printed to an operator is copy-pasted verbatim. `gcloud sql instances patch` has no
    `--backup`; it has `--no-backup`, and backups are enabled by giving `--backup-start-time`."""
    text = (SCRIPTS / "create-data-tier.sh").read_text(encoding="utf-8")
    assert "--backup --backup-start-time" not in text, (
        "the remediation prints `--backup`, which sql instances patch rejects - the operator's "
        "paste aborts with 'unrecognized arguments'")


# ── the surface itself, against the CLI that will run it ────────────────────────────────────────


@pytest.mark.skipif(shutil.which("gcloud") is None, reason="gcloud not installed")
def test_every_argument_the_deploy_passes_is_one_this_gcloud_accepts():
    """THE GATE. Not a copy of gcloud's surface kept in this file - a copy drifts the same way the
    scripts did. The installed CLI is asked directly."""
    assert checker.main([]) == 0, (
        "the deploy passes arguments this gcloud does not accept; see the failures printed above")


@pytest.mark.skipif(shutil.which("gcloud") is None, reason="gcloud not installed")
def test_the_gate_still_catches_the_flag_that_stopped_the_release(tmp_path, capsys):
    """Non-vacuity: a gate that cannot fail proves nothing. The flag that aborted v0.1.5 is put
    back into a throwaway script and the checker must name it."""
    script = tmp_path / "roll.sh"
    script.write_text(
        "gcloud compute instance-groups managed rolling-action start-update mig \\\n"
        '  --zone z --version "template=t" --min-ready 180s\n',
        encoding="utf-8",
    )
    assert checker.main([str(script)]) == 1
    assert "--min-ready" in capsys.readouterr().out
