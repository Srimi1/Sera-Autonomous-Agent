"""Eval harness — golden conversations + telemetry.

Outclass: `sera eval run` is the single command that gates a release. The
stub mode is deterministic + CI-safe; bench mode hits the configured LLM
profile to measure real latency / cost / cache-hit ratio.
"""
from sera.eval.cases import EvalCase, ExpectedOutcome, ScriptStep, load_cases
from sera.eval.runner import CaseResult, RunReport, run_cases
from sera.eval.scoring import score
from sera.eval.stub_llm import StubLLM
from sera.eval.telemetry import TelemetryStore

__all__ = [
    "EvalCase",
    "ExpectedOutcome",
    "ScriptStep",
    "load_cases",
    "CaseResult",
    "RunReport",
    "run_cases",
    "score",
    "StubLLM",
    "TelemetryStore",
]
