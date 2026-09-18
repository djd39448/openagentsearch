#!/usr/bin/env bash
# Regenerates the flop-session-router-provider journey evidence from scratch.
#
# Run from the repository root as:
#   bash integrations/flop-session-router/journey/reproduce.sh
#
# (chmod +x is not meaningful from a Windows checkout; invoke it via `bash` as above.)
set -euo pipefail

ROUTER_COMMIT="dba6525554c4ea5965ef6dd23e93194736aa0ef3"
VENDOR_DIR="vendor/flop-session-router"

if [ ! -d "$VENDOR_DIR/.git" ]; then
  git clone https://github.com/retardio73-boop/flop-session-router "$VENDOR_DIR"
fi
git -C "$VENDOR_DIR" checkout "$ROUTER_COMMIT"

(cd "$VENDOR_DIR" && npm ci --ignore-scripts && npm run build)
(cd integrations/flop-session-router && npm ci --ignore-scripts && npm run check && node journey/run.mjs)

node -e "
const j = require('./integrations/flop-session-router/journey/out/journey.json');
console.log('ok=' + j.ok);
for (const c of j.checks) {
  console.log((c.ok ? 'ok' : 'FAIL') + ' - ' + c.step);
}
"
