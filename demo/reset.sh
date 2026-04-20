#!/usr/bin/env bash
# Reset demo/instance back to the staged post-failure state.
set -e

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTANCE="$DEMO_DIR/instance"
COMMIT_B="71d5259400a240085808c01ae33f54549f64b0ed"

cat > "$INSTANCE/app.py" <<'EOF'
"""Main application entry point."""


def run():
    print("starting service")


if __name__ == "__main__":
    run()
EOF

cat > "$INSTANCE/app.py.rej" <<'EOF'
--- a/my-service/app.py
+++ b/my-service/app.py
@@ -1,9 +1,12 @@
 """Main application entry point."""
+import logging

+logging.basicConfig(level=logging.INFO)

 def run():
-    print("starting service")
+    logging.info("starting service")


 if __name__ == "__main__":
EOF

python - <<EOF
import json
p = "$INSTANCE/.cruft.json"
d = json.load(open(p))
d["commit"] = "$COMMIT_B"
open(p, "w").write(json.dumps(d, indent=2) + "\n")
EOF

echo "instance/ reset to the staged post-failure state (hash=B, .rej planted, app.py clean)"
