# cruft-guard

[![ci](https://github.com/nash226/Cruft-Guard/actions/workflows/ci.yml/badge.svg)](https://github.com/nash226/Cruft-Guard/actions/workflows/ci.yml)

> A trust layer for `cruft update`.
> `.cruft.json` only advances when the template patch actually landed.
> Invisible `.rej` files become CI-breaking conflict markers.

## The two guarantees

| state | means |
|---|---|
| `.cruft.json` says v1.7 | the v1.7 patch fully applied; no conflicts; the fix landed |
| `.cruft.json` still says v1.6 | a conflict exists; CI is failing; a human has been told |

The ambiguous middle state — *"claims v1.7 but the fix silently never landed"* — no longer exists. That state is the silent data loss vanilla cruft produces on every partial apply (see [CONTEXT.md](CONTEXT.md)).

---

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# unit tests (16 passed | 0 failed)
python tests/test_core.py

# full before/after demo
bash demo/run_demo.sh
```

The demo walks through a staged "post-failed-update" instance and shows cruft-guard (1) detecting the leftover `.rej` as a CI gate, then (2) injecting inline conflict markers and rolling back the `.cruft.json` hash. See [demo/](demo/) for details. Presenting this to a team? [DEMO.md](DEMO.md) has an 8–10 minute script with narration, commands, and anticipated Q&A.

---

## CLI reference

### `cruft-guard check`

Standalone CI gate. Exits non-zero if any `.rej` files exist. No side effects. Safe to run on every PR.

```bash
cruft-guard check [--repo PATH] [-v]
```

### `cruft-guard update`

Runs `cruft update`, then executes the full guard cycle. Exits non-zero on partial apply so CI blocks the merge.

```bash
cruft-guard update [--repo PATH] [--dry-run] [-v] [-- <extra cruft args>]
```

| flag | effect |
|---|---|
| `--repo PATH` | path to repo root. Defaults to `.`. Must contain `.cruft.json`. |
| `--dry-run` | report what *would* change — no files are modified, no `.rej` is deleted, `cruft update` is not invoked, `.cruft.json` is not rewritten. |
| `-v` / `--verbose` | enable DEBUG logging on stderr. INFO-level logs are emitted by default at every phase (snapshot hash, rej found, markers injected, hash rolled back). |
| `--skip-apply-ask` | forwarded to cruft for non-interactive CI runs. On by default. |

All `cruft update` flags can be passed after a `--` separator.

---

## If you cloned this repo

`bash demo/run_demo.sh` works out of the box after `pip install -e .` — it doesn't invoke `cruft`, it drives cruft-guard's post-processing against an already-staged failure state. Nothing else is required to see the before/after output.

Two caveats only matter if you want to go further and actually exercise `cruft update` against `demo/instance/`:

1. **`demo/template/` is not a git repo in this clone.** The original template had two commits (A and B) so cruft could track versions, but we flattened it to avoid nested-repo problems on GitHub. To make cruft happy:

   ```bash
   cd demo/template
   git init -q && git add -A && git commit -q -m "A"
   ```

2. **`demo/instance/.cruft.json` has an absolute path to the original author's machine.** Patch the two path fields to your clone:

   ```bash
   bash demo/setup.sh
   ```

Neither step is needed for `run_demo.sh`, the unit tests, or `cruft-guard check` / `cruft-guard update` against your own projects.

---

## Repo layout

```
cruft prototype/
├── cruft_guard/
│   ├── __init__.py
│   ├── core.py           — all logic (parsers, injectors, orchestration)
│   └── cli.py            — Click CLI: `update` and `check`
│
├── tests/
│   └── test_core.py      — 16 unit tests covering happy path + 8 edge cases
│
├── demo/                 — end-to-end walkthrough
│   ├── template/         — cookiecutter template (flattened — see above)
│   ├── instance/         — staged post-failure repo
│   ├── run_demo.sh       — BEFORE → check → fix → AFTER
│   ├── reset.sh          — restore instance to broken state
│   └── fix_with_cruft_guard.py
│
├── .github/workflows/
│   └── ci.yml            — runs tests on Python 3.10 / 3.11 / 3.12
│
├── pyproject.toml        — pip-installable; entry point `cruft-guard`
├── LICENSE               — MIT
├── CONTEXT.md            — problem statement + background
└── README.md             — this file
```

---

## Package internals

### `cruft_guard/core.py`

Pure logic. No CLI concerns beyond filesystem reads and a single `subprocess.run` of `cruft`.

| function | responsibility |
|---|---|
| `read_cruft_hash(repo_root)` | Read `commit` from `.cruft.json`. Returns `None` on any failure. |
| `write_cruft_hash(repo_root, commit, *, dry_run)` | Write `commit` back. Honours `dry_run`. |
| `assert_cruft_json_usable(repo_root)` | Boundary check. Raises `CruftGuardError` with a friendly message if `.cruft.json` is missing, unreadable, malformed, or missing `commit`. |
| `find_rej_files(repo_root)` | Recursive glob for `*.rej`. |
| `parse_rej_file(rej_path)` | Parse unified-diff hunks. ~30-line regex parser, no `unidiff` dependency. |
| `inject_conflict_markers(source, hunks, *, dry_run)` | Append labelled `<<<<<<< / ======= / >>>>>>>` blocks. Refuses binary files and already-injected sources. |
| `process_rej_files(repo_root, *, dry_run)` | Orchestrate find → parse → inject → delete. |
| `guard_update(repo_root, extra_args, *, dry_run)` | Full cycle: snapshot hash, run `cruft update`, post-process, rollback on conflict. |

All public functions return typed dataclasses (`Hunk`, `RejFile`, `ConflictResult`, `UpdateResult`) rather than raw tuples so callers — including future integrations — can reason about results structurally.

Errors that are *expected* and *user-actionable* raise `CruftGuardError`. Everything else bubbles as `Exception` and is caught at `process_rej_files` so one bad `.rej` can't abort the rest of the batch.

### `cruft_guard/cli.py`

Thin Click wrapper. Handles logging configuration, boundary checks, report printing, and exit codes. No business logic.

---

## Architectural decisions

### 1. Wrap `cruft`, don't fork it

cruft-guard invokes `cruft update` as a subprocess and reacts to its output. We don't patch cruft's internals or fork it.

**Why:** forking ties us to cruft's release cadence and forces us to carry patches forward. Wrapping means cruft-guard survives cruft upgrades with zero maintenance.

**Tradeoff:** we can't fix the *root cause* of `.rej` files (shallow-clone blob absence in `git apply --3way`). That belongs in cruft itself. We only surface the failure — we don't prevent it. This is deliberate: the trust-layer problem is worth solving independently of the root cause.

### 2. Append conflict markers at end-of-file, not inline at the hunk location

`inject_conflict_markers` appends a labelled `<<<<<<< / ======= / >>>>>>>` block at the **end** of the source file — it does not try to place it at the original hunk's line range (which the `.rej` file does record).

**Why:** mid-file surgery on structured content (YAML, JSON, TOML, Dockerfiles, lockfiles) risks producing output that still parses but is semantically wrong — or worse, output that parses differently depending on the surrounding lines we're inserting between. Appending to the end guarantees:

- The source file still parses as its original language (the markers may cause a syntax error, which is what we want — CI should fail).
- We never split a multi-line string, comment block, or structured literal.
- The hunk range is preserved in the marker header itself (`<<<<<<< CRUFT-GUARD (hunk 1 — original lines 1-9)`) so resolvers know where the change belongs.

**Tradeoff:** developers lose spatial context — the marker is physically distant from the code it conflicts with. Resolution is a little more manual than a native git conflict marker. We accepted this because the alternative (inline injection with language-aware heuristics) is a much larger project and one that a human fixer can still solve: they see the marker, they know what file, they read the hunk range from the marker header.

### 3. Roll back `.cruft.json` on *any* `.rej` presence

If `process_rej_files` finds even one `.rej`, we write the pre-update hash back to `.cruft.json` — regardless of whether other parts of the update applied cleanly.

**Why:** the failure we're fixing is the claim that the instance is *on* template version B when in reality some fraction of the patch never landed. A partial advance is exactly what we want to prevent. Rolling back to A preserves the invariant: "the recorded hash is either fully applied or not claimed."

**Tradeoff:** a single unparseable `.rej` on an otherwise-clean update fails the whole thing. That's the point — the two guarantees above are worthless if we leak any ambiguous middle state. The work already applied isn't lost from the working tree; it's just not *claimed* by `.cruft.json` until the conflict is resolved and the update re-run.

### 4. Hand-written unified-diff parser, no `patch` or `unidiff` dependency

`parse_rej_file` uses a single regex to find `@@ ... @@` headers and collects prefix-bearing lines (`+`, `-`, ` `, `\`). It doesn't use a full patch library.

**Why:** `.rej` files are an extremely restricted slice of the unified-diff format — just hunk headers and body lines, no file-rename syntax, no mode-change headers, no binary patch encoding. A 30-line parser handles them correctly; a full dependency is overkill and adds a supply-chain surface.

**Tradeoff:** the parser won't handle genuinely malformed `.rej` files gracefully (it silently yields zero hunks). If that ever becomes a real failure mode, swap in `unidiff` — the parser is isolated to one function so replacement is local. The malformed-`.rej` test (see edge-case matrix below) pins this as the expected behaviour: *do not corrupt the source*.

### 5. `check` and `update` as separate commands

**Why:** teams adopt this tool in two stages. Stage one: put `cruft-guard check` in CI to surface `.rej` files that were already being missed. Stage two: once teams trust the tool, switch their update jobs to `cruft-guard update` for the full rollback guarantee. Splitting the commands lets stage one land without any change to how updates are performed.

**Tradeoff:** two commands to document instead of one flag. Worth it for the adoption story.

### 6. Staged demo rather than forcing a real `--reject` fallback

The `demo/` directory stages a post-failure state rather than reproducing the shallow-clone-blob-absence failure that causes `git apply --3way` to fall back to `--reject`.

**Why:** the real failure mode requires either (a) a shallow clone with a missing blob, or (b) a malformed patch. Both are fiddly to reproduce deterministically on a developer laptop. What cruft-guard actually *does* is react to the state left behind — so staging that state is the honest demo.

**Tradeoff:** the demo elides the `cruft update` call itself and invokes the post-processing directly (`demo/fix_with_cruft_guard.py`). In production, the user runs `cruft-guard update` as a single command.

### 7. `CruftGuardError` + boundary check, not silent `None` returns

`read_cruft_hash` returns `None` on any failure. `assert_cruft_json_usable` is a separate boundary function that raises `CruftGuardError` with an actionable message.

**Why:** the two call sites want different things. Internal orchestration in `guard_update` wants "did we have a hash to roll back to?" — a nullable is fine. The CLI entry point wants "tell the human exactly what's wrong with their repo" — an exception with a message is right. Splitting them keeps the core nullable-friendly while giving the CLI a single `try/except CruftGuardError` layer.

**Tradeoff:** two ways of surfacing the same underlying condition. The duplication is deliberate — coupling them would force the core to carry user-facing error strings it doesn't need.

### 8. `--dry-run` threaded through, not implemented at the CLI

The `dry_run` parameter flows through `guard_update → process_rej_files → inject_conflict_markers → write_cruft_hash`. Each write-site becomes a no-op with a log line. `cruft update` itself is not invoked under dry-run.

**Why:** the alternative — checking `dry_run` only at the CLI and short-circuiting before any core call — would make dry-run a lie. It would skip the whole pipeline, including the `.rej` discovery and parsing that we specifically want to exercise to preview the effect. Threading the flag through lets dry-run execute the real logic, report the real conflicts, and only suppress the filesystem mutations.

**Tradeoff:** every future write-site in core has to remember to honour `dry_run`. We accept this because dry-run is a correctness feature (users rely on "what would happen" being accurate), not a convenience feature.

### 9. Refuse to inject into binary files

`inject_conflict_markers` checks for a NUL byte in the first 8 KB of the source file. If present, it raises `CruftGuardError` and leaves the file untouched; `process_rej_files` surfaces this as a failed `ConflictResult` and does not delete the `.rej`.

**Why:** a `.rej` next to a binary file means cruft tried to patch an image, font, or compiled artifact via unified diff. Appending text conflict markers to that file would corrupt it — and the markers wouldn't be useful to a human anyway, because you don't "resolve" a binary conflict by editing text.

**Tradeoff:** the NUL-byte heuristic has false negatives (UTF-16 text files contain NULs) and vanishingly rare false positives. We accept the heuristic because the cost of being wrong in either direction is low — a false positive surfaces a clear error; a false negative produces a corrupted binary that still has the `.rej` sitting next to it for the human to deal with.

### 10. Refuse to inject into a source that already has cruft-guard markers

If the source file already contains `<<<<<<< CRUFT-GUARD`, injection is refused and the `.rej` is not deleted. The user sees: *"this file already contains cruft-guard conflict markers; resolve them first."*

**Why:** double-injection is almost always a sign that a previous conflict was never resolved. Adding a second set of markers on top makes the file harder to understand, not easier. Failing loud here is the correct behaviour because the repo is in a state the human needs to see.

**Tradeoff:** we could dedupe per-hunk to make re-runs idempotent. We chose not to, because idempotency would mask exactly the signal ("you still haven't fixed the last conflict") that the failure is meant to produce.

### 11. Single module-level logger; CLI owns handler configuration

`core.py` uses `logging.getLogger("cruft_guard")` and emits `INFO` at each phase and `DEBUG` for per-file detail. The CLI attaches a stderr handler (with `[LEVEL] message` formatting) and flips to DEBUG under `-v`. Nothing in core touches handler configuration.

**Why:** this is the standard Python library pattern. Library code that configures handlers breaks callers (downstream integrations, test suites) that wanted to wire their own. Putting handler config at the CLI layer means `cruft_guard.core` is importable from any host process without log spam.

**Tradeoff:** the core emits logs that silently drop unless the caller configures a handler. That's the right failure mode — silent-by-default is friendlier than forcing log output on an embedder.

---

## Edge-case handling

Every case below has a dedicated unit test. What the tool does, concretely:

| input condition | behaviour |
|---|---|
| `.cruft.json` missing | `cruft-guard update` exits 2 with `not a cruft-managed repo (no .cruft.json at …)` |
| `.cruft.json` malformed JSON | exits 2 with `could not parse .cruft.json: <parser error>` |
| `.cruft.json` missing `commit` key | exits 2 with `.cruft.json is missing a commit key` |
| `cruft` binary not on PATH | raised as `CruftGuardError` with install instructions |
| Empty `.rej` file | no hunks; source untouched; `.rej` cleaned up |
| Malformed `.rej` (no `@@` headers) | no hunks parsed; source is not corrupted |
| Binary source file | injection refused; file untouched; `.rej` preserved |
| Source already contains `CRUFT-GUARD` markers | injection refused; file untouched; `.rej` preserved |
| Multiple nested `.rej` files | all discovered by `rglob`; each processed independently |
| Source file does not exist (new-in-template) | created, with the hunks as initial content |

Each of these is a real failure mode that existed in earlier iterations of the prototype. The test suite pins the current behaviour so regressions show up as red CI.

---

## Demo

The `demo/` directory contains a real cookiecutter template (`template/`, a git repo with two commits) and a real cruft-scaffolded instance (`instance/`, scaffolded at commit A via `cruft create`, then staged into a post-failure state).

**The staged state simulates what the repo looks like immediately after a failed `cruft update` on a CI runner:**

- `.cruft.json` has already been advanced to commit B (unconditional advancement)
- `app.py` is untouched (patch never applied — the file looks fine)
- `app.py.rej` sits next to it, containing the hunks that didn't land

```bash
bash demo/run_demo.sh    # walks BEFORE -> check gate -> post-processing -> AFTER
bash demo/reset.sh       # put the instance back in the broken state
```

Run `run_demo.sh` and you'll see:

1. **BEFORE** — `.cruft.json` claims B, source looks normal, `.rej` is the only signal.
2. **`cruft-guard check`** — exits non-zero, lists the `.rej` (this is the CI gate).
3. **Post-processing** — conflict markers appended to `app.py`, `.rej` deleted, `.cruft.json` rolled back to A.
4. **AFTER** — `.cruft.json` claims A again, `app.py` has `<<<<<<< CRUFT-GUARD` blocks, no `.rej` files.

---

## What's not done

**Hardening — next**
- Run the benchmark fleet from the research paper (four synthetic drift categories × three tools, aggregate report).

**Extensions — follow-up project**
- Generated-file detection (lockfiles: re-run the generator, don't merge).
- Fleet runner: run across many repos, produce an aggregated report.
- JSON output mode for dashboards.
- Rename / structural drift detection (content-similarity scoring).
- Multi-template instances: reconcile updates from N independent templates into one repo.

**Recently shipped (previously in this list)**
- `.github/workflows/ci.yml` — Python 3.10 / 3.11 / 3.12 matrix on every PR.
- Edge-case tests: binary files, empty `.rej`, malformed diffs, already-injected sources, missing / malformed / truncated `.cruft.json`.
- `--dry-run` flag on `cruft-guard update`.
- Structured logging (`cruft_guard` logger, INFO/DEBUG split, `-v` flag).
- Friendly errors at the CLI boundary via `CruftGuardError`.

---

## Deliberately out of scope

- **Generated-file reconciliation** (lockfiles, `go.sum`, `package-lock.json`). Requires file-type classification and re-running the generator. Separate problem.
- **Structural / rename drift.** Requires rename-aware diffing. Separate problem.
- **The shallow-clone root cause.** That's a cruft bug. cruft-guard makes its failure visible; the fix itself belongs upstream.
