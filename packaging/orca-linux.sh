#!/usr/bin/env bash
# Launcher for the Orca Linux build. Runs the bundled server and opens the console.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CFG="${1:-$HERE/site.example.yaml}"
echo "Starting Orca server with config: $CFG"
"$HERE/orca-server" --config "$CFG" &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 2
URL="http://127.0.0.1:8088/"
( command -v xdg-open >/dev/null && xdg-open "$URL" ) 2>/dev/null || echo "Open $URL in your browser"
wait $SRV
