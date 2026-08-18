# Responsibility: Verify the extractor returns the last assistant message, and an empty string for any malformed shape.
from meshpipeline.agents.intake.agent import _extract_reply


def test_returns_single_assistant_content():
    result = {"messages": [{"role": "assistant", "content": "Hello, what mesh type?"}]}
    assert _extract_reply(result) == "Hello, what mesh type?"


def test_returns_last_assistant_message_when_multiple():
    result = {
        "messages": [
            {"role": "assistant", "content": "First reply."},
            {"role": "user",      "content": "Thanks."},
            {"role": "assistant", "content": "Second reply."},
        ]
    }
    assert _extract_reply(result) == "Second reply."


def test_skips_user_messages():
    result = {
        "messages": [
            {"role": "user",      "content": "User message"},
            {"role": "assistant", "content": "Assistant reply"},
        ]
    }
    assert _extract_reply(result) == "Assistant reply"


def test_skips_tool_role_messages():
    result = {
        "messages": [
            {"role": "tool",      "content": "tool result"},
            {"role": "assistant", "content": "Based on the result..."},
        ]
    }
    assert _extract_reply(result) == "Based on the result..."


def test_returns_empty_string_when_messages_key_missing():
    assert _extract_reply({}) == ""


def test_returns_empty_string_when_messages_is_none():
    assert _extract_reply({"messages": None}) == ""


def test_returns_empty_string_when_messages_is_empty_list():
    assert _extract_reply({"messages": []}) == ""


def test_returns_empty_string_when_no_assistant_message():
    result = {"messages": [{"role": "user", "content": "Hello"}]}
    assert _extract_reply(result) == ""


def test_returns_empty_string_when_assistant_content_empty():
    result = {"messages": [{"role": "assistant", "content": ""}]}
    assert _extract_reply(result) == ""


def test_skips_non_dict_entries_in_list():
    result = {
        "messages": [
            "bare string",
            None,
            42,
            {"role": "assistant", "content": "valid reply"},
        ]
    }
    assert _extract_reply(result) == "valid reply"


def test_skips_dicts_missing_role_key():
    result = {
        "messages": [
            {"content": "no role key"},
            {"role": "assistant", "content": "has role"},
        ]
    }
    assert _extract_reply(result) == "has role"


def test_returns_empty_string_for_entirely_non_dict_messages():
    result = {"messages": ["a", "b", "c"]}
    assert _extract_reply(result) == ""



def test_extract_reply_is_importable_from_intake():
    from meshpipeline.agents.intake.agent import _extract_reply as fn
    assert callable(fn)


def test_chat_imports_extract_reply_from_intake():
    import ast
    from pathlib import Path
    src = (Path(__file__).parent.parent.parent.parent.parent / "src" / "meshpipeline" / "api" / "v1" / "chat.py").read_text()
    tree = ast.parse(src)

    imported_from_intake = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "meshpipeline.agents.intake.agent"
        and any(alias.name == "_extract_reply" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imported_from_intake, "chat.py must import _extract_reply from meshpipeline.agents.intake.agent"

    local_def = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_extract_reply"
        for node in ast.walk(tree)
    )
    assert not local_def, "chat.py must not define its own _extract_reply"
