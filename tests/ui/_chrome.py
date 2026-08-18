# Responsibility: Drive a real Chrome over the DevTools Protocol: load a page and interrogate it.
# Boundaries: just enough protocol to serve the UI tier, with no dependency added; it asserts nothing.
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from websockets.sync.client import connect

#: Where a browser is looked for, in order. CHROME_BIN wins so CI can point at its own.
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

INSTALL_HINT = (
    "No Chrome/Chromium on PATH. The UI gate validates the shipped page in a REAL browser, so "
    "this is a hard failure rather than a skip - a release must not pass with its browser "
    "validation quietly absent.\n"
    "  Debian        : apt-get install -y chromium\n"
    "  Ubuntu        : the `chromium` package is a snap wrapper and will NOT run where snapd is\n"
    "                  absent (containers, CI). Install Google Chrome's .deb instead:\n"
    "                    curl -fsSLo /tmp/chrome.deb "
    "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb\n"
    "                    apt-get install -y /tmp/chrome.deb\n"
    "  macOS         : brew install --cask chromium\n"
    "  any           : set CHROME_BIN=/path/to/chrome"
)

# Chrome asks for these on its own; the PAGE does not reference them, so a miss is not a
# defect in what we ship.
_BROWSER_OWN_REQUESTS = ("/favicon.ico",)

# What the page needs in order to RUN. A failure here is a broken build - the user gets a
# blank or half-wired page. Requests the application makes at runtime (XHR/Fetch) are excluded
# on purpose: an API answering 401 or 503 is the application working correctly against a
# backend that is unavailable, which is a different thing from a missing asset and is asserted
# where it matters instead.
_ASSET_TYPES = {"Document", "Script", "Stylesheet", "Image", "Font", "Media", "Manifest"}


def find_chrome() -> str | None:
    env = os.environ.get("CHROME_BIN")
    if env and os.access(env, os.X_OK):
        return env
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def usable_chrome(binary: str) -> str:
    try:
        browser = Browser(binary)
    except Exception as exc:            # noqa: BLE001 - the reason is the return value
        return f"{binary}: {exc}"
    try:
        return ""
    finally:
        browser.close()


class BrowserError(RuntimeError):
    pass


