"""Sera CLI. Entry: `sera chat`, `sera setup`, `sera tools`, `sera sessions`."""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sera import __version__
from sera.agent.budget import IterationBudget
from sera.agent.interrupt import InterruptToken, Interrupted, install_sigint
from sera.agent.loop import TokenSink, run_turn
from sera.config import CONFIG_PATH, SESSIONS_DB, SKILLS_DIR, load, save
from sera.llm.router import for_profile
from sera.llm.secrets import get_key, set_key
from sera.memory.session import Session, recover_aborted_sessions
from sera.safety.approval import CliApprovalGate
from sera.safety.redact import redact
from sera.tools.base import Permission
from sera.tools.registry import all_tools

console = Console()


@click.group()
def main() -> None:
    """Sera — autonomous agent CLI."""


@main.command()
def version() -> None:
    click.echo(f"sera {__version__}")


@main.command()
def setup() -> None:
    """Interactive: pick a profile, paste an API key, save to keyring."""
    cfg = load()
    console.print(Panel.fit("Sera setup", style="bold"))
    console.print(f"Config: {CONFIG_PATH}")
    provider = click.prompt(
        "Provider for default reasoning profile",
        type=click.Choice(["anthropic", "openai"]),
        default=cfg["llm"]["profiles"]["reasoning"]["provider"],
    )
    model_default = (
        "claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o-mini"
    )
    model = click.prompt("Model", default=model_default)
    cfg["llm"]["profiles"]["reasoning"] = {"provider": provider, "model": model}
    save(cfg)

    if click.confirm("Save an API key to your OS keychain now?", default=True):
        key = click.prompt(f"{provider.upper()} API key", hide_input=True)
        try:
            set_key(provider, key)
            console.print(f"[green]Stored {provider}_api_key in keychain.[/green]")
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]Keychain unavailable: {e}. Export the env var instead.[/yellow]")
    console.print("[bold green]Setup complete.[/bold green]")


@main.command(name="tools")
def list_tools_cmd() -> None:
    """Print the tool inventory with permission tiers."""
    table = Table(title="Sera tools", show_lines=False)
    table.add_column("name", style="bold")
    table.add_column("permission")
    table.add_column("scope")
    table.add_column("description", overflow="fold")
    for t in sorted(all_tools(), key=lambda t: t.name):
        table.add_row(t.name, t.permission.name, t.scope.name, t.description)
    console.print(table)


