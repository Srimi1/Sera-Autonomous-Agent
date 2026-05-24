"""Tool-gen at runtime — agent authors a new tool safely.

Pipeline (P-48 the big one):
  spec → AST safety scan → mypy --strict → sandbox dry-run → write file → register

The agent describes a tool (name, description, parameters, handler body, capabilities).
genesis() runs the pipeline; on success the tool file lands in ~/.sera/tools/auto/<name>.py
and the tool registers in the live process — appears in `sera tools` after one turn.

Safety:
  - AST scan blocks __import__, eval, exec, compile, globals/locals access
  - mypy --strict enforces type discipline (skipped with warning if mypy missing)
  - Sandbox dry-run imports the file in a subprocess and confirms register() runs cleanly
  - File path strict: only auto_dir/, only [a-z_][a-z0-9_]*.py — no traversal

Heritage: this is the big one — agents grow their own toolboxes without
review. Nobody ships this safely; we do because each step gates the next.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sera.config import SERA_HOME
from sera.tools.base import Permission
from sera.tools.registry import all_tools

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_AUTO_DIR = SERA_HOME / "tools" / "auto"
DEFAULT_QUARANTINE_DIR = SERA_HOME / "tools" / "quarantine"

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{1,63}$")

# AST node names that signal dangerous patterns in handler bodies.
# Covers: direct code execution (eval/exec/compile), introspection escape hatches
# (globals/locals/vars/__builtins__/__globals__/__import__), and shell-execution
# sinks that would let a tool run `rm -rf /` without going through subprocess at all.
_DANGEROUS_NAMES: frozenset[str] = frozenset({
    "eval", "exec", "compile",
    "__import__", "__builtins__", "__globals__",
    "globals", "locals", "vars",
    # os shell-exec sinks
    "system", "popen", "execv", "execve", "execvp", "execvpe",
    "execl", "execle", "execlp", "execlpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "posix_spawn", "posix_spawnp",
    # pty / process injection
    "spawn", "fork", "forkpty",
    # subprocess shell-string sinks
    "getoutput", "getstatusoutput",
})

# Subprocess call function names — checked for shell=True kwarg regardless
# of attribute form (subprocess.Popen) or bare name (Popen after `from subprocess import Popen`).
_SUBPROCESS_FUNCS: frozenset[str] = frozenset({
    "Popen", "call", "run", "check_call", "check_output",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    """Agent-authored proposal for a new tool."""
    name: str
    description: str
    parameters: dict[str, Any]
    handler_body: str                 # Python source — function body (will be indented)
    permission: str = "READ_ONLY"     # READ_ONLY / WRITE / EXECUTE / DANGEROUS
    capabilities: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


@dataclass
class GenesisResult:
    ok: bool
    tool_name: str
    file_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mypy_output: str = ""
    dry_run_output: str = ""

    def reason(self) -> str:
        return "; ".join(self.errors) if self.errors else "ok"


# ---------------------------------------------------------------------------
# Step 1: name + spec validation
# ---------------------------------------------------------------------------

def validate_name(name: str) -> str | None:
    """Return error string or None."""
    if not _NAME_RE.match(name):
        return f"invalid tool name {name!r}: must match {_NAME_RE.pattern}"
    return None


def validate_permission(p: str) -> str | None:
    try:
        Permission.parse(p)
        return None
    except ValueError as e:
        return str(e)


# ---------------------------------------------------------------------------
# Step 2: AST safety scan
# ---------------------------------------------------------------------------

def ast_safety_scan(source: str) -> list[str]:
    """Walk the AST and flag dangerous name references. Returns list of issues."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"syntax error: {e.msg} at line {e.lineno}"]

    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _DANGEROUS_NAMES:
            issues.append(f"dangerous name {node.id!r} used at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in _DANGEROUS_NAMES:
            issues.append(f"dangerous attribute {node.attr!r} at line {node.lineno}")
        elif isinstance(node, ast.Call):
            # Catch shell=True on both attribute calls (subprocess.Popen) AND
            # bare-name calls (Popen after `from subprocess import Popen`).
            func_name: str | None = None
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id
            if func_name in _SUBPROCESS_FUNCS:
                for kw in node.keywords:
                    if (
                        kw.arg == "shell"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                    ):
                        issues.append(
                            f"subprocess with shell=True at line {node.lineno}"
                        )
    return issues


# ---------------------------------------------------------------------------
# Step 3: template render
# ---------------------------------------------------------------------------

_TEMPLATE = '''"""Auto-generated tool: {name}
Description: {description}
Created: {created}
Capabilities: {capabilities}
"""
from __future__ import annotations

from typing import Any
{imports}

from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register


async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
{handler_body}


register(Tool(
    name={name!r},
    description={description!r},
    parameters={parameters!r},
    permission=Permission.{permission},
    scope=ToolScope.INTEGRATION,
    handler=_handler,
))
'''


def _indent_body(body: str, spaces: int = 4) -> str:
    """Indent each line of handler_body by `spaces` so it sits inside _handler."""
    pad = " " * spaces
    lines = body.strip("\n").splitlines()
    if not lines:
        return f"{pad}return ''"
    return "\n".join(pad + line if line.strip() else "" for line in lines)


def render_file(spec: ToolSpec) -> str:
    imports = "\n".join(spec.imports) if spec.imports else ""
    return _TEMPLATE.format(
        name=spec.name,
        description=spec.description,
        created=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        capabilities=", ".join(spec.capabilities) or "(none)",
        imports=imports,
        handler_body=_indent_body(spec.handler_body),
        parameters=spec.parameters,
        permission=spec.permission,
    )


# ---------------------------------------------------------------------------
# Step 4: mypy --strict (optional)
# ---------------------------------------------------------------------------

async def mypy_check(file_path: Path, *, timeout: float = 30.0) -> tuple[bool, str]:
    """Run mypy --strict against the file. Returns (ok, output).

    If mypy isn't installed, returns (True, "mypy not installed — skipped") so
    the pipeline degrades gracefully rather than failing closed on dev machines
    without mypy.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mypy", "--strict", "--no-color-output", str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return True, "mypy not installed — skipped"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return False, "mypy timed out"

    output = (stdout_b + stderr_b).decode("utf-8", errors="replace")
    if "No module named mypy" in output:
        return True, "mypy not installed — skipped"
    return proc.returncode == 0, output


# ---------------------------------------------------------------------------
# Step 5: sandbox dry-run — import the file, confirm it parses + register works
# ---------------------------------------------------------------------------

_DRY_RUN_DRIVER = """\
import sys, importlib.util
sys.path.insert(0, {sera_root!r})  # generated tools import sera.tools.base
spec = importlib.util.spec_from_file_location("_genesis_dryrun", {path!r})
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
    print("DRY_RUN_OK")
except Exception as exc:
    print(f"DRY_RUN_FAIL: {{type(exc).__name__}}: {{exc}}")
    sys.exit(2)
"""

# Repo root that contains the `sera` package — baked into the dry-run driver so
# the sandbox subprocess can resolve `import sera.*` even with a stripped env.
_SERA_ROOT = str(Path(__file__).resolve().parents[2])


async def sandbox_dry_run(file_path: Path, *, timeout: float = 10.0) -> tuple[bool, str]:
    """Run the tool file in a clean Python subprocess to confirm it imports.

    Uses a driver script that imports the file via importlib — if register()
    runs without raising, we know the tool will register cleanly when the
    main process imports it.
    """
    from sera.sandbox.local import LocalSubprocessSandbox

    driver = _DRY_RUN_DRIVER.format(path=str(file_path), sera_root=_SERA_ROOT)
    sandbox = LocalSubprocessSandbox()
    # The driver imports `sera.tools.*` modules — allow it past the network gate
    # (none of the sera imports are network modules).
    result = await sandbox.run(driver, timeout=timeout, allow_network=False)
    ok = result.ok and "DRY_RUN_OK" in result.stdout
    output = result.as_tool_output()
    return ok, output


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

async def genesis(
    spec: ToolSpec,
    *,
    auto_dir: Path | None = None,
    skip_mypy: bool = False,
    skip_dry_run: bool = False,
) -> GenesisResult:
    """Run the full author-then-register pipeline. Returns GenesisResult."""
    result = GenesisResult(ok=False, tool_name=spec.name)

    # Step 1: validate name + permission
    if err := validate_name(spec.name):
        result.errors.append(err)
        return result
    if err := validate_permission(spec.permission):
        result.errors.append(err)
        return result

    # Step 2: AST safety scan of the body + imports
    safety_target = spec.handler_body + "\n" + "\n".join(spec.imports)
    issues = ast_safety_scan(safety_target)
    if issues:
        result.errors.extend(issues)
        return result

    # Step 3: render file
    rendered = render_file(spec)
    # Verify the rendered file itself parses (catches indentation/template bugs)
    try:
        ast.parse(rendered)
    except SyntaxError as e:
        result.errors.append(f"rendered file syntax error: {e.msg} at line {e.lineno}")
        return result

    dest_dir = (auto_dir or DEFAULT_AUTO_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_path = dest_dir / f"{spec.name}.py"
    # Strict containment: resolve and confirm the file lives under dest_dir
    resolved = file_path.resolve()
    if dest_dir.resolve() not in resolved.parents:
        result.errors.append(f"path traversal detected: {file_path}")
        return result

    file_path.write_text(rendered, encoding="utf-8")
    result.file_path = file_path

    # Step 4: mypy
    if not skip_mypy:
        ok, mypy_out = await mypy_check(file_path)
        result.mypy_output = mypy_out
        if not ok:
            result.errors.append(f"mypy failed: {mypy_out.strip()[:300]}")
            # Roll back the file write
            file_path.unlink(missing_ok=True)
            result.file_path = None
            return result

    # Step 5: sandbox dry-run
    if not skip_dry_run:
        ok, dr_out = await sandbox_dry_run(file_path)
        result.dry_run_output = dr_out
        if not ok:
            result.errors.append(f"dry-run failed: {dr_out.strip()[:300]}")
            file_path.unlink(missing_ok=True)
            result.file_path = None
            return result

    # Step 6: live import in this process → register() runs at module level
    try:
        _import_tool_file(file_path)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"live import failed: {exc}")
        file_path.unlink(missing_ok=True)
        result.file_path = None
        return result

    # Confirm the tool actually landed in the registry
    if not _is_registered(spec.name):
        result.warnings.append("import succeeded but tool not found in registry")

    result.ok = True
    return result


def _import_tool_file(file_path: Path) -> None:
    """Import a tool file by path. Module name is unique per file to avoid cache hits."""
    mod_name = f"sera_auto_{file_path.stem}_{int(time.time() * 1000)}"
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build module spec for {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)


def _is_registered(tool_name: str) -> bool:
    return any(t.name == tool_name for t in all_tools())


# ---------------------------------------------------------------------------
# Listing + deletion of auto-tools
# ---------------------------------------------------------------------------

def list_auto_tools(auto_dir: Path | None = None) -> list[Path]:
    """Return paths of auto-generated tool files."""
    d = auto_dir or DEFAULT_AUTO_DIR
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.py") if p.is_file())


def delete_auto_tool(name: str, auto_dir: Path | None = None) -> bool:
    """Delete an auto-generated tool file. Returns True if removed."""
    d = auto_dir or DEFAULT_AUTO_DIR
    p = d / f"{name}.py"
    if p.exists():
        p.unlink()
        return True
    return False
