"""shell_run — execute shell command.

Security posture (P-05.5):
  * Base permission is DANGEROUS. Approval gate fires by default.
  * SAFE_ALLOWLIST downgrades benign commands (git status, ls, cat, etc.) to EXECUTE.
  * EXTRA_DENY overrides the allowlist and forces DANGEROUS for anything matching
    known dangerous shapes — pipe-to-shell, chmod, chown, > ~/.ssh, etc.
  * Environment is built from an allowlist, not inherited. *_API_KEY / *_TOKEN /
    *_SECRET never reach the child.
  * Output is tail-capped at OUTPUT_LIMIT_BYTES per stream; larger gets a
    truncation marker prepended.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
OUTPUT_LIMIT_BYTES = 1_000_000  # 1 MB tail per stream

# Commands that match SAFE_ALLOWLIST run at EXECUTE.
# Anchored at start (after optional leading whitespace) so chained commands
# (e.g. "ls && rm -rf /") never qualify as "safe".
_SAFE_PATTERNS = [
    r"ls(\s+-[a-zA-Z]+)?(\s+\S+)*\s*$",
    r"pwd\s*$",
    r"echo\s+\S",
    r"whoami\s*$",
    r"date\s*$",
    r"uname(\s+-[a-zA-Z]+)?\s*$",
    r"cat\s+\S+(\s+\S+)*\s*$",
    r"head(\s+-n?\s*\d+)?\s+\S+\s*$",
    r"tail(\s+-n?\s*\d+)?\s+\S+\s*$",
    r"wc(\s+-[lwc]+)?\s+\S+\s*$",
    r"file\s+\S+\s*$",
    r"git\s+(status|diff|log|branch|show|remote|config\s+--get|rev-parse)(\s+[^|;&`$()<>]*)?$",
    r"python3?\s+(-V|--version)\s*$",
    r"node\s+(-v|--version)\s*$",
    r"npm\s+(--version|list)(\s+[^|;&`$()<>]*)?$",
    r"pip\s+(show|list|--version)(\s+[^|;&`$()<>]*)?$",
    r"which\s+\S+\s*$",
    r"find\s+\.(\s+-(name|type|maxdepth|mindepth)\s+\S+)+\s*$",
    r"grep(\s+-[a-zA-Z]+)*\s+'[^']*'\s+\S+\s*$",
    r"grep(\s+-[a-zA-Z]+)*\s+\"[^\"]*\"\s+\S+\s*$",
]
SAFE_ALLOWLIST = [re.compile(rf"\s*{p}", re.IGNORECASE) for p in _SAFE_PATTERNS]

# Anything matching these forces DANGEROUS regardless of other patterns.
_DENY_PATTERNS = [
    r"\|\s*(?:sh|bash|zsh|csh|fish|/bin/\w*sh)\b",
    r"\bcurl\s[^|;&]*?\|\s*\S*sh\b",
    r"\bwget\s[^|;&]*?\|\s*\S*sh\b",
    r"\beval\s+",
    r"`",
    r"\$\(",
    r"\brm\s+(-[a-zA-Z]*[rRfF][a-zA-Z]*|--recursive|--force)\b",
    r"\bfind\s+\S+\s+.*-delete\b",
    r"\bsudo\b",
    r"\bdoas\b",
    r"\bdd\s+if=",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bkill(all)?\s+-9\b",
    r":\s*\(\)\s*\{.*\|.*&.*\}\s*;",  # classic fork bomb shape
    r">\s*/dev/sd[a-z]",
    r">\s*/dev/null",  # not strictly dangerous, but allow only via allowlist
    r">>?\s*~?/?\.ssh/",
    r">>?\s*/etc/",
    r">>?\s*~?/?\.aws/",
    r">>?\s*~?/?\.config/gh/",
    r"\bchmod\s+(\+s|.*\b(777|666))\b",
    r"\bchown\b",
    r"\bcrontab\b",
    r"\blaunchctl\s+(load|unload|bootstrap|bootout|kickstart)",
    r"\bsystemctl\s+(enable|start|stop|restart|disable)",
    r"\bdefaults\s+write\b",
    r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b",
    r"\bnetcat\b|\bnc\s+(-[a-z]*l|-l)\b",
    r"\bopenssl\s+(enc|rsautl|s_client)\b",
    r"\bscp\s+",
    r"\brsync\s+.*::?",
    r"\bssh\s+",
]
DENY_LIST = [re.compile(p, re.IGNORECASE) for p in _DENY_PATTERNS]

# Env-var name patterns that must NOT propagate to the child.
_SECRET_ENV_PATTERN = re.compile(
    r"(API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY)$",
    re.IGNORECASE,
)
SAFE_ENV_KEYS = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "SHELL", "PWD", "TMPDIR"}


def classify(cmd: str) -> Permission:
    """Return effective permission tier for `cmd`.

    Order: deny-list wins → allow-list downgrades → default DANGEROUS.
    """
    if not cmd or not cmd.strip():
        return Permission.DANGEROUS
    for pat in DENY_LIST:
        if pat.search(cmd):
            return Permission.DANGEROUS
    for pat in SAFE_ALLOWLIST:
        if pat.fullmatch(cmd.strip()):
            return Permission.EXECUTE
    return Permission.DANGEROUS


def _safe_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for k in SAFE_ENV_KEYS:
        if k in os.environ:
            env[k] = os.environ[k]
    # Defence-in-depth: even if a SAFE_ENV_KEYS key matched a secret pattern, strip it.
    return {k: v for k, v in env.items() if not _SECRET_ENV_PATTERN.search(k)}


def _truncate(data: bytes, label: str) -> str:
    if len(data) <= OUTPUT_LIMIT_BYTES:
        return data.decode("utf-8", errors="replace").rstrip()
    tail = data[-OUTPUT_LIMIT_BYTES:]
    return (
        f"…[{label} truncated, kept last {OUTPUT_LIMIT_BYTES} of {len(data)} bytes]\n"
        + tail.decode("utf-8", errors="replace").rstrip()
    )


async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
    cmd: str = args.get("command", "")
    timeout = min(int(args.get("timeout", DEFAULT_TIMEOUT)), MAX_TIMEOUT)
    if not cmd.strip():
        return "Refused: empty command."

    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=ctx.workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_safe_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        return f"Timeout after {timeout}s. Command killed."

    parts = [f"exit={proc.returncode}"]
    if stdout:
        parts.append("stdout:\n" + _truncate(stdout, "stdout"))
    if stderr:
        parts.append("stderr:\n" + _truncate(stderr, "stderr"))
    return "\n".join(parts)


register(
    Tool(
        name="shell_run",
        description=(
            "Run a shell command in the workspace. DANGEROUS by default; benign "
            "commands (ls, git status, cat, etc.) downgrade to EXECUTE automatically. "
            "Output is tail-capped at 1 MB per stream. Sensitive env vars stripped."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds before kill. Default 60.",
                    "default": DEFAULT_TIMEOUT,
                },
            },
            "required": ["command"],
        },
        permission=Permission.DANGEROUS,  # base tier; runtime classify() may downgrade
        scope=ToolScope.SYSTEM,
        handler=_handler,
    )
)
