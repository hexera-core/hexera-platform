# Responsibility: Verify CI path scoping keeps cheap changes off expensive lanes.
# Boundaries: it tests the path classifier only; the workflow remains responsible for diffing.
from __future__ import annotations

from devtools.quality.ci_scope import classify


def test_unknown_scope_runs_everything():
    scope = classify([])

    assert scope.docs_only is False
    assert scope.full_ci is True
    assert scope.run_python is True
    assert scope.run_images is True
    assert scope.run_web is True


def test_documentation_only_skips_language_lanes():
    scope = classify(["docs/development/overview.md", "README.md"])

    assert scope.docs_only is True
    assert scope.full_ci is True
    assert scope.run_python is False
    assert scope.run_images is False
    assert scope.run_web is False


def test_web_workspace_change_runs_only_web_lane():
    scope = classify(["apps/console/src/page.tsx", "packages/hexera-api-client/src/index.ts"])

    assert scope.docs_only is False
    assert scope.full_ci is True
    assert scope.run_python is False
    assert scope.run_images is False
    assert scope.run_web is True


def test_unit_test_only_change_skips_image_and_integration_lanes():
    scope = classify(["tests/unit/api/test_chat.py"])

    assert scope.docs_only is False
    assert scope.full_ci is True
    assert scope.run_python is True
    assert scope.run_images is False
    assert scope.run_web is False


def test_runtime_python_change_runs_python_and_image_lanes():
    scope = classify(["src/meshpipeline/runtime/api_server.py"])

    assert scope.docs_only is False
    assert scope.full_ci is True
    assert scope.run_python is True
    assert scope.run_images is True
    assert scope.run_web is False


def test_image_build_inputs_run_image_lanes():
    for path in ("Makefile", "deploy/verify_install.py", "deploy/docker/entrypoint.sh"):
        scope = classify([path])

        assert scope.run_images is True, path


def test_integration_and_native_tests_keep_python_signal_on_pull_requests():
    for path in ("tests/integration/test_graph_runtime.py", "tests/native/test_native_cad_repair.py"):
        scope = classify([path], event_name="pull_request")

        assert scope.full_ci is False, path
        assert scope.run_python is True, path
        assert scope.run_images is True, path


def test_mixed_python_and_web_change_runs_both_without_docs_only():
    scope = classify(["src/meshpipeline/settings/runtime.py", "apps/admin-console/src/app.tsx"])

    assert scope.docs_only is False
    assert scope.full_ci is True
    assert scope.run_python is True
    assert scope.run_images is True
    assert scope.run_web is True


def test_pull_request_scope_is_not_full_ci():
    scope = classify(["src/meshpipeline/runtime/api_server.py"], event_name="pull_request")

    assert scope.full_ci is False
    assert scope.run_python is True
    assert scope.run_images is True
