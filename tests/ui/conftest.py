# Responsibility: Serve the UI tier the page a user is served, in a real browser.
# Boundaries: the page comes from the running application, not from a file.
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest
from _chrome import INSTALL_HINT, Browser, find_chrome, usable_chrome

REPO = Path(__file__).parents[2]


@pytest.fixture(scope="session")
def base_url():
    external = os.environ.get("UI_BROWSER_BASE_URL")
    if external:
        yield external.rstrip("/")
        return

    os.environ.setdefault("STATIC_DIR", str(REPO / "ui"))
    os.environ.setdefault("DEEPSEEK_API_KEY", "x")
    os.environ.setdefault("DEEPINFRA_API_KEY", "x")
    os.environ.setdefault("POSTGRES_PASSWORD", "x")
    # The runtime data roots default to CWD-relative ./data and ./workspaces, and
    # api/v1/upload.py mkdirs its jobs dir at IMPORT time - so importing the app below created
    # data/jobs/ in the checkout. Set before the import, as tests/unit/conftest.py does.
    _runtime = Path(tempfile.mkdtemp(prefix="meshpipeline-ui-"))
    os.environ.setdefault("DATA_ROOT", str(_runtime / "data"))
    os.environ.setdefault("JOBS_DIR", str(_runtime / "data" / "jobs"))
    os.environ.setdefault("CORPUS_DIR", str(_runtime / "data" / "corpus"))
    os.environ.setdefault("WORKSPACE_BASE", str(_runtime / "workspaces"))
    import uvicorn

    from meshpipeline.api.app import app

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        if time.time() > deadline:
            raise AssertionError("the application did not start for the browser suite")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=15)
    shutil.rmtree(_runtime, ignore_errors=True)


@pytest.fixture(scope="session")
def browser():
    endpoint = os.environ.get("CHROME_CDP_URL")
    if endpoint:
        b = Browser.remote(endpoint)
        yield b
        b.close()
        return
    binary = find_chrome()
    if binary is None:
        pytest.fail(INSTALL_HINT, pytrace=False)
    broken = usable_chrome(binary)
    if broken:
        pytest.fail(
            f"the browser on this host cannot start: {broken}\n"
            "This is the host, not the page - it fails on about:blank with no product code "
            "loaded. Run the suite against a working browser, or set CHROME_CDP_URL to one "
            "(Gate C does this automatically).", pytrace=False)
    b = Browser(binary)
    yield b
    b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    browser.close_page(p)


@pytest.fixture(scope="session")
def event_vocabulary():
    supplied = os.environ.get("UI_EVENT_VOCABULARY")
    if supplied:
        types, stages = json.loads(supplied)
        assert types and stages, "UI_EVENT_VOCABULARY was supplied but is empty"
        return tuple(types), tuple(stages)
    import meshpipeline.events as E

    return tuple(E.EVENT_TYPES), tuple(E.STAGES)


@pytest.fixture
def live(page, base_url):
    page.open(f"{base_url}/ui", ready="document.getElementById('stage').children.length > 0")
    return page


def assert_clean(page, what: str) -> None:
    page.settle()
    problems = []
    if page.page_errors:
        problems.append("uncaught page errors:\n    " + "\n    ".join(page.page_errors))
    if page.console_errors:
        problems.append("console errors:\n    " + "\n    ".join(page.console_errors))
    if page.failed_requests:
        problems.append("requests a user would see fail:\n    "
                        + "\n    ".join(page.failed_requests))
    assert not problems, f"{what} did not load cleanly:\n  " + "\n  ".join(problems)
