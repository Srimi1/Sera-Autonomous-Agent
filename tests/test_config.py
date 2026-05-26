"""Backfill coverage for sera.config — DEFAULT_CONFIG shape + save/load round-trip."""
from __future__ import annotations

from pathlib import Path

import pytest

import sera.config as config


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / ".sera"
    monkeypatch.setattr(config, "SERA_HOME", home)
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.yaml")
    monkeypatch.setattr(config, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(config, "VAULT_DIR", home / "vault")
    return home


class TestEnsureHome:
    def test_creates_dirs(self, isolated_home: Path) -> None:
        config.ensure_home()
        assert isolated_home.is_dir()
        assert (isolated_home / "skills").is_dir()
        assert (isolated_home / "vault").is_dir()


class TestLoadSave:
    def test_load_writes_default_when_absent(self, isolated_home: Path) -> None:
        cfg = config.load()
        assert cfg == config.DEFAULT_CONFIG
        assert config.CONFIG_PATH.exists()

    def test_round_trip_preserves_values(self, isolated_home: Path) -> None:
        custom = {"identity": {"name": "Sera", "timezone": "America/New_York"}, "extra": [1, 2, 3]}
        config.save(custom)
        assert config.load() == custom

    def test_default_config_structure(self) -> None:
        cfg = config.DEFAULT_CONFIG
        assert cfg["llm"]["default_profile"] == "reasoning"
        assert "reasoning" in cfg["llm"]["profiles"]
        assert "fast" in cfg["llm"]["profiles"]
        assert cfg["safety"]["approval_required_at_or_above"] == "DANGEROUS"
        assert cfg["safety"]["max_iterations"] == 25
        assert cfg["budget"]["session_hard_usd"] >= cfg["budget"]["session_soft_usd"]
        assert cfg["budget"]["day_hard_usd"] >= cfg["budget"]["day_soft_usd"]

    def test_default_config_yaml_safe(self, isolated_home: Path) -> None:
        # DEFAULT_CONFIG must survive a yaml.safe_dump → safe_load cycle.
        config.save(config.DEFAULT_CONFIG)
        assert config.load() == config.DEFAULT_CONFIG

    def test_empty_file_falls_back_to_default(self, isolated_home: Path) -> None:
        config.ensure_home()
        config.CONFIG_PATH.write_text("")  # empty YAML → safe_load returns None
        assert config.load() == config.DEFAULT_CONFIG


class TestWorkspaceSkillsDir:
    def test_appends_skills_dirname(self, tmp_path: Path) -> None:
        result = config.workspace_skills_dir(tmp_path)
        assert result == tmp_path.resolve() / config.WORKSPACE_SKILLS_DIRNAME

    def test_accepts_string(self, tmp_path: Path) -> None:
        result = config.workspace_skills_dir(str(tmp_path))
        assert result.name == "skills"
