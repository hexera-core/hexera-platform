from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _load():
    path = REPO / "devtools" / "validation" / "crs_repair_phase0.py"
    spec = importlib.util.spec_from_file_location("crs_repair_phase0_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_measure_inputs_summarizes_each_episode_and_preserves_raw_rows(monkeypatch, tmp_path):
    phase0 = _load()
    part = tmp_path / "part.step"
    part.write_text("fixture")
    episodes = iter(
        [
            {
                "row": {
                    "file": str(part),
                    "located_after": {"by_code": {}},
                    "repaired_defects": {"small_edge": 1},
                    "outcome": "repaired",
                },
                "wall_seconds": 1.25,
                "cpu_seconds": 0.75,
                "max_rss_kb": 1200,
                "raw_rows_path": "/tmp/episode-0/rows.json",
            },
            {
                "row": {
                    "file": str(part),
                    "located_after": {"by_code": {}},
                    "repaired_defects": {"small_edge": 1},
                    "outcome": "repaired",
                },
                "wall_seconds": 1.75,
                "cpu_seconds": 1.25,
                "max_rss_kb": 1500,
                "raw_rows_path": "/tmp/episode-1/rows.json",
            },
        ]
    )
    monkeypatch.setattr(phase0, "_run_episode", lambda *args, **kwargs: next(episodes))

    result = phase0.measure_inputs([part], repeats=2, planner="off")

    assert [row["raw_rows_path"] for row in result["rows"]] == ["/tmp/episode-0/rows.json", "/tmp/episode-1/rows.json"]
    assert result["wall_seconds"] == {"min": 1.25, "median": 1.5, "max": 1.75}
    assert result["cpu_seconds"] == {"min": 0.75, "median": 1.0, "max": 1.25}
    assert result["max_rss_kb"] == 1500
    assert result["determinism"] == {"deterministic": True, "mismatches": []}


def test_measure_inputs_reports_kernel_result_mismatches(monkeypatch, tmp_path):
    phase0 = _load()
    part = tmp_path / "part.step"
    part.write_text("fixture")
    episodes = iter(
        [
            {
                "row": {
                    "file": str(part),
                    "located_after": {"by_code": {"small_edge": 1}},
                    "repaired_defects": {},
                    "outcome": "unchanged",
                },
                "wall_seconds": 1.0,
                "cpu_seconds": 0.5,
                "max_rss_kb": 1000,
                "raw_rows_path": "/tmp/episode-0/rows.json",
            },
            {
                "row": {
                    "file": str(part),
                    "located_after": {"by_code": {}},
                    "repaired_defects": {"small_edge": 1},
                    "outcome": "repaired",
                },
                "wall_seconds": 1.0,
                "cpu_seconds": 0.5,
                "max_rss_kb": 1000,
                "raw_rows_path": "/tmp/episode-1/rows.json",
            },
        ]
    )
    monkeypatch.setattr(phase0, "_run_episode", lambda *args, **kwargs: next(episodes))

    result = phase0.measure_inputs([part], repeats=2, planner="off")

    assert result["determinism"]["deterministic"] is False
    assert result["determinism"]["mismatches"] == [
        {
            "file": str(part),
            "repeat": 2,
            "fields": ["located_after", "outcome", "repaired_defects"],
        }
    ]


def test_episode_timeout_kills_the_native_process_group(monkeypatch, tmp_path):
    phase0 = _load()
    killed = []

    class HungWorker:
        pid = 4321
        returncode = None

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(["worker"], timeout)
            self.returncode = -9
            return "", ""

    monkeypatch.setattr(phase0.subprocess, "Popen", lambda *args, **kwargs: HungWorker())
    monkeypatch.setattr(phase0.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = phase0._run_episode(tmp_path / "part.step", "off", timeout_seconds=0.01)

    assert killed == [(4321, phase0.signal.SIGKILL)]
    assert result["row"]["outcome"] == "timeout"


def test_worker_keeps_input_copies_out_of_the_artifact_directory(monkeypatch, tmp_path, capsys):
    """Corpus STEP files are never committed, and the artifact directory is committed evidence."""
    phase0 = _load()
    part = tmp_path / "part.step"
    part.write_text("fixture")
    artifact_dir = tmp_path / "evidence"
    staged = []

    def fake_run(command, **_kwargs):
        staged.append(Path(command[2]))
        assert (Path(command[2]) / part.name).read_text() == "fixture"
        rows_path = Path(command[command.index("--json") + 1])
        rows_path.write_text('[{"file": "part.step", "outcome": "repaired"}]')
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(phase0.subprocess, "run", fake_run)

    assert phase0._worker(part, "off", artifact_dir) == 0

    assert artifact_dir not in staged[0].parents
    assert not list(artifact_dir.rglob("*.step"))
    assert (artifact_dir / "rows.json").exists()
    assert not staged[0].exists()
    capsys.readouterr()
