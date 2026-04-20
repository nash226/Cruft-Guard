#!/usr/bin/env bash
# Rewrite absolute template paths in demo/instance/.cruft.json to point at
# this clone's demo/template directory. Safe to run repeatedly.
set -e

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_PATH="$DEMO_DIR/template"
CRUFT_JSON="$DEMO_DIR/instance/.cruft.json"

python3 - <<EOF
import json, pathlib
p = pathlib.Path("$CRUFT_JSON")
d = json.loads(p.read_text())
d["template"] = "$TEMPLATE_PATH"
d["context"]["cookiecutter"]["_template"] = "$TEMPLATE_PATH"
p.write_text(json.dumps(d, indent=2) + "\n")
EOF

echo "patched $CRUFT_JSON -> template=$TEMPLATE_PATH"
