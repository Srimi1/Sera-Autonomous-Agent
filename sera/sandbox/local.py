"""Local subprocess sandbox — free tier.

Safety properties:
  - Timeout: asyncio.wait_for kills the process at timeout_s (default 10s).
  - Network: AST scan blocks known network-capable imports unless allow_network=True.
  - Environment: stripped to a minimal allowlist — no API keys, no secrets.
  - Execution: temp file, not shell=True — no shell injection path.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from sera.sandbox.base import SandboxResult, SandboxTier

# Modules that can make outbound connections.
_NETWORK_MODULES: frozenset[str] = frozenset({
    "requests", "urllib", "urllib2", "urllib3", "httpx", "aiohttp",
    "http", "https", "ftplib", "smtplib", "imaplib", "poplib",
    "socket", "ssl", "paramiko", "boto3", "botocore",
    "google", "azure", "openai", "anthropic", "twilio",
})

# Minimum safe environment — strips secrets, tokens, keys.
_SAFE_ENV_KEYS: frozenset[str] = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
    "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX",
})


def _scan_network_imports(code: str) -> list[str]:
    """Return root module names that can reach the network, or [] if none found."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _NETWORK_MODULES:
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            root = mod.split(".")[0]
            if root in _NETWORK_MODULES:
                found.append(mod)
    return found


def _build_env() -> dict[str, str]:
    """Build a stripped environment — no secrets, keys, or tokens."""
    env: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


class LocalSubprocessSandbox:
    """Run Python code in an isolated subprocess with timeout + network guard."""

    tier = SandboxTier.LOCAL

    async def run(
        self,
        code: str,
        *,
        timeout: float = 10.0,
        allow_network: bool = False,
    ) -> SandboxResult:
        # Network gate: refuse before spawning anything.
        if not allow_network:
            bad = _scan_network_imports(code)
            if bad:
                return SandboxResult(
                    stdout="",
                    stderr=(
                        f"Network access refused: {', '.join(bad)} "
                        f"require allow_network=True grant."
                    ),
                    exit_code=1,
                    timed_out=False,
                    tier=self.tier,
                )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            script = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_build_env(),
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                return SandboxResult(
                    stdout=stdout_b.decode("utf-8", errors="replace"),
                    stderr=stderr_b.decode("utf-8", errors="replace"),
                    exit_code=proc.returncode or 0,
                    timed_out=False,
                    tier=self.tier,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return SandboxResult(
                    stdout="",
                    stderr=f"[sandbox: process killed after {timeout:.0f}s timeout]",
                    exit_code=-1,
                    timed_out=True,
                    tier=self.tier,
                )
        finally:
            try:
                Path(script).unlink(missing_ok=True)
            except Exception:
                pass
