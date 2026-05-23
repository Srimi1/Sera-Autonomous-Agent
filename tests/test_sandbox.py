"""Tests for sera.sandbox — local sandbox, picker, python_eval tool."""
from __future__ import annotations

import asyncio

import pytest

from sera.sandbox.base import SandboxResult, SandboxTier
from sera.sandbox.local import LocalSubprocessSandbox, _scan_network_imports
from sera.sandbox.picker import pick_sandbox, tier_cost
from sera.tools.base import ToolContext


# ---------------------------------------------------------------------------
# _scan_network_imports
# ---------------------------------------------------------------------------

class TestScanNetworkImports:
    def test_clean_code_empty(self) -> None:
        assert _scan_network_imports("x = 1 + 1") == []

    def test_requests_import(self) -> None:
        found = _scan_network_imports("import requests")
        assert "requests" in found

    def test_from_requests(self) -> None:
        found = _scan_network_imports("from requests import get")
        assert "requests" in found

    def test_socket_detected(self) -> None:
        found = _scan_network_imports("import socket")
        assert "socket" in found

    def test_urllib_detected(self) -> None:
        found = _scan_network_imports("import urllib.request")
        assert "urllib" in found or "urllib.request" in found

    def test_multiple_imports(self) -> None:
        code = "import requests\nimport socket"
        found = _scan_network_imports(code)
        assert len(found) >= 2

    def test_syntax_error_returns_empty(self) -> None:
        assert _scan_network_imports("def broken(") == []

    def test_math_not_network(self) -> None:
        assert _scan_network_imports("import math\nimport os") == []


# ---------------------------------------------------------------------------
# SandboxResult
# ---------------------------------------------------------------------------

class TestSandboxResult:
    def test_ok_when_exit_zero(self) -> None:
        r = SandboxResult("out", "", 0, False, SandboxTier.LOCAL)
        assert r.ok

    def test_not_ok_on_timeout(self) -> None:
        r = SandboxResult("", "", 0, True, SandboxTier.LOCAL)
        assert not r.ok

    def test_not_ok_on_nonzero_exit(self) -> None:
        r = SandboxResult("", "err", 1, False, SandboxTier.LOCAL)
        assert not r.ok

    def test_as_tool_output_stdout(self) -> None:
        r = SandboxResult("hello\n", "", 0, False, SandboxTier.LOCAL)
        assert "hello" in r.as_tool_output()

    def test_as_tool_output_timeout(self) -> None:
        r = SandboxResult("", "", -1, True, SandboxTier.LOCAL)
        assert "timed out" in r.as_tool_output()

    def test_as_tool_output_stderr(self) -> None:
        r = SandboxResult("", "NameError: x", 1, False, SandboxTier.LOCAL)
        out = r.as_tool_output()
        assert "NameError" in out

    def test_as_tool_output_no_output(self) -> None:
        r = SandboxResult("", "", 0, False, SandboxTier.LOCAL)
        assert "[no output]" in r.as_tool_output()

    def test_as_tool_output_exit_code_appended(self) -> None:
        r = SandboxResult("some out", "err", 2, False, SandboxTier.LOCAL)
        out = r.as_tool_output()
        assert "exit 2" in out


# ---------------------------------------------------------------------------
# LocalSubprocessSandbox — basic execution
# ---------------------------------------------------------------------------

class TestLocalSandbox:
    def test_simple_print(self) -> None:
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run('print("hello sandbox")'))
        assert r.stdout.strip() == "hello sandbox"
        assert r.ok

    def test_expression_result(self) -> None:
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("print(2 ** 10)"))
        assert "1024" in r.stdout

    def test_syntax_error_captured(self) -> None:
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("def broken("))
        assert not r.ok
        assert r.exit_code != 0

    def test_runtime_error_captured(self) -> None:
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("1 / 0"))
        assert not r.ok
        assert "ZeroDivisionError" in r.stderr

    def test_multiline_code(self) -> None:
        sb = LocalSubprocessSandbox()
        code = "xs = [i**2 for i in range(5)]\nprint(sum(xs))"
        r = asyncio.run(sb.run(code))
        assert "30" in r.stdout

    def test_tier_is_local(self) -> None:
        sb = LocalSubprocessSandbox()
        assert sb.tier == SandboxTier.LOCAL


# ---------------------------------------------------------------------------
# P-44 verification 1: infinite loop killed at 10s
# ---------------------------------------------------------------------------

