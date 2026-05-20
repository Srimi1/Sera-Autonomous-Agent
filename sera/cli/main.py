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
from sera.agent.loop import TokenSink, run_turn
from sera.config import CONFIG_PATH, SESSIONS_DB, load, save
from sera.llm.router import for_profile
from sera.llm.secrets import get_key, set_key
from sera.memory.session import Session
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
    """List recent sessions (most recently updated first)."""
    if not SESSIONS_DB.exists():
        console.print("[dim]No sessions yet.[/dim]")
        return
    with sqlite3.connect(SESSIONS_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, workspace, updated_at, "
            "(SELECT COUNT(*) FROM messages WHERE session_id = sessions.id) AS n "
            "FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        console.print("[dim]No sessions yet.[/dim]")
        return
    table = Table(title="Sessions")
    table.add_column("id", style="bold")
    table.add_column("updated")
    table.add_column("msgs", justify="right")
    table.add_column("workspace", overflow="fold")
    table.add_column("title")
    for r in rows:
        when = datetime.fromtimestamp(r["updated_at"]).strftime("%Y-%m-%d %H:%M")
        table.add_row(r["id"], when, str(r["n"]), r["workspace"] or "", r["title"] or "")
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
        try:
            await run_turn(
                session,
                msg,
                llm,
                sink=sink,
                approval=approval,
                approval_threshold=threshold,
                max_iterations=max_iters,
            )
        except Exception as e:  # noqa: BLE001 — show error and continue REPL
            console.print(f"\n[red]Turn failed: {type(e).__name__}: {e}[/red]")


if __name__ == "__main__":
    main()
