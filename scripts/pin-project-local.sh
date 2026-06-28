#!/bin/bash
# Force-download the entire project folder from iCloud so it works offline.
# Run this once while you still have internet, before joining an offline LAN.
#
# Note: brctl download was removed on modern macOS (Sonoma 14.4+). This script uses
# pin xattrs + reading each file instead (same effect as Finder "Keep Downloaded").

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$SCRIPT_DIR/scripts/icloud-materialize.sh"

if [ ! -f "$HELPER" ]; then
    echo "❌ Missing helper: $HELPER"
    exit 1
fi

# shellcheck source=/dev/null
source "$HELPER"

echo "📥 Pinning project locally (may take a few minutes)..."
echo "   $SCRIPT_DIR"
echo ""

if ! icloud_materialize_tree "$SCRIPT_DIR"; then
    echo ""
    echo "Manual fallback:"
    echo "  Finder → right-click the project folder → Download Now"
    echo "  Or move the project out of iCloud Desktop, e.g.:"
    echo "    cp -R \"$SCRIPT_DIR\" \"\$HOME/Projects/AiTC-RAMP-CONTROL\""
    exit 1
fi

echo ""
echo "Tip: For the most reliable offline use, keep a copy outside iCloud Desktop:"
echo "  ~/Projects/AiTC-RAMP-CONTROL"
echo ""
echo "Then run ./run.sh from that folder on your offline Wi‑Fi network."
