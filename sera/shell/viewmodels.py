"""View-models for the desktop panels (P-65 / P-66 / P-67).

The React panels are presentation only; everything they render is produced
here as plain JSON-serializable dicts. Keeping the logic in Python means the
panels' real behavior is unit-testable without a browser — the .tsx files just
fetch and lay out these structures.

  - entity_card  (P-65) — Memory Tree browser: an entity + its typed relations,
    each carrying a provenance breadcrumb (the chunk that asserted it).
  - accounts_overview (P-66) — connected integrations and their tool counts.
  - skills_overview / settings_overview (P-67) — skill manager + config view.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sera.memory.tree import MemoryTree


# ---------------------------------------------------------------------------
# P-65 — Memory Tree browser
# ---------------------------------------------------------------------------

def entity_card(tree: MemoryTree, name: str, *, consent: bool = False) -> dict[str, Any] | None:
    """Build the entity card for `name`: identity + relations + provenance.

    OUTCLASS (provenance breadcrumbs): every relation links back to the chunk
    that asserted it, so the user can see *why* Sera believes "Alice works_at
    OpenAI" — not just that it does. Returns None if the entity is unknown.
    """
    entity = tree.find_entity(name)
    if entity is None:
        return None

    relations: list[dict[str, Any]] = []
    for rel in tree.relations_for(name):
        dst = tree.get_entity(rel.dst_entity_id)
        provenance: dict[str, Any] | None = None
        if rel.provenance_chunk_id is not None:
            chunk = tree.get_chunk(rel.provenance_chunk_id, consent=consent)
            if chunk is not None:
                provenance = {
                    "chunk_id": chunk.id,
                    "source": chunk.source,
                    "summary": chunk.summary or chunk.content[:200],
                    "confidence": chunk.confidence,
                }
        relations.append({
            "kind": rel.kind,
            "dst": dst.name if dst else f"#{rel.dst_entity_id}",
            "dst_type": dst.type if dst else None,
            "confidence": rel.confidence,
            "provenance": provenance,
        })

    return {
        "entity": {
            "id": entity.id,
            "name": entity.name,
            "type": entity.type,
            "first_seen": entity.first_seen,
            "last_seen": entity.last_seen,
        },
        "relations": relations,
    }


# ---------------------------------------------------------------------------
# P-66 — Accounts panel
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountView:
    app: str
    connected: bool
    tool_count: int


def accounts_overview(discovery: Any) -> dict[str, Any]:
    """Summarize connected Composio integrations.

    `discovery` is a ComposioDiscovery (or any object exposing
    `registered_tools() -> list[str]`). Tool names are `composio__<app>__<action>`;
    we group by app so the panel shows one row per connected service.
    """
    try:
        tools = list(discovery.registered_tools())
    except Exception:  # noqa: BLE001 — a disconnected/erroring client shows empty
        tools = []

    by_app: dict[str, int] = {}
    for name in tools:
        # composio__github__list_events → app = "github"
        parts = name.split("__")
        app = parts[1] if len(parts) >= 3 and parts[0] == "composio" else parts[0]
        by_app[app] = by_app.get(app, 0) + 1

    accounts = [
        {"app": app, "connected": True, "tool_count": n}
        for app, n in sorted(by_app.items())
    ]
    return {"accounts": accounts, "total_tools": len(tools)}


# ---------------------------------------------------------------------------
# P-67 — Settings + skill manager
# ---------------------------------------------------------------------------

def skills_overview(
    root: Path,
    *,
    lifecycle: Any | None = None,
    scorer: Any | None = None,
) -> dict[str, Any]:
    """List skills with lifecycle state + quality score for the manager UI.

    lifecycle: SkillLifecycle (state_of) — optional.
    scorer:    SkillScorer (all_scores) — optional.
    Both optional so the panel renders even before either store exists.
    """
    from sera.skills.loader import discover_skills

    skills = discover_skills(root) if root.is_dir() else []

    score_map: dict[str, float] = {}
    if scorer is not None:
        try:
            score_map = {name: score for name, score, _ in scorer.all_scores()}
        except Exception:  # noqa: BLE001
            score_map = {}

    rows: list[dict[str, Any]] = []
    for s in skills:
        state = "active"
        if lifecycle is not None:
            try:
                state = lifecycle.state_of(s.name).value
            except Exception:  # noqa: BLE001
                state = "active"
        rows.append({
            "name": s.name,
            "trigger": s.trigger,
            "permission": s.permission,
            "version": s.version,
            "council": s.council,
            "lineage": list(s.lineage),
            "state": state,
            "enabled": state in ("pinned", "active"),
            "score": score_map.get(s.name),
        })
    return {"skills": rows}


_REDACT_KEYS = ("key", "token", "secret", "password")


def _redact_config(cfg: Any) -> Any:
    """Recursively blank any value whose key looks secret — the settings panel
    must never receive raw credentials."""
    if isinstance(cfg, dict):
        out: dict[str, Any] = {}
        for k, v in cfg.items():
            if any(s in k.lower() for s in _REDACT_KEYS) and isinstance(v, str):
                out[k] = "••••••" if v else ""
            else:
                out[k] = _redact_config(v)
        return out
    if isinstance(cfg, list):
        return [_redact_config(x) for x in cfg]
    return cfg


def settings_overview(cfg: dict[str, Any]) -> dict[str, Any]:
    """Config for the settings panel, with secret-shaped values redacted."""
    return {"config": _redact_config(cfg)}
