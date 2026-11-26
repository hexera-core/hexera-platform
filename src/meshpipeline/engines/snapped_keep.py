# Responsibility: Keep a snappyHexMesh attempt's snapped mesh until its layer verdict is known, so the relative retry adds only the layers.
# Owns: the kept snapped mesh beside the attempt (KEEP_DIR), its key, the layers-only plan and the record of which path ran.
# Boundaries: files in the attempt workspaces only; it decides nothing about the stack - the gate and the ladder do (CFDREQ-33).
# Collaborates with: engines/snappy/native (splits the run, keeps, restarts), engines/snappy/drivers (adopts it on the retry),
# pipeline/executor and pipeline/graph (drop it once the attempt's choice is made), engines/return_scope (leaves it out first).
"""THE RELATIVE RETRY ADDS ONLY THE LAYERS (CFDREQ-33 amended, rulebook 116).

The layer stack is written in metres first and, after a layer-stack refusal of that attempt, the same plan is tried
with the stack on each wall face's own cells. That retry used to rebuild the whole mesh: castellation and snapping were
258 of its 387 minutes on the 24 external parts that ran it (ENGINEER 38), and they come out the same - the retry's
case differs from the first one only in its layer stage.

So an attempt whose dictionary may be written relative on the retry runs snappyHexMesh in two parts: castellation and
snapping, then the layers. Between them the decomposed snapped mesh (processor*/constant/polyMesh, written in binary so
it is the mesh snappyHexMesh held in memory, digit for digit) is copied aside here. When the layer gate refuses the
stack in metres, the retry takes it over - only when its castellation and snapping inputs are the same (snap_key) and
it runs on the same number of ranks - and runs snappyHexMesh with castellatedMesh and snap off, addLayers on. Every
other case, and a kept mesh that is missing, incomplete or unreadable, rebuilds in full as before; the record
(REUSE_FILE) says which path ran and why. The kept mesh is removed as soon as the attempt's choice is made, and never
rides in a delivered workspace."""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where the attempt's snapped mesh waits for its layer verdict: KEEP_DIR/processorN/constant/polyMesh + KEY_FILE.
KEEP_DIR = ".snapped_mesh"
#: The kept mesh's record: its snap key, its rank count and every file with its size (a check it is whole).
KEY_FILE = "key.json"
#: The retry's plan to add only the layers to the kept mesh (written by the driver, read by the native run).
LAYERS_ONLY_FILE = "layers_only.json"
#: Which path the attempt's snappyHexMesh ran (layers only, or the whole mesh) and why.
REUSE_FILE = "snapped_reuse.json"
#: The relative retry adds only the layers when it can (False: it rebuilds the whole mesh, as before rulebook 116 -
#: the side-by-side proof's other arm).
LAYERS_ONLY_RETRY = True
#: The dictionary's stage switches, as render_snappy_case writes them.
_SWITCHES = re.compile(r"castellatedMesh\s+\w+\s*;\s*snap\s+\w+\s*;\s*addLayers\s+\w+\s*;")


def _block(text: str, name: str) -> tuple[int, int] | None:
    """(start, end) of the `name { ... }` block of a dictionary text, braces matched; None when absent."""
    m = re.search(rf"\b{name}\s*\{{", text)
    if not m:
        return None
    depth, i = 0, m.end() - 1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return m.start(), i + 1
        i += 1
    return None


def _switches(text: str) -> str | None:
    m = _SWITCHES.search(text)
    return m.group(0) if m else None


def layers_on(text: str) -> bool:
    s = _switches(text)
    return s is not None and re.search(r"addLayers\s+true", s) is not None


def stage_text(text: str, stage: str) -> str:
    """The dictionary for one part of the split run: "snap" (castellation and snapping, no layers) or "layers"
    (the layers only, on the mesh in constant/polyMesh). Everything else is the dictionary as authored."""
    words = {"snap": "castellatedMesh true; snap true; addLayers false;",
             "layers": "castellatedMesh false; snap false; addLayers true;"}[stage]
    return _SWITCHES.sub(words, text, count=1)