@main.command(name="sessions")
@click.option("--limit", default=20, help="Max rows to show.")
def list_sessions_cmd(limit: int) -> None:
    """List recent sessions (most recently updated first).

    Connecting via `Session._connect` (through `recover_aborted_sessions`)
    runs the crash-recovery scan first so the displayed status column is
    always current.
    """
    if not SESSIONS_DB.exists():
        console.print("[dim]No sessions yet.[/dim]")
        return
    # Run recovery first so dangling turns get flagged before we render.
    recover_aborted_sessions()
    with sqlite3.connect(SESSIONS_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, workspace, updated_at, last_status, "
            "(SELECT COUNT(*) FROM messages WHERE session_id = sessions.id) AS n "
            "FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        console.print("[dim]No sessions yet.[/dim]")
        return
    table = Table(title="Sessions")
    table.add_column("id", style="bold")
    table.add_column("status")
    table.add_column("updated")
    table.add_column("msgs", justify="right")
    table.add_column("workspace", overflow="fold")
    table.add_column("title")
    for r in rows:
        when = datetime.fromtimestamp(r["updated_at"]).strftime("%Y-%m-%d %H:%M")
        status = r["last_status"] or "active"
        status_cell = (
            f"[yellow]{status}[/yellow]" if status == "aborted" else status
        )
        table.add_row(
            r["id"],
            status_cell,
            when,
            str(r["n"]),
            r["workspace"] or "",
            r["title"] or "",
        )
    console.print(table)


@main.group(name="skills", invoke_without_command=True)
@click.option("--root", default=None, type=click.Path(path_type=Path),
              help="Skills directory. Defaults to ~/.sera/skills.")
@click.option("--reload", "reload_flag", is_flag=True, default=False,
              help="Refresh tool registry against on-disk manifests.")
@click.pass_context
def list_skills_cmd(ctx: click.Context, root: Path | None, reload_flag: bool) -> None:
    """Discover every skill manifest under `--root` and print a summary.

    With `--reload`, register / re-register / unregister skill-derived
    tools in the live tool registry and print the delta. Without it,
    the listing is read-only. Subcommands (`ab`) handle skill ablation.
    """
    if ctx.invoked_subcommand is not None:
        return
    from sera.skills.loader import discover_skills, get_default_registry

    target = (root or SKILLS_DIR).resolve()
    if not target.is_dir():
        console.print(f"[dim]No skills directory at {target}.[/dim]")
        return

    if reload_flag:
        reg = get_default_registry(target)
        summary = reg.refresh()
        if not summary.changed:
            console.print(f"[dim]{target}: no changes since last reload.[/dim]")
        else:
            console.print(
                f"[bold]Reload summary:[/bold] "
                f"+{len(summary.added)} added, "
                f"-{len(summary.removed)} removed, "
                f"~{len(summary.updated)} updated"
            )
            for kind, names_ in (
                ("added", summary.added),
                ("updated", summary.updated),
                ("removed", summary.removed),
            ):
                if names_:
                    console.print(f"  [bold]{kind}[/bold]: {', '.join(names_)}")
        return

    skills = discover_skills(target)
    if not skills:
        console.print(f"[dim]No skills in {target}.[/dim]")
        return
    table = Table(title=f"Skills — {target}")
    table.add_column("name", style="bold")
    table.add_column("trigger")
    table.add_column("permission")
    table.add_column("version")
    table.add_column("council")
    table.add_column("lineage", overflow="fold")
    for s in skills:
        table.add_row(
            s.name,
            s.trigger,
            s.permission,
            s.version,
            "✓" if s.council else "",
            ", ".join(s.lineage),
        )
    console.print(table)


@main.group()
def curator() -> None:
    """Post-session curator — reviews high-tool-density sessions."""


@curator.command(name="log")
@click.option("--db", "db_path", default=None, type=click.Path(path_type=Path),
              help="Curator log DB path. Defaults to ~/.sera/curator.db.")
@click.option("--limit", default=20, type=int)
def curator_log_cmd(db_path: Path | None, limit: int) -> None:
    """Print the most recent curator reports."""
    from sera.curator.loop import CuratorStore

    store = CuratorStore(db_path=db_path)
    if not store.db_path.exists():
        console.print("[dim]No curator reports yet.[/dim]")
        return
    reports = store.recent_reports(limit=limit)
    if not reports:
        console.print("[dim]No curator reports yet.[/dim]")
        return
    table = Table(title="Curator reports")
    table.add_column("session", style="bold")
    table.add_column("when")
    table.add_column("proposals", justify="right")
    table.add_column("kinds")
    table.add_column("error", overflow="fold")
    for r in reports:
        when = datetime.fromtimestamp(r.finished_at).strftime("%Y-%m-%d %H:%M:%S")
        kinds = ",".join(sorted({p.kind for p in r.proposals})) or "-"
        table.add_row(
            r.session_id,
            when,
            str(len(r.proposals)),
            kinds,
            r.error or "",
        )
    console.print(table)


@curator.command(name="discover")
@click.option("--curator-db", "curator_db", default=None, type=click.Path(path_type=Path),
              help="Curator log DB to scan for session patterns.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print proposals without persisting.")
@click.option("--limit", default=50, type=int,
              help="Max recent curator reports to scan.")
def curator_discover_cmd(curator_db: Path | None, dry_run: bool, limit: int) -> None:
    """Scan recent curator reports for repeated patterns and propose new skills."""
    from sera.curator.loop import CuratorStore
    from sera.curator.discovery import MIN_PATTERN_FREQUENCY

    store = CuratorStore(db_path=curator_db)
    if not store.db_path.exists():
        console.print("[dim]No sessions found. Run some sessions first.[/dim]")
        return

    reports = store.recent_reports(limit=limit)
    if not reports:
        console.print("[dim]No sessions found. 0 reports in curator log.[/dim]")
        return

    # Count tool_hint payloads as proxy for tool usage patterns.
    tool_counts: dict[str, int] = {}
    for r in reports:
        for p in r.proposals:
            if p.kind == "tool_hint":
                tool = p.payload.get("tool") or ""
                if tool:
                    tool_counts[tool] = tool_counts.get(tool, 0) + 1

    hot = {t: n for t, n in tool_counts.items() if n >= MIN_PATTERN_FREQUENCY}
    if not hot:
        console.print(
            f"[dim]No repeated patterns (need ≥{MIN_PATTERN_FREQUENCY} occurrences). "
            f"Scanned {len(reports)} reports.[/dim]"
        )
        return

    table = Table(title="Discovery — proposed new skills")
    table.add_column("tool pattern", style="bold")
    table.add_column("occurrences", justify="right")
    table.add_column("action")
    for tool, count in sorted(hot.items(), key=lambda kv: -kv[1]):
        table.add_row(tool, str(count), "propose skill" if dry_run else "would propose skill")
    console.print(table)
    if dry_run:
        console.print(f"[dim]dry-run: {len(hot)} pattern(s) above threshold.[/dim]")


@list_skills_cmd.command(name="commit")
@click.argument("skill_name")
@click.option("--message", "-m", required=True, help="Commit message.")
@click.option("--author", default=None,
              help='Override author, e.g. "curator <curator@sera>".')
@click.pass_context
def skills_commit_cmd(
    ctx: click.Context, skill_name: str, message: str, author: str | None,
) -> None:
    """Stage <skill>/SKILL.md and commit the change to the skills repo."""
    from sera.skills.git import commit_skill_change

    root = (ctx.parent.params if ctx.parent else {}).get("root")
    target = (root or SKILLS_DIR).resolve()
    info = commit_skill_change(target, skill_name, message, author=author)
    if info is None:
        console.print(f"[dim]{skill_name}: no changes to commit.[/dim]")
        return
    console.print(f"[green]committed[/green] {info.sha[:8]}  {info.message}")


@list_skills_cmd.command(name="log")
@click.argument("skill_name")
@click.option("--limit", default=20, type=int)
@click.pass_context
def skills_log_cmd(
    ctx: click.Context, skill_name: str, limit: int,
) -> None:
    """Walk a skill's commit history (newest first)."""
    from sera.skills.git import skill_log

    root = (ctx.parent.params if ctx.parent else {}).get("root")
    target = (root or SKILLS_DIR).resolve()
    log = skill_log(target, skill_name, limit=limit)
    if not log:
        console.print(f"[dim]{skill_name}: no history (no commits).[/dim]")
        return
    table = Table(title=f"History — {skill_name}")
    table.add_column("sha", style="bold")
    table.add_column("when")
    table.add_column("author")
    table.add_column("message", overflow="fold")
    for c in log:
        when = datetime.fromtimestamp(c.when).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(c.sha[:8], when, c.author, c.message)
    console.print(table)


@list_skills_cmd.command(name="diff")
@click.argument("skill_name")
@click.option("--from", "ref_a", default=None,
              help="Older ref. Omit to default to HEAD~1.")
@click.option("--to", "ref_b", default=None,
              help="Newer ref. Omit to default to HEAD.")
@click.pass_context
def skills_diff_cmd(
    ctx: click.Context, skill_name: str,
    ref_a: str | None, ref_b: str | None,
) -> None:
    """Print the diff between two refs for one skill's SKILL.md."""
    from sera.skills.git import skill_diff

    root = (ctx.parent.params if ctx.parent else {}).get("root")
    target = (root or SKILLS_DIR).resolve()
    diff = skill_diff(target, skill_name, ref_a=ref_a, ref_b=ref_b)
    if not diff:
        console.print(f"[dim]{skill_name}: no diff to show.[/dim]")
        return
    console.print(diff)


@list_skills_cmd.command(name="ab")
@click.option("--a", "path_a", required=True, type=click.Path(path_type=Path,
              exists=True), help="Variant A SKILL.md.")
@click.option("--b", "path_b", required=True, type=click.Path(path_type=Path,
              exists=True), help="Variant B SKILL.md.")
@click.option("--cases", "cases_path", required=True, type=click.Path(
              path_type=Path, exists=True), help="Replay cases yaml.")
@click.option("--cost-a", default=1.0, type=float, help="Per-call cost of A.")
@click.option("--cost-b", default=1.0, type=float, help="Per-call cost of B.")
@click.option("--lifecycle-db", default=None, type=click.Path(path_type=Path),
              help="Lifecycle DB path. Defaults to ~/.sera/skills_lifecycle.db.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show verdict but do not persist lifecycle changes.")
def skills_ab_cmd(
    path_a: Path,
    path_b: Path,
    cases_path: Path,
    cost_a: float,
    cost_b: float,
    lifecycle_db: Path | None,
    dry_run: bool,
) -> None:
    """Run A/B ablation; winner verified, loser archived (revive-able)."""
    import asyncio

    from sera.skills.ab import Variant, decide_and_persist, run_ab
    from sera.skills.lifecycle import SkillLifecycle
    from sera.skills.loader import load_skill
    from sera.skills.verify import load_replay_cases

    skill_a = load_skill(path_a)
    skill_b = load_skill(path_b)
    cases = load_replay_cases(cases_path)
    if not cases:
        console.print(f"[red]No cases loaded from {cases_path}.[/red]")
        sys.exit(2)

    variant_a = Variant(skill=skill_a, cost=cost_a)
    variant_b = Variant(skill=skill_b, cost=cost_b)

    if dry_run:
        result_a, result_b, verdict = asyncio.run(run_ab(variant_a, variant_b, cases))
    else:
        lc = SkillLifecycle(db_path=lifecycle_db)
        verdict = asyncio.run(decide_and_persist(lc, variant_a, variant_b, cases))
        result_a, result_b, _ = asyncio.run(run_ab(variant_a, variant_b, cases))

    table = Table(title=f"A/B — {cases_path.name}")
    table.add_column("variant", style="bold")
    table.add_column("pass", justify="right")
    table.add_column("total", justify="right")
    table.add_column("success", justify="right")
    table.add_column("cost", justify="right")
    for r in (result_a, result_b):
        table.add_row(
            r.name,
            str(r.n_passed),
            str(r.total_cases),
            f"{r.success_rate:.2%}",
            f"{r.total_cost:.3f}",
        )
    console.print(table)
    console.print(f"[bold green]winner:[/bold green] {verdict.winner}")
    console.print(f"[bold red]loser (archived, revive-able):[/bold red] {verdict.loser}")
    console.print(f"[dim]reason: {verdict.reason}[/dim]")


@list_skills_cmd.command(name="export")
@click.argument("name")
@click.option("--out", "out_path", default=None, type=click.Path(path_type=Path),
              help="Output path. Defaults to ./<name>.skillpack")
@click.option("--key", "key_file", default=None, type=click.Path(path_type=Path),
              help="Ed25519 private key PEM file for signing.")
def skills_export_cmd(name: str, out_path: Path | None, key_file: Path | None) -> None:
    """Export a skill as a signed .skillpack archive."""
    from sera.skills.pack import PackError, pack_skill

    ctx = click.get_current_context()
    root = (ctx.parent.params if ctx.parent else {}).get("root")
    dest = out_path or Path(f"{name}.skillpack")
    private_key_pem: bytes | None = None
    if key_file is not None:
        private_key_pem = Path(key_file).read_bytes()
    try:
        pack_skill(root, name, dest, private_key_pem=private_key_pem)
    except PackError as e:
        console.print(f"[red]export failed: {e}[/red]")
        raise SystemExit(1) from e
    console.print(f"[green]exported:[/green] {dest}")
    if private_key_pem is not None:
        console.print("[dim]signed with provided key[/dim]")


@list_skills_cmd.command(name="import")
@click.argument("pack_path", type=click.Path(path_type=Path))
@click.option("--key", "key_file", default=None, type=click.Path(path_type=Path),
              help="Ed25519 public key PEM file for verification.")
def skills_import_cmd(pack_path: Path, key_file: Path | None) -> None:
    """Import a .skillpack into the skills directory."""
    from sera.skills.pack import PackError, unpack_skill

    ctx = click.get_current_context()
    root = (ctx.parent.params if ctx.parent else {}).get("root")
    public_key_pem: bytes | None = None
    if key_file is not None:
        public_key_pem = Path(key_file).read_bytes()
    try:
        skill_name = unpack_skill(pack_path, root, public_key_pem=public_key_pem)
    except PackError as e:
        console.print(f"[red]import failed: {e}[/red]")
        raise SystemExit(1) from e
    console.print(f"[green]imported:[/green] {skill_name} → {root / skill_name}")


@list_skills_cmd.command(name="scores")
@click.option("--db", "db_path", default=None, type=click.Path(path_type=Path),
              help="Scores DB path. Defaults to ~/.sera/skills_scores.db.")
@click.option("--threshold", default=None, type=float,
              help="Suggest threshold (default 0.35). Skills below shown as demoted.")
def skills_scores_cmd(db_path: Path | None, threshold: float | None) -> None:
    """Show quality scores for all tracked skills."""
    from sera.skills.scoring import DEFAULT_SUGGEST_THRESHOLD, SkillScorer

    from rich.table import Table

    sc = SkillScorer(db_path=db_path)
    thresh = threshold if threshold is not None else DEFAULT_SUGGEST_THRESHOLD
    entries = sc.all_scores()
    if not entries:
        console.print("[dim]No scores recorded yet.[/dim]")
        return
    table = Table(title="Skill quality scores")
    table.add_column("skill", style="bold")
    table.add_column("score", justify="right")
    table.add_column("invocations", justify="right")
    table.add_column("success%", justify="right")
    table.add_column("👍", justify="right")
    table.add_column("👎", justify="right")
    table.add_column("suggest?", justify="center")
    for name, score, s in entries:
        suc_pct = f"{s.successes/s.invocations:.0%}" if s.invocations else "—"
        ok = "[green]yes[/green]" if score >= thresh else "[red]no[/red]"
        table.add_row(
            name, f"{score:.3f}", str(s.invocations), suc_pct,
            str(s.thumbs_up), str(s.thumbs_down), ok,
        )
    console.print(table)


@main.group()
def eval() -> None:  # noqa: A001 — `eval` is the user-facing verb here
    """Golden-conversation harness — release gate."""


def _default_cases_dir() -> Path:
    """Pick the first plausible cases dir for the eval CLI.

    Order:
      1. `tests/eval_cases/` under CWD (running from repo root in dev).
      2. `tests/eval_cases/` under the package source tree (editable install).
    """
    cwd_dir = Path.cwd() / "tests" / "eval_cases"
    if cwd_dir.is_dir():
        return cwd_dir
    pkg_root = Path(__file__).resolve().parents[2]
    return pkg_root / "tests" / "eval_cases"


@eval.command(name="run")
@click.option("--cases", "cases_dir", default=None, type=click.Path(path_type=Path),
              help="Directory of *.yaml eval cases. Defaults to ./tests/eval_cases.")
@click.option("--no-store", is_flag=True, default=False,
              help="Run without persisting telemetry.")
def eval_run_cmd(cases_dir: Path | None, no_store: bool) -> None:
    """Run every eval case under `cases_dir` with the stub LLM."""
    from sera.eval import load_cases, run_cases as _run_cases
    from sera.eval.telemetry import TelemetryStore

    target = (cases_dir or _default_cases_dir()).resolve()
    if not target.is_dir():
        console.print(f"[red]Cases dir not found: {target}[/red]")
        sys.exit(2)
    cases = load_cases(target)
    if not cases:
        console.print(f"[yellow]No cases under {target}[/yellow]")
        return

    store = None if no_store else TelemetryStore()
    report = _run_cases(cases, telemetry=store, profile="stub")

    table = Table(title=f"Eval run {report.run_id} — {target}")
    table.add_column("case", style="bold")
    table.add_column("pass")
    table.add_column("ms", justify="right")
    table.add_column("iters", justify="right")
    table.add_column("tools")
    table.add_column("reason", overflow="fold")
    for r in report.results:
        mark = "[green]✓[/green]" if r.passed else "[red]✗[/red]"
        table.add_row(
            r.case_id,
            mark,
            str(r.latency_ms),
            str(r.iterations),
            ",".join(r.tool_calls) or "-",
            r.reason or "",
        )
    console.print(table)
    console.print(f"[bold]{report.n_pass}/{len(report.results)} passed.[/bold]")
    if report.n_fail:
        sys.exit(1)


@eval.command(name="bench")
@click.option("--cases", "cases_dir", default=None, type=click.Path(path_type=Path))
@click.pass_context
def eval_bench_cmd(ctx: click.Context, cases_dir: Path | None) -> None:
    """Alias for `eval run` — same stub harness, named for muscle memory."""
    ctx.invoke(eval_run_cmd, cases_dir=cases_dir, no_store=False)


@eval.command(name="bench-memory")
@click.option("--corpus", "corpus_path", default=None, type=click.Path(path_type=Path),
              help="Recall corpus yaml. Defaults to tests/eval_cases/recall/corpus.yaml.")
@click.option("--queries", "queries_path", default=None, type=click.Path(path_type=Path),
              help="Recall queries yaml. Defaults to tests/eval_cases/recall/queries.yaml.")
@click.option("--min-mrr", default=0.8, type=float,
              help="Hybrid MRR floor — non-zero exit if hybrid < threshold.")
def eval_bench_memory_cmd(
    corpus_path: Path | None,
    queries_path: Path | None,
    min_mrr: float,
) -> None:
    """Retrieval recall benchmark (per-mode MRR + Recall@k)."""
    from sera.eval.memory_bench import run_memory_bench

    base = _default_cases_dir() / "recall"
    corpus = (corpus_path or base / "corpus.yaml").resolve()
    queries = (queries_path or base / "queries.yaml").resolve()
    if not corpus.is_file() or not queries.is_file():
        console.print(f"[red]Missing bench fixtures: {corpus} or {queries}[/red]")
        sys.exit(2)
    results = run_memory_bench(corpus, queries)

    table = Table(title=f"Recall bench — {corpus.parent.name}")
    table.add_column("mode", style="bold")
    table.add_column("MRR", justify="right")
    table.add_column("R@1", justify="right")
    table.add_column("R@5", justify="right")
    table.add_column("R@10", justify="right")
    table.add_column("ms/q", justify="right")
    table.add_column("Q", justify="right")
    hybrid_mrr = 0.0
    for r in results:
        table.add_row(*r.as_row())
        if r.mode == "hybrid":
            hybrid_mrr = r.mrr
    console.print(table)
    console.print(f"[bold]hybrid MRR = {hybrid_mrr:.3f}[/bold] (floor {min_mrr})")
    if hybrid_mrr < min_mrr:
        console.print("[red]Hybrid MRR below floor.[/red]")
        sys.exit(1)


@eval.command(name="show")
@click.option("--limit", default=5, help="Most recent runs to display.")
def eval_show_cmd(limit: int) -> None:
    """Print the most recent eval runs from the telemetry store."""
    from sera.eval.telemetry import TelemetryStore

    store = TelemetryStore()
    runs = store.recent_runs(limit=limit)
    if not runs:
        console.print("[dim]No eval runs yet — try `sera eval run`.[/dim]")
        return
    table = Table(title="Recent eval runs")
    table.add_column("run", style="bold")
    table.add_column("started")
    table.add_column("profile")
    table.add_column("pass", justify="right")
    table.add_column("fail", justify="right")
    for r in runs:
        started = datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(
            r["id"], started, r["profile"] or "-",
            str(r["n_pass"]), str(r["n_fail"]),
        )
    console.print(table)


@main.group()
def route() -> None:
    """Routing + provider telemetry (cache hits, token totals)."""


@route.command(name="stats")
@click.option("--limit", default=20, help="Max sessions to show.")
@click.option("--session-id", default=None, help="Show one session only.")
def route_stats_cmd(limit: int, session_id: str | None) -> None:
    """Show prompt-cache hit ratio per session.

    Reads token totals accumulated by the agent loop (Anthropic only —
    OpenAI's usage block doesn't expose cache_read tokens). A session with
    `cache_read > 0` confirms the freeze-at-start cache is working.
    """
    if not SESSIONS_DB.exists():
        console.print("[dim]No sessions yet.[/dim]")
        return
    with sqlite3.connect(SESSIONS_DB) as conn:
        conn.row_factory = sqlite3.Row
        if session_id:
            rows = conn.execute(
                "SELECT id, title, input_tokens, output_tokens, "
                "cache_read_tokens, cache_creation_tokens "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, input_tokens, output_tokens, "
                "cache_read_tokens, cache_creation_tokens "
                "FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    if not rows:
        console.print("[dim]No sessions yet.[/dim]")
        return
    table = Table(title="Prompt-cache stats")
    table.add_column("session", style="bold")
    table.add_column("in", justify="right")
    table.add_column("out", justify="right")
    table.add_column("cache-read", justify="right")
    table.add_column("cache-write", justify="right")
    table.add_column("hit%", justify="right")
    table.add_column("title", overflow="fold")
    for r in rows:
        in_t = int(r["input_tokens"] or 0)
        out_t = int(r["output_tokens"] or 0)
        cr = int(r["cache_read_tokens"] or 0)
        cw = int(r["cache_creation_tokens"] or 0)
        denom = in_t + cr + cw
        hit = (cr / denom * 100) if denom else 0.0
        table.add_row(
            r["id"],
            f"{in_t}",
            f"{out_t}",
            f"{cr}",
            f"{cw}",
            f"{hit:.1f}",
            r["title"] or "",
        )
    console.print(table)


@main.group()
def council() -> None:
    """In-process council — N models answer in parallel, anonymous labels."""


@council.command(name="run")
@click.argument("question")
@click.option("--models", default=None,
              help="Comma-separated model IDs. Defaults to 3 stub models in dry-run.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Use stub LLM responses (no real API calls).")
def council_run_cmd(question: str, models: str | None, dry_run: bool) -> None:
    """Ask N models the same question in parallel and show anonymous answers."""
    import asyncio
    from sera.council.runner import run_council

    from rich.table import Table

    if dry_run or models is None:
        model_list = ["stub-a", "stub-b", "stub-c"]

        def stub_factory(model_id: str):
            async def call(prompt: str) -> str:
                return "[stub answer from council member]"
            return call
        factory = stub_factory
    else:
        model_list = [m.strip() for m in models.split(",") if m.strip()]
        raise click.UsageError("Non-dry-run council requires LLM wiring (not yet integrated).")

    run = asyncio.run(run_council(question, model_list, factory))
    table = Table(title=f"Council — {question[:60]}")
    table.add_column("label", style="bold", width=6)
    table.add_column("answer")
    table.add_column("ms", justify="right", width=8)
    table.add_column("error", overflow="fold")
    for a in sorted(run.answers, key=lambda x: x.label):
        table.add_row(
            a.label,
            a.content or "",
            f"{a.latency_ms:.0f}",
            a.error or "",
        )
    console.print(table)
    if dry_run:
        console.print("[dim]dry-run: stub responses, no real LLM calls.[/dim]")


@main.command()
@click.option("--profile", default=None, help="LLM profile (reasoning|fast).")
@click.option("--workspace", default=None, help="Workspace root for tools.")
@click.option("--session-id", default=None, help="Resume an existing session id.")
def chat(profile: str | None, workspace: str | None, session_id: str | None) -> None:
    """Interactive REPL — type messages, agent streams responses + uses tools."""
    cfg = load()
    ws = workspace or os.getcwd()

    # Resolve the profile + check the API key is reachable before we open the REPL.
    profile_name = profile or cfg["llm"]["default_profile"]
    try:
        profile_cfg = cfg["llm"]["profiles"][profile_name]
    except KeyError:
        console.print(f"[red]No such profile: {profile_name}[/red]")
        sys.exit(2)
    provider = profile_cfg["provider"]
    if not get_key(provider):
        env_name = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider, "API_KEY")
        console.print(
            f"[red]Missing API key for provider '{provider}'.[/red] "
            f"Run [bold]sera setup[/bold] or export {env_name}."
        )
        sys.exit(1)

    if session_id:
        session = Session.load(session_id)
        if session is None:
            console.print(f"[red]No session {session_id}; creating a new one.[/red]")
            session = Session.create(workspace=ws)
    else:
        session = Session.create(workspace=ws)

    try:
        llm = for_profile(cfg, profile=profile_name)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Failed to init LLM: {e}[/red]")
        sys.exit(1)

    # Approval threshold: prompt when a tool's effective tier is AT or ABOVE the floor.
    # Config key `approval_required_at_or_above` is canonical; `approval_required_above`
    # is the legacy alias (P-03 / P-04) — kept for back-compat with shipped configs.
    safety = cfg.get("safety", {})
    floor_name = (
        safety.get("approval_required_at_or_above")
        or safety.get("approval_required_above")
        or "DANGEROUS"
    )
    try:
        threshold = Permission.parse(floor_name)
    except ValueError:
        console.print(f"[yellow]Invalid approval floor {floor_name!r}, defaulting to DANGEROUS.[/yellow]")
        threshold = Permission.DANGEROUS
    max_iters = int(cfg.get("safety", {}).get("max_iterations", 25))

    console.print(
        Panel.fit(
            f"[bold]Sera[/bold] · session {session.id} · workspace {Path(ws).name}\n"
            f"provider: {llm.name}/{getattr(llm, 'model', '?')} · "
            f"approval ≥ {threshold.name} · max iters {max_iters}\n"
            f"commands: 'exit' · ':search q' (cross-session) · ':hist q' (this session)",
            style="cyan",
        )
    )

    approval = CliApprovalGate()
    sink = _make_sink()

    asyncio.run(_repl(session, llm, sink, approval, threshold, max_iters))


def _make_sink() -> TokenSink:
    def on_text(t: str) -> None:
        sys.stdout.write(t)
        sys.stdout.flush()

    def on_tool_start(name: str, args: dict) -> None:
        console.print(f"\n[dim]→ {name}({_shorten_args(args)})[/dim]")

    def on_tool_end(name: str, result: str) -> None:
        first = result.splitlines()[0] if result else ""
        console.print(f"[dim]← {name}: {first[:120]}[/dim]")

    return TokenSink(on_text=on_text, on_tool_start=on_tool_start, on_tool_end=on_tool_end)


_SECRET_KEY_NAMES = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|bearer|authorization)"
)


