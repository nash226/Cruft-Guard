#!/usr/bin/env bash
# End-to-end walkthrough of the cruft-guard failure scenario.
# Run from the repo root:  bash demo/run_demo.sh

set -e

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTANCE="$DEMO_DIR/instance"
COMMIT_A="459b6f460143d9073aad4f2a17bd9ac94f54e165"

hr()    { printf '%.0s-' {1..72}; printf '\n'; }
stage() { printf '\n'; hr; printf '== %s\n' "$*"; hr; }
show()  { printf '  %s\n' "$*"; }

stage "BEFORE — the silently-broken instance state"
show ".cruft.json claims (hash advanced to commit B):"
grep '"commit"' "$INSTANCE/.cruft.json" | sed 's/^/    /'
show ""
show "app.py content (looks completely normal — no markers, no errors):"
sed 's/^/    /' "$INSTANCE/app.py"
show ""
show "but there is a .rej file sitting next to it that no one will notice:"
ls "$INSTANCE"/*.rej | sed 's|.*/|    |'

stage "STEP 1 — cruft-guard check (standalone CI gate)"
set +e
cruft-guard check --repo "$INSTANCE"
GATE_EXIT=$?
set -e
show "exit code: $GATE_EXIT  (non-zero -> CI fails -> someone has to look)"

stage "STEP 2 — cruft-guard post-processing (inject markers + roll back hash)"
set +e
python "$DEMO_DIR/fix_with_cruft_guard.py" "$INSTANCE" "$COMMIT_A"
FIX_EXIT=$?
set -e
show "exit code: $FIX_EXIT  (non-zero -> CI fails on the newly-injected markers)"

stage "AFTER — the repo is now in a trustworthy state"
show ".cruft.json claims (rolled back to commit A):"
grep '"commit"' "$INSTANCE/.cruft.json" | sed 's/^/    /'
show ""
show "app.py now has inline conflict markers (CI will fail loudly on this):"
sed 's/^/    /' "$INSTANCE/app.py"
show ""
show ".rej files remaining:"
REJ=$(ls "$INSTANCE"/*.rej 2>/dev/null || true)
if [ -z "$REJ" ]; then show "    (none)"; else echo "$REJ" | sed 's|.*/|    |'; fi

printf '\n'
hr
printf 'run  bash %s/reset.sh  to put the instance back in the broken state\n' "$DEMO_DIR"
hr
