# Responsibility: Prove a local stack that cannot look at anything says so before it starts, and says it about
#                 the .env its containers will read rather than about the developer's shell.
# Boundaries: it runs the real gate against a temporary .env; it starts nothing and reads no real secret.
#
# THE DEFECT. GEOMETRY_VISION_PROVIDER defaults to openai, the local .env carried no OPENAI_API_KEY, and the
# stack started clean. `reader()` returned None, every look stored `not_attempted` with an honest reason, and
# the only place that said so out loud was `deploy/docker/entrypoint.sh`'s warning INSIDE the container - which
# nobody reads unless they already suspect something. So a stack whose Surveyor could never look at a part was
# indistinguishable, from the outside, from one that could, until an upload came back with half the product
# missing. An honest refusal on a row is still a useless upload.
#
# AND THE OBVIOUS FIX WOULD HAVE HAD THE SIGNATURE FAILURE. `policy.vision_reader_has_no_key()` answers for the
# process that imported policy. `make dev-up` runs on the HOST, where the key is in .env and almost never
# exported, so a gate that called that function would have reported on the developer's shell and warned on
# every machine including the ones that are fine - a check whose subject is not the thing it checks. The gate
# reads the `.env` view the containers get and asks policy only for the WORDING.
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

import meshpipeline.settings.policy as policy

REPO = Path(__file__).resolve().parents[3]
GATE_PATH = REPO / "devtools" / "env" / "check_runtime_config.py"

