# Responsibility: Verify Alembic resolves its own config and script location, never a stray one in the cwd.
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.runtime import migrate

REPO = Path(__file__).resolve().parents[3]


def test_a_stray_alembic_ini_in_the_cwd_is_never_selected(tmp_path, monkeypatch):
    # a COMPLETE but untrusted asset pair - exactly what a cwd-first resolver would happily accept
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    fake_scripts = tmp_path / "alembic"
    (fake_scripts / "versions").mkdir(parents=True)
    (fake_scripts / "env.py").write_text("raise SystemExit('hostile env.py must never run')\n")
    monkeypatch.delenv("ALEMBIC_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    ini, scripts = migrate._resolve_alembic_assets()

    assert Path(ini).resolve() != (tmp_path / "alembic.ini").resolve(), "the cwd config was selected"
    assert Path(scripts).resolve() != fake_scripts.resolve(), "the cwd script dir was selected"
    # whatever was chosen is a complete, trusted asset pair
    assert Path(ini).is_file() and Path(scripts).is_dir()
    assert (Path(scripts) / "env.py").is_file()


def test_the_repository_root_is_the_fallback_and_is_complete(tmp_path, monkeypatch):
    monkeypatch.delenv("ALEMBIC_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    ini, scripts = migrate._resolve_alembic_assets()
    # on a host checkout (/srv absent) this is the repo; in the image it is /srv - both are trusted
    assert Path(ini).name == "alembic.ini"
    assert Path(scripts).is_absolute() and (Path(scripts) / "versions").is_dir()


def test_explicit_alembic_config_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEMBIC_CONFIG", str(REPO / "alembic.ini"))
    monkeypatch.chdir(tmp_path)
    ini, scripts = migrate._resolve_alembic_assets()
    assert Path(ini) == REPO / "alembic.ini"
    assert Path(scripts) == REPO / "alembic"


def test_explicit_alembic_config_must_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("ALEMBIC_CONFIG", str(tmp_path / "missing.ini"))
    with pytest.raises(SystemExit, match="is not a file"):
        migrate._resolve_alembic_assets()


def test_explicit_config_without_a_sibling_alembic_uses_its_declared_script_location(tmp_path, monkeypatch):
    custom = tmp_path / "custom"
    (custom / "scripts").mkdir(parents=True)
    ini = custom / "alembic.ini"
    ini.write_text("[alembic]\nscript_location = scripts\n")
    monkeypatch.setenv("ALEMBIC_CONFIG", str(ini))
    _, scripts = migrate._resolve_alembic_assets()
    assert Path(scripts) == custom / "scripts"


def test_alembic_config_sets_an_absolute_usable_script_location(tmp_path, monkeypatch):
    monkeypatch.delenv("ALEMBIC_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = migrate._alembic_config()
    loc = Path(cfg.get_main_option("script_location"))
    assert loc.is_absolute(), "script_location must not be cwd-relative"
    assert (loc / "env.py").is_file() and (loc / "versions").is_dir()
