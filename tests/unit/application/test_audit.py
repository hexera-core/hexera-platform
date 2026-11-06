# Responsibility: Verify an audit line is routable, structured, and never carries a credential or an address.
from __future__ import annotations

import json

from meshpipeline.application import audit


def _capture(**kw) -> dict:
    lines: list[str] = []
    audit.record(write=lines.append, **kw)
    assert len(lines) == 1
    return json.loads(lines[0])


def test_a_line_carries_the_routing_label_and_the_event():
    line = _capture(action="api_key.created", actor="console", organization_id="org-1",
                    target="key-1")
    assert line["logging.googleapis.com/labels"] == {"component": "api", "kind": "audit"}
    assert line["severity"] == "NOTICE"
    assert line["audit"]["action"] == "api_key.created"
    assert line["audit"]["outcome"] == "success"
    assert line["audit"]["target"] == "key-1"


def test_secrets_and_addresses_are_redacted_but_identifiers_survive():
    line = _capture(action="x", detail={
        "email": "ada@example.com", "owner_email": "ada@example.com", "id_token": "eyJ...",
        "api_key": "hx_live_abc", "key": "k", "password": "p", "key_id": "kept",
        "nested": {"refresh_token": "r", "amount": 5}})
    detail = line["audit"]["detail"]
    for name in ("email", "owner_email", "id_token", "api_key", "key", "password"):
        assert detail[name] == audit.REDACTED, name
    assert detail["key_id"] == "kept"
    assert detail["nested"] == {"refresh_token": audit.REDACTED, "amount": 5}
    assert "ada@example.com" not in json.dumps(line)


def test_a_failed_write_never_raises():
    def broken(_line):
        raise OSError("stdout closed")

    audit.record("x", write=broken)
