#!/usr/bin/env python3
# Responsibility: Prove no deployment spec or deployment environment carries a secret VALUE instead of a reference.
# Owns: the roster of secret-bearing settings (read from the catalogue), and what counts as a reference.
# Boundaries: a repository gate; it reads files, makes no cloud call, and never prints a value it finds.

# Plaintext-secret gate for deployment specs.
#
# docs/deployment/gcp-live-inventory.md records what this exists to stop: DEEPINFRA_API_KEY,
# DEEPSEEK_API_KEY, POSTGRES_PASSWORD and MINIO_SECRET_KEY sitting as literal values in the public
# `hexera-dev-api` Cloud Run service spec - readable by anyone with `run.services.get` and echoed
# into every `gcloud` output and deploy log that dumps the service. The discipline already existed
# one layer down: scripts/bootstrap-env.sh writes "NO SECRET VALUES: only Secret Manager CONTAINER
# names appear below" into deploy/gcp/generated.env, and deploy-preflight.sh refuses that file if it
# carries a credential value. This gate is the same rule, said once, over the artefact that actually
# reaches Google: the rendered spec.
#
# WHICH settings are secret-bearing is not decided here. src/meshpipeline/settings/inventory.py
# declares `secret=True` on the entries that hold a real credential; this reads that flag. A regex
# over names would have its own opinion, and the two would drift - a new credential setting would be
# declared in the catalogue and silently unpoliced here.
#
#   python devtools/quality/check_deploy_secrets.py                   # the repository's deploy specs
#   python devtools/quality/check_deploy_secrets.py SUBJECT [...]     # named specs / env files
#
# Auditing a LIVE service is the same command over its rendered spec:
#
#   gcloud run services describe hexera-dev-api --region europe-west2 --format=json > /tmp/svc.json
#   python devtools/quality/check_deploy_secrets.py /tmp/svc.json
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

#: Spec documents the default scan reads. A rendered `gcloud ... --format=json`, a Knative manifest,
#: and the templates those are rendered from all parse as one of these.
SPEC_SUFFIXES = (".json", ".yaml", ".yml")

#: The machine-owned deployment environment. bootstrap-env.sh writes it, deploy-preflight.sh already
#: refuses a credential value in it, and it is .gitignored - so it is named explicitly rather than
#: discovered through git.
GENERATED_ENV = ROOT / "deploy" / "gcp" / "generated.env"

#: A Secret Manager resource name. The value in the spec is then a POINTER: reading it needs
#: secretmanager.versions.access on that secret, which is the property we are protecting.
_RESOURCE_NAME = re.compile(r"projects/[^/\s]+/secrets/[A-Za-z0-9_-]{1,255}(/versions/(latest|\d+))?")

#: The `gcloud run deploy --set-secrets` shorthand, `<secret>:<version>`. Accepting it is a
#: deliberate, narrow false-negative: a credential whose literal text is a Secret Manager id
#: followed by ":latest" or ":<digits>" would pass. The alternative - refusing the shorthand - makes
#: the rule unstatable in the very file (generated.env) where the discipline already lives.
_SET_SECRETS_SHORTHAND = re.compile(r"[A-Za-z0-9_-]{1,255}:(latest|\d+)")

#: `KEY=value`, with the `export ` prefix and the surrounding quotes a .env file may carry.
_ENV_LINE = re.compile(r"\A\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)\Z")


class Unavailable(RuntimeError):
    # The check could not be PERFORMED - an unparseable document, a git that cannot answer, no
    # subject at all. Distinct from a violation, which is a real verdict about a real spec. Both
    # exit non-zero, because a gate that reached no verdict must not read as a pass.
    pass


@dataclass(frozen=True)
class Finding:
    name: str        # the declared setting that carries a credential
    subject: str     # the file it was found in, relative to the repository when it is inside one
    locator: str     # where in that file: a spec path, or "line N" for an env file
    length: int      # how long the literal is - never the literal itself


def secret_settings() -> frozenset[str]:
    # THE roster, from the one place that declares it. An interpreter that cannot reach the
    # catalogue has learned NOTHING about the file it was pointed at, so this is Unavailable and
    # never a violation: Gate D runs this under whatever PREFLIGHT_PYTHON names, and a bare
    # virtualenv there must not be reported to an operator as a credential in their deployment.
    try:
        from meshpipeline.settings import inventory as cat
    except ImportError as exc:
        raise Unavailable(f"the settings catalogue is not importable, so the roster of "
                          f"secret-bearing settings is unknown ({exc})") from exc
    return frozenset(v.name for v in cat.all_vars() if v.secret)


def is_reference(value: str) -> bool:
    # A reference names a secret; a value IS one. Both accepted spellings put nothing readable in
    # the spec, which is the whole test.
    v = value.strip()
    return bool(_RESOURCE_NAME.fullmatch(v) or _SET_SECRETS_SHORTHAND.fullmatch(v))


def _env_entries(doc: Any, path: str = "") -> list[tuple[str, dict]]:
    # Every container env entry in the document, wherever it sits. Deliberately NOT a fixed path:
    # a Service nests template/spec once, a Job twice, a `describe` response repeats the whole thing
    # under `status`, and a hardcoded path would leave each new shape silently unchecked. The anchor
    # is the key `env` holding a list, which is the Knative env spelling and nothing else's.
    out: list[tuple[str, dict]] = []
    if isinstance(doc, dict):
        for key, val in doc.items():
            here = f"{path}.{key}" if path else key
            if key == "env" and isinstance(val, list):
                for i, entry in enumerate(val):
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                        out.append((f"{here}[{i}]", entry))
            else:
                out.extend(_env_entries(val, here))
    elif isinstance(doc, list):
        for i, item in enumerate(doc):
            out.extend(_env_entries(item, f"{path}[{i}]"))
    return out


