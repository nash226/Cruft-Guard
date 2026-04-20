# cruft-guard — Project Context

This file exists to onboard a new Claude session (or human) to the cruft-guard project.
Read this before touching any code.

---

## What Problem We Are Solving

### Background — Platform Engineering & Golden Path Templates

Large engineering organisations use **Golden Path templates** to standardise how services
are created. A platform team maintains a template (e.g. a Cookiecutter/Cruft template on
GitHub). Developer teams scaffold new services from that template. The scaffolded output
is called an **instance**.

The moment a service is scaffolded, the instance and the template begin to diverge
independently:
- The platform team ships improvements to the template (security patches, new CI steps,
  updated base images)
- Developer teams edit their instance to fit their specific needs

This is called **day-2 drift** — and it is the core unsolved problem in platform engineering
at mid-to-large scale (roughly 50–500 services).

### What Cruft Is

**Cruft** is the most widely adopted tool for keeping live instances in sync with their
templates after initial scaffolding. Its core algorithm is clever:

1. Read `.cruft.json` to recover the template URL, pinned commit, and saved variable context
2. Clone the template at the pinned commit ("old template") and at HEAD ("new template")
3. Run Cookiecutter over each using the saved context → produces `old_rendered` and `new_rendered`
4. Diff `old_rendered` vs `new_rendered` → this is purely the platform team's changes
5. Apply that diff onto the live repo using `git apply --3way` with `old_rendered` as the merge base
6. If `--3way` fails → fall back to `git apply --reject` (writes `.rej` files)
7. Advance `.cruft.json` commit hash to new template HEAD

The reconstruction of `old_rendered` solves the hardest part of the problem — there is no
shared git history between a template and an instance, so Cruft synthesises the common
ancestor rather than finding it.

### Where Cruft Falls Over — The Two Failures We Are Fixing

**Failure 1 — Unconditional Hash Advancement**

Step 7 always advances `.cruft.json` to the new template HEAD — even when the patch only
partially applied and `.rej` files were written. This means:

- `.cruft.json` says the instance is on template `v1.7`
- In reality, 4 of 7 template changes are sitting in unread `.rej` files
- A security fix that the platform team shipped is reported as delivered when it was not
- The platform team's fleet dashboard shows a green tick on a service that is still vulnerable

This is **silent data loss at the metadata layer**.

**Failure 2 — Invisible `.rej` Files**

