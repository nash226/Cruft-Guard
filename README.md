# cruft-guard

A trustworthy wrapper around `cruft update`. Enforces that `.cruft.json`'s recorded
template hash only advances when the template patch has *actually* been applied to
the instance — and converts invisible `.rej` files into inline conflict markers
that CI will fail on.

For the full problem statement (day-2 drift, silent data loss at the metadata layer,
the `.rej` fallback failure mode), read [CONTEXT.md](CONTEXT.md) first. This README
covers the repo layout, the architectural decisions, and the tradeoffs we made.

---

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# unit tests
python tests/test_core.py        # -> 8 passed | 0 failed

# full before/after demo
bash demo/run_demo.sh
```

The demo walks through a staged "post-failed-update" instance and shows
cruft-guard (1) detecting the leftover `.rej` as a CI gate, then (2) injecting
inline conflict markers and rolling back the `.cruft.json` hash. See
[demo/](demo/) for details.

Presenting this to a team? [DEMO.md](DEMO.md) has an 8–10 minute script with
narration, commands, and anticipated Q&A.

---

## If you cloned this repo

`bash demo/run_demo.sh` works out of the box after `pip install -e .` — it doesn't
invoke `cruft`, it drives cruft-guard's post-processing against an already-staged
failure state. Nothing else is required to see the before/after output.

Two caveats only matter if you want to go further and actually exercise
`cruft update` against `demo/instance/`:

1. **`demo/template/` is not a git repo in this clone.** The original
   template had two commits (A and B) so cruft could track versions, but we
   flattened it to avoid nested-repo problems on GitHub. To make cruft happy:

   ```bash
   cd demo/template
   git init -q && git add -A && git commit -q -m "A"
   ```

   (You won't get commit B back — it was the "audit logging" update. If you
   want the full two-commit history, re-create it by hand or just edit
   `app.py` and commit again.)

2. **`demo/instance/.cruft.json` has an absolute path to the original
   author's machine** in its `template` and `context.cookiecutter._template`
   fields. Patch both to the location of your clone:

   ```bash
   bash demo/setup.sh
   ```

   (Safe to re-run. Rewrites the two path fields to point at this clone's
   `demo/template` directory.)

Neither step is needed for `run_demo.sh`, the unit tests, or
`cruft-guard check` / `cruft-guard update` against your own projects.

---

## Repo layout

```
cruft prototype/
|
|-- cruft_guard/               the installable package
|   |-- __init__.py
|   |-- core.py                all the logic — no CLI concerns
|   +-- cli.py                 Click CLI: `cruft-guard update` and `check`
|
|-- tests/
|   +-- test_core.py           8 unit tests for the core primitives
|
|-- demo/                      end-to-end walkthrough (see demo/ section below)
|   |-- template/              a real cookiecutter template, git repo, 2 commits
|   |-- instance/              scaffolded from template at commit A,
|   |                          staged into a post-failure state
|   |-- run_demo.sh            walks through BEFORE -> check -> fix -> AFTER
|   |-- reset.sh               restores instance/ to the staged broken state
|   +-- fix_with_cruft_guard.py  invokes post-processing without re-running cruft
|
|-- pyproject.toml             pip-installable; declares `cruft-guard` entry point
|-- CONTEXT.md                 problem statement + background
+-- README.md                  this file
```

---

## Package internals

### `cruft_guard/core.py`

Contains every unit of logic, with no CLI or I/O concerns beyond filesystem reads.
Each function is small and composable so the tests can exercise the primitives
directly without a full end-to-end setup.

| Function | Responsibility |
|---|---|
| `read_cruft_hash(repo_root)` | Read `commit` from `.cruft.json`. |
| `write_cruft_hash(repo_root, commit)` | Write `commit` back to `.cruft.json`. |
| `find_rej_files(repo_root)` | Recursive glob for `*.rej`. |
| `parse_rej_file(rej_path)` | Parse unified-diff hunks from a `.rej`. |
| `inject_conflict_markers(source_path, hunks)` | Append `<<<<<<< / ======= / >>>>>>>` blocks to the source file. |
| `process_rej_files(repo_root)` | Orchestrate find -> parse -> inject -> delete. |
| `guard_update(repo_root, extra_args)` | Full cycle: snapshot hash, run `cruft update`, post-process, rollback on conflict. |

The core returns typed dataclasses (`Hunk`, `RejFile`, `ConflictResult`,
`UpdateResult`) rather than raw tuples so the CLI — and any future integrations —
can reason about results structurally.

### `cruft_guard/cli.py`

Thin Click wrapper exposing two commands:

- **`cruft-guard check --repo PATH`** — standalone CI gate. Scans for `.rej`
  files. Exits non-zero if any are found. No side effects. Safe to run on every
  PR as a pre-merge hook.
- **`cruft-guard update --repo PATH`** — runs `cruft update`, then executes the
  full guard cycle. Exits non-zero if the update was partial (any `.rej` files
  surfaced), so CI blocks the merge.

Keeping these separate matters: a lot of teams will want the cheap `check` gate
enabled globally before they're ready to have cruft-guard run `cruft update` for
them in CI.

---

## Architectural decisions

### 1. Wrap `cruft`, don't fork it

cruft-guard invokes `cruft update` as a subprocess and reacts to its output.
We don't patch cruft's internals or fork it.

**Why:** forking ties us to cruft's release cadence and forces us to carry patches
forward. Wrapping means cruft-guard survives cruft upgrades with zero maintenance.

**Tradeoff:** we can't fix the *root cause* of `.rej` files (shallow-clone blob
absence in `git apply --3way`). That belongs in cruft itself. We only surface
the failure — we don't prevent it. This is deliberate: the trust-layer problem
is worth solving independently of the root cause.

### 2. Append conflict markers at end-of-file, not inline at the hunk location

`inject_conflict_markers` appends a labelled `<<<<<<< / ======= / >>>>>>>` block
at the **end** of the source file — it does not try to place it at the original
hunk's line range (which the `.rej` file does record).

**Why:** mid-file surgery on structured content (YAML, JSON, TOML, Dockerfiles,
lockfiles) risks producing output that still parses but is semantically wrong —
or worse, output that parses differently depending on the surrounding lines
we're inserting between. Appending to the end guarantees:
- The source file still parses as its original language (the markers may cause
  a syntax error, which is what we want — CI should fail).
- We never split a multi-line string, comment block, or structured literal.
- The hunk range is preserved in the marker header itself
  (`<<<<<<< CRUFT-GUARD (hunk 1 — original lines 1-9)`) so resolvers know where
  the change belongs.

**Tradeoff:** developers lose spatial context — the marker is physically distant
from the code it conflicts with. Resolution is a little more manual than a
native git conflict marker. We accepted this because the alternative (inline
injection with language-aware heuristics) is a much larger project and one that
a human fixer can still solve: they see the marker, they know what file, they
read the hunk range from the marker header.

### 3. Roll back `.cruft.json` on *any* `.rej` presence

If `process_rej_files` finds even one `.rej`, we write the pre-update hash back
to `.cruft.json` — regardless of whether other parts of the update applied cleanly.

**Why:** the failure we're fixing is the claim that the instance is *on* template
version B when in reality some fraction of the patch never landed. A partial
advance is exactly what we want to prevent. Rolling back to A preserves the
invariant: "the recorded hash is either fully applied or not claimed".

**Tradeoff:** a single unparseable `.rej` on an otherwise-clean update fails the
whole thing. That's the point — the two guarantees in CONTEXT.md are worthless
if we leak any ambiguous middle state. The work already applied isn't lost from
the working tree; it's just not *claimed* by `.cruft.json` until the conflict
is resolved and the update re-run.

### 4. Hand-written unified-diff parser, no `patch` or `unidiff` dependency

`parse_rej_file` uses a single regex to find `@@ ... @@` headers and collects
prefix-bearing lines (`+`, `-`, ` `, `\`). It doesn't use a full patch library.

**Why:** `.rej` files are an extremely restricted slice of the unified-diff format
— just hunk headers and body lines, no file-rename syntax, no mode-change
headers, no binary patch encoding. A 30-line parser handles them correctly; a
full dependency is overkill and adds a supply-chain surface.

**Tradeoff:** the parser won't handle genuinely malformed `.rej` files gracefully
(it may silently skip lines). If that ever becomes a real failure mode, swap in
`unidiff` — the parser is isolated to one function so replacement is local.

### 5. `check` and `update` as separate commands

**Why:** teams adopt this tool in two stages. Stage one: put `cruft-guard check`
in CI to surface `.rej` files that were already being missed. Stage two: once
teams trust the tool, switch their update jobs to `cruft-guard update` for the
full rollback guarantee. Splitting the commands lets stage one land without any
change to how updates are performed.

**Tradeoff:** two commands to document instead of one flag. Worth it for the
adoption story.

### 6. Staged demo rather than forcing a real `--reject` fallback

The `demo/` directory stages a post-failure state rather than reproducing the
shallow-clone-blob-absence failure that causes `git apply --3way` to fall back
to `--reject`.

**Why:** the real failure mode requires either (a) a shallow clone with a
missing blob, or (b) a malformed patch. Both are fiddly to reproduce
deterministically on a developer laptop. What cruft-guard actually *does* is
react to the state left behind — so staging that state is the honest demo.

**Tradeoff:** the demo elides the `cruft update` call itself and invokes the
post-processing directly (`demo/fix_with_cruft_guard.py`). In production, the
user runs `cruft-guard update` as a single command.

---

## Demo

The `demo/` directory contains a real cookiecutter template (`template/`, a git
repo with two commits) and a real cruft-scaffolded instance (`instance/`,
scaffolded at commit A via `cruft create`, then staged into a post-failure
state).

**The staged state simulates what the repo looks like immediately after a
failed `cruft update` on a CI runner:**

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

From [CONTEXT.md](CONTEXT.md):

**Hardening**
- CI workflow (`.github/workflows/ci.yml`) — run tests on every PR
- Edge-case tests: binary files, empty `.rej` files, malformed diffs, missing source file
- `--dry-run` flag
- Structured logging
- Friendly errors when `cruft` isn't installed or `.cruft.json` is missing

**Extensions**
- Generated-file detection (lockfiles: re-run the generator, don't merge)
- Fleet runner: run across many repos, produce an aggregated report
- JSON output mode for dashboards
- Rename / structural drift detection (content-similarity scoring)

---

## Deliberately out of scope

- **Generated-file reconciliation** (lockfiles, `go.sum`, `package-lock.json`). Requires file-type classification and re-running the generator. Separate problem.
- **Structural / rename drift.** Requires rename-aware diffing. Separate problem.
- **The shallow-clone root cause.** That's a cruft bug. cruft-guard makes its failure visible; the fix itself belongs upstream.