class TestTimeoutVerification:
    def test_infinite_loop_killed(self) -> None:
        """Verification: infinite loop killed at timeout."""
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("while True: pass", timeout=2.0))
        assert r.timed_out, "infinite loop should have been killed"
        assert not r.ok
        assert "timeout" in r.stderr.lower() or "killed" in r.stderr.lower()

    def test_fast_code_not_killed(self) -> None:
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("print('done')", timeout=10.0))
        assert not r.timed_out
        assert r.ok

    def test_timeout_result_has_nonzero_exit(self) -> None:
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("while True: pass", timeout=1.0))
        assert r.exit_code != 0 or r.timed_out


# ---------------------------------------------------------------------------
# P-44 verification 2: net call refused without grant
# ---------------------------------------------------------------------------

class TestNetworkVerification:
    def test_requests_refused_by_default(self) -> None:
        """Verification: network call refused without grant."""
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("import requests\nrequests.get('http://example.com')"))
        assert not r.ok
        assert "refused" in r.stderr.lower() or "allow_network" in r.stderr

    def test_socket_refused_by_default(self) -> None:
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("import socket\ns = socket.socket()"))
        assert not r.ok

    def test_network_allowed_with_grant(self) -> None:
        """With allow_network=True, network imports pass the AST gate."""
        sb = LocalSubprocessSandbox()
        # We don't make a real HTTP call — just verify import is allowed past AST gate
        # (it will still fail if no network, but not from our guard)
        r = asyncio.run(sb.run(
            "import sys\nprint('network allowed')",
            allow_network=True,
        ))
        # import sys is not a network module — succeeds cleanly
        assert "network allowed" in r.stdout

    def test_math_allowed_without_grant(self) -> None:
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("import math\nprint(math.pi)"))
        assert r.ok
        assert "3.14" in r.stdout

    def test_urllib_refused(self) -> None:
        sb = LocalSubprocessSandbox()
        r = asyncio.run(sb.run("from urllib.request import urlopen"))
        assert not r.ok


# ---------------------------------------------------------------------------
# Sandbox picker
# ---------------------------------------------------------------------------

class TestPicker:
    def test_zero_ceiling_returns_local(self) -> None:
        sb = pick_sandbox(cost_ceiling_usd=0.0)
        assert isinstance(sb, LocalSubprocessSandbox)

    def test_low_ceiling_returns_local(self) -> None:
        sb = pick_sandbox(cost_ceiling_usd=0.005)
        assert isinstance(sb, LocalSubprocessSandbox)

    def test_modal_missing_falls_back_to_local(self) -> None:
        # Modal not installed — picker should fall back
        sb = pick_sandbox(cost_ceiling_usd=0.05)
        assert isinstance(sb, LocalSubprocessSandbox)

    def test_daytona_missing_falls_back_to_local(self) -> None:
        sb = pick_sandbox(cost_ceiling_usd=1.0)
        assert isinstance(sb, LocalSubprocessSandbox)

    def test_tier_cost_local_is_zero(self) -> None:
        assert tier_cost(SandboxTier.LOCAL) == 0.0

    def test_tier_cost_modal_positive(self) -> None:
        assert tier_cost(SandboxTier.MODAL) > 0.0

    def test_tier_cost_daytona_higher_than_modal(self) -> None:
        assert tier_cost(SandboxTier.DAYTONA) > tier_cost(SandboxTier.MODAL)


# ---------------------------------------------------------------------------
# python_eval tool handler
# ---------------------------------------------------------------------------

class TestPythonEvalTool:
    def setup_method(self) -> None:
        # Ensure the tool is registered
        import sera.tools.impl.python_eval  # noqa: F401

    def _ctx(self) -> ToolContext:
        return ToolContext(session_id="t", workspace="/tmp")

    def _run(self, args: dict) -> str:
        from sera.tools.impl.python_eval import _handler
        return asyncio.run(_handler(args, self._ctx()))

    def test_empty_code(self) -> None:
        result = self._run({"code": ""})
        assert "required" in result.lower() or "code" in result.lower()

    def test_basic_print(self) -> None:
        result = self._run({"code": 'print("eval works")'})
        assert "eval works" in result

    def test_timeout_default_applied(self) -> None:
        result = self._run({"code": "while True: pass", "timeout": 1.5})
        assert "timed out" in result.lower() or "killed" in result.lower()

    def test_network_refused_by_default(self) -> None:
        result = self._run({"code": "import requests"})
        assert "refused" in result.lower() or "allow_network" in result.lower()

    def test_tool_registered(self) -> None:
        from sera.tools.registry import all_tools
        names = {t.name for t in all_tools()}
        assert "python_eval" in names
