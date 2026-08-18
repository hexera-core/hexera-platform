# Responsibility: Assemble the evidence a builder attempt is allowed to see.
# Boundaries: assembly and bounding.
from __future__ import annotations

import json

_BUILDER_CONTEXT_WINDOW = 262_144

_CHECKPOINT_INJECT_THRESHOLD = int(_BUILDER_CONTEXT_WINDOW * 0.35)

_CHECKPOINT_RECOVER_THRESHOLD = int(_BUILDER_CONTEXT_WINDOW * 0.40)

_CHECKPOINT_REINJECTION_GAP = 10_000

_TOKENIZER_ENCODING_NAME = "cl100k_base"
_PER_MESSAGE_OVERHEAD_TOKENS = 4

_tokenizer_singleton = None


def _get_tokenizer():
    global _tokenizer_singleton
    if _tokenizer_singleton is None:
        import tiktoken
        _tokenizer_singleton = tiktoken.get_encoding(_TOKENIZER_ENCODING_NAME)
    return _tokenizer_singleton


def _count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_get_tokenizer().encode(text, disallowed_special=()))


def _count_message_tokens(messages: list) -> int:
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            total += _count_tokens(str(m)) + _PER_MESSAGE_OVERHEAD_TOKENS
            continue
        total += _PER_MESSAGE_OVERHEAD_TOKENS
        for key in ("role", "name", "tool_call_id"):
            v = m.get(key)
            if isinstance(v, str):
                total += _count_tokens(v)
        content = m.get("content")
        if isinstance(content, str):
            total += _count_tokens(content)
        elif content is not None:
            total += _count_tokens(json.dumps(content, ensure_ascii=False))
        rc = m.get("reasoning_content")
        if isinstance(rc, str):
            total += _count_tokens(rc)
        tcs = m.get("tool_calls")
        if tcs:
            try:
                total += _count_tokens(json.dumps(tcs, ensure_ascii=False))
            except (TypeError, ValueError):
                total += sum(_count_tokens(str(tc)) for tc in tcs)
    return total



def _compress_tool_output(tool_name: str, args: dict, result_str: str,
                          protect: tuple = ()) -> str:
    try:
        result_dict = json.loads(result_str)
    except json.JSONDecodeError:
        result_dict = {}

    if tool_name == "write_file":
        return result_str

    elif tool_name == "read_file":
        path = args.get("path", "")
        _lower = path.lower().lstrip("./")
        if any(_lower.endswith(_rel.lower()) for _rel in protect) or any(
            tok in _lower for tok in [
                "mesh_manifest.json", "knowledge_block",
                ".log", "error", "stderr", "stdout", "warning",
            ]
        ):
            return result_str
        content = result_dict.get("content", "")
        lines = content.count("\n") + 1 if content else 0
        return f"[read_file: {path} ({lines} lines) - content omitted for brevity]"

    elif tool_name == "list_directory":
        path = args.get("path", ".")
        entries = result_dict.get("entries", [])
        if isinstance(entries, list):
            names = ", ".join(str(e) for e in entries[:10])
            suffix = f" (+{len(entries) - 10} more)" if len(entries) > 10 else ""
            return f"[list_directory: {path} - {len(entries)} items: {names}{suffix}]"
        return result_str

    else:
        return result_str


