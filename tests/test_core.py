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
    CruftGuardError,
    assert_cruft_json_usable,
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


# ── Edge-case tests ───────────────────────────────────────────────────────────

def test_empty_rej_file_is_noop(tmp_path):
    """An empty .rej file produces zero hunks; source is left untouched and .rej is removed."""
    source = tmp_path / "ci.yml"
    source.write_text("steps:\n  - run: pytest\n")
    rej = tmp_path / "ci.yml.rej"
    rej.write_text("")

    original = source.read_text()
    results = process_rej_files(tmp_path)

    assert len(results) == 1
    assert results[0].injected is True
    assert source.read_text() == original, "source must be unchanged when .rej is empty"
    assert not rej.exists(), ".rej should be cleaned up even when empty"
    print("✓ empty .rej file — clean no-op")


def test_malformed_rej_preserves_source(tmp_path):
    """A .rej missing @@ headers produces no hunks; source file is not corrupted."""
    source = tmp_path / "app.py"
    source.write_text("def main():\n    pass\n")
    rej = tmp_path / "app.py.rej"
    rej.write_text("this is not a unified diff at all\njust random text\n")

    original = source.read_text()
    results = process_rej_files(tmp_path)

    assert len(results) == 1
    assert source.read_text() == original, "malformed .rej must not corrupt source"
    print("✓ malformed .rej — source preserved, no corruption")


def test_binary_source_file_is_refused(tmp_path):
    """Injecting markers into a binary source file raises CruftGuardError
    and does not mutate the file."""
    source = tmp_path / "logo.png"
    binary_payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256 + b"\xff\xd8\xff"
    source.write_bytes(binary_payload)
    rej = tmp_path / "logo.png.rej"
    rej.write_text(SAMPLE_REJ)

    results = process_rej_files(tmp_path)

    assert len(results) == 1
    assert results[0].injected is False
    assert results[0].error is not None and "binary" in results[0].error.lower()
    assert source.read_bytes() == binary_payload, "binary file must not be modified"
    assert rej.exists(), ".rej must not be deleted when injection was refused"
    print("✓ binary source file — refused with clear error, file untouched")


def test_already_injected_source_not_double_injected(tmp_path):
    """A source file that already contains CRUFT-GUARD markers is not re-injected."""
    source = tmp_path / "ci.yml"
    pre_existing = (
        "steps:\n  - run: pytest\n\n"
        "<<<<<<< CRUFT-GUARD (hunk 1 — original lines 3-9)\n"
        "=======\n>>>>>>> TEMPLATE UPDATE\n"
    )
    source.write_text(pre_existing)
    rej = tmp_path / "ci.yml.rej"
    rej.write_text(SAMPLE_REJ)

    results = process_rej_files(tmp_path)

    assert len(results) == 1
    assert results[0].injected is False
    assert results[0].error is not None and "already contains" in results[0].error
    assert source.read_text() == pre_existing, "source with existing markers must not be mutated"
    assert rej.exists(), ".rej must not be deleted when injection was refused"
    print("✓ already-injected source — refused, no double injection")


def test_assert_cruft_json_missing(tmp_path):
    """Boundary helper raises a friendly error when .cruft.json is absent."""
    try:
        assert_cruft_json_usable(tmp_path)
    except CruftGuardError as e:
        assert "no .cruft.json" in str(e)
        print("✓ missing .cruft.json — friendly error raised")
        return
    raise AssertionError("expected CruftGuardError for missing .cruft.json")


def test_assert_cruft_json_malformed(tmp_path):
    """Boundary helper raises a friendly error when .cruft.json is unparseable."""
    (tmp_path / ".cruft.json").write_text("{ not json at all")
    try:
        assert_cruft_json_usable(tmp_path)
    except CruftGuardError as e:
        assert "could not parse" in str(e)
        print("✓ malformed .cruft.json — friendly error raised")
        return
    raise AssertionError("expected CruftGuardError for malformed .cruft.json")


def test_assert_cruft_json_missing_commit_key(tmp_path):
    """Boundary helper raises a friendly error when .cruft.json lacks `commit`."""
    (tmp_path / ".cruft.json").write_text(json.dumps({"template": "foo"}))
    try:
        assert_cruft_json_usable(tmp_path)
    except CruftGuardError as e:
        assert "commit" in str(e)
        print("✓ .cruft.json without commit key — friendly error raised")
        return
    raise AssertionError("expected CruftGuardError for missing commit key")


def test_dry_run_leaves_filesystem_untouched(tmp_path):
    """guard_update(dry_run=True) must not modify source, delete .rej, or rewrite .cruft.json."""
    cruft_json = tmp_path / ".cruft.json"
    make_cruft_json(cruft_json, commit="advanced_hash")
    source = tmp_path / "ci.yml"
    source.write_text("steps:\n  - run: pytest\n")
    rej = tmp_path / "ci.yml.rej"
    rej.write_text(SAMPLE_REJ)

    pre_source = source.read_text()
    pre_cruft_json = cruft_json.read_text()

    result = guard_update(tmp_path, extra_args=[], dry_run=True)

    assert result.dry_run is True
    assert result.conflicts, "dry-run must still report conflicts"
    assert source.read_text() == pre_source, "dry-run must not touch source file"
    assert rej.exists(), "dry-run must not delete .rej file"
    assert cruft_json.read_text() == pre_cruft_json, "dry-run must not rewrite .cruft.json"
    print("✓ dry-run — no filesystem mutations, conflicts still reported")


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
        # edge cases
        test_empty_rej_file_is_noop,
        test_malformed_rej_preserves_source,
        test_binary_source_file_is_refused,
        test_already_injected_source_not_double_injected,
        test_assert_cruft_json_missing,
        test_assert_cruft_json_malformed,
        test_assert_cruft_json_missing_commit_key,
        test_dry_run_leaves_filesystem_untouched,
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