class Browser:

    @classmethod
    def remote(cls, endpoint: str) -> Browser:
        from urllib.parse import urlparse, urlunparse

        self = cls.__new__(cls)
        self._profile = None
        self._proc = None
        base = endpoint.rstrip("/")
        version = httpx.get(f"{base}/json/version", timeout=30).json()
        self.name = version["Browser"]
        reached, ws = urlparse(base), urlparse(version["webSocketDebuggerUrl"])
        self._ws = connect(urlunparse(ws._replace(netloc=reached.netloc)),
                           max_size=None, open_timeout=30)
        self._next_id = 0
        return self

    def __init__(self, binary: str):
        self._profile = Path(tempfile.mkdtemp(prefix="amp-ui-chrome-"))
        self._proc = subprocess.Popen(
            [binary,
             "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions",
             "--disable-background-networking", "--disable-component-update",
             "--disable-client-side-phishing-detection", "--metrics-recording-only",
             "--mute-audio", "--window-size=1280,900",
             # The mesh viewer needs a WebGL context. There is no GPU on a build machine, so
             # WebGL is served by Chrome's software rasteriser - which recent Chrome will only
             # expose behind this flag. Software rendering is slow and entirely sufficient:
             # nothing here asserts pixels, only that the renderer initialises.
             "--enable-unsafe-swiftshader",
             "--remote-debugging-port=0", f"--user-data-dir={self._profile}",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        port = self._await_devtools_port()
        version = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=10).json()
        self.name = version["Browser"]
        self._ws = connect(version["webSocketDebuggerUrl"], max_size=None, open_timeout=20)
        self._next_id = 0

    def _await_devtools_port(self, limit: float = 30.0) -> int:
        # Port 0 means "pick one"; Chrome writes what it picked here once it is listening.
        portfile = self._profile / "DevToolsActivePort"
        deadline = time.time() + limit
        while time.time() < deadline:
            if portfile.exists():
                text = portfile.read_text().splitlines()
                if text and text[0].strip().isdigit():
                    return int(text[0].strip())
            if self._proc.poll() is not None:
                raise BrowserError(f"the browser exited during startup (rc={self._proc.returncode})")
            time.sleep(0.05)
        raise BrowserError("the browser never opened a DevTools port")

    # protocol
    def _send(self, method: str, params: dict | None = None, session: str | None = None,
              sink: list | None = None, timeout: float = 30.0) -> dict:
        self._next_id += 1
        msg = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise BrowserError(f"{method} did not answer within {timeout}s")
            frame = json.loads(self._ws.recv(timeout=remaining))
            if frame.get("id") == msg["id"]:
                if "error" in frame:
                    raise BrowserError(f"{method}: {frame['error']}")
                return frame.get("result", {})
            if sink is not None and "method" in frame:
                sink.append(frame)

    def drain(self, sink: list, seconds: float = 0.25) -> None:
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                frame = json.loads(self._ws.recv(timeout=remaining))
            except TimeoutError:
                return
            if "method" in frame:
                sink.append(frame)

    def new_page(self) -> Page:
        target = self._send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self._send(
            "Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        page = Page(self, session, target)
        for domain in ("Page", "Runtime", "Log", "Network"):
            self._send(f"{domain}.enable", session=session, sink=page.frames)
        return page

    def close_page(self, page: Page) -> None:
        try:
            self._send("Target.closeTarget", {"targetId": page.target_id})
        except Exception:      # a page that already went away is not a test failure
            pass

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass
        if self._proc is None:      # attached to a browser we did not start
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        # The profile lives in the system temp dir, never in the repository.
        shutil.rmtree(self._profile, ignore_errors=True)


class Page:

    def __init__(self, browser: Browser, session: str, target_id: str):
        self._b = browser
        self._s = session
        self.target_id = target_id
        self.frames: list[dict] = []

    def _send(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        return self._b._send(method, params, session=self._s, sink=self.frames, timeout=timeout)

    # driving
    def open(self, url: str, ready: str = "document.readyState === 'complete'") -> None:
        self._send("Page.navigate", {"url": url})
        self.wait_for(ready, what=f"{url} to finish loading")

    def evaluate(self, expression: str, timeout: float = 30.0):
        result = self._send("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
        }, timeout=timeout)
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            text = (detail.get("exception", {}).get("description")
                    or detail.get("text") or json.dumps(detail))
            raise AssertionError(f"the page threw while evaluating:\n{text}")
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout: float = 20.0, what: str = "") -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.evaluate(f"(() => {{ try {{ return !!({expression}); }} "
                                 f"catch (e) {{ return false; }} }})()")
            if last:
                return
            time.sleep(0.05)
        raise AssertionError(
            f"timed out after {timeout}s waiting for {what or expression!r}"
            + self._diagnosis())

    def press(self, key: str, *, code: str | None = None, vk: int = 0, text: str | None = None):
        base = {"key": key, "code": code or key,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        down = dict(base, type="keyDown")
        if text is not None:
            down["text"] = text
        self._send("Input.dispatchKeyEvent", down)
        self._send("Input.dispatchKeyEvent", dict(base, type="keyUp"))

    def settle(self, seconds: float = 0.3) -> None:
        self._b.drain(self.frames, seconds)

    # what the browser reported
    def _events(self, method: str) -> list[dict]:
        return [f.get("params", {}) for f in self.frames if f.get("method") == method]

    @property
    def page_errors(self) -> list[str]:
        out = []
        for e in self._events("Runtime.exceptionThrown"):
            d = e.get("exceptionDetails", {})
            out.append(d.get("exception", {}).get("description") or d.get("text") or str(d))
        return out

    @property
    def console_errors(self) -> list[str]:
        out = []
        for e in self._events("Runtime.consoleAPICalled"):
            if e.get("type") in ("error", "assert"):
                out.append(" ".join(
                    str(a.get("value", a.get("description", ""))) for a in e.get("args", [])))
        for e in self._events("Log.entryAdded"):
            entry = e.get("entry", {})
            if (entry.get("level") == "error" and entry.get("source") != "network"
                    and not self._browsers_own(entry.get("url", ""))):
                out.append(f"{entry.get('source')}: {entry.get('text')} {entry.get('url', '')}")
        return out

    @property
    def requests(self) -> list[str]:
        return [e.get("request", {}).get("url", "")
                for e in self._events("Network.requestWillBeSent")]

    @property
    def failed_requests(self) -> list[str]:
        out = []
        for e in self._events("Network.loadingFailed"):
            url = self._url_of(e.get("requestId", ""))
            if e.get("type") in _ASSET_TYPES and not self._browsers_own(url):
                out.append(f"{url or e.get('requestId')} failed: {e.get('errorText')}")
        for e in self._events("Network.responseReceived"):
            response, url = e["response"], e["response"]["url"]
            if (e.get("type") in _ASSET_TYPES and response.get("status", 0) >= 400
                    and not self._browsers_own(url)):
                out.append(f"{url} -> {response['status']}")
        return out

    def _url_of(self, request_id: str) -> str:
        for e in self._events("Network.requestWillBeSent"):
            if e.get("requestId") == request_id:
                return e.get("request", {}).get("url", "")
        return ""

    @staticmethod
    def _browsers_own(url: str) -> bool:
        return any(url.endswith(suffix) for suffix in _BROWSER_OWN_REQUESTS)

    def _diagnosis(self) -> str:
        bits = []
        if self.page_errors:
            bits.append("page errors: " + "; ".join(self.page_errors[:4]))
        if self.console_errors:
            bits.append("console errors: " + "; ".join(self.console_errors[:4]))
        if self.failed_requests:
            bits.append("failed requests: " + "; ".join(self.failed_requests[:6]))
        return ("\n  " + "\n  ".join(bits)) if bits else ""
