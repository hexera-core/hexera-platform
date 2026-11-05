# Responsibility: Verify the Mesh report route serves the stored viewer payload as a downloaded HTML page, refuses a job with no delivered mesh, and shows a foreign tenant nothing.
from __future__ import annotations

import json

from tests.unit.api.test_viewer_endpoints import _PAYLOAD, JOB, _client, _Store


def test_the_report_route_serves_the_stored_payload_as_a_download(monkeypatch):
    store = _Store({"jobs/x/viewer_data.json": json.dumps(_PAYLOAD).encode()})
    client, store = _client(monkeypatch, store=store)

    r = client.get(f"/api/v1/simulation/{JOB}/report.html")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    # a download, never a page opened in place
    assert r.headers["content-disposition"] == f'attachment; filename="mesh_report_{JOB[:8]}.html"'
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.text.startswith("<!DOCTYPE html>") and "Mesh report" in r.text and "4,321" in r.text
    assert store.reads == ["jobs/x/viewer_data.json"]


def test_a_job_with_no_delivered_mesh_has_no_report(monkeypatch):
    store = _Store({"jobs/x/viewer_data.json": json.dumps({**_PAYLOAD, "surface": None}).encode()})
    client, _ = _client(monkeypatch, store=store)
    assert client.get(f"/api/v1/simulation/{JOB}/report.html").status_code == 404


def test_another_tenant_sees_nothing(monkeypatch):
    client, store = _client(monkeypatch, owner="someone-else")
    assert client.get(f"/api/v1/simulation/{JOB}/report.html").status_code == 404
    assert store.reads == []
