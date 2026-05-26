"""Workspace PROFILE.md management + prompt injection."""
from __future__ import annotations

import re
from pathlib import Path

PROFILE_FILENAME = "PROFILE.md"
PROFILE_HEADER = "# User Profile"

MANAGED_SECTIONS: tuple[tuple[str, str], ...] = (
    ("style", "Style"),
    ("tooling", "Tooling"),
    ("workflow", "Workflow"),
    ("vetoes", "Vetoes"),
    ("current-priorities", "Current priorities"),
)


def profile_path(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / PROFILE_FILENAME


def load_profile_text(workspace: str | Path) -> str:
    path = profile_path(workspace)
    if not path.exists():
        return ""
    return path.read_text().strip()


def build_profile_prompt(workspace: str | Path) -> str:
    text = load_profile_text(workspace)
    if not text:
        return ""
    return f"### PROFILE.md\n{text}"


def default_profile_sections() -> dict[str, list[str]]:
    return {
        "style": [
            "Prefer concise, direct answers.",
            "Keep plans explicit and implementation-oriented.",
        ],
        "tooling": [
            "Prefer fast local iteration and simple testable workflows.",
            "Favor reusable skills over one-off prompt habits.",
        ],
        "workflow": [
            "Do a quick preflight before substantial edits.",
            "Include verification steps for anything reusable.",
        ],
        "vetoes": [
            "Do not hide important tradeoffs.",
            "Do not default to vague automation without review points.",
        ],
        "current-priorities": [
            "Build reusable local systems that reduce repeated guidance.",
        ],
    }


def render_profile(
    existing_text: str = "",
    *,
    sections: dict[str, list[str]] | None = None,
) -> str:
    sections = sections or default_profile_sections()
    text = existing_text.strip()
    if not text:
        text = PROFILE_HEADER
    elif PROFILE_HEADER not in text:
        text = f"{PROFILE_HEADER}\n\n{text}"

    for key, title in MANAGED_SECTIONS:
        block = _managed_block(key, title, sections.get(key, []))
        text = _upsert_managed_block(text, key, block)
    return text.rstrip() + "\n"


def init_profile(workspace: str | Path, *, force: bool = False) -> Path:
    path = profile_path(workspace)
    existing = path.read_text() if path.exists() and not force else ""
    rendered = render_profile(existing)
    path.write_text(rendered)
    return path


def _managed_block(key: str, title: str, items: list[str]) -> str:
    bullet_lines = "\n".join(f"- {item}" for item in items) if items else "-"
    return (
        f"<!-- sera:{key}:start -->\n"
        f"## {title}\n\n"
        f"{bullet_lines}\n"
        f"<!-- sera:{key}:end -->"
    )


def _upsert_managed_block(text: str, key: str, block: str) -> str:
    pattern = re.compile(
        rf"<!-- sera:{re.escape(key)}:start -->.*?<!-- sera:{re.escape(key)}:end -->",
        re.DOTALL,
    )
    if pattern.search(text):
        return pattern.sub(block, text)
    if not text.endswith("\n"):
        text += "\n"
    return f"{text}\n{block}\n"
