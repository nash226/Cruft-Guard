"""
cruft_guard.core
~~~~~~~~~~~~~~~~
Core logic for detecting, injecting, and reporting .rej conflicts
after a cruft update run.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


log = logging.getLogger("cruft_guard")


# ── Exceptions ────────────────────────────────────────────────────────────────

class CruftGuardError(Exception):
    """Raised for expected, user-actionable failures (bad state, bad input)."""


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class Hunk:
    """A single failed hunk parsed from a .rej file."""
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]  # raw lines including +/- prefix


@dataclass
class RejFile:
    """Represents a parsed .rej file and its corresponding source file."""
    rej_path: Path
    source_path: Path
    hunks: list[Hunk]


@dataclass
class ConflictResult:
    """Result of processing a single .rej file."""
    rej_path: Path
    source_path: Path
    injected: bool
    error: str | None = None


@dataclass
class UpdateResult:
    """Overall result of a cruft-guard update run."""
    pre_update_hash: str | None
    post_update_hash: str | None
    conflicts: list[ConflictResult] = field(default_factory=list)
    hash_rolled_back: bool = False
    cruft_exit_code: int = 0
    cruft_output: str = ""
    dry_run: bool = False

    @property
    def success(self) -> bool:
        return self.cruft_exit_code == 0 and not self.conflicts

    @property
    def partial(self) -> bool:
        return self.cruft_exit_code == 0 and bool(self.conflicts)


# ── .cruft.json helpers ────────────────────────────────────────────────────────

def read_cruft_hash(repo_root: Path) -> str | None:
    """Read the current commit hash from .cruft.json.

    Returns None when the file is missing, unreadable, malformed JSON,
    or missing the `commit` key. Callers that need to distinguish these
    cases should use `assert_cruft_json_usable` at the CLI boundary.
    """
    cruft_json = repo_root / ".cruft.json"
    if not cruft_json.exists():
        return None
    try:
        data = json.loads(cruft_json.read_text())
        return data.get("commit")
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def write_cruft_hash(repo_root: Path, commit: str, *, dry_run: bool = False) -> None:
    """Write a commit hash back into .cruft.json.

    When `dry_run=True`, log what would happen but do not modify the file.
    """
    cruft_json = repo_root / ".cruft.json"
    if dry_run:
        log.info("[dry-run] would write .cruft.json commit → %s", commit)
        return
    data = json.loads(cruft_json.read_text())
    data["commit"] = commit
    cruft_json.write_text(json.dumps(data, indent=2) + "\n")
    log.info("wrote .cruft.json commit → %s", commit)


def assert_cruft_json_usable(repo_root: Path) -> None:
    """Boundary check for the CLI. Raises CruftGuardError with a friendly
    message if .cruft.json is missing, unreadable, malformed, or lacks
    a `commit` key.
    """
    cruft_json = repo_root / ".cruft.json"
    if not cruft_json.exists():
        raise CruftGuardError(
            f"not a cruft-managed repo (no .cruft.json at {cruft_json})"
        )
    try:
        data = json.loads(cruft_json.read_text())
    except json.JSONDecodeError as e:
        raise CruftGuardError(f"could not parse .cruft.json: {e}") from e
    except OSError as e:
        raise CruftGuardError(f"could not read .cruft.json: {e}") from e
    if "commit" not in data:
        raise CruftGuardError(
            ".cruft.json is missing a `commit` key — file is malformed"
        )


# ── .rej file discovery ────────────────────────────────────────────────────────

def find_rej_files(repo_root: Path) -> list[Path]:
    """Recursively find all .rej files under repo_root."""
    return list(repo_root.rglob("*.rej"))


def source_path_for_rej(rej_path: Path) -> Path:
    """Derive the source file path from a .rej file path."""
    # Strip the .rej suffix to get the original file path
    return rej_path.with_suffix("")


# ── .rej parser ───────────────────────────────────────────────────────────────

def parse_rej_file(rej_path: Path) -> list[Hunk]:
    """
    Parse a unified-diff .rej file into a list of Hunk objects.

    .rej files produced by git apply --reject are standard unified diff
    fragments, each starting with a @@ line.
    """
    content = rej_path.read_text(errors="replace")
    hunks: list[Hunk] = []
    current_hunk: Hunk | None = None
    hunk_pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for line in content.splitlines(keepends=True):
        m = hunk_pattern.match(line)
        if m:
            if current_hunk:
                hunks.append(current_hunk)
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            current_hunk = Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=[],
            )
        elif current_hunk is not None and line.startswith(("+", "-", " ", "\\")):
            current_hunk.lines.append(line)

    if current_hunk:
        hunks.append(current_hunk)

    return hunks


# ── Conflict marker injection ──────────────────────────────────────────────────

CRUFT_GUARD_MARKER = "<<<<<<< CRUFT-GUARD"


def _looks_binary(data: bytes) -> bool:
    """Heuristic: a NUL byte in the first 8KB strongly suggests a binary file."""
    return b"\x00" in data[:8192]


def inject_conflict_markers(
    source_path: Path,
    hunks: list[Hunk],
    *,
    dry_run: bool = False,
) -> None:
    """
    Inject inline git-style conflict markers into source_path for each hunk.

    For each hunk we append a conflict block at the end of the file
    (safe for all file types — avoids corrupting structured files mid-parse)
    clearly labelled with the hunk's line range.

    Raises:
        CruftGuardError: if the source file already contains cruft-guard
            conflict markers (prevents double-injection) or is binary.
    """
    if not hunks:
        log.debug("no hunks to inject for %s", source_path)
        return

    if source_path.exists():
        raw = source_path.read_bytes()
        if _looks_binary(raw):
            raise CruftGuardError(
                f"refusing to inject markers into binary file {source_path}"
            )
        existing = raw.decode("utf-8", errors="replace")
    else:
        existing = ""

    if CRUFT_GUARD_MARKER in existing:
        raise CruftGuardError(
            f"{source_path} already contains cruft-guard conflict markers; "
            f"resolve them before re-running cruft-guard"
        )

    conflict_blocks: list[str] = []
    for i, hunk in enumerate(hunks, start=1):
        removed = [l[1:] for l in hunk.lines if l.startswith("-")]
        added   = [l[1:] for l in hunk.lines if l.startswith("+")]

        block = (
            f"\n{CRUFT_GUARD_MARKER} (hunk {i} — original lines {hunk.old_start}"
            f"-{hunk.old_start + hunk.old_count - 1})\n"
            + "".join(removed)
            + "=======\n"
            + "".join(added)
            + ">>>>>>> TEMPLATE UPDATE\n"
        )
        conflict_blocks.append(block)

    if dry_run:
        log.info(
            "[dry-run] would inject %d conflict block(s) into %s",
            len(conflict_blocks), source_path,
        )
        return

    updated = existing + "".join(conflict_blocks)
    source_path.write_text(updated)
    log.info(
        "injected %d conflict block(s) into %s",
        len(conflict_blocks), source_path,
    )


# ── Main orchestration ────────────────────────────────────────────────────────

def run_cruft_update(repo_root: Path, extra_args: list[str]) -> tuple[int, str]:
    """Run cruft update and return (exit_code, combined_output)."""
    cmd = ["cruft", "update"] + extra_args
    log.debug("invoking: %s (cwd=%s)", " ".join(cmd), repo_root)
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise CruftGuardError(
            "`cruft` executable not found on PATH — install it with `pip install cruft`"
        ) from e
    output = result.stdout + result.stderr
    return result.returncode, output


def process_rej_files(
    repo_root: Path,
    *,
    dry_run: bool = False,
) -> list[ConflictResult]:
    """Find, parse, inject, and delete all .rej files. Return results.

    When `dry_run=True`, no files are modified or deleted — the returned
    ConflictResults still describe what would have happened.
    """
    rej_paths = find_rej_files(repo_root)
    results: list[ConflictResult] = []

    log.info("found %d .rej file(s) under %s", len(rej_paths), repo_root)

    for rej_path in rej_paths:
        source_path = source_path_for_rej(rej_path)
        try:
            hunks = parse_rej_file(rej_path)
            inject_conflict_markers(source_path, hunks, dry_run=dry_run)
            if not dry_run:
                rej_path.unlink()
            results.append(ConflictResult(
                rej_path=rej_path,
                source_path=source_path,
                injected=True,
            ))
        except CruftGuardError as exc:
            log.warning("skipping %s: %s", rej_path, exc)
            results.append(ConflictResult(
                rej_path=rej_path,
                source_path=source_path,
                injected=False,
                error=str(exc),
            ))
        except Exception as exc:  # pragma: no cover — defensive
            log.error("unexpected error processing %s: %s", rej_path, exc)
            results.append(ConflictResult(
                rej_path=rej_path,
                source_path=source_path,
                injected=False,
                error=str(exc),
            ))

    return results


def guard_update(
    repo_root: Path,
    extra_args: list[str],
    *,
    dry_run: bool = False,
) -> UpdateResult:
    """
    Full cruft-guard update cycle:
      1. Snapshot pre-update hash
      2. Run cruft update  (skipped under dry_run)
      3. Scan for .rej files
      4. Inject conflict markers  (skipped under dry_run)
      5. Roll back hash if conflicts found  (skipped under dry_run)
    """
    pre_hash = read_cruft_hash(repo_root)
    log.info("pre-update hash: %s", pre_hash)

    if dry_run:
        log.info("[dry-run] skipping `cruft update` invocation")
        exit_code, output = 0, "[dry-run] cruft update not invoked"
    else:
        exit_code, output = run_cruft_update(repo_root, extra_args)

    post_hash = read_cruft_hash(repo_root)
    log.info("post-update hash: %s", post_hash)

    conflicts = process_rej_files(repo_root, dry_run=dry_run)

    rolled_back = False
    if conflicts and pre_hash:
        write_cruft_hash(repo_root, pre_hash, dry_run=dry_run)
        rolled_back = True

    return UpdateResult(
        pre_update_hash=pre_hash,
        post_update_hash=post_hash,
        conflicts=conflicts,
        hash_rolled_back=rolled_back,
        cruft_exit_code=exit_code,
        cruft_output=output,
        dry_run=dry_run,
    )
