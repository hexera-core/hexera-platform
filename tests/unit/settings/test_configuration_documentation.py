# Responsibility: Verify the operator-facing configuration document states what the runtime does.
# Boundaries: it renders from the authorities and runs documented examples through real parsers.
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
DOC = REPO / "docs/reference/configuration.md"

from meshpipeline.settings import inventory as cat  # noqa: E402
from meshpipeline.settings.env import ConfigurationError  # noqa: E402


def doc() -> str:
    return DOC.read_text(encoding="utf-8")


# generated blocks are exactly what the authorities render

@pytest.mark.parametrize("begin,end,render", [
    (cat.REFERENCE_BEGIN, cat.REFERENCE_END, cat.render_reference),
    (cat.REMOVED_BEGIN, cat.REMOVED_END, cat.render_removed_reference),
    (cat.DEVENV_BEGIN, cat.DEVENV_END, cat.render_dev_environments),
])
def test_a_generated_block_matches_its_authority(begin, end, render):
    text = doc()
    assert text.count(begin) == 1 and text.count(end) == 1
    i, j = text.index(begin), text.index(end) + len(end)
    assert text[i:j] == render(), (
        f"the block at {begin} is stale - regenerate it from settings/inventory.py")


def test_every_removed_name_and_family_appears_in_the_generated_section():
    block = cat.render_removed_reference()
    for name in cat.REMOVED:
        assert f"`{name}`" in block, f"{name} is retired but absent from the generated section"
    for fam in cat.REMOVED_FAMILIES:
        assert f"`{fam.label}`" in block and f"`{fam.replacement}`" in block


def test_no_handwritten_removed_list_survives_outside_the_generated_block():
    text = doc()
    i, j = text.index(cat.REMOVED_BEGIN), text.index(cat.REMOVED_END)
    outside = text[:i] + text[j:]
    # the prose may point AT the authority; it may not restate its contents or its size
    # Word-boundary matched, not substring: "213 entries" in the settings roster CONTAINS
    # "13 entries", and this check is about the removed inventory's size, not the roster's.
    # A bare `in` made an unrelated settings addition fail this test.
    for stale in ("Currently one entry", "eight entries", "13 entries"):
        assert not re.search(rf"\b{re.escape(stale)}", outside), \
            f"the prose restates the removed inventory: {stale!r}"
    named = [n for n in cat.REMOVED if f"`{n}`" in outside]
    assert named == [], f"retired names are hand-listed outside the generated block: {named}"


def test_block_replacement_touches_only_its_own_region():
    text = doc()
    out = cat.replace_block(text, cat.REMOVED_BEGIN, cat.REMOVED_END, "<!-- BEGIN GENERATED REMOVED SETTINGS -->X<!-- END GENERATED REMOVED SETTINGS -->")
    i = text.index(cat.REMOVED_BEGIN)
    assert out[:i] == text[:i], "prose before the block changed"
    tail = text[text.index(cat.REMOVED_END) + len(cat.REMOVED_END):]
    assert out.endswith(tail), "prose after the block changed"


def test_block_replacement_refuses_missing_or_duplicated_markers():
    with pytest.raises(ValueError):
        cat.replace_block("no markers here", cat.REMOVED_BEGIN, cat.REMOVED_END, "x")
    doubled = doc() + cat.REMOVED_BEGIN + cat.REMOVED_END
    with pytest.raises(ValueError):
        cat.replace_block(doubled, cat.REMOVED_BEGIN, cat.REMOVED_END, "x")


def test_generation_is_deterministic_and_idempotent():
    for render in (cat.render_reference, cat.render_removed_reference, cat.render_dev_environments):
        assert render() == render()
    once = cat.replace_block(doc(), cat.REMOVED_BEGIN, cat.REMOVED_END, cat.render_removed_reference())
    twice = cat.replace_block(once, cat.REMOVED_BEGIN, cat.REMOVED_END, cat.render_removed_reference())
    assert once == twice


def test_the_generated_sections_carry_no_secret():
    # A settings document is rendered from real values, so a key-shaped string reaching it is a
    # leak into a file people paste into issues.
    block = cat.render_removed_reference() + cat.render_reference()
    for bad in ("sk-", "tvly-", "AKIA"):
        assert bad not in block


# the document's environment contract matches the classifier

def test_the_document_does_not_claim_only_production_is_hardened():
    text = doc()
    assert "ENV=production` enforces" not in text
    assert "requires_hardened_runtime" in text, "the document does not name the real classifier"


