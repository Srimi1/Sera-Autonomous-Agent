"""Tests for sera.shell.viewmodels — P-65/66/67 panel backends.

These are the testable cores the React panels render. The .tsx files are not
executed here (no Tauri/Vite); this proves the data each panel shows is correct.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.memory.tree import MemoryTree
from sera.shell.viewmodels import (
    accounts_overview,
    entity_card,
    settings_overview,
    skills_overview,
)


# ---------------------------------------------------------------------------
# P-65 — entity_card (Memory Tree browser)
# ---------------------------------------------------------------------------

class TestEntityCard:
    def _tree(self, tmp_path: Path) -> MemoryTree:
        t = MemoryTree(db_path=tmp_path / "mem.db")
        # Provenance chunk: where "Alice works_at OpenAI" came from.
        chunk_id = t.add_chunk(
            source="email:gmail/abc",
            content="Alice just joined OpenAI as a researcher.",
            summary="Alice -> OpenAI",
        )
        t.add_relation(src="Alice", dst="OpenAI", kind="works_at",
                       provenance_chunk_id=chunk_id, src_type="person", dst_type="org")
        return t

    def test_unknown_entity_returns_none(self, tmp_path: Path) -> None:
        t = MemoryTree(db_path=tmp_path / "mem.db")
        assert entity_card(t, "Nobody") is None

    def test_entity_card_has_identity(self, tmp_path: Path) -> None:
        card = entity_card(self._tree(tmp_path), "Alice")
        assert card is not None
        assert card["entity"]["name"] == "Alice"
        assert card["entity"]["type"] == "person"

    def test_relations_listed(self, tmp_path: Path) -> None:
        card = entity_card(self._tree(tmp_path), "Alice")
        assert card is not None
        assert len(card["relations"]) == 1
        rel = card["relations"][0]
        assert rel["kind"] == "works_at"
        assert rel["dst"] == "OpenAI"

    def test_provenance_breadcrumb_present(self, tmp_path: Path) -> None:
        """The outclass: every relation links back to its source chunk."""
        card = entity_card(self._tree(tmp_path), "Alice")
        assert card is not None
        prov = card["relations"][0]["provenance"]
        assert prov is not None
        assert prov["source"] == "email:gmail/abc"
        assert "OpenAI" in prov["summary"] or "Alice" in prov["summary"]

    def test_search_alice_yields_relations(self, tmp_path: Path) -> None:
        """Phase verification: search 'Alice' → entity card with relations."""
        card = entity_card(self._tree(tmp_path), "Alice")
        assert card is not None and card["relations"], "Alice must show her relations"


# ---------------------------------------------------------------------------
# P-66 — accounts_overview
# ---------------------------------------------------------------------------

class _FakeDiscovery:
    def __init__(self, tools: list[str]) -> None:
        self._tools = tools

    def registered_tools(self) -> list[str]:
        return self._tools


class TestAccountsOverview:
    def test_groups_by_app(self) -> None:
        d = _FakeDiscovery([
            "composio__github__list_events",
            "composio__github__create_issue",
            "composio__gmail__send",
        ])
        view = accounts_overview(d)
        apps = {a["app"]: a["tool_count"] for a in view["accounts"]}
        assert apps == {"github": 2, "gmail": 1}
        assert view["total_tools"] == 3

    def test_empty_when_nothing_connected(self) -> None:
        view = accounts_overview(_FakeDiscovery([]))
        assert view["accounts"] == []
        assert view["total_tools"] == 0

    def test_errors_degrade_to_empty(self) -> None:
        class Broken:
            def registered_tools(self):
                raise RuntimeError("composio client down")

        view = accounts_overview(Broken())
        assert view["accounts"] == []

    def test_non_composio_name_falls_back(self) -> None:
        view = accounts_overview(_FakeDiscovery(["weird_tool"]))
        assert view["accounts"][0]["app"] == "weird_tool"


# ---------------------------------------------------------------------------
# P-67 — skills_overview + settings_overview
# ---------------------------------------------------------------------------

_SKILL_MD = """---
name: weekly-digest
trigger: user asks for a weekly digest
permission: READ_ONLY
version: 1.0.0
---
Steps: do the thing.
"""


class TestSkillsOverview:
    def _skills_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "skills" / "weekly-digest"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_SKILL_MD)
        return tmp_path / "skills"

    def test_lists_skill(self, tmp_path: Path) -> None:
        view = skills_overview(self._skills_dir(tmp_path))
        assert len(view["skills"]) == 1
        s = view["skills"][0]
        assert s["name"] == "weekly-digest"
        assert s["enabled"] is True
        assert s["state"] == "active"

    def test_missing_dir_is_empty(self, tmp_path: Path) -> None:
        view = skills_overview(tmp_path / "nope")
        assert view["skills"] == []

    def test_score_attached_when_scorer_present(self, tmp_path: Path) -> None:
        class FakeScorer:
            def all_scores(self):
                return [("weekly-digest", 0.87, object())]

        view = skills_overview(self._skills_dir(tmp_path), scorer=FakeScorer())
        assert view["skills"][0]["score"] == 0.87


class TestSettingsOverview:
    def test_redacts_secret_shaped_values(self) -> None:
        cfg = {
            "llm": {"profiles": {"reasoning": {"provider": "anthropic"}}},
            "anthropic_api_key": "sk-ant-SECRET",
            "nested": {"auth_token": "xoxb-SECRET"},
        }
        view = settings_overview(cfg)
        c = view["config"]
        assert c["anthropic_api_key"] == "••••••"
        assert c["nested"]["auth_token"] == "••••••"
        # Non-secret values pass through untouched.
        assert c["llm"]["profiles"]["reasoning"]["provider"] == "anthropic"

    def test_empty_secret_stays_empty(self) -> None:
        view = settings_overview({"api_key": ""})
        assert view["config"]["api_key"] == ""
