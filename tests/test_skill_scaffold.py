from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from sera.cli.main import main
from sera.skills.loader import load_skill, skill_to_tool
from sera.skills.scaffold import scaffold_skill


def test_scaffold_skill_writes_skill_and_replay(tmp_path: Path):
    result = scaffold_skill(
        tmp_path / "skills",
        name="Animated GIF Workflow",
        description="Validate GIF assets before use.",
    )
    assert result.skill_path.exists()
    assert result.replay_path.exists()
    assert "description: Validate GIF assets before use." in result.skill_path.read_text()
    assert "skill: animated-gif-workflow" in result.replay_path.read_text()


def test_skill_to_tool_prefers_frontmatter_description(tmp_path: Path):
    result = scaffold_skill(
        tmp_path / "skills",
        name="Screenshot Asset Workflow",
        description="Check replacement screenshots before shipping.",
    )
    tool = skill_to_tool(load_skill(result.skill_path))
    assert tool.description == "Check replacement screenshots before shipping."


def test_cli_scaffold_creates_structured_skill(tmp_path: Path):
    runner = CliRunner()
    skills_root = tmp_path / "skills"
    result = runner.invoke(
        main,
        [
            "skills",
            "--root",
            str(skills_root),
            "scaffold",
            "png-checklist",
            "--description",
            "Validate PNG assets.",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (skills_root / "png-checklist" / "SKILL.md").exists()
    assert (skills_root / "png-checklist" / "replay.yaml").exists()
