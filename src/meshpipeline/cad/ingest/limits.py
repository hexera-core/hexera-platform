# Responsibility: Bound what reading an uploaded geometry file may cost, and refuse the files built to make it cost more.
# Owns: the size and count ceilings, the zip-bomb and path checks for zipped formats, and the glTF self-containment check.
# Boundaries: answers "is this safe to read"; reads only directories, headers and JSON, never the geometry itself.
# Collaborates with: cad/ingest/upload_check.py (at upload) and cad/ingest/readers.py (again, at read time).
from __future__ import annotations

import json
import zipfile

#: More triangles than any surface a mesher here can use: a 500 MB binary STL holds 10.5 million.
MAX_TRIANGLES = 40_000_000
#: A zipped model (3MF) may expand to at most this much XML in total...
MAX_UNZIPPED_BYTES = 4 * 1024 ** 3
#: ...from at most this many parts...
MAX_ZIP_ENTRIES = 10_000
#: ...and no part may claim to expand by more than this. Real 3MF XML compresses 5-20x; a zip bomb
#: compresses a thousandfold and more.
MAX_ZIP_RATIO = 200
#: Below this, a high ratio is harmless (a few MB of repeated zeros is not a bomb).
ZIP_RATIO_FLOOR_BYTES = 64 * 1024 * 1024

#: glTF extensions that compress mesh data into a form no open reader here decodes.
_COMPRESSED_MESH_EXTENSIONS = ("KHR_draco_mesh_compression", "EXT_meshopt_compression",
                               "KHR_meshopt_compression")


class ReadLimitExceeded(ValueError):
    pass


def too_many_triangles() -> str:
    return (f"the file holds more than {MAX_TRIANGLES:,} triangles - far more than a CFD surface "
            f"needs; export it coarser and upload that")


def zip_refusal(zf: zipfile.ZipFile) -> str:
    """Why this archive must not be read, in a sentence a user can act on; "" when it is fine."""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        return f"the archive holds {len(infos):,} parts; a 3MF model needs a handful"
    total = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or any(p == ".." for p in name.split("/")) or ":" in name:
            return "the archive contains a part with an unsafe path"
        if info.flag_bits & 0x1:
            return "the archive is encrypted"
        total += int(info.file_size)
        if info.file_size > ZIP_RATIO_FLOOR_BYTES and \
                info.file_size > MAX_ZIP_RATIO * max(int(info.compress_size), 1):
            return "the archive expands far beyond any real model (a zip bomb)"
    if total > MAX_UNZIPPED_BYTES:
        return (f"the archive expands to {total / 1024 ** 3:.1f} GB, more than the "
                f"{MAX_UNZIPPED_BYTES / 1024 ** 3:.0f} GB a model is allowed")
    return ""


class CappedReader:
    """A file object that stops at MAX_UNZIPPED_BYTES whatever the zip directory claimed: the
    sizes in a zip's directory are written by whoever made the file, and can lie."""

    def __init__(self, raw, cap: int = MAX_UNZIPPED_BYTES):
        self._raw = raw
        self._left = cap

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = 1 << 20
        chunk = self._raw.read(min(n, self._left + 1))
        self._left -= len(chunk)
        if self._left < 0:
            raise ReadLimitExceeded("the archive expands beyond the size a model is allowed")
        return chunk


def gltf_json(data: bytes) -> dict:
    """The JSON half of a .gltf or a .glb."""
    if data[:4] == b"glTF":
        if len(data) < 20:
            raise ValueError("the GLB file is truncated")
        length = int.from_bytes(data[12:16], "little")
        if data[16:20] != b"JSON" or 20 + length > len(data):
            raise ValueError("the GLB file has no JSON chunk where the format puts it")
        return json.loads(data[20:20 + length].decode("utf-8"))
    return json.loads(data.decode("utf-8"))


def gltf_refusal(data: bytes) -> str:
    """Why this glTF cannot be read here, as a sentence with the way on; "" when it can.

    A .gltf may keep its geometry in separate .bin files; an upload is one file, so those never
    arrive - and a reader that went looking for them would be reading this machine's disk."""
    try:
        doc = gltf_json(data)
    except (ValueError, UnicodeDecodeError) as exc:
        return f"the glTF file is not valid ({str(exc)[:120]})"
    if not isinstance(doc, dict):
        return "the glTF file is not valid (its JSON is not an object)"
    for buf in doc.get("buffers") or []:
        uri = buf.get("uri") if isinstance(buf, dict) else None
        if uri is not None and not str(uri).startswith("data:"):
            return ("the glTF file keeps its geometry in a separate file "
                    f"({str(uri)[:60]}), which an upload does not carry - export a single binary "
                    ".glb file instead and upload that")
    used = set(doc.get("extensionsRequired") or []) | set(doc.get("extensionsUsed") or [])
    compressed = [e for e in _COMPRESSED_MESH_EXTENSIONS if e in used]
    if compressed:
        return (f"the glTF file's meshes are compressed ({compressed[0]}) - export it again with "
                "mesh compression turned off")
    if not doc.get("meshes"):
        return "the glTF file holds no meshes"
    return ""
