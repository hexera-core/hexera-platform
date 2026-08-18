# Responsibility: Verify a missing or empty prompt file raises actionably, and every shipped prompt loads.
from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

import meshpipeline.runtime.startup as startupcfg
import meshpipeline.settings.env as envcfg
import meshpipeline.settings.policy as polcfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.settings.env import load_prompt

APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


# The builder system prompt is authored inline (cfMesh); it is not a prompt file.
EXPECTED_REQUIRED_PROMPTS: list[tuple[str, str]] = [
    ("reviewer_system",       "reviewer/system.txt"),
    ("intake_system",         "intake/system.txt"),
    # All three agent roles load a general contract here. The Builder's was added when its shared
    # behaviour stopped being duplicated across the five engine packs.
    ("builder_system",        "builder/system.txt"),
]

REAL_PROMPTS_DIR = APP_DIR / "prompts"


def test_load_prompt_raises_for_missing_file(tmp_path):
    from meshpipeline.settings.env import ConfigurationError
    with pytest.raises(ConfigurationError) as exc:
        load_prompt(tmp_path / "nonexistent.txt")
    assert "not found" in str(exc.value).lower()
    assert "nonexistent.txt" in str(exc.value)


def test_load_prompt_raises_for_empty_file(tmp_path):
    from meshpipeline.settings.env import ConfigurationError
    p = tmp_path / "empty.txt"
    p.write_text("")
    with pytest.raises(ConfigurationError) as exc:
        load_prompt(p)
    assert "empty" in str(exc.value).lower()
    assert "empty.txt" in str(exc.value)


def test_load_prompt_raises_for_whitespace_only_file(tmp_path):
    from meshpipeline.settings.env import ConfigurationError
    p = tmp_path / "whitespace.txt"
    p.write_text("   \n\n\t  \n")
    with pytest.raises(ConfigurationError) as exc:
        load_prompt(p)
    assert "empty" in str(exc.value).lower()


def test_load_prompt_returns_stripped_content(tmp_path):
    p = tmp_path / "good.txt"
    p.write_text("  Hello, prompt!\n\n")
    result = load_prompt(p)
    assert result == "Hello, prompt!"


def test_load_prompt_error_message_is_actionable(tmp_path):
    from meshpipeline.settings.env import ConfigurationError
    with pytest.raises(ConfigurationError) as exc:
        load_prompt(tmp_path / "missing.txt")
    msg = str(exc.value)
    assert "create" in msg.lower() or "add" in msg.lower() or "before" in msg.lower()


def test_load_prompt_empty_error_message_is_actionable(tmp_path):
    from meshpipeline.settings.env import ConfigurationError
    p = tmp_path / "empty.txt"
    p.write_text("")
    with pytest.raises(ConfigurationError) as exc:
        load_prompt(p)
    msg = str(exc.value)
    assert "add" in msg.lower() or "content" in msg.lower() or "before" in msg.lower()



def _make_full_prompts_dir(base: Path) -> Path:
    d = base / "prompts"
    d.mkdir()
    for _, fname in EXPECTED_REQUIRED_PROMPTS:
        (d / fname).parent.mkdir(parents=True, exist_ok=True)
        (d / fname).write_text(f"Prompt content for {fname}.")
    return d


def test_load_all_prompts_raises_when_dir_missing(tmp_path):
    from meshpipeline.settings.env import ConfigurationError
    from meshpipeline.settings.policy import _load_all_prompts
    missing_dir = tmp_path / "no_such_prompts"
    with patch.object(envcfg, "PROMPTS_DIR", missing_dir):
        with pytest.raises(ConfigurationError) as exc:
            _load_all_prompts()
    msg = str(exc.value).lower()
    assert "not found" in msg or "directory" in msg


def test_load_all_prompts_error_lists_required_files(tmp_path):
    from meshpipeline.settings.env import ConfigurationError
    from meshpipeline.settings.policy import _load_all_prompts
    with patch.object(envcfg, "PROMPTS_DIR", tmp_path / "absent"):
        with pytest.raises(ConfigurationError) as exc:
            _load_all_prompts()
    msg = str(exc.value)
    assert "reviewer/system.txt" in msg
    assert "intake/system.txt" in msg


@pytest.mark.parametrize("missing_attr,missing_fname", EXPECTED_REQUIRED_PROMPTS)
def test_load_all_prompts_raises_for_each_missing_file(tmp_path, missing_attr, missing_fname):
    from meshpipeline.settings.env import ConfigurationError
    from meshpipeline.settings.policy import _load_all_prompts
    d = _make_full_prompts_dir(tmp_path)
    (d / missing_fname).unlink()
    with patch.object(envcfg, "PROMPTS_DIR", d):
        with pytest.raises(ConfigurationError) as exc:
            _load_all_prompts()
    assert missing_fname in str(exc.value)


