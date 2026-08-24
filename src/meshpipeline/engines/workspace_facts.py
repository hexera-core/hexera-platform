# Responsibility: Read back the facts a job's workspace already records, so later stages need not be told them again.
# Boundaries: reads only; it writes nothing and infers nothing not present on disk.
from __future__ import annotations

import json
from pathlib import Path


def read_engine_params(workspace) -> dict:
    try:
        p = Path(workspace) / "engine_params.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def read_flow_topology(workspace) -> str:
    try:
        p = Path(workspace) / "flow_topology"
        return p.read_text(encoding="utf-8").strip().lower() if p.exists() else ""
    except Exception:
        return ""


def read_purpose(workspace) -> str:
    try:
        p = Path(workspace) / "purpose"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except Exception:
        return ""


def read_dimensionality(workspace) -> str:
    try:
        p = Path(workspace) / "dimensionality"
        return p.read_text(encoding="utf-8").strip().upper() if p.exists() else ""
    except Exception:
        return ""


def port_declaration(workspace) -> list[dict]:
    """The verbatim intake patch declaration (sizes/locations included), or [] when none was
    written - programmatic submits have no declaration and keep engine-canonical names."""
    import json as _json
    f = Path(workspace) / "port_declaration.json"
    try:
        out = _json.loads(f.read_text(encoding="utf-8"))
        return out if isinstance(out, list) else []
    except Exception:  # noqa: BLE001 - absent or unreadable means undeclared, never fatal
        return []


def contract_patches(workspace) -> list[dict]:
    cf = Path(workspace) / "patches_contract.txt"
    out: list[dict] = []
    if not cf.exists():
        return out
    for line in cf.read_text(errors="replace").splitlines():
        # e.g. "  • wing  →  type wall"
        if ("→" in line or "->" in line) and "type " in line.lower():
            left, right = line.replace("•", "").replace("->", "→").split("→", 1)
            name = left.strip()
            role = (right.lower().replace("type", "").strip().split()[0]
                    if "type" in right.lower() else "")
            if name and role:
                out.append({"name": name, "type": role})
    return out


def contract_wall_patch(workspace) -> str | None:
    for p in contract_patches(workspace):
        if p["type"] == "wall":
            return p["name"]
    return None