def test_the_documented_development_names_are_the_classifier_s_own():
    block = cat.render_dev_environments()
    from meshpipeline.settings.policy import _AUTH_OPTIONAL_ENVS, requires_hardened_runtime
    for name in _AUTH_OPTIONAL_ENVS:
        assert f"`{name}`" in block
        assert requires_hardened_runtime(name) is False
    listed = set(re.findall(r"`([a-z]+)`", block))
    assert listed == set(_AUTH_OPTIONAL_ENVS), "the block and the classifier disagree"


@pytest.mark.parametrize("name,hardened", [
    ("production", True), ("staging", True), ("prod", True), ("hosted", True),
    ("aurora-eu-west-1", True), ("DEV", False), (" dev ", True),
])
def test_documented_classification_matches_behaviour(name, hardened):
    from meshpipeline.settings.policy import requires_hardened_runtime
    assert requires_hardened_runtime(name) is hardened


def test_the_document_states_the_case_and_whitespace_rule():
    text = doc()
    assert "lower-cases" in text or "case-fold" in text
    assert '`" dev "`' in text and "whitespace" in text


def test_the_document_does_not_call_the_user_token_secret_optional():
    text = doc()
    i = text.index("## Authentication and environment")
    section = text[i:text.index("## ", i + 10)]
    assert "Required in every hardened environment" in section
    assert "it is not optional there" in section


def test_the_document_admits_both_database_forms():
    text = doc()
    i = text.index("## Authentication and environment")
    section = text[i:text.index("## ", i + 10)]
    assert "DATABASE_URL" in section and "POSTGRES_PASSWORD" in section
    assert "either" in section.lower(), "the document presents one database path as the only one"


# exposure and path prose agree with the catalogue and with runtime

def test_the_document_explains_that_absence_from_the_template_is_not_unsupported():
    text = doc()
    assert "absence from `.env.example` does not mean a name is unsupported" in text.lower() or \
           "does not mean a name is unsupported" in text


def test_the_documented_path_table_matches_the_catalogue():
    text = doc()
    for name in ("DATA_ROOT", "WORKSPACE_BASE", "STATIC_DIR"):
        assert f"| `{name}` | `{cat.get(name).default}` |" in text, f"{name}'s documented default is stale"
    for name in ("JOBS_DIR", "CORPUS_DIR"):
        assert f"*derived* `{cat.get(name).derived}`" in text, f"{name}'s documented derivation is stale"


# the documented migration examples are run through the production parsers

def _fenced_examples(setting: str) -> list[str]:
    return [m.group(1).strip() for m in
            re.finditer(rf"```\n{setting}=(.+?)\n```", doc(), re.S)]


@pytest.mark.parametrize("setting", ["MODEL_PRICE_OVERRIDES", "MODEL_DOMAIN_BUDGETS"])
def test_the_documented_examples_parse(setting, monkeypatch):
    from meshpipeline.settings import inference_overrides as io
    parser = io.price_overrides if setting == "MODEL_PRICE_OVERRIDES" else io.domain_budgets
    examples = _fenced_examples(setting)
    assert examples, f"the document shows no example for {setting}"
    for ex in examples:
        monkeypatch.setenv(setting, ex)
        got = parser()
        assert got, f"the documented {setting} example parsed to nothing: {ex!r}"


@pytest.mark.parametrize("setting,bad", [
    ("MODEL_PRICE_OVERRIDES", "deepinfra:m=1,2"),
    ("MODEL_PRICE_OVERRIDES", "openai:gpt-4=1,2,3"),
    ("MODEL_DOMAIN_BUDGETS", "deepinfra:default:m=0"),
    ("MODEL_DOMAIN_BUDGETS", "deepinfra:default=4"),
])
def test_forms_the_document_says_are_refused_really_are(setting, bad, monkeypatch):
    from meshpipeline.settings import inference_overrides as io
    parser = io.price_overrides if setting == "MODEL_PRICE_OVERRIDES" else io.domain_budgets
    monkeypatch.setenv(setting, bad)
    with pytest.raises(ConfigurationError):
        parser()


def test_the_documented_examples_use_only_supported_providers():
    from meshpipeline.settings.providers import LLM_PROVIDER_KEY_ENV
    for setting in ("MODEL_PRICE_OVERRIDES", "MODEL_DOMAIN_BUDGETS"):
        for ex in _fenced_examples(setting):
            for record in ex.split(";"):
                provider = record.split(":", 1)[0].strip()
                assert provider in LLM_PROVIDER_KEY_ENV, (
                    f"the {setting} example names {provider!r}, which this product does not support")


