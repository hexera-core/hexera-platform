# Responsibility: Prove the SARIF reader fails on serious findings, passes on minor ones, and never passes on no verdict.
# Boundaries: the reader's own contract and the CI lanes that rely on it; no scanner runs here.
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "devtools" / "quality" / "sarif_findings.py"
CI = REPO / ".github" / "workflows" / "ci.yml"


def _module():
    spec = importlib.util.spec_from_file_location("sarif_findings_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def _result(rule_index: int, *, level: str | None = None, suppressed: bool = False) -> dict:
    result: dict = {
        "ruleId": f"py/rule-{rule_index}",
        "rule": {"index": rule_index, "toolComponent": {"index": 0}},
        "message": {"text": "something\nwrong"},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "src/x.py"}, "region": {"startLine": 7}}}],
    }
    if level:
        result["level"] = level
    if suppressed:
        result["suppressions"] = [{"kind": "inSource"}]
    return result


def _sarif(results: list[dict]) -> dict:
    # CodeQL puts its rules on the query-pack extension, not the driver.
    rules = [
        {"id": "py/rule-0", "properties": {"security-severity": "7.5"}},
        {"id": "py/rule-1", "properties": {"security-severity": "5.0"}},
        {"id": "py/rule-2", "defaultConfiguration": {"level": "error"}},
        {"id": "py/rule-3", "defaultConfiguration": {"level": "warning"}},
    ]
    return {"runs": [{"tool": {"driver": {"name": "CodeQL"}, "extensions": [{"rules": rules}]},
                      "results": results}]}


def _write(tmp_path: Path, document: dict) -> Path:
    (tmp_path / "python.sarif").write_text(json.dumps(document), encoding="utf-8")
    return tmp_path


def test_a_high_severity_finding_fails_the_gate_and_is_printed(tmp_path, capsys):
    mod = _module()
    assert mod.main(["--gate", str(_write(tmp_path, _sarif([_result(0)])))]) == 1
    out = capsys.readouterr().out
    assert "py/rule-0" in out and "src/x.py:7" in out
    assert "::error file=src/x.py,title=py/rule-0,line=7::something wrong" in out


def test_an_error_level_rule_without_a_security_severity_blocks(tmp_path):
    assert _module().main(["--gate", str(_write(tmp_path, _sarif([_result(2)])))]) == 1


def test_minor_and_suppressed_findings_are_shown_but_pass(tmp_path, capsys):
    mod = _module()
    document = _sarif([_result(1), _result(3), _result(0, suppressed=True)])
    assert mod.main(["--gate", str(_write(tmp_path, document))]) == 0
    out = capsys.readouterr().out
    assert "py/rule-1" in out and "py/rule-3" in out
    assert "py/rule-0" not in out


def test_without_gate_a_blocking_finding_is_reported_not_failed(tmp_path, capsys):
    assert _module().main([str(_write(tmp_path, _sarif([_result(0)])))]) == 0
    assert "::warning " in capsys.readouterr().out


def test_no_sarif_is_no_verdict_and_never_passes(tmp_path):
    assert _module().main(["--gate", str(tmp_path)]) == 2


def test_an_unreadable_sarif_never_passes(tmp_path):
    (tmp_path / "python.sarif").write_text("{not json", encoding="utf-8")
    assert _module().main(["--gate", str(tmp_path)]) == 2


def test_a_message_cannot_forge_a_workflow_command(tmp_path, capsys):
    result = _result(0)
    result["message"]["text"] = "x %0A::error::forged"
    _module().main([str(_write(tmp_path, _sarif([result])))])
    annotations = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::")]
    assert len(annotations) == 1 and "%250A" in annotations[0]


def _steps(job: str) -> list[dict]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"][job]["steps"]


def test_codeql_uploads_only_when_code_scanning_is_available_and_gates_otherwise():
    steps = _steps("codeql")
    uses = [s.get("uses", "") for s in steps]
    probe = uses.index("./.github/actions/code-scanning-available")
    analyze = next(i for i, u in enumerate(uses) if u.startswith("github/codeql-action/analyze"))
    assert probe < analyze, "the probe must answer before the analysis decides whether to upload"
    inputs = steps[analyze]["with"]
    assert "steps.code-scanning.outputs.available" in str(inputs["upload"])
    assert "steps.code-scanning.outputs.available" in str(inputs["upload-database"])
    gate = next(s for s in steps if "sarif_findings.py" in s.get("run", ""))
    assert gate["env"]["GATE"] == "${{ steps.code-scanning.outputs.available != 'true' }}"


def test_the_workflow_audit_fails_on_findings_when_it_cannot_upload():
    steps = _steps("workflow-audit")
    zizmor = next(s for s in steps if s.get("uses", "").startswith("zizmorcore/zizmor-action"))
    assert zizmor["with"]["advanced-security"] == "${{ steps.code-scanning.outputs.available == 'true' }}"
    assert zizmor["with"]["annotations"] == "${{ steps.code-scanning.outputs.available != 'true' }}"
