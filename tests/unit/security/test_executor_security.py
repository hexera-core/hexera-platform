# Responsibility: Verify the static scan rejects network, environment and exfiltration code before it runs.
from meshpipeline.sandbox import safe_exec as se  # noqa: E402


def test_scrubbed_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("MINIO_SECRET_KEY", "mk")
    monkeypatch.setenv("MESH_API_KEY", "ak")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = se.scrubbed_subprocess_env({"OMP_NUM_THREADS": "4"})
    assert "DEEPSEEK_API_KEY" not in env
    assert "POSTGRES_PASSWORD" not in env
    assert "MINIO_SECRET_KEY" not in env
    assert "MESH_API_KEY" not in env
    # non-secret runtime vars survive
    assert env.get("PATH") == "/usr/bin"
    assert env.get("OMP_NUM_THREADS") == "4"


def test_scan_rejects_network_import():
    reason = se.scan_python_source("import numpy as np\nimport socket\n")
    assert reason and "socket" in reason


def test_scan_rejects_environ_access():
    reason = se.scan_python_source("import os\nprint(os.environ['DEEPSEEK_API_KEY'])\n")
    assert reason and "environ" in reason.lower()


def test_scan_rejects_urllib_exfil():
    reason = se.scan_python_source("import urllib.request\nurllib.request.urlopen('http://x')\n")
    assert reason and "urllib" in reason


def test_scan_allows_legit_compute():
    src = "import numpy as np, math, json, os\np = os.path.join('.', 'out')\nprint((10*1.5)**(1/3))\n"
    assert se.scan_python_source(src) is None


def test_scan_fails_open_on_syntax_error():
    # a broken snippet is caught at execution; the scan must not crash
    assert se.scan_python_source("def (:\n") is None
