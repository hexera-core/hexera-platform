# Responsibility: Verify the token estimate counts tool calls, reasoning and results, and survives a malformed message.
from meshpipeline.agents.builder.agent import (
    _BUILDER_CONTEXT_WINDOW,
    _CHECKPOINT_INJECT_THRESHOLD,
    _CHECKPOINT_RECOVER_THRESHOLD,
    _PER_MESSAGE_OVERHEAD_TOKENS,
    _count_message_tokens,
    _count_tokens,
    _get_tokenizer,
)


def test_threshold_constants_sane():
    assert 0 < _CHECKPOINT_INJECT_THRESHOLD < _CHECKPOINT_RECOVER_THRESHOLD < _BUILDER_CONTEXT_WINDOW


def test_tokenizer_loads():
    tok = _get_tokenizer()
    assert tok is not None
    assert tok.encode("hello") == _get_tokenizer().encode("hello")
    assert tok.n_vocab > 100_000
    assert tok.name == "cl100k_base"


def test_tokenizer_handles_unicode_and_special_strings():
    tok = _get_tokenizer()
    assert len(tok.encode("中文测试", disallowed_special=())) >= 2
    assert len(tok.encode("<|endoftext|>", disallowed_special=())) >= 1


def test_count_tokens_empty():
    assert _count_tokens("") == 0
    assert _count_tokens(None) == 0


def test_count_tokens_realistic_ratio():
    text = "The quick brown fox jumps over the lazy dog."
    n = _count_tokens(text)
    assert 8 <= n <= 15


def test_empty_messages_returns_zero():
    assert _count_message_tokens([]) == 0


def test_message_includes_overhead():
    msgs = [{"role": "user", "content": "hi"}]
    n = _count_message_tokens(msgs)
    assert n >= _PER_MESSAGE_OVERHEAD_TOKENS + 1


def test_count_grows_with_payload():
    short = [{"role": "user", "content": "hello"}]
    big = [{"role": "user", "content": "hello " * 5000}]
    assert _count_message_tokens(big) > _count_message_tokens(short) * 100


def test_count_includes_tool_calls():
    base = [{"role": "assistant", "content": "ok"}]
    with_tc = [{
        "role": "assistant",
        "content": "ok",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "write_file", "arguments": "x " * 2000},
        }],
    }]
    assert _count_message_tokens(with_tc) > _count_message_tokens(base) + 500


def test_count_includes_reasoning_content():
    base = [{"role": "assistant", "content": "ok"}]
    with_rc = [{"role": "assistant", "content": "ok", "reasoning_content": "thinking " * 1000}]
    assert _count_message_tokens(with_rc) > _count_message_tokens(base) + 500


def test_count_includes_tool_result_messages():
    msgs = [{
        "role": "tool",
        "content": "result " * 500,
        "tool_call_id": "call_1",
        "name": "write_file",
    }]
    assert _count_message_tokens(msgs) > 400


def test_count_crosses_inject_threshold_at_realistic_volume():
    unit = "def f():\n    return 42\n\n"
    reps = (_CHECKPOINT_INJECT_THRESHOLD * 6 // _count_tokens(unit)) + 1000
    msgs = [{"role": "user", "content": unit * reps}]
    n = _count_message_tokens(msgs)
    assert n >= _CHECKPOINT_INJECT_THRESHOLD, f"expected >= {_CHECKPOINT_INJECT_THRESHOLD}, got {n}"


def test_count_robust_to_non_dict_entries():
    msgs = ["unexpected", {"role": "user", "content": "ok"}]
    assert _count_message_tokens(msgs) > 0


def test_count_robust_to_missing_content():
    assert _count_message_tokens([{"role": "assistant"}]) >= _PER_MESSAGE_OVERHEAD_TOKENS


def test_count_robust_to_non_string_content():
    msgs = [{"role": "user", "content": {"text": "wrapped"}}]
    assert _count_message_tokens(msgs) > _PER_MESSAGE_OVERHEAD_TOKENS
