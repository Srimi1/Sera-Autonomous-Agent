"""Tests for sera.tools.genesis — runtime tool authoring pipeline."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sera.tools.genesis import (
    GenesisResult,
    ToolSpec,
    ast_safety_scan,
    delete_auto_tool,
    genesis,
    list_auto_tools,
    render_file,
    validate_name,
    validate_permission,
)
from sera.tools.registry import all_tools, reset as reset_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _echo_spec(name: str = "echo_tool") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Echo the input message.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler_body="return f\"echo: {args.get('text', '')}\"",
        permission="READ_ONLY",
        capabilities=["tools.register"],
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_name(self) -> None:
        assert validate_name("hn_top_stories") is None

    def test_uppercase_rejected(self) -> None:
        assert validate_name("HNTopStories") is not None

    def test_dash_rejected(self) -> None:
        assert validate_name("hn-top") is not None

    def test_traversal_rejected(self) -> None:
        assert validate_name("../etc/passwd") is not None

    def test_dotpath_rejected(self) -> None:
        assert validate_name("a.b") is not None

    def test_permission_valid(self) -> None:
        assert validate_permission("READ_ONLY") is None
        assert validate_permission("DANGEROUS") is None

    def test_permission_invalid(self) -> None:
        assert validate_permission("GOD_MODE") is not None


# ---------------------------------------------------------------------------
# AST safety scan
# ---------------------------------------------------------------------------

class TestASTSafety:
    def test_clean_code(self) -> None:
        assert ast_safety_scan("return args.get('x', '')") == []

    def test_eval_rejected(self) -> None:
        issues = ast_safety_scan("return eval(args['code'])")
        assert any("eval" in i for i in issues)

    def test_exec_rejected(self) -> None:
        issues = ast_safety_scan("exec(args['code'])")
        assert any("exec" in i for i in issues)

    def test_dunder_import_rejected(self) -> None:
        issues = ast_safety_scan("__import__('os').system('rm -rf /')")
        assert any("__import__" in i for i in issues)

    def test_compile_rejected(self) -> None:
        issues = ast_safety_scan("compile('x', '<>', 'exec')")
        assert any("compile" in i for i in issues)

    def test_shell_true_rejected(self) -> None:
        issues = ast_safety_scan(
            "import subprocess\nsubprocess.run(['ls'], shell=True)"
        )
        assert any("shell=True" in i for i in issues)

    def test_subprocess_without_shell_ok(self) -> None:
        # shell-less subprocess is fine
        issues = ast_safety_scan("import subprocess\nsubprocess.run(['ls'])")
        assert issues == []

    def test_syntax_error(self) -> None:
        issues = ast_safety_scan("def broken(")
        assert any("syntax" in i for i in issues)


# ---------------------------------------------------------------------------
# render_file
# ---------------------------------------------------------------------------

class TestRender:
    def test_renders_valid_python(self) -> None:
        import ast as _ast
        rendered = render_file(_echo_spec())
        _ast.parse(rendered)  # raises if invalid

    def test_includes_name_description(self) -> None:
        rendered = render_file(_echo_spec("my_tool"))
        assert "my_tool" in rendered
        assert "Echo the input message." in rendered

    def test_includes_permission(self) -> None:
        rendered = render_file(_echo_spec())
        assert "Permission.READ_ONLY" in rendered

    def test_includes_register_call(self) -> None:
        rendered = render_file(_echo_spec())
        assert "register(Tool(" in rendered

    def test_imports_inserted(self) -> None:
        spec = _echo_spec()
        spec.imports = ["import json", "import urllib.request"]
        rendered = render_file(spec)
        assert "import json" in rendered
        assert "import urllib.request" in rendered

    def test_empty_body_safe(self) -> None:
        spec = _echo_spec()
        spec.handler_body = ""
        rendered = render_file(spec)
        import ast as _ast
        _ast.parse(rendered)


# ---------------------------------------------------------------------------
# Full pipeline — P-48 verification
# ---------------------------------------------------------------------------

class TestGenesisPipeline:
    def setup_method(self) -> None:
        reset_registry()

    def teardown_method(self) -> None:
        reset_registry()

    def test_echo_tool_end_to_end(self, tmp_path: Path) -> None:
        spec = _echo_spec("echo_t")
        result: GenesisResult = _run(genesis(spec, auto_dir=tmp_path, skip_mypy=True))
        assert result.ok, f"errors: {result.errors}"
        assert result.file_path is not None
        assert result.file_path.exists()
        # Tool registered in this process — appears in sera tools
        names = {t.name for t in all_tools()}
        assert "echo_t" in names

    def test_hn_top_stories_tool(self, tmp_path: Path) -> None:
        """Verification: 'make me a Hacker News top-stories tool' → working tool."""
        spec = ToolSpec(
            name="hn_top_stories",
            description="Fetch top N Hacker News story titles.",
            parameters={
                "type": "object",
                "properties": {"n": {"type": "integer", "default": 10}},
                "required": [],
            },
            # Body uses a stub return so the test doesn't hit the network;
            # the real agent would write urllib.request.urlopen() here with the
            # net.fetch capability declared.
            handler_body=(
                "n = args.get('n', 10)\n"
                "stories = [f'HN Story #{i}: Title {i}' for i in range(1, n+1)]\n"
                "return '\\n'.join(stories)"
            ),
            permission="READ_ONLY",
            capabilities=["net.fetch"],
        )
        result = _run(genesis(spec, auto_dir=tmp_path, skip_mypy=True))
        assert result.ok, f"errors: {result.errors}"
        # File landed in ~/.sera/tools/auto/
        assert (tmp_path / "hn_top_stories.py").exists()
        # Tool listed in sera tools
        names = {t.name for t in all_tools()}
        assert "hn_top_stories" in names

    def test_handler_runs_after_genesis(self, tmp_path: Path) -> None:
        from sera.tools.base import ToolContext
        spec = _echo_spec("echo_runtime")
        _run(genesis(spec, auto_dir=tmp_path, skip_mypy=True))
        tool = next(t for t in all_tools() if t.name == "echo_runtime")
        ctx = ToolContext(session_id="t", workspace="/tmp")
        result = _run(tool.handler({"text": "hello world"}, ctx))
        assert "hello world" in result

    def test_bad_name_rejected(self, tmp_path: Path) -> None:
        spec = _echo_spec("Bad Name")
        result = _run(genesis(spec, auto_dir=tmp_path, skip_mypy=True))
        assert not result.ok
        assert any("name" in e.lower() for e in result.errors)
        # No file written
        assert result.file_path is None

    def test_eval_in_body_rejected(self, tmp_path: Path) -> None:
        spec = _echo_spec("evil_tool")
        spec.handler_body = "return eval(args['code'])"
        result = _run(genesis(spec, auto_dir=tmp_path, skip_mypy=True))
        assert not result.ok
        assert any("eval" in e for e in result.errors)

    def test_dry_run_failure_rolls_back(self, tmp_path: Path) -> None:
        """If the rendered file fails to import, the file is removed."""
        spec = _echo_spec("broken_imports")
        # Force a broken import
        spec.imports = ["import nonexistent_module_xyz123abc"]
        result = _run(genesis(spec, auto_dir=tmp_path, skip_mypy=True))
        assert not result.ok
        # File should not persist after dry-run failure
        assert not (tmp_path / "broken_imports.py").exists()

    def test_skip_dry_run_flag(self, tmp_path: Path) -> None:
        """skip_dry_run lets a broken-import file persist (testing hatch)."""
        spec = _echo_spec("skipped_dryrun")
        result = _run(genesis(
            spec, auto_dir=tmp_path, skip_mypy=True, skip_dry_run=True,
        ))
        # File written; pipeline continues to live-import which loads it cleanly
        assert (tmp_path / "skipped_dryrun.py").exists()

    def test_list_auto_tools(self, tmp_path: Path) -> None:
        _run(genesis(_echo_spec("tool_a"), auto_dir=tmp_path, skip_mypy=True))
        _run(genesis(_echo_spec("tool_b"), auto_dir=tmp_path, skip_mypy=True))
        files = list_auto_tools(tmp_path)
        names = {f.stem for f in files}
        assert "tool_a" in names
        assert "tool_b" in names

    def test_delete_auto_tool(self, tmp_path: Path) -> None:
        _run(genesis(_echo_spec("ephemeral"), auto_dir=tmp_path, skip_mypy=True))
        assert (tmp_path / "ephemeral.py").exists()
        removed = delete_auto_tool("ephemeral", auto_dir=tmp_path)
        assert removed is True
        assert not (tmp_path / "ephemeral.py").exists()

    def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        assert delete_auto_tool("never_existed", auto_dir=tmp_path) is False


# ---------------------------------------------------------------------------
# GenesisResult
# ---------------------------------------------------------------------------

class TestGenesisResult:
    def test_reason_ok(self) -> None:
        r = GenesisResult(ok=True, tool_name="x")
        assert r.reason() == "ok"

    def test_reason_errors(self) -> None:
        r = GenesisResult(ok=False, tool_name="x", errors=["bad name", "eval used"])
        assert "bad name" in r.reason()
        assert "eval used" in r.reason()