When `git apply --3way` fails (most commonly on CI runners doing shallow clones, where
the required blob is absent from git's object store), Cruft silently falls back to
`git apply --reject`. This writes `.rej` files *next to* the source files rather than
*inside* them.

Unlike inline conflict markers (which break the file, fail CI, and force resolution),
`.rej` files:
- Leave the source file looking completely normal
- Do not break CI
- Are easy to miss and frequently merged without being read

**How the failures compound:**

```
Shallow clone on CI → three-way merge fails → silent fallback to --reject
→ .rej files written → source file looks fine → CI passes → PR merged
→ hash advances unconditionally → .cruft.json says v1.7
→ platform dashboard shows ✅ → security fix was never applied
```

### The Engineering Problem In One Sentence

The system cannot distinguish between "fully applied" and "partially applied" —
so its reporting is structurally unreliable and platform teams have false confidence
across their fleet.

---

## What We Built

A Python CLI tool called **cruft-guard** that wraps `cruft update` and enforces two
guarantees before allowing `.cruft.json` to advance.

### The Two Guarantees

| `.cruft.json` says v1.7 | Patch fully applied. No conflicts. Fix landed. |
|-------------------------|------------------------------------------------|
| `.cruft.json` still says v1.6 | Conflict exists. CI is failing. Someone knows. |

The ambiguous middle state — *"claims v1.7 but fix never landed"* — no longer exists.

### How It Works

```
cruft update runs
      ↓
snapshot pre-update hash from .cruft.json
      ↓
scan repo for .rej files
      ↓
.rej files found?
  YES → parse each .rej hunk (unified diff format)
      → inject inline conflict markers into the source file
      → delete the .rej file
      → roll back .cruft.json to pre-update hash
      → exit non-zero (CI fails, someone has to fix it)
  NO  → .cruft.json stays advanced
      → exit zero (CI passes, fix is genuinely delivered)
```

### Project Structure

```
cruft-guard/
├── README.md                  ← user-facing docs and usage
├── CONTEXT.md                 ← this file
├── pyproject.toml             ← pip installable, entry point: cruft-guard
│
├── cruft_guard/
│   ├── __init__.py
│   ├── core.py                ← all logic: find/parse/inject/rollback
│   └── cli.py                 ← Click CLI: `cruft-guard update` and `cruft-guard check`
│
├── tests/
│   └── test_core.py           ← 8 unit tests, all passing
│
└── docs/
    ├── problem_statement.pdf  ← full problem/solution writeup
    └── cruft_algorithm.pdf    ← breakdown of Cruft's update algorithm
```

### Key Modules

**`cruft_guard/core.py`**

Contains all the logic with no CLI concerns:
- `read_cruft_hash(repo_root)` — reads commit from `.cruft.json`
- `write_cruft_hash(repo_root, commit)` — writes commit back to `.cruft.json`
- `find_rej_files(repo_root)` — recursively finds all `.rej` files
- `parse_rej_file(rej_path)` — parses unified diff hunks from a `.rej` file
- `inject_conflict_markers(source_path, hunks)` — injects `<<<<<<< / ======= / >>>>>>>` blocks
- `process_rej_files(repo_root)` — orchestrates find → parse → inject → delete
- `guard_update(repo_root, extra_args)` — full update cycle, returns `UpdateResult`

**`cruft_guard/cli.py`**

Two commands built with Click:
- `cruft-guard update [--repo PATH]` — runs the full guard cycle
- `cruft-guard check [--repo PATH]` — standalone CI gate, scans for leftover `.rej` files

### Installation

```bash
git clone https://github.com/your-org/cruft-guard
cd cruft-guard
pip install -e .
cruft-guard --help
```

### Running Tests

```bash
python tests/test_core.py
# Expected: 8 passed | 0 failed
```

---

## What Is Done

- [x] Core logic written (`core.py`)
- [x] CLI entry point (`cli.py`) with `update` and `check` commands
- [x] Installable via pip (`pyproject.toml`)
- [x] 8 unit tests passing
- [x] README written
- [x] Problem statement documented (PDF)

---

## What Is Not Done Yet

These are the known next steps in rough priority order:

**Hardening (do first)**
- [ ] CI workflow (`.github/workflows/ci.yml`) — run tests on every PR
- [ ] Edge case tests — binary files, empty `.rej` files, malformed diffs, missing source file
- [ ] `--dry-run` flag — preview what would change without modifying anything
- [ ] Proper logging — structured output showing exactly what happened at each step
- [ ] Handle cruft not being installed — friendly error rather than crash
- [ ] Handle repos with no `.cruft.json` more gracefully

**Extensions (do after hardening)**
- [ ] Generated file detection — declare lockfiles in a manifest, skip merging them,
      re-run the generator tool after merge (`poetry lock`, `go mod tidy`, `npm install`)
- [ ] Fleet runner — run cruft-guard across multiple repos and produce an aggregated report
- [ ] JSON output mode — machine-readable report for dashboard consumption
- [ ] Rename/structural drift detection — content-similarity scoring to detect renamed files

---

## What We Deliberately Did Not Solve

cruft-guard addresses the **trust layer** only. It does not attempt to fix:

- **Generated file reconciliation** (lockfiles, go.sum, package-lock.json) — these require
  file-type classification and re-running the generator tool. Out of scope for this iteration.
- **Structural / rename drift** — files that moved or were renamed on either side of the
  template/instance divide. Requires rename-aware diffing. Out of scope.
- **The shallow clone root cause** — the missing blob that causes the three-way merge to
  fail is unchanged. cruft-guard ensures this failure surfaces visibly (CI fails) rather
  than silently (false green). The root cause fix belongs in cruft itself.

---

## Key Concepts To Know

**Day-0 / Day-1 / Day-2**
- Day-0: designing and provisioning something new
- Day-1: deploying and configuring it for the first time
- Day-2: everything after it's live — maintenance, patches, updates. Never ends.
  Templates are built for Day-0. cruft-guard exists because Day-2 has no good answer.

**Three-way merge**
The algorithm git uses when merging branches. Needs three inputs: BASE (common ancestor),
OURS (our changes), THEIRS (their changes). Can auto-resolve when only one side changed
a chunk. Fails with a conflict when both sides changed the same chunk differently.
Cruft reconstructs the BASE by re-rendering the old template — that's the clever part.

**Fleet**
The entire collection of service instances across the organisation that were scaffolded
from templates. At 50–500 services, manual reconciliation is impossible and codemod
infrastructure is too expensive to build. That's the gap cruft-guard targets.

**`.cruft.json`**
The metadata file Cruft writes into every scaffolded repo. Contains the template URL,
the pinned commit hash (which version of the template this instance was scaffolded/last
synced at), and the saved variable context (the answers given at scaffolding time).
This file is the trust signal cruft-guard is protecting.