def snap_key(workspace) -> str:
    """What decides the mesh after snapping: the background box, the snappyHexMeshDict without its stage switches,
    its layer stage (addLayersControls) and the volume bar the relative stack lowers for its layer cells (minVol),
    and the surfaces snappyHexMesh reads (constant/triSurface *.stl and the authored contact line)."""
    ws = Path(workspace)
    h = hashlib.sha1()
    for rel in ("system/blockMeshDict", "system/surfaceFeatureExtractDict"):
        p = ws / rel
        h.update(rel.encode() + b"\0" + (p.read_bytes() if p.is_file() else b"-") + b"\0")
    text = (ws / "system" / "snappyHexMeshDict").read_text(errors="replace")
    text = _SWITCHES.sub("", text, count=1)
    b = _block(text, "addLayersControls")
    if b:
        text = text[:b[0]] + text[b[1]:]
    b = _block(text, "meshQualityControls")
    if b:
        text = text[:b[0]] + re.sub(r"\bminVol\s+[-+0-9.eE]+\s*;", "minVol;", text[b[0]:b[1]], count=1) + text[b[1]:]
    h.update(re.sub(r"\s+", " ", text).encode())
    tri = ws / "constant" / "triSurface"
    from meshpipeline.engines.snappy.snappy_runner import CONTACT_LINE_FILE
    for p in sorted(tri.rglob("*")) if tri.is_dir() else ():
        if p.is_file() and (p.suffix.lower() == ".stl" or p.name == CONTACT_LINE_FILE):
            h.update(p.relative_to(ws).as_posix().encode() + b"\0")
            with p.open("rb") as fh:
                while chunk := fh.read(1 << 22):
                    h.update(chunk)
    return h.hexdigest()


def wants_keep(workspace) -> bool:
    """The attempt may be followed by the relative retry: a stack in metres marked relative-on-retry, layers on."""
    from meshpipeline.engines.cfd_requirements import relative_on_retry
    ws = Path(workspace)
    try:
        text = (ws / "system" / "snappyHexMeshDict").read_text(errors="replace")
    except OSError:
        return False
    return relative_on_retry(ws) and layers_on(text) and _switches(text) is not None


@contextlib.contextmanager
def stage_dict(workspace, stage: str, *, binary: bool = False) -> Iterator[None]:
    """system/snappyHexMeshDict as one part of the split run (stage_text) - and, for the snapped mesh that is kept,
    controlDict writing binary - for the length of the block; the authored files are put back after it."""
    sys_dir = Path(workspace) / "system"
    shm, ctl = sys_dir / "snappyHexMeshDict", sys_dir / "controlDict"
    shm_text = shm.read_text()
    ctl_text = ctl.read_text() if ctl.is_file() else None
    try:
        shm.write_text(stage_text(shm_text, stage))
        if binary and ctl_text is not None:
            ctl.write_text(re.sub(r"\bwriteFormat\s+\w+\s*;", "writeFormat binary;", ctl_text, count=1))
        yield
    finally:
        shm.write_text(shm_text)
        if ctl_text is not None:
            ctl.write_text(ctl_text)


def _files(root: Path) -> dict[str, int]:
    return {p.relative_to(root).as_posix(): p.stat().st_size for p in sorted(root.rglob("*")) if p.is_file()
            and p.name != KEY_FILE}


def keep(workspace, nproc: int, key: str) -> dict | None:
    """Copy the decomposed snapped mesh aside (KEEP_DIR), replacing an older one. Never raises; None when nothing
    was kept. The record has its bytes and the seconds the copy took."""
    ws = Path(workspace)
    dst = ws / KEEP_DIR
    t0 = time.monotonic()
    try:
        drop(ws)
        procs = sorted(ws.glob("processor*"))
        if len(procs) != int(nproc) or not all((p / "constant" / "polyMesh" / "owner").is_file() for p in procs):
            return None
        for p in procs:
            shutil.copytree(p / "constant" / "polyMesh", dst / p.name / "constant" / "polyMesh")
        files = _files(dst)
        rec = {"key": key, "nproc": int(nproc), "files": files, "bytes": int(sum(files.values())),
               "keep_s": round(time.monotonic() - t0, 2)}
        (dst / KEY_FILE).write_text(json.dumps(rec))
        return {k: v for k, v in rec.items() if k != "files"}
    except OSError:
        logger.warning("snappy: the snapped mesh could not be kept aside", exc_info=True)
        drop(ws)
        return None