def _shorten_args(args: dict) -> str:
    parts = []
    for k, v in list(args.items())[:3]:
        if _SECRET_KEY_NAMES.search(k):
            parts.append(f"{k}=<redacted>")
            continue
        # Value-side redaction: even if the key name looks innocent, the
        # value might be a literal `sk-…` or env-style assignment. Run the
        # shared redactor before display.
        if isinstance(v, str):
            v = redact(v)
        s = repr(v)
        if len(s) > 40:
            s = s[:37] + "…"
        parts.append(f"{k}={s}")
    out = ", ".join(parts)
    return out[:80] + ("…" if len(out) > 80 else "")


async def _repl(session, llm, sink, approval, threshold, max_iters) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    ps: PromptSession = PromptSession()
    while True:
        try:
            with patch_stdout():
                msg = await ps.prompt_async("\nyou › ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye.[/dim]")
            return
        msg = msg.strip()
        if not msg:
            continue
        if msg in {"exit", "quit", ":q"}:
            return
        if msg.startswith(":search ") or msg.startswith(":hist "):
            current_only = msg.startswith(":hist ")
            q = msg.split(" ", 1)[1]
            hits = session.search(q, current_only=current_only)
            scope = "current session" if current_only else "all sessions"
            if not hits:
                console.print(f"[dim]No matches for {q!r} in {scope}.[/dim]")
                continue
            console.print(f"[dim]{len(hits)} hits ({scope}):[/dim]")
            for role, snip in hits:
                console.print(f"[bold]{role}[/bold] {snip}")
            continue

        console.print("[bold green]sera ›[/bold green] ", end="")
        budget = IterationBudget.of(max_iters)
        token = InterruptToken()
        try:
            with install_sigint(token):
                await run_turn(
                    session,
                    msg,
                    llm,
                    sink=sink,
                    approval=approval,
                    approval_threshold=threshold,
                    budget=budget,
                    interrupt=token,
                )
        except Interrupted:
            console.print("\n[yellow][interrupted][/yellow]")
        except KeyboardInterrupt:
            # Second Ctrl+C during the turn — exit the REPL cleanly.
            console.print("\n[dim]bye.[/dim]")
            return
        except Exception as e:  # noqa: BLE001 — show error and continue REPL
            console.print(f"\n[red]Turn failed: {type(e).__name__}: {e}[/red]")


if __name__ == "__main__":
    main()
