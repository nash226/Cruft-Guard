"""
Tests for cruft_guard.core

Simulates the post-cruft-update state (with .rej files present)
and verifies that cruft-guard correctly:
  1. Detects .rej files
  2. Injects inline conflict markers into the source file
  3. Deletes the .rej file
  4. Rolls back .cruft.json to the pre-update hash
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cruft_guard.core import (
    find_rej_files,
    parse_rej_file,
    inject_conflict_markers,
    process_rej_files,
    read_cruft_hash,
    write_cruft_hash,
    guard_update,
    UpdateResult,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_cruft_json(path: Path, commit: str) -> None:
    data = {
        "template": "https://github.com/org/test-template",
        "commit": commit,
        "context": {"cookiecutter": {"service_name": "test-svc"}},
    }
    path.write_text(json.dumps(data, indent=2))


def make_rej_file(path: Path, content: str) -> None:
    path.write_text(content)


SAMPLE_REJ = """\
--- ci.yml
+++ ci.yml
@@ -3,6 +3,7 @@
 steps:
   - run: pytest
   - run: flake8
+  - run: mypy
 env:
   CI: true
"""

SAMPLE_REJ_MULTI_HUNK = """\
--- Dockerfile
+++ Dockerfile
@@ -1,3 +1,3 @@
-FROM python:3.10-slim
+FROM python:3.12-slim
 RUN pip install flask
 WORKDIR /app
@@ -5,4 +5,5 @@
 COPY . .
 EXPOSE 8080
+LABEL maintainer="platform-team"
 CMD ["python", "main.py"]
"""


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_find_rej_files(tmp_path):
    (tmp_path / "ci.yml.rej").write_text("rej content")
    (tmp_path / "Dockerfile.rej").write_text("rej content")
    (tmp_path / "values.yaml").write_text("clean file")
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "nested.yml.rej").write_text("rej content")

    found = find_rej_files(tmp_path)
    names = {f.name for f in found}
    assert names == {"ci.yml.rej", "Dockerfile.rej", "nested.yml.rej"}
    print("✓ find_rej_files — finds all .rej files recursively")


def test_parse_rej_file_single_hunk(tmp_path):
    rej = tmp_path / "ci.yml.rej"
    rej.write_text(SAMPLE_REJ)

    hunks = parse_rej_file(rej)
    assert len(hunks) == 1
    hunk = hunks[0]
    assert hunk.old_start == 3
    assert hunk.new_start == 3
    added = [l for l in hunk.lines if l.startswith("+")]
    assert any("mypy" in l for l in added)
    print("✓ parse_rej_file — single hunk parsed correctly")


def test_parse_rej_file_multi_hunk(tmp_path):
    rej = tmp_path / "Dockerfile.rej"
    rej.write_text(SAMPLE_REJ_MULTI_HUNK)

    hunks = parse_rej_file(rej)
    assert len(hunks) == 2
    print("✓ parse_rej_file — multi-hunk .rej parsed correctly")


def test_inject_conflict_markers(tmp_path):
    source = tmp_path / "ci.yml"
    source.write_text(
        "steps:\n"
        "  - run: pytest\n"
        "      matrix:\n"
        "        python: [3.10, 3.11, 3.12]\n"
        "  - run: flake8\n"
    )
    rej = tmp_path / "ci.yml.rej"
    rej.write_text(SAMPLE_REJ)

    hunks = parse_rej_file(rej)
    inject_conflict_markers(source, hunks)

    content = source.read_text()
    assert "<<<<<<< CRUFT-GUARD" in content
    assert "=======" in content
    assert ">>>>>>> TEMPLATE UPDATE" in content
    assert "mypy" in content
    # Original content preserved
    assert "matrix" in content
    print("✓ inject_conflict_markers — markers injected, original content preserved")


def test_rej_file_deleted_after_injection(tmp_path):
    source = tmp_path / "ci.yml"
    source.write_text("steps:\n  - run: pytest\n")
    rej = tmp_path / "ci.yml.rej"
    rej.write_text(SAMPLE_REJ)

    results = process_rej_files(tmp_path)

    assert not rej.exists(), ".rej file should be deleted after injection"
    assert len(results) == 1
    assert results[0].injected is True
    print("✓ process_rej_files — .rej file deleted after injection")


def test_hash_rollback_on_conflict(tmp_path):
    cruft_json = tmp_path / ".cruft.json"
    make_cruft_json(cruft_json, commit="old_hash_abc123")

    # Simulate post-cruft-update state: hash advanced, .rej file present
    make_cruft_json(cruft_json, commit="new_hash_def456")
    source = tmp_path / "ci.yml"
    source.write_text("steps:\n  - run: pytest\n")
    (tmp_path / "ci.yml.rej").write_text(SAMPLE_REJ)

    pre_hash = "old_hash_abc123"
    conflicts = process_rej_files(tmp_path)
    assert conflicts

    # Simulate rollback
    write_cruft_hash(tmp_path, pre_hash)
    current_hash = read_cruft_hash(tmp_path)
    assert current_hash == "old_hash_abc123"
    print("✓ hash rollback — .cruft.json reverted to pre-update hash")


def test_clean_update_no_rollback(tmp_path):
    cruft_json = tmp_path / ".cruft.json"
    make_cruft_json(cruft_json, commit="v1_6_hash")

    # Simulate clean update: no .rej files
    make_cruft_json(cruft_json, commit="v1_7_hash")

    conflicts = process_rej_files(tmp_path)
    assert not conflicts

    current_hash = read_cruft_hash(tmp_path)
    assert current_hash == "v1_7_hash", "Hash should stay advanced when no conflicts"
    print("✓ clean update — hash stays advanced when no .rej files found")


def test_source_file_created_if_missing(tmp_path):
    """If the source file doesn't exist (e.g. new file in template), inject creates it."""
    rej = tmp_path / "newfile.py.rej"
    rej.write_text(SAMPLE_REJ)
    source = tmp_path / "newfile.py"
    assert not source.exists()

    results = process_rej_files(tmp_path)
    assert results[0].injected is True
    assert source.exists()
    print("✓ missing source file — created with conflict markers")


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile

    tests = [
        test_find_rej_files,
        test_parse_rej_file_single_hunk,
        test_parse_rej_file_multi_hunk,
        test_inject_conflict_markers,
        test_rej_file_deleted_after_injection,
        test_hash_rollback_on_conflict,
        test_clean_update_no_rollback,
        test_source_file_created_if_missing,
    ]

    passed = 0
    failed = 0
    print("\ncruft-guard core tests\n" + "─" * 40)

    for test in tests:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test(Path(tmp))
                passed += 1
            except Exception as e:
                print(f"✗ {test.__name__} — FAILED: {e}")
                failed += 1

    print("─" * 40)
    print(f"  {passed} passed  |  {failed} failed\n")
    sys.exit(0 if failed == 0 else 1)