@pytest.mark.parametrize("empty_attr,empty_fname", EXPECTED_REQUIRED_PROMPTS)
def test_load_all_prompts_raises_for_each_empty_file(tmp_path, empty_attr, empty_fname):
    from meshpipeline.settings.env import ConfigurationError
    from meshpipeline.settings.policy import _load_all_prompts
    d = _make_full_prompts_dir(tmp_path)
    (d / empty_fname).write_text("")
    with patch.object(envcfg, "PROMPTS_DIR", d):
        with pytest.raises(ConfigurationError) as exc:
            _load_all_prompts()
    assert empty_fname in str(exc.value)


def test_load_all_prompts_success(tmp_path):
    from meshpipeline.settings.policy import _load_all_prompts
    d = _make_full_prompts_dir(tmp_path)
    with patch.object(envcfg, "PROMPTS_DIR", d):
        p = _load_all_prompts()
    for attr, fname in EXPECTED_REQUIRED_PROMPTS:
        val = getattr(p, attr, None)
        assert val, f"prompts.{attr} must be non-empty after successful load"
        assert val == f"Prompt content for {fname}."



def test_required_prompts_matches_expected():
    # The required-prompt CONTRACT is the exact set of names (semantic), not a count:
    # this catches an added/removed/renamed required prompt with an actionable diff.
    # (Replaces a former `len(REQUIRED_PROMPTS) == 7` lock, which had no unique failure
    # mode over this and only fossilised an arbitrary number.)
    from meshpipeline.settings.policy import REQUIRED_PROMPTS
    assert set(REQUIRED_PROMPTS) == set(EXPECTED_REQUIRED_PROMPTS)


def test_required_prompts_includes_intake():
    from meshpipeline.settings.policy import REQUIRED_PROMPTS
    fnames = [fname for _, fname in REQUIRED_PROMPTS]
    assert "intake/system.txt" in fnames




def test_validate_logs_all_prompt_files(tmp_path, caplog):
    from meshpipeline.settings.policy import REQUIRED_PROMPTS, _load_all_prompts
    d = _make_full_prompts_dir(tmp_path)

    with patch.object(envcfg, "PROMPTS_DIR", d), \
         patch.object(polcfg, "prompts", _load_all_prompts.__wrapped__() if hasattr(_load_all_prompts, "__wrapped__") else _load_all_prompts()), \
         patch.object(rtcfg, "WORKSPACE_BASE", tmp_path), \
         patch.object(rtcfg, "JOBS_DIR", tmp_path, create=True), \
         patch.object(rtcfg, "CORPUS_DIR", str(tmp_path)), \
         patch.object(rtcfg, "OPENFOAM_BASHRC", str(tmp_path / "nonexistent")), \
         caplog.at_level(logging.INFO, logger="meshpipeline.runtime.startup"):
        with patch.object(envcfg, "PROMPTS_DIR", d):
            fresh_prompts = _load_all_prompts() if True else None
        with patch.object(polcfg, "prompts", fresh_prompts):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                startupcfg.validate()

    logged = caplog.text
    for _, fname in REQUIRED_PROMPTS:
        assert fname in logged, f"validate() must log that {fname!r} was loaded"



def _intake_src() -> str:
    return (APP_DIR / "agents" / "intake" / "agent.py").read_text(encoding="utf-8")


def test_intake_no_fallback_branch_in_ast():
    src = _intake_src()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_FALLBACK_SYSTEM":
                    pytest.fail("AST found assignment to _FALLBACK_SYSTEM in intake.py")
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name == "_FALLBACK_SYSTEM":
                pytest.fail("AST found reference to _FALLBACK_SYSTEM in intake.py")



@pytest.mark.parametrize("attr,fname", EXPECTED_REQUIRED_PROMPTS)
def test_real_prompt_file_exists(attr, fname):
    path = REAL_PROMPTS_DIR / fname
    assert path.exists(), f"Required prompt file missing: {path}"


@pytest.mark.parametrize("attr,fname", EXPECTED_REQUIRED_PROMPTS)
def test_real_prompt_file_nonempty(attr, fname):
    path = REAL_PROMPTS_DIR / fname
    if path.exists():
        content = path.read_text(encoding="utf-8").strip()
        assert content, f"Required prompt file is empty: {path}"


def test_real_prompts_load_without_error():
    from meshpipeline.settings.policy import _load_all_prompts
    p = _load_all_prompts()
    for attr, _ in EXPECTED_REQUIRED_PROMPTS:
        assert getattr(p, attr, ""), f"prompts.{attr} is empty after loading real prompts"
