"""
Invokes cruft-guard's post-processing directly on a repo that is already in a
post-failed-update state. Simulates what guard_update() does after cruft itself
has run — used by the demo so we don't re-invoke cruft (which would re-apply the
patch cleanly and muddy the before/after picture).

Usage:
    python fix_with_cruft_guard.py <repo-path> <pre-update-hash>
"""

import sys
from pathlib import Path

from cruft_guard.core import (
    process_rej_files,
    read_cruft_hash,
    write_cruft_hash,
)


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <repo-path> <pre-update-hash>")
        return 2

    repo = Path(sys.argv[1])
    pre_hash = sys.argv[2]
    post_hash = read_cruft_hash(repo)

    print(f"  pre-update hash  (snapshotted before cruft ran): {pre_hash[:12]}")
    print(f"  post-update hash (currently in .cruft.json):     {post_hash[:12] if post_hash else 'MISSING'}")

    conflicts = process_rej_files(repo)

    if not conflicts:
        print("  no .rej files found — nothing to do")
        return 0

    for c in conflicts:
        marker = "OK" if c.injected else "FAIL"
        print(f"  [{marker}] injected markers into {c.source_path.name}, deleted {c.rej_path.name}")

    write_cruft_hash(repo, pre_hash)
    final_hash = read_cruft_hash(repo)
    print(f"  rolled back .cruft.json: {post_hash[:12]} -> {final_hash[:12]}")

    return 1


if __name__ == "__main__":
    sys.exit(main())
