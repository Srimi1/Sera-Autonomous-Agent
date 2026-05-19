"""Generate per-phase markdown files from STEP-BY-STEP.md.

Each `### P-NN — name` heading in STEP-BY-STEP.md becomes
phases/NN-<slug>.md with a Status/Outclass/Goal/Deliverables/Files/
Verification/Dependencies/Notes structure preserved from the source.

Idempotent: rerunning overwrites files (status field preserved if you
keep edits in the Notes section).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "STEP-BY-STEP.md"
OUT_DIR = ROOT / "phases"

PHASE_HEAD = re.compile(r"^### P-(\d{2,3}) — (.+)$")
SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    s = name.lower()
    s = SLUG.sub("-", s).strip("-")
    return s[:48]


def split_phases(text: str) -> list[tuple[str, str, str]]:
    """Return list of (id, name, body) for each phase block."""
    lines = text.splitlines()
    phases: list[tuple[str, str, list[str]]] = []
    current_id: str | None = None
    current_name: str | None = None
    current_body: list[str] = []

    for line in lines:
        m = PHASE_HEAD.match(line)
        if m:
            if current_id is not None:
                phases.append((current_id, current_name or "", list(current_body)))
            current_id = m.group(1)
            current_name = m.group(2).strip()
            current_body = []
            continue
        if current_id is None:
            continue
        # Stop a phase when we hit a section heading at depth <= 2 (## ...).
        if line.startswith("## "):
            phases.append((current_id, current_name or "", list(current_body)))
            current_id = None
            current_name = None
            current_body = []
            continue
        current_body.append(line)
    if current_id is not None:
        phases.append((current_id, current_name or "", list(current_body)))

    return [(pid, pname, "\n".join(body).strip()) for pid, pname, body in phases]


def write_phase(pid: str, name: str, body: str) -> Path:
    slug = slugify(name)
    fname = f"{pid}-{slug}.md"
    path = OUT_DIR / fname
    title = f"# P-{pid} — {name}"
    notes_section = "\n\n## Notes\n\n_Journal: decisions, blockers, commit refs go here._\n"
    # The body already contains Status / Outclass / Goal / Deliverables / Files / Verification / Dependencies
    # rendered as `- **Field:** value` bullets. Convert to ## headings for readability.
    converted = convert_bullets_to_sections(body)
    out = f"{title}\n\n{converted}{notes_section}"
    path.write_text(out, encoding="utf-8")
    return path


FIELD_PATTERN = re.compile(r"^- \*\*(?P<field>[^:*]+):\*\*\s*(?P<value>.*)$")


def convert_bullets_to_sections(body: str) -> str:
    """Turn `- **Field:** value` bullets into `## Field\n\nvalue` blocks."""
    lines = body.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = FIELD_PATTERN.match(line)
        if m:
            field = m.group("field").strip()
            value = m.group("value").strip()
            # Collect continuation lines until the next top-level bullet or blank-then-bullet.
            cont: list[str] = []
            j = i + 1
            while j < len(lines):
                if FIELD_PATTERN.match(lines[j]):
                    break
                cont.append(lines[j])
                j += 1
            i = j
            block = value
            if cont:
                block += "\n" + "\n".join(cont).rstrip()
            out.append(f"## {field}\n\n{block.strip()}\n")
            continue
        out.append(line)
        i += 1
    return "\n".join(out).strip() + "\n"


def main() -> int:
    if not SOURCE.exists():
        print(f"Missing source: {SOURCE}", file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = SOURCE.read_text(encoding="utf-8")
    phases = split_phases(text)
    if len(phases) != 100:
        print(f"WARNING: parsed {len(phases)} phases (expected 100)", file=sys.stderr)
    written: list[Path] = []
    for pid, name, body in phases:
        written.append(write_phase(pid, name, body))
    print(f"Wrote {len(written)} phase files to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
