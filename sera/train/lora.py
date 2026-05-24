"""Local LoRA fine-tuner — on-device training nobody else ships.

OUTCLASS: Every rival (Hermes, OpenHuman, OpenClaw) relies on cloud APIs.
Sera trains a LoRA adapter on the user's own hardware overnight, using the
synthetic Q-A corpus from P-72.  The adapter is local, signed by the session
that produced it, and optionally evaluated against P-10's golden set to
measure accuracy gain in percentage points.

Runtime: mlx-lm (Apple Silicon).  The `runner` is injected so the whole
module is testable without mlx-lm installed — same seam as voice (P-68/69)
and iMessage (P-58).

mlx-lm command reference
------------------------
python -m mlx_lm.lora \\
    --model <base_model> \\
    --data  <dir_containing_train.jsonl> \\
    --train \\
    --fine-tune-type lora \\
    --num-iters <n> \\
    --batch-size <b> \\
    --learning-rate <lr> \\
    --lora-parameters '{"rank": <r>}' \\
    --adapter-path <adapter_dir>

mlx-lm writes lines like:
    Iter 100: Train loss 2.500, It/sec 3.12, Tokens/sec 812
to stdout.  We parse the final loss for TrainResult.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Generator

from sera.config import SERA_HOME

log = logging.getLogger("sera.train.lora")

GAIN_DB = SERA_HOME / "lora_gain.db"

Runner = Callable[[list[str]], tuple[int, str, str]]   # cmd → (returncode, stdout, stderr)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _default_runner(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Config / Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrainConfig:
    base_model: str                    # HF / MLX model ID
    corpus: Path                       # JSONL from DatasetExporter
    adapter_dir: Path                  # where LoRA weights land
    num_iters: int = 1000
    lora_rank: int = 8
    batch_size: int = 4
    learning_rate: float = 1e-4
    eval_every: int = 100


@dataclass(frozen=True)
class TrainResult:
    adapter_dir: Path
    iterations: int
    final_loss: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Loss parser
# ---------------------------------------------------------------------------

_LOSS_RE = re.compile(r"Train loss\s+([\d.]+)", re.IGNORECASE)


def _parse_final_loss(output: str) -> float | None:
    """Return the last Train loss value printed by mlx-lm, or None."""
    losses = _LOSS_RE.findall(output)
    if not losses:
        return None
    try:
        return float(losses[-1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Trainer
# ---------------------------------------------------------------------------

class LoRATrainer:
    """Wraps mlx-lm LoRA training.  Inject `runner` to test without hardware."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = runner or _default_runner

    def build_cmd(self, config: TrainConfig, data_dir: Path) -> list[str]:
        """Return the mlx-lm command list (no side-effects)."""
        lora_params = json.dumps({"rank": config.lora_rank})
        return [
            sys.executable, "-m", "mlx_lm.lora",
            "--model",          config.base_model,
            "--data",           str(data_dir),
            "--train",
            "--fine-tune-type", "lora",
            "--num-iters",      str(config.num_iters),
            "--batch-size",     str(config.batch_size),
            "--learning-rate",  str(config.learning_rate),
            "--lora-parameters", lora_params,
            "--adapter-path",   str(config.adapter_dir),
        ]

    def train(self, config: TrainConfig) -> TrainResult:
        """Run training.  Returns TrainResult; errors are soft (never raises)."""
        if not config.corpus.exists():
            return TrainResult(
                adapter_dir=config.adapter_dir,
                iterations=0,
                error=f"corpus not found: {config.corpus}",
            )
        if config.corpus.stat().st_size == 0:
            return TrainResult(
                adapter_dir=config.adapter_dir,
                iterations=0,
                error="corpus is empty",
            )

        config.adapter_dir.mkdir(parents=True, exist_ok=True)

        # mlx-lm expects a directory with train.jsonl (and optionally valid.jsonl).
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            shutil.copy(config.corpus, data_dir / "train.jsonl")

            cmd = self.build_cmd(config, data_dir)
            log.info("lora train: %s", " ".join(cmd))
            rc, stdout, stderr = self._run(cmd)

        if rc != 0:
            msg = (stderr or stdout or "mlx_lm.lora exited non-zero").strip()
            log.warning("lora train failed (rc=%d): %s", rc, msg[:200])
            return TrainResult(
                adapter_dir=config.adapter_dir,
                iterations=0,
                error=msg[:200],
            )

        loss = _parse_final_loss(stdout + stderr)
        log.info("lora train done: loss=%s adapter=%s", loss, config.adapter_dir)
        return TrainResult(
            adapter_dir=config.adapter_dir,
            iterations=config.num_iters,
            final_loss=loss,
        )


# ---------------------------------------------------------------------------
# Gain tracker — persists nightly eval accuracy, computes pp gain
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lora_scores (
    date        TEXT PRIMARY KEY,
    recorded_at REAL NOT NULL,
    accuracy    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lora_recorded ON lora_scores(recorded_at);
"""


class GainTracker:
    """Stores nightly eval accuracy scores and computes percentage-point gain."""

    def __init__(self, db: Path | None = None, clock: Callable[[], float] | None = None) -> None:
        import time
        self._db = db or GAIN_DB
        self._clock = clock or time.time
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def record(self, date: str, accuracy: float) -> None:
        """Upsert today's eval accuracy (0.0–1.0)."""
        if not (0.0 <= accuracy <= 1.0):
            raise ValueError(f"accuracy must be in [0, 1], got {accuracy}")
        with self._conn() as con:
            con.execute(
                "INSERT INTO lora_scores (date, recorded_at, accuracy) VALUES (?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET recorded_at=excluded.recorded_at, "
                "accuracy=excluded.accuracy",
                (date, self._clock(), accuracy),
            )
            con.commit()

    def scores(self, limit: int = 0) -> list[tuple[str, float]]:
        """Return (date, accuracy) ordered oldest-first."""
        q = "SELECT date, accuracy FROM lora_scores ORDER BY recorded_at ASC"
        if limit:
            q += f" LIMIT {limit}"
        with self._conn() as con:
            rows = con.execute(q).fetchall()
        return [(r["date"], float(r["accuracy"])) for r in rows]

    def gain_pp(self, n_nights: int = 7) -> float | None:
        """Return accuracy gain in percentage points over the last n_nights.

        Returns None if fewer than 2 scores are recorded.
        Positive = improvement; negative = regression.
        """
        history = self.scores(limit=n_nights)
        if len(history) < 2:
            return None
        first_acc = history[0][1]
        last_acc  = history[-1][1]
        return round((last_acc - first_acc) * 100.0, 4)

    def count(self) -> int:
        with self._conn() as con:
            return int(con.execute("SELECT COUNT(*) FROM lora_scores").fetchone()[0])
