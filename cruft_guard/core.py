"""
cruft_guard.core
~~~~~~~~~~~~~~~~
Core logic for detecting, injecting, and reporting .rej conflicts
after a cruft update run.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


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

    @property
    def success(self) -> bool:
        return self.cruft_exit_code == 0 and not self.conflicts

    @property
    def partial(self) -> bool:
        return self.cruft_exit_code == 0 and bool(self.conflicts)


# ── .cruft.json helpers ────────────────────────────────────────────────────────

def read_cruft_hash(repo_root: Path) -> str | None:
    """Read the current commit hash from .cruft.json."""
    cruft_json = repo_root / ".cruft.json"
    if not cruft_json.exists():
        return None
    try:
        data = json.loads(cruft_json.read_text())
        return data.get("commit")
    except (json.JSONDecodeError, KeyError):
        return None


def write_cruft_hash(repo_root: Path, commit: str) -> None:
    """Write a commit hash back into .cruft.json."""
    cruft_json = repo_root / ".cruft.json"
    data = json.loads(cruft_json.read_text())
    data["commit"] = commit
    cruft_json.write_text(json.dumps(data, indent=2) + "\n")


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

def inject_conflict_markers(source_path: Path, hunks: list[Hunk]) -> None:
    """
    Inject inline git-style conflict markers into source_path for each hunk.

    For each hunk we append a conflict block at the end of the file
    (safe for all file types — avoids corrupting structured files mid-parse)
    clearly labelled with the hunk's line range.
    """
    existing = source_path.read_text(errors="replace") if source_path.exists() else ""

    conflict_blocks: list[str] = []
    for i, hunk in enumerate(hunks, start=1):
        removed = [l[1:] for l in hunk.lines if l.startswith("-")]
        added   = [l[1:] for l in hunk.lines if l.startswith("+")]

        block = (
            f"\n<<<<<<< CRUFT-GUARD (hunk {i} — original lines {hunk.old_start}"
            f"-{hunk.old_start + hunk.old_count - 1})\n"
            + "".join(removed)
            + "=======\n"
            + "".join(added)
            + ">>>>>>> TEMPLATE UPDATE\n"
        )
        conflict_blocks.append(block)

    updated = existing + "".join(conflict_blocks)
    source_path.write_text(updated)


# ── Main orchestration ────────────────────────────────────────────────────────

def run_cruft_update(repo_root: Path, extra_args: list[str]) -> tuple[int, str]:
    """Run cruft update and return (exit_code, combined_output)."""
    cmd = ["cruft", "update"] + extra_args
    result = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    return result.returncode, output


def process_rej_files(repo_root: Path) -> list[ConflictResult]:
    """Find, parse, inject, and delete all .rej files. Return results."""
    rej_paths = find_rej_files(repo_root)
    results: list[ConflictResult] = []

    for rej_path in rej_paths:
        source_path = source_path_for_rej(rej_path)
        try:
            hunks = parse_rej_file(rej_path)
            inject_conflict_markers(source_path, hunks)
            rej_path.unlink()
            results.append(ConflictResult(
                rej_path=rej_path,
                source_path=source_path,
                injected=True,
            ))
        except Exception as exc:
            results.append(ConflictResult(
                rej_path=rej_path,
                source_path=source_path,
                injected=False,
                error=str(exc),
            ))

    return results


def guard_update(repo_root: Path, extra_args: list[str]) -> UpdateResult:
    """
    Full cruft-guard update cycle:
      1. Snapshot pre-update hash
      2. Run cruft update
      3. Scan for .rej files
      4. Inject conflict markers
      5. Roll back hash if conflicts found
    """
    pre_hash = read_cruft_hash(repo_root)

    exit_code, output = run_cruft_update(repo_root, extra_args)

    post_hash = read_cruft_hash(repo_root)

    conflicts = process_rej_files(repo_root)

    rolled_back = False
    if conflicts and pre_hash:
        write_cruft_hash(repo_root, pre_hash)
        rolled_back = True

    return UpdateResult(
        pre_update_hash=pre_hash,
        post_update_hash=post_hash,
        conflicts=conflicts,
        hash_rolled_back=rolled_back,
        cruft_exit_code=exit_code,
        cruft_output=output,
    )