def scan_spec(doc: Any, subject: str, roster: frozenset[str]) -> list[Finding]:
    findings: list[Finding] = []
    for locator, entry in _env_entries(doc):
        name = entry["name"]
        if name not in roster:
            continue
        raw = entry.get("value")
        value = raw.strip() if isinstance(raw, str) else ""
        # An absent or empty value is the correct state for a secret the runtime has not been given
        # yet, and for one supplied entirely through valueFrom. Flagging it would train operators to
        # ignore this gate. A valueFrom BESIDE a literal is still a violation: the reference does
        # not erase the value, which remains readable in the spec.
        if not value or is_reference(value):
            continue
        findings.append(Finding(name, subject, locator, len(value)))
    return findings


def scan_env_file(text: str, subject: str, roster: frozenset[str]) -> list[Finding]:
    findings: list[Finding] = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m or m.group(1) not in roster:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value.strip() or is_reference(value):
            continue
        findings.append(Finding(m.group(1), subject, f"line {i}", len(value)))
    return findings


def _label(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _scan(path: Path, roster: frozenset[str]) -> tuple[list[Finding], int]:
    # The violations AND how many environment entries were read to find them, because a report that
    # cannot say what it looked at cannot be distinguished from one that looked at nothing.
    subject = _label(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Unavailable(f"{subject}: cannot be read ({exc.strerror or exc})") from exc
    if path.suffix not in SPEC_SUFFIXES:
        return scan_env_file(text, subject, roster), sum(1 for ln in text.splitlines() if _ENV_LINE.match(ln))
    try:
        if path.suffix == ".json":
            doc = json.loads(text)
        else:
            import yaml
            doc = yaml.safe_load(text)
    except Exception as exc:
        # An unparseable spec is an unreached verdict, never a pass: "it did not parse, so it holds
        # no secret" is exactly the reasoning that lets one through.
        raise Unavailable(f"{subject}: does not parse as {path.suffix.lstrip('.')} ({exc.__class__.__name__})") from exc
    return scan_spec(doc, subject, roster), len(_env_entries(doc))


def scan_path(path: Path, roster: frozenset[str] | None = None) -> list[Finding]:
    return _scan(path, secret_settings() if roster is None else roster)[0]


def default_subjects() -> list[Path]:
    # Every tracked spec document under deploy/, found rather than listed - a manifest added to a
    # directory nobody thought to enumerate is exactly what a handwritten list misses - plus the
    # deployment environment this checkout has, if any.
    try:
        done = subprocess.run(["git", "ls-files", "deploy"], cwd=ROOT, capture_output=True, text=True)
    except OSError as exc:
        raise Unavailable(f"git could not be run: {exc.strerror or exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise Unavailable(f"git ls-files failed (exit {done.returncode}): "
                          f"{detail[0] if detail else 'no output'}")
    subjects = [ROOT / rel for rel in done.stdout.split() if Path(rel).suffix in SPEC_SUFFIXES]
    # The same environment file the deploy scripts read, chosen the same way (DEPLOY_ENV_FILE, else
    # generated.env), so this gate and deploy-preflight.sh can never judge different files.
    override = os.environ.get("DEPLOY_ENV_FILE")
    for env_file in (GENERATED_ENV, Path(override) if override else None):
        if env_file is not None and env_file not in subjects:
            subjects.append(env_file)
    return [p for p in subjects if p.is_file()]


def enforce(subjects: list[Path]) -> tuple[list[Finding], int]:
    # Returns the violations and the number of env entries actually examined, so the report can say
    # what was looked at. A run with no subject raises: scanning nothing and printing OK asserts
    # nothing, and would go green forever the day the discovery breaks.
    if not subjects:
        raise Unavailable("no deployment spec or environment file to check - the scan found nothing, "
                          "so any verdict it printed would be vacuous")
    roster = secret_settings()
    findings: list[Finding] = []
    examined = 0
    for path in subjects:
        found, count = _scan(path, roster)
        findings.extend(found)
        examined += count
    return findings, examined


def main(argv: list[str]) -> int:
    try:
        subjects = [Path(a) for a in argv[1:]] if len(argv) > 1 else default_subjects()
        findings, examined = enforce(subjects)
    except Unavailable as exc:
        print(f"UNAVAILABLE: the plaintext-secret gate reached no verdict\n\n  - {exc}")
        return 2
    if findings:
        print("FAIL: a deployment spec carries a secret VALUE\n")
        for f in findings:
            print(f"  - {f.name} at {f.subject} {f.locator}: a literal {f.length}-character value")
        print("\nThe values themselves are not printed: a gate that echoed them into a CI log would "
              "have re-published\nthe thing it is refusing. Treat every credential named above as "
              "disclosed - rotate it, put it in Secret\nManager, and reference it "
              "(valueFrom.secretKeyRef, or `gcloud run deploy --set-secrets NAME=secret:latest`).")
        return 1
    print(f"OK: no secret VALUE in any deployment spec "
          f"({len(subjects)} subject(s), {examined} environment entr{'y' if examined == 1 else 'ies'} "
          f"examined against {len(secret_settings())} secret-bearing settings).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
