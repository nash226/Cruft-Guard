"""
cruft_guard.cli
~~~~~~~~~~~~~~~
CLI entry point for cruft-guard.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from .core import (
    CruftGuardError,
    UpdateResult,
    assert_cruft_json_usable,
    guard_update,
)


# ── Colours ───────────────────────────────────────────────────────────────────

def _green(s: str) -> str:  return click.style(s, fg="green",  bold=True)
def _red(s: str)   -> str:  return click.style(s, fg="red",    bold=True)
def _yellow(s: str)-> str:  return click.style(s, fg="yellow", bold=True)
def _cyan(s: str)  -> str:  return click.style(s, fg="cyan")
def _bold(s: str)  -> str:  return click.style(s, bold=True)
def _dim(s: str)   -> str:  return click.style(s, dim=True)


# ── Logging setup ─────────────────────────────────────────────────────────────

def _configure_logging(verbose: bool) -> None:
    """Wire the `cruft_guard` logger to stderr at INFO or DEBUG."""
    log = logging.getLogger("cruft_guard")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        log.addHandler(handler)


# ── Boundary error helper ─────────────────────────────────────────────────────

def _die(msg: str, code: int = 2) -> None:
    """Print a friendly error to stderr and exit with `code`."""
    click.echo(_red(f"✗ {msg}"), err=True)
    sys.exit(code)


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(result: UpdateResult, repo_root: Path) -> None:
    width = 60
    click.echo()
    click.echo(_bold("─" * width))
    header = "  cruft-guard report"
    if result.dry_run:
        header += _yellow("  [dry-run]")
    click.echo(_bold(header))
    click.echo(_bold("─" * width))

    # Cruft output (dimmed, for context)
    if result.cruft_output.strip():
        click.echo(_dim("\n[cruft output]"))
        for line in result.cruft_output.strip().splitlines():
            click.echo(_dim(f"  {line}"))
        click.echo()

    # Hash section
    click.echo(_bold("  Template hash"))
    click.echo(f"    before : {_cyan(result.pre_update_hash or 'unknown')}")

    if result.hash_rolled_back:
        click.echo(
            f"    after  : {_red(result.post_update_hash or 'unknown')} "
            f"{_red('→ rolled back')}"
        )
        click.echo(
            f"    current: {_yellow(result.pre_update_hash or 'unknown')} "
            f"{_yellow('(pre-update — conflicts must be resolved first)')}"
        )
    else:
        click.echo(f"    after  : {_green(result.post_update_hash or 'unknown')}")

    click.echo()

    # Conflict section
    if not result.conflicts:
        click.echo(_green("  ✓ Patch applied cleanly — no conflicts detected."))
        click.echo(_green("  ✓ .cruft.json is trustworthy."))
    else:
        click.echo(_red(f"  ✗ {len(result.conflicts)} conflict(s) detected:\n"))
        for c in result.conflicts:
            rel_source = c.source_path.relative_to(repo_root) if c.source_path.is_relative_to(repo_root) else c.source_path
            rel_rej    = c.rej_path.relative_to(repo_root)    if c.rej_path.is_relative_to(repo_root)    else c.rej_path

            if c.injected:
                preview_verb = "would inject" if result.dry_run else "conflict markers injected into source"
                click.echo(
                    f"    {_red('✗')} {_bold(str(rel_source))}\n"
                    f"      {_dim(str(rel_rej))} → {preview_verb}\n"
                    f"      {_yellow('→ resolve the <<<<<<< blocks, then re-run cruft-guard')}"
                )
            else:
                click.echo(
                    f"    {_red('!')} {_bold(str(rel_source))}\n"
                    f"      {_dim(str(rel_rej))} → injection failed: {c.error}"
                )
            click.echo()

        rollback_msg = (
            "  ✗ .cruft.json hash would be rolled back — not advanced until conflicts clear."
            if result.dry_run else
            "  ✗ .cruft.json hash rolled back — not advanced until conflicts clear."
        )
        click.echo(_red(rollback_msg))
        click.echo(_yellow("  → Fix the conflict markers above, then re-run: cruft-guard update"))

    click.echo(_bold("─" * width))
    click.echo()


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(package_name="cruft-guard")
def cli() -> None:
    """cruft-guard — a trustworthy wrapper around cruft update.

    Ensures .cruft.json only advances when a template patch fully applies.
    Converts invisible .rej files into inline conflict markers that CI will catch.
    """


@cli.command("update")
@click.option(
    "--repo",
    default=".",
    show_default=True,
    help="Path to the repository root (must contain .cruft.json).",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--skip-apply-ask",
    is_flag=True,
    default=True,
    help="Pass --skip-apply-ask to cruft (non-interactive mode).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview changes without modifying any files or invoking `cruft update`.",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    default=False,
    help="Enable DEBUG-level logging on stderr.",
)
@click.argument("cruft_args", nargs=-1)
def update_cmd(
    repo: Path,
    skip_apply_ask: bool,
    dry_run: bool,
    verbose: bool,
    cruft_args: tuple[str, ...],
) -> None:
    """Run cruft update with conflict detection and hash integrity enforcement.

    Any additional arguments are forwarded directly to cruft update.

    \b
    Examples:
      cruft-guard update
      cruft-guard update --repo ./my-service
      cruft-guard update --dry-run
      cruft-guard update -v -- --skip-apply-ask
    """
    _configure_logging(verbose)
    repo_root = repo.resolve()

    try:
        assert_cruft_json_usable(repo_root)
    except CruftGuardError as e:
        _die(str(e))

    extra: list[str] = []
    if skip_apply_ask:
        extra.append("--skip-apply-ask")
    extra.extend(cruft_args)

    prefix = "[dry-run] " if dry_run else ""
    click.echo(_bold(f"→ {prefix}Running cruft update in {repo_root} ..."))

    try:
        result = guard_update(
            repo_root=repo_root,
            extra_args=extra,
            dry_run=dry_run,
        )
    except CruftGuardError as e:
        _die(str(e))

    print_report(result, repo_root)

    # Exit non-zero if conflicts found — allows CI to gate on this
    if result.conflicts or result.cruft_exit_code != 0:
        sys.exit(1)


@cli.command("check")
@click.option(
    "--repo",
    default=".",
    show_default=True,
    help="Path to the repository root.",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    default=False,
    help="Enable DEBUG-level logging on stderr.",
)
def check_cmd(repo: Path, verbose: bool) -> None:
    """Check for leftover .rej files without running an update.

    Useful as a standalone CI gate to catch any .rej files that
    slipped through a previous cruft run.

    \b
    Examples:
      cruft-guard check
      cruft-guard check --repo ./my-service
    """
    _configure_logging(verbose)
    from .core import find_rej_files

    repo_root = repo.resolve()
    rej_files = find_rej_files(repo_root)

    if not rej_files:
        click.echo(_green("✓ No .rej files found — repo is clean."))
        sys.exit(0)
    else:
        click.echo(_red(f"✗ {len(rej_files)} .rej file(s) found:\n"))
        for r in rej_files:
            rel = r.relative_to(repo_root) if r.is_relative_to(repo_root) else r
            click.echo(f"  {_red('✗')} {rel}")
        click.echo()
        click.echo(_yellow("→ Run cruft-guard update to process these conflicts."))
        sys.exit(1)