def test_both_structured_settings_are_operator_facing():
    for name in ("MODEL_PRICE_OVERRIDES", "MODEL_DOMAIN_BUDGETS"):
        assert cat.get(name).exposure == "template", (
            f"{name} is the migration target for operator configuration; it belongs in the template")
        assert f"\n{name}=" in cat.render_env()


# retired configuration is refused - exact names and anchored families alike

def _boot(**env) -> subprocess.CompletedProcess:
    src = str(REPO / "src")
    code = (f"import sys;sys.path.insert(0,{src!r});"
            "from meshpipeline.runtime import startup;startup.validate();print('STARTED')")
    base = {"DEEPSEEK_API_KEY": "x", "DEEPINFRA_API_KEY": "x", "POSTGRES_PASSWORD": "x", "ENV": "dev"}
    return subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env={**base, **env},
                          capture_output=True, text=True)


SENTINEL = "sk-Zq7-SENTINEL-CREDENTIAL-9f3"


@pytest.mark.parametrize("name", sorted(cat.REMOVED))
def test_an_exact_retired_name_refuses_before_any_side_effect(name):
    r = _boot(**{name: SENTINEL})
    assert r.returncode != 0 and "STARTED" not in r.stdout
    assert name in r.stderr, "the refusal does not name the offending variable"
    assert SENTINEL not in r.stderr, "the refusal echoed the configured value"


@pytest.mark.parametrize("name,family", [
    ("MODEL_PRICE_DEEPSEEK_DEEPSEEK_V4_PRO", "MODEL_PRICE_OVERRIDES"),
    ("MODEL_PRICE_DEEPINFRA_ZAI_ORG_GLM_5_2", "MODEL_PRICE_OVERRIDES"),
    ("MODEL_PRICE_ANYTHING_AT_ALL", "MODEL_PRICE_OVERRIDES"),
    ("MODEL_BUDGET_DEEPINFRA_DEFAULT_GLM", "MODEL_DOMAIN_BUDGETS"),
    ("MODEL_BUDGET_X", "MODEL_DOMAIN_BUDGETS"),
])
def test_a_retired_family_name_refuses_and_names_its_replacement(name, family):
    r = _boot(**{name: SENTINEL})
    assert r.returncode != 0 and "STARTED" not in r.stdout, f"{name} was silently ignored"
    assert name in r.stderr and family in r.stderr
    assert SENTINEL not in r.stderr


@pytest.mark.parametrize("value", ["1,2,3", "garbage", ""])
def test_a_retired_family_refuses_whatever_its_value_looks_like(value):
    # A malformed legacy value must refuse for the same reason a well-formed one does: the NAME is
    # retired. Parsing it would imply the old form still means something.
    r = _boot(MODEL_PRICE_DEEPSEEK_X=value)
    assert r.returncode != 0, f"a legacy price variable with value {value!r} was accepted"


@pytest.mark.parametrize("name,value", [
    ("MODEL_PRICE_OVERRIDES", "deepinfra:zai-org/GLM-5.2=1,2,3"),
    ("MODEL_DOMAIN_BUDGETS", "deepinfra:default:zai-org/GLM-5.2=8"),
])
def test_the_current_replacement_settings_are_not_mistaken_for_retired_ones(name, value):
    r = _boot(**{name: value})
    assert r.returncode == 0 and "STARTED" in r.stdout, (
        f"{name} is the live replacement but startup refused it: {r.stderr[-400:]}")


def test_no_live_catalogue_entry_can_match_a_retired_family():
    live = {v.name for v in cat.all_vars()}
    for fam in cat.REMOVED_FAMILIES:
        clash = sorted(n for n in live if fam.matches(n))
        assert clash == [], f"{fam.label} would refuse live settings: {clash}"


def test_live_and_retired_remain_disjoint():
    live = {v.name for v in cat.all_vars()}
    assert live & set(cat.REMOVED) == set()


def test_planner_timeout_seconds_is_retired_with_an_explanation():
    assert "PLANNER_TIMEOUT_SECONDS" in cat.REMOVED
    reason = cat.REMOVED["PLANNER_TIMEOUT_SECONDS"]
    assert "PLANNER_TIMEOUT" in reason, "the retired name does not point at what replaced it"


# the certification gate stays wired

def test_gate_a_still_runs_the_full_configuration_certification():
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    i = makefile.index("check-fast:")
    recipe = makefile[i:makefile.index("\ncheck:", i)]
    assert "config_inventory.py --full" in recipe, (
        "make check-fast no longer runs the blocking configuration certification")


def test_the_full_certification_is_green():
    r = subprocess.run([sys.executable, str(REPO / "devtools/quality/config_inventory.py"), "--full"],
                       cwd=str(REPO), capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