def _whole(keep_dir: Path) -> dict | None:
    """The kept mesh's record when every file it listed is there at its size, else None."""
    try:
        rec = json.loads((keep_dir / KEY_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("files"), dict) or not rec["files"]:
        return None
    return rec if _files(keep_dir) == rec["files"] else None


def drop(workspace) -> bool:
    """Remove the attempt's kept snapped mesh and its layers-only plan. True when there was one."""
    ws = Path(workspace)
    had = (ws / KEEP_DIR).exists()
    shutil.rmtree(ws / KEEP_DIR, ignore_errors=True)
    (ws / LAYERS_ONLY_FILE).unlink(missing_ok=True)
    return had


def settle(workspace, *, refused_gate: str | None, facts) -> None:
    """THE ATTEMPT'S CHOICE IS MADE (pipeline/executor, after its gates): the kept mesh stays only for the one answer
    that reads it - a layer-stack refusal whose next attempt writes the same stack relative (`relative_next`)."""
    if refused_gate == "layer_stack" and isinstance(facts, dict) and facts.get("relative_next") is True:
        return
    if drop(workspace):
        logger.info("snappy: the kept snapped mesh is removed - the attempt's choice is made (%s)",
                    refused_gate or "its gates passed")


def write_reuse(workspace, rec: dict) -> None:
    try:
        (Path(workspace) / REUSE_FILE).write_text(json.dumps(rec))
    except OSError:
        pass


def read_reuse(workspace) -> dict | None:
    try:
        rec = json.loads((Path(workspace) / REUSE_FILE).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def adopt(workspace) -> dict | None:
    """THE RELATIVE RETRY (driver side, before its run): take over the previous attempt's kept snapped mesh when its
    castellation and snapping inputs are this case's, and plan the layers only (LAYERS_ONLY_FILE). Returns the record
    of the path planned, None for an attempt that is not the relative retry. The previous attempt keeps nothing
    either way."""
    from meshpipeline.engines.gate_facts import relative_stack_next
    ws = Path(workspace)
    (ws / LAYERS_ONLY_FILE).unlink(missing_ok=True)
    (ws / REUSE_FILE).unlink(missing_ok=True)
    if not relative_stack_next(ws):
        return None
    m = re.fullmatch(r"attempt_(\d+)", ws.name)
    prev = ws.parent / f"attempt_{int(m.group(1)) - 1}" if m and int(m.group(1)) >= 2 else None
    src = prev / KEEP_DIR if prev is not None else None
    rec: dict = {"path": "full", "rule": "CFDREQ-33"}
    if not LAYERS_ONLY_RETRY:
        rec["why"] = "the layers-only retry is switched off"
    elif src is None or not src.is_dir():
        rec["why"] = "the first attempt kept no snapped mesh"
    else:
        kept = _whole(src)
        try:
            key = snap_key(ws)
        except OSError:
            key = ""
        if kept is None:
            rec["why"] = "the kept snapped mesh is incomplete"
        elif kept.get("key") != key:
            rec["why"] = "this attempt's castellation or snapping is not the first attempt's"
        else:
            try:
                drop(ws)
                src.rename(ws / KEEP_DIR)
                (ws / LAYERS_ONLY_FILE).write_text(json.dumps({"from": prev.name if prev else "", "key": key,
                                                               "nproc": int(kept.get("nproc") or 0)}))
                rec = {"path": "layers_only", "rule": "CFDREQ-33", "from": prev.name if prev else "",
                       "kept_bytes": int(kept.get("bytes") or 0)}
            except OSError:
                drop(ws)
                rec["why"] = "the kept snapped mesh could not be moved into this attempt"
    if src is not None:
        shutil.rmtree(src, ignore_errors=True)
    write_reuse(ws, rec)
    logger.info("snappy relative retry: %s%s", rec["path"], f" ({rec['why']})" if rec.get("why") else "")
    return rec


def layers_only_ready(workspace, nproc: int) -> tuple[bool, str]:
    """Native side: (True, "") when the layers-only plan and a whole kept mesh on `nproc` ranks are both here;
    otherwise (False, why) and the kept mesh is removed - the run rebuilds in full."""
    ws = Path(workspace)
    if not (ws / LAYERS_ONLY_FILE).is_file():
        return False, ""
    kept = _whole(ws / KEEP_DIR)
    why = ("the kept snapped mesh is incomplete" if kept is None else
           f"the kept snapped mesh is on {kept.get('nproc')} ranks, this run on {nproc}"
           if int(kept.get("nproc") or 0) != int(nproc) else "")
    if why:
        drop(ws)
        return False, why
    return True, ""


def unpack(workspace) -> None:
    """Put the kept processor meshes in place for the layers-only run (a rename, no copy)."""
    ws = Path(workspace)
    for p in sorted((ws / KEEP_DIR).glob("processor*")):
        shutil.rmtree(ws / p.name, ignore_errors=True)
        p.rename(ws / p.name)
    drop(ws)


__all__ = ["KEEP_DIR", "KEY_FILE", "LAYERS_ONLY_FILE", "REUSE_FILE", "adopt", "drop", "keep", "layers_on",
           "layers_only_ready", "read_reuse", "settle", "snap_key", "stage_dict", "stage_text", "unpack",
           "wants_keep", "write_reuse"]
