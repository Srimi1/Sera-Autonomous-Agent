"""P-100: public honest comparison — Sera is in the room."""
from __future__ import annotations

import pytest

from sera.eval.compare import (
    RIVALS,
    Capability,
    ComparisonError,
    ComparisonReport,
    Metric,
    RivalClaim,
    Source,
    build_from_run,
)


# ---------------------------------------------------------------------------
# RivalClaim honesty guard
# ---------------------------------------------------------------------------

def test_measured_claim_needs_source_url():
    claim = RivalClaim("Hermes", "eval pass rate", 80.0, Source.MEASURED)
    with pytest.raises(ComparisonError):
        claim.validate()


def test_measured_claim_with_url_ok():
    claim = RivalClaim("Hermes", "eval pass rate", 80.0, Source.MEASURED,
                       source_url="https://example.com/bench")
    claim.validate()


def test_unpublished_must_be_none():
    claim = RivalClaim("OpenHuman", "p50 latency", 500.0, Source.UNPUBLISHED)
    with pytest.raises(ComparisonError):
        claim.validate()


def test_unpublished_none_ok():
    claim = RivalClaim("OpenHuman", "p50 latency", None, Source.UNPUBLISHED)
    claim.validate()


def test_self_reported_needs_value():
    claim = RivalClaim("OpenClaw", "eval pass rate", None, Source.SELF_REPORTED)
    with pytest.raises(ComparisonError):
        claim.validate()


def test_unknown_rival_rejected():
    claim = RivalClaim("FakeRival", "eval pass rate", 50.0, Source.SELF_REPORTED)
    with pytest.raises(ComparisonError):
        claim.validate()


# ---------------------------------------------------------------------------
# Capability outclass logic
# ---------------------------------------------------------------------------

def test_capability_is_outclass_when_alone():
    c = Capability("typed causal edges", sera=True,
                   rivals={"Hermes": False, "OpenHuman": False, "OpenClaw": False})
    assert c.is_outclass


def test_capability_not_outclass_when_rival_has_it():
    c = Capability("chat", sera=True,
                   rivals={"Hermes": True, "OpenHuman": False, "OpenClaw": False})
    assert not c.is_outclass


def test_capability_not_outclass_when_sera_lacks_it():
    c = Capability("voice", sera=False, rivals={"Hermes": False})
    assert not c.is_outclass


# ---------------------------------------------------------------------------
# ComparisonReport
# ---------------------------------------------------------------------------

def test_report_validate_passes_on_clean_claims():
    report = ComparisonReport(
        metrics=[Metric("eval pass rate", 94.0, unit="%")],
        rival_claims=[
            RivalClaim("Hermes", "eval pass rate", 81.0, Source.SELF_REPORTED),
            RivalClaim("OpenHuman", "eval pass rate", None, Source.UNPUBLISHED),
        ],
    )
    report.validate()


def test_report_validate_rejects_claim_for_unknown_metric():
    report = ComparisonReport(
        metrics=[Metric("eval pass rate", 94.0, unit="%")],
        rival_claims=[RivalClaim("Hermes", "made up metric", 1.0, Source.SELF_REPORTED)],
    )
    with pytest.raises(ComparisonError):
        report.validate()


def test_outclasses_filters_correctly():
    report = ComparisonReport(
        capabilities=[
            Capability("typed causal edges", sera=True,
                       rivals={"Hermes": False, "OpenHuman": False, "OpenClaw": False}),
            Capability("chat", sera=True, rivals={"Hermes": True}),
        ]
    )
    outs = report.outclasses
    assert len(outs) == 1
    assert outs[0].name == "typed causal edges"


def test_claims_for_groups_by_metric():
    report = ComparisonReport(
        metrics=[Metric("eval pass rate", 94.0, unit="%")],
        rival_claims=[
            RivalClaim("Hermes", "eval pass rate", 81.0, Source.SELF_REPORTED),
            RivalClaim("OpenHuman", "eval pass rate", None, Source.UNPUBLISHED),
        ],
    )
    claims = report.claims_for("eval pass rate")
    assert set(claims.keys()) == {"Hermes", "OpenHuman"}


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def test_markdown_contains_sera_number():
    report = ComparisonReport(metrics=[Metric("eval pass rate", 94.0, unit="%")])
    md = report.to_markdown()
    assert "94.0%" in md


def test_markdown_unpublished_shown_as_dash():
    report = ComparisonReport(
        metrics=[Metric("p50 latency", 120, unit="ms")],
        rival_claims=[RivalClaim("Hermes", "p50 latency", None, Source.UNPUBLISHED)],
    )
    md = report.to_markdown()
    assert "—" in md


def test_markdown_marks_self_reported():
    report = ComparisonReport(
        metrics=[Metric("eval pass rate", 94.0, unit="%")],
        rival_claims=[RivalClaim("Hermes", "eval pass rate", 81.0, Source.SELF_REPORTED)],
    )
    md = report.to_markdown()
    assert "self-reported" in md


def test_markdown_lists_outclasses():
    report = ComparisonReport(
        capabilities=[
            Capability("typed causal edges", sera=True,
                       rivals={"Hermes": False, "OpenHuman": False, "OpenClaw": False}),
        ]
    )
    md = report.to_markdown()
    assert "typed causal edges" in md
    assert "What only Sera ships" in md


def test_markdown_honest_when_no_outclass():
    report = ComparisonReport(capabilities=[])
    md = report.to_markdown()
    assert "unfinished" in md


def test_markdown_validates_first():
    """to_markdown must refuse a dishonest report."""
    report = ComparisonReport(
        metrics=[Metric("x", 1.0)],
        rival_claims=[RivalClaim("Hermes", "x", 2.0, Source.MEASURED)],  # no url
    )
    with pytest.raises(ComparisonError):
        report.to_markdown()


def test_markdown_has_all_rival_columns():
    report = ComparisonReport(metrics=[Metric("eval pass rate", 94.0, unit="%")])
    md = report.to_markdown()
    for rival in RIVALS:
        assert rival in md


# ---------------------------------------------------------------------------
# build_from_run
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, passed):
        self.passed = passed


class _FakeReport:
    def __init__(self, n_pass, n_total):
        self.results = [_FakeResult(i < n_pass) for i in range(n_total)]

    @property
    def n_pass(self):
        return sum(1 for r in self.results if r.passed)


def test_build_from_run_computes_pass_rate():
    report = build_from_run(_FakeReport(9, 10))
    assert report.metrics[0].name == "eval pass rate"
    assert report.metrics[0].sera_value == 90.0


def test_build_from_run_with_latency():
    report = build_from_run(_FakeReport(10, 10), p50_latency_ms=150)
    names = [m.name for m in report.metrics]
    assert "p50 latency" in names


def test_build_from_run_empty():
    report = build_from_run(_FakeReport(0, 0))
    assert report.metrics[0].sera_value == 0.0
