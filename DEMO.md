# Demo script

An 8–10 minute presenter's script for showing cruft-guard to a team. Open this
in one window, a terminal in another. Narration is in quote blocks; commands
are in code blocks.

---

## Pre-demo setup (30 seconds, before your audience joins)

```bash
cd "<path-to-repo>/cruft prototype"
source .venv/bin/activate
bash demo/reset.sh        # guarantees clean starting state
clear
```

Bump terminal font size. Close unrelated windows. Have this file open on a
second screen if you need it.

---

## 1. The problem (2 minutes, no terminal)

> We use Cruft to keep services in sync with our Golden Path templates. When a
> security fix lands in the template, we bump every downstream service by
> running `cruft update`. Our compliance dashboard tracks the commit hash in
> each service's `.cruft.json` — if it matches the template's latest, we call
> that service patched.
>
> There are two ways this lies to us.
>
> First, `cruft` advances the hash in `.cruft.json` even when the patch only
> partially applied.
>
> Second, when `git apply --3way` can't find a blob — common on shallow CI
> clones — cruft silently falls back to `git apply --reject`, which writes a
> `.rej` file next to the source file instead of inline conflict markers.
>
> The source looks untouched. CI is green. PR merges. The fix never landed.
> The dashboard says patched. We are wrong.

---

## 2. Show the failure state on disk (1 minute)

```bash
ls demo/instance/
cat demo/instance/.cruft.json
cat demo/instance/app.py
cat demo/instance/app.py.rej
```

Point out, in order:

- `.cruft.json` claims commit `71d52594…` — the template's latest, "audit
  logging added."
- `app.py` has no logging. The patch did not land.
- `app.py.rej` sits there with the unapplied hunks — invisible to anyone not
  specifically looking for it.

> If this were a real repo and I ran `git diff` on the source file, I would
> see nothing. CI does not know to grep for `.rej` files. This PR merges.

---

## 3. Run the demo (2 minutes)

```bash
bash demo/run_demo.sh
```

The script prints three sections. Narrate as each appears:

- **BEFORE** — the same broken state you just showed.
- **`cruft-guard check`** —
  > This is the cheap CI gate. It exits non-zero. Any team can drop this into
  > their pipeline today without changing how updates are performed.
- **AFTER** —
  > `.cruft.json` is rolled back to the previous commit. `app.py` now has
  > `<<<<<<< CRUFT-GUARD` markers appended. The `.rej` file is gone. CI will
  > now fail on the syntax error. The silent-success case is impossible.

---

## 4. State the guarantee (1 minute)

> cruft-guard enforces one invariant: if `.cruft.json` claims commit X, the
> patch for commit X actually applied to this instance. Any ambiguous middle
> state is eliminated. Either the hash is rolled back, or the source has a
> conflict marker that CI will catch. You cannot silently end up somewhere in
> between.

---

## 5. Show the code, briefly (1 minute, optional)

```bash
wc -l cruft_guard/*.py tests/test_core.py
```

> Around 200 lines of core logic, 8 unit tests. cruft-guard wraps `cruft` as a
> subprocess — it does not fork cruft, does not patch cruft's internals. It
> survives cruft upgrades with zero maintenance.

If asked for detail, open `cruft_guard/core.py` and point at:
- `guard_update` — the full cycle (snapshot → run → detect → rollback).
- `inject_conflict_markers` — append markers at EOF, never mid-file.
- `process_rej_files` — the orchestrator.

---

## 6. What's deliberately out of scope (30 seconds)

> cruft-guard does not fix the root cause — the shallow-clone blob-absence
> bug that makes `git apply --3way` fall back to `--reject`. That belongs in
> `cruft` itself. We do not reconcile generated files or lockfiles. We do not
> detect renames or structural drift. This is a trust layer on top of
> `cruft update`, not a replacement for it.

---

## Anticipated questions

**Why not just grep for `.rej` files in CI?**
That is exactly what `cruft-guard check` does — it is the lightweight gate.
The harder guarantee is the rollback. Without rewinding `.cruft.json`, a
later compliance scan would still think the service is on the new version.

**Why append conflict markers at end-of-file instead of inline at the hunk
location?**
Mid-file injection into structured content (YAML, JSON, TOML, Dockerfiles,
lockfiles) risks producing output that still parses but is semantically
wrong. End-of-file guarantees the file becomes invalid and CI notices. The
hunk's original line range is preserved in the marker header so a human
resolver knows where the change belongs.

**Does this work on Windows?**
Not tested. Only uses `pathlib` and standard regex, so it should.

**What about `cruft diff`?**
`cruft diff` tells you what *would* change. It does not enforce that the
change *did* happen. That is the gap cruft-guard closes.

**What if the `.rej` file is malformed?**
The parser is deliberately minimal — it will skip unparseable hunks rather
than fail. If this becomes a real issue, the parser is isolated to one
function and can be swapped for the `unidiff` library.

**What about legitimate conflicts where the template change should not apply?**
cruft-guard surfaces the conflict; a human resolves it. After resolution,
re-running `cruft-guard update` advances `.cruft.json` to the new commit
cleanly.

---

## If something breaks live

```bash
bash demo/reset.sh && bash demo/run_demo.sh
```

The demo state is fully regenerable — nothing persistent on disk outside of
`demo/instance/` itself.

---

## Follow-ups to offer at the end

- "I can stand this up as a `cruft-guard check` gate on one repo this week,
  no behavior change to updates."
- "Stage two is flipping the update job from `cruft update` to
  `cruft-guard update` for the full rollback guarantee."
- "CONTEXT.md has the longer-form problem statement if you want to read
  async."
