"""P-100: Sera is in the room — public, honest side-by-side comparison.

OUTCLASS: We publish numbers, not adjectives. Sera's metrics come from a REAL
eval run (RunReport). Rival numbers carry a provenance tag — SELF_REPORTED,
MEASURED (needs a source URL), or UNPUBLISHED (value must be None). The honesty
guard refuses to emit a fabricated rival win. Capabilities are framed as gaps
the rivals have, never as features Sera merely matches.

The egoist rule, encoded: a capability row where every rival also ships it is
not an outclass — `outclasses` filters to rows only Sera has.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

RIVALS = ("Hermes", "OpenHuman", "OpenClaw")


class Source(str, Enum):
    MEASURED = "measured"          # we ran it; needs source_url
    SELF_REPORTED = "self_reported"  # rival's own published claim
    UNPUBLISHED = "unpublished"    # no public number; value must be None


class ComparisonError(RuntimeError):
    """Raised when a comparison would publish a dishonest or malformed number."""


@dataclass
class Metric:
    """A Sera number measured from a real run."""

    name: str
    sera_value: float | str
    unit: str = ""
    higher_is_better: bool = True


@dataclass
class RivalClaim:
    """A single rival's number for a metric, with provenance."""

    rival: str
    metric: str
    value: float | str | None
    source: Source
    source_url: str = ""

    def validate(self) -> None:
        if self.rival not in RIVALS:
            raise ComparisonError(f"unknown rival: {self.rival}")
        if self.source == Source.MEASURED and not self.source_url:
            raise ComparisonError(
                f"{self.rival}/{self.metric}: MEASURED claims require a source_url"
            )
        if self.source == Source.UNPUBLISHED and self.value is not None:
            raise ComparisonError(
                f"{self.rival}/{self.metric}: UNPUBLISHED must have value=None"
            )
        if self.source == Source.SELF_REPORTED and self.value is None:
            raise ComparisonError(
                f"{self.rival}/{self.metric}: SELF_REPORTED needs a value"
            )


@dataclass
class Capability:
    """A capability and which systems ship it. `sera` should be True here."""

    name: str
    sera: bool
    rivals: dict[str, bool] = field(default_factory=dict)

    @property
    def is_outclass(self) -> bool:
        """True iff Sera ships it and no rival does."""
        return self.sera and not any(self.rivals.values())


@dataclass
class ComparisonReport:
    metrics: list[Metric] = field(default_factory=list)
    rival_claims: list[RivalClaim] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=list)

    def validate(self) -> None:
        """Honesty guard — raise on any malformed or fabricated claim."""
        for c in self.rival_claims:
            c.validate()
        metric_names = {m.name for m in self.metrics}
        for c in self.rival_claims:
            if c.metric not in metric_names:
                raise ComparisonError(f"claim references unknown metric: {c.metric}")

    @property
    def outclasses(self) -> list[Capability]:
        return [c for c in self.capabilities if c.is_outclass]

    def claims_for(self, metric: str) -> dict[str, RivalClaim]:
        return {c.rival: c for c in self.rival_claims if c.metric == metric}

    def to_markdown(self) -> str:
        self.validate()
        lines: list[str] = ["# Sera — side-by-side", ""]
        lines.append("Numbers below are honest. Sera's are measured from a real "
                     "eval run. Rival cells are tagged by provenance; unpublished "
                     "numbers are shown as `—`, never guessed.")
        lines.append("")

        # Metrics table
        header = "| Metric | Sera | " + " | ".join(RIVALS) + " |"
        sep = "|" + "---|" * (2 + len(RIVALS))
        lines += [header, sep]
        for m in self.metrics:
            claims = self.claims_for(m.name)
            cells = []
            for r in RIVALS:
                c = claims.get(r)
                if c is None or c.value is None:
                    cells.append("—")
                elif c.source == Source.SELF_REPORTED:
                    cells.append(f"{c.value}{m.unit} (self-reported)")
                else:
                    cells.append(f"{c.value}{m.unit}")
            sera_cell = f"**{m.sera_value}{m.unit}**"
            lines.append(f"| {m.name} | {sera_cell} | " + " | ".join(cells) + " |")
        lines.append("")

        # Capabilities — gaps, not features
        lines.append("## What only Sera ships")
        lines.append("")
        outs = self.outclasses
        if not outs:
            lines.append("_(none — this is unfinished by the egoist's rule)_")
        else:
            for cap in outs:
                lines.append(f"- **{cap.name}** — none of {', '.join(RIVALS)} ship this.")
        lines.append("")
        return "\n".join(lines)


def build_from_run(report, *, p50_latency_ms: int | None = None) -> ComparisonReport:
    """Seed a ComparisonReport's Sera metrics from a real eval RunReport."""
    total = len(report.results)
    pass_rate = round(100.0 * report.n_pass / total, 1) if total else 0.0
    metrics = [
        Metric(name="eval pass rate", sera_value=pass_rate, unit="%"),
    ]
    if p50_latency_ms is not None:
        metrics.append(
            Metric(
                name="p50 latency",
                sera_value=p50_latency_ms,
                unit="ms",
                higher_is_better=False,
            )
        )
    return ComparisonReport(metrics=metrics)
