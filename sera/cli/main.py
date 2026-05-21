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


@main.command(name="skills")
@click.option("--root", default=None, type=click.Path(path_type=Path),
              help="Skills directory. Defaults to ~/.sera/skills.")
def list_skills_cmd(root: Path | None) -> None:
    """Discover every skill manifest under `--root` and print a summary."""
    from sera.skills.loader import discover_skills

    target = (root or SKILLS_DIR).resolve()
    if not target.is_dir():
        console.print(f"[dim]No skills directory at {target}.[/dim]")
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
