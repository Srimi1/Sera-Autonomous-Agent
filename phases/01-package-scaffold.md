# P-01 — Package scaffold

## Status

done (shipped 2026-05-19)

## Outclass claim

none yet — table stakes.

## Goal

A Python package that installs cleanly, exposes a CLI entry point, and has the directory layout the rest of the work will hang on.

## Deliverables

- `pyproject.toml` with deps: openai, anthropic, pydantic, click, rich, prompt_toolkit, keyring, pyyaml, ddgs, httpx; dev: pytest, pytest-asyncio, ruff.
  - Package layout: `sera/{agent,llm/adapters,tools/impl,memory,safety,cli,context}/`.
  - `sera/__init__.py` exports `__version__ = "0.1.0"`.
  - `sera/config.py` writes `~/.sera/config.yaml` defaults; resolves `SERA_HOME`, paths for sessions DB, memory DB, skills dir, vault dir.
  - Entry point `sera = sera.cli.main:main` declared in `pyproject.toml`.

## Files touched

`pyproject.toml`, `sera/__init__.py`, `sera/config.py`, all `__init__.py` shims.

## Verification

```bash
  python3.11 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && sera version
  ```
  Expect: `sera 0.1.0`.

## Dependencies

none.


## Notes

_Journal: decisions, blockers, commit refs go here._