#: Not a credential. Nothing here reaches a network; the gate only asks whether the value is non-empty.
NOT_A_KEY = "not-a-real-key"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """The real gate module, pointed at a .env of this test's own.

    Loaded by path because devtools/ is not an importable package, which is how the existing onboarding
    contract test reaches the same file.
    """
    spec = importlib.util.spec_from_file_location("check_runtime_config_under_test", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ENV", tmp_path / ".env")
    # The host environment must not decide the outcome: these four are what the verdict turns on.
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPINFRA_API_KEY", "DEEPSEEK_API_KEY",
                 "GEOMETRY_VISION_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    return mod


def _write(gate, text: str) -> None:
    gate.ENV.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- the notice, and when there is none

def test_a_stack_with_no_reader_key_says_so_and_names_what_to_put_where(gate):
    _write(gate, "GEOMETRY_VISION_PROVIDER=openai\n")
    (notice,) = gate.notices()
    assert "OPENAI_API_KEY" in notice, notice
    assert "no upload will be looked at" in notice, notice
    # a diagnosis that does not say what to do is half a notice
    assert "in .env" in notice and "GEOMETRY_VISION_PROVIDER=off" in notice, notice
    # and it says the stack is not being stopped, so nobody reads it as the reason a start failed
    assert "still starts" in notice, notice


def test_the_key_being_in_the_env_file_is_enough_which_is_the_whole_point(gate):
    """THE REGRESSION. The key lives in .env and not in the shell that runs `make dev-up`: that is the normal,
    correct arrangement, and it is exactly the case a gate built on `vision_reader_has_no_key()` gets wrong."""
    _write(gate, f"GEOMETRY_VISION_PROVIDER=openai\nOPENAI_API_KEY={NOT_A_KEY}\n")
    assert gate.notices() == []
    # The premise, stated as the mechanism rather than as a claim about the runner's own shell, which this test
    # does not control: policy builds its key table at IMPORT from os.environ, so what it would say about a
    # process that never exported the key is this - a warning, on a configuration that is correct.
    assert "OPENAI_API_KEY" in policy.vision_reader_verdict("openai", key_is_set=False)
    assert policy.vision_reader_verdict("openai", key_is_set=True) == ""


def test_a_provider_that_takes_no_look_is_a_decision_and_not_a_notice(gate):
    _write(gate, "GEOMETRY_VISION_PROVIDER=off\n")
    assert gate.notices() == []


@pytest.mark.parametrize("provider,key", [("openai", "OPENAI_API_KEY"),
                                          ("anthropic", "ANTHROPIC_API_KEY"),
                                          ("deepinfra", "DEEPINFRA_API_KEY"),
                                          ("deepseek", "DEEPSEEK_API_KEY")])
def test_every_provider_is_judged_on_its_own_key_and_no_other(gate, provider, key):
    """A provider with no key is a look that does not happen, never a fall-through, so the other three keys
    being set may not silence the notice. Parametrised over the providers policy itself declares, so a fifth
    one added there fails here rather than being judged by nothing."""
    assert provider in policy.GEOMETRY_VISION_PROVIDERS
    others = "\n".join(f"{policy.vision_reader_key_name(p)}={NOT_A_KEY}"
                       for p in policy.GEOMETRY_VISION_PROVIDERS
                       if p not in (provider, "off"))
    _write(gate, f"GEOMETRY_VISION_PROVIDER={provider}\n{others}\n")
    (notice,) = gate.notices()
    assert key in notice, notice
    _write(gate, f"GEOMETRY_VISION_PROVIDER={provider}\n{key}={NOT_A_KEY}\n")
    assert gate.notices() == []


def test_the_default_provider_is_what_an_env_that_names_none_is_judged_as(gate):
    # The local .env the desktop shortcut's stack reads names no GEOMETRY_ setting at all, so the catalogue
    # default decides - and that default is openai. An empty file must reach the same verdict as an explicit one.
    _write(gate, "# nothing about the look at all\n")
    (notice,) = gate.notices()
    assert policy.GEOMETRY_VISION_PROVIDER == "openai"
    assert "OPENAI_API_KEY" in notice, notice


# ---------------------------------------------------------------- it is a notice, not a refusal

def test_a_missing_reader_does_not_stop_the_stack(gate):
    """The look is the one step built on never taking anything down. A developer with no OpenAI account must
    still be able to run the platform, so this may never become a problem."""
    _write(gate, "GEOMETRY_VISION_PROVIDER=openai\n")
    assert gate.notices(), "the premise is gone: there is no notice to check is harmless"
    assert not any("OPENAI" in p or "look" in p for p in gate.problems()), gate.problems()


def test_the_notice_is_printed_even_when_something_else_stops_the_stack(gate, capsys, monkeypatch):
    # The two are independent: a developer fixing a credential should see the reader notice in the same run,
    # not on the next one.
    _write(gate, "GEOMETRY_VISION_PROVIDER=openai\n")
    monkeypatch.setattr(gate, "problems", lambda: ["something else is wrong"])
    assert gate.main(["check_runtime_config.py"]) == 1
    out = capsys.readouterr()
    assert "OPENAI_API_KEY" in out.out, out.out
    assert "something else is wrong" in out.err, out.err


def test_machine_output_carries_no_notice(gate, capsys):
    # `make dev-up` pipes --credential-path straight into a Compose bind mount. A sentence in that stream is a
    # broken mount, so the notice must not reach it.
    _write(gate, "GEOMETRY_VISION_PROVIDER=openai\n")
    assert gate.main(["check_runtime_config.py", "--credential-path"]) == 0
    printed = capsys.readouterr().out
    assert printed.strip() == str(gate.canonical()), printed
    assert "OPENAI" not in printed


# ---------------------------------------------------------------- one wording, in one place

def test_the_gate_keeps_no_copy_of_the_sentence_or_of_any_key_name():
    """policy.py owns what the verdict says. Three things repeat it - the container entrypoints, the startup
    banner and this gate - and a fourth spelling is a wording that drifts from what the application reads."""
    source = GATE_PATH.read_text(encoding="utf-8")
    assert "no upload will be looked at" not in source, (
        "the gate spells the verdict out itself instead of asking policy for it")
    for provider in policy.GEOMETRY_VISION_PROVIDERS:
        name = policy.vision_reader_key_name(provider)
        if name:
            assert name not in source, f"the gate keeps its own copy of {name}"
    assert "vision_reader_verdict" in source and "vision_reader_key_name" in source, (
        "the gate no longer reads the verdict from the module that owns it")


def test_policys_own_answer_is_the_general_one_applied_to_this_process():
    """`vision_reader_has_no_key()` must stay the same function as the general one, or the container entrypoint
    and the dev-up gate answer the same question two ways."""
    for provider in policy.GEOMETRY_VISION_PROVIDERS:
        expect_quiet = provider == "off"
        assert (policy.vision_reader_verdict(provider, False) == "") is expect_quiet
        assert policy.vision_reader_verdict(provider, True) == ""
    src = Path(policy.__file__).read_text(encoding="utf-8")
    body = re.search(r"def vision_reader_has_no_key\(\).*?\n(?=\n\n|\Z)", src, re.S).group(0)
    assert "vision_reader_verdict(" in body, (
        "vision_reader_has_no_key composes its own sentence again instead of calling the one that owns it")


def test_the_question_that_cannot_be_asked_is_not_answered_yes(gate, monkeypatch):
    """The entrypoint learned this the hard way: `2>/dev/null || echo ""` collapsed "fine", "no key" and "could
    not ask" into one empty string, and a hosted container skipped the gate in silence. The same rule here."""
    _write(gate, "GEOMETRY_VISION_PROVIDER=openai\n")

    def refuse(*_a, **_k):
        raise RuntimeError("the reader question cannot be answered here")

    monkeypatch.setattr(policy, "vision_reader_verdict", refuse)
    (notice,) = gate.notices()
    assert "could not be determined" in notice, notice
    assert "is not a question that answered yes" in notice, notice
