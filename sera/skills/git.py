"""Git-tracked skill history.

Every skill edit lands as a git commit inside `<skills_dir>/.git`. Combined
with P-24's archive→revive contract, this gives total recall + undo: a
mistaken edit is `git revert HEAD`; an archived skill keeps its full edit
history alongside its archived flag.

Outclass: rivals overwrite skill text in place. Sera commits every change,
attributes the author (`curator`, `user`, custom), and surfaces the chain
via `sera skills log <name>`. The skills directory becomes an audit log.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class GitNotAvailable(RuntimeError):
    """Raised when the `git` CLI is not on PATH.

    Skill history is opt-in — every callsite catches this and either logs
    a warning (auto-commit path) or surfaces it to the CLI (explicit
    `sera skills log` / `commit` / `diff`).
    """


def _run_git(repo: Path, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run `git -C <repo> <args>`. Raises on non-zero exit by default."""
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=capture,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise GitNotAvailable("git CLI not on PATH") from e
    if cp.returncode != 0:
        # Surface git stderr in the exception — debugging history bugs
        # without it is pure pain.
        raise subprocess.CalledProcessError(
            cp.returncode, cp.args, output=cp.stdout, stderr=cp.stderr,
        )
    return cp


_GITIGNORE = (
    "# Sera skills repo — ignore lifecycle DB, locks, transient state.\n"
    ".lock\n"
)


def ensure_repo(skills_dir: Path) -> None:
    """Initialize `skills_dir` as a git repo if needed. Idempotent.

    Sets user.name + user.email so commits work in headless CI environments
    that don't have a global git identity. The author of any individual
    commit is overridden per-commit via `commit_skill_change(author=...)`.
    """
    skills_dir = Path(skills_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)
    if (skills_dir / ".git").is_dir():
        return
    _run_git(skills_dir, "init", "-q")
    _run_git(skills_dir, "config", "user.name", "sera")
    _run_git(skills_dir, "config", "user.email", "sera@local")
    gitignore = skills_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE)


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    when: float  # author timestamp, unix seconds
    author: str
    message: str


def commit_skill_change(
    skills_dir: Path,
    skill_name: str,
    message: str,
    *,
    author: str | None = None,
) -> CommitInfo | None:
    """Stage `<skills_dir>/<skill_name>/SKILL.md` and commit if there's a diff.

    Returns the new `CommitInfo`. Returns None when there's nothing to
    commit (idempotent re-commit). The `author` override lets the
    curator attribute its edits ("curator <curator@sera>") so user vs
    machine edits stay distinguishable in `skill_log`.
    """
    skills_dir = Path(skills_dir)
    ensure_repo(skills_dir)
    manifest = f"{skill_name}/SKILL.md"

    _run_git(skills_dir, "add", "--", manifest)

    # Detect staged diff. `git diff --cached --quiet` returns 0 when there
    # are NO changes staged, 1 when there ARE. Direct subprocess call —
    # `_run_git` raises on non-zero, which is the wrong contract here.
    raw = subprocess.run(
        ["git", "-C", str(skills_dir), "diff", "--cached", "--quiet", "--", manifest],
        capture_output=True,
    )
    if raw.returncode == 0:
        return None  # nothing staged → nothing to commit

    args = ["commit", "-q", "-m", message]
    if author:
        args[2:2] = ["--author", author]
    _run_git(skills_dir, *args)

    return skill_log(skills_dir, skill_name, limit=1)[0]


def skill_log(
    skills_dir: Path,
    skill_name: str,
    *,
    limit: int = 20,
) -> list[CommitInfo]:
    """Walk the manifest history. Newest first.

    Returns `[]` when no commits touch the named skill or the repo doesn't
    exist yet. Each line of `git log --pretty=format:%H%x00%at%x00%an <%ae>%x00%s`
    parses to one `CommitInfo`.
    """
    skills_dir = Path(skills_dir)
    if not (skills_dir / ".git").is_dir():
        return []
    manifest = f"{skill_name}/SKILL.md"
    try:
        cp = _run_git(
            skills_dir,
            "log",
            f"-n{int(limit)}",
            "--pretty=format:%H%x00%at%x00%an <%ae>%x00%s",
            "--",
            manifest,
        )
    except subprocess.CalledProcessError:
        return []
    out: list[CommitInfo] = []
    for line in (cp.stdout or "").splitlines():
        if not line:
            continue
        parts = line.split("\x00", 3)
        if len(parts) != 4:
            continue
        sha, ts, author, msg = parts
        out.append(
            CommitInfo(
                sha=sha,
                when=float(ts),
                author=author,
                message=msg,
            )
        )
    return out


def skill_diff(
    skills_dir: Path,
    skill_name: str,
    ref_a: str | None = None,
    ref_b: str | None = None,
) -> str:
    """Return `git diff ref_a..ref_b -- <skill>/SKILL.md`.

    Both refs omitted → `HEAD~1..HEAD` (latest edit). Single ref given →
    that ref vs working tree. Unknown skill / no history → empty string.
    """
    skills_dir = Path(skills_dir)
    if not (skills_dir / ".git").is_dir():
        return ""
    manifest = f"{skill_name}/SKILL.md"
    if ref_a is None and ref_b is None:
        # Default: latest edit. HEAD~1..HEAD is invalid on a single-commit
        # repo, so fall back to "everything in HEAD" if only one commit exists.
        log = skill_log(skills_dir, skill_name, limit=2)
        if len(log) < 2:
            return ""
        ref_a, ref_b = log[1].sha, log[0].sha
    elif ref_a is None:
        ref_a = "HEAD"
    elif ref_b is None:
        ref_b = "HEAD"
    try:
        cp = _run_git(skills_dir, "diff", f"{ref_a}..{ref_b}", "--", manifest)
    except subprocess.CalledProcessError:
        return ""
    return cp.stdout or ""
