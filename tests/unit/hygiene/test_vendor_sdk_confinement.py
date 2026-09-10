# Responsibility: Verify each vendor SDK is imported only in its own adapter, and no writable path sits in the package.
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).parents[3]
SRC = REPO / "src" / "meshpipeline"


def _files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    return roots


def _offenders(sdk: str, allowed: set[str]) -> dict[str, str]:
    bad = {}
    for p in _files():
        rel = p.relative_to(SRC).as_posix()
        if rel in allowed:
            continue
        if sdk in _imported_roots(p):
            bad[rel] = sdk
    return bad


# Each SDK -> the file(s) allowed to import it. Keep these lists SHORT; a growing allow-list
# is the signal that a seam is leaking.
VENDOR_ALLOWLIST: dict[str, set[str]] = {
    # Redis: client construction is centralised in ONE module; the capability adapters build
    # their clients through it.
    "redis": {"adapters/_shared/redis_client.py"},
    # The OpenAI-compatible model adapters (DeepSeek + DeepInfra-hosted models).
    "openai": {
        "adapters/model_inference/router.py",
        "adapters/model_inference/tracing.py",
        "adapters/model_inference/streaming.py",
        # providers.py is where a provider label becomes a client and an SDK exception becomes
        # a NEUTRAL FailureCategory. That translation is the reason nothing above the adapter
        # needs the SDK's exception types to decide whether a failure may fail over.
        "adapters/model_inference/providers.py",
    },
    # Google Cloud: the object store + the Cloud Run mesh/pipeline adapters, and the one
    # adapter that verifies a Google Identity Platform ID token.
    "google": {
        "adapters/firebase_token/identity_platform.py",
        "adapters/object_storage/gcs.py",
        "adapters/mesh_execution/cloud_run_client.py",
        "adapters/mesh_execution/gcs_exchange.py",
        "adapters/pipeline_execution/cloud_run.py",
    },
    "minio": {"adapters/object_storage/minio.py"},
    # Celery is the pipeline-execution transport + the worker entrypoint that runs it.
    "celery": {
        "adapters/pipeline_execution/celery_app.py",
        "adapters/pipeline_execution/celery.py",
        "adapters/pipeline_execution/maintenance_tasks.py",
        "runtime/celery_worker.py",
    },
    # HTTP clients belong to the adapters that call a remote API.
    "httpx": {
        # Google's ID-token signing certificates: an outbound call to a provider's key endpoint,
        # so it lives with the provider's adapter and not in the contract the product calls.
        "adapters/firebase_token/identity_platform.py",
        "adapters/search/_http.py",
        "adapters/model_inference/tracing.py",
        "adapters/model_inference/deepinfra.py",
    },
}


def test_vendor_sdks_are_confined_to_their_adapters():
    bad: dict[str, str] = {}
    for sdk, allowed in VENDOR_ALLOWLIST.items():
        bad.update(_offenders(sdk, allowed))
    assert not bad, (
        "a vendor SDK is imported outside the adapter that owns it - the product would have "
        f"to be edited to replace that provider: {bad}")


def test_the_redis_sdk_has_exactly_one_import_site():
    sites = sorted(p.relative_to(SRC).as_posix() for p in _files() if "redis" in _imported_roots(p))
    assert sites == ["adapters/_shared/redis_client.py"], (
        f"the redis SDK is imported in {len(sites)} module(s), expected exactly 1: {sites}")


def test_no_product_module_imports_a_model_provider_sdk():
    product = ("agents", "engines", "pipeline", "cad", "render", "sandbox", "capture",
               "agent_tools", "application", "api", "contracts")
    bad = {}
    for p in _files():
        rel = p.relative_to(SRC).as_posix()
        if not rel.startswith(product):
            continue
        for sdk in ("openai", "anthropic"):
            if sdk in _imported_roots(p):
                bad[rel] = sdk
    assert not bad, f"a model-provider SDK reached the product: {bad}"


def test_no_writable_runtime_path_is_anchored_inside_the_package():
    import meshpipeline.settings.runtime as rtcfg
    from meshpipeline.settings.env import PROJECT_ROOT

    for name in ("WORKSPACE_BASE", "DATA_ROOT", "JOBS_DIR"):
        p = Path(str(getattr(rtcfg, name))).resolve()
        assert PROJECT_ROOT not in p.parents and p != PROJECT_ROOT, (
            f"{name}={p} is inside the package ({PROJECT_ROOT}) - an installed wheel would "
            f"try to write into site-packages")
    corpus = Path(str(rtcfg.CORPUS_DIR)).resolve()
    assert PROJECT_ROOT not in corpus.parents, f"CORPUS_DIR={corpus} is inside the package"

    src = (SRC / "settings" / "runtime.py").read_text()
    assert "PROJECT_ROOT" not in src.split("# mesh toolchain")[0].replace(
        "PROJECT_ROOT is where the package IS", ""), (
        "settings/runtime.py anchors a runtime path to PROJECT_ROOT again")
