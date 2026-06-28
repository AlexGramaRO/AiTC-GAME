#!/bin/bash
# Materialize (download) iCloud / evicted files without brctl download (removed on modern macOS).
# Used by pin-project-local.sh and run.sh offline mode.
#
# Strategy:
#   1. Set com.apple.fileprovider.pinned#PX (same as Finder "Keep Downloaded")
#   2. Read each file once so macOS pulls cloud-only content to disk

icloud_pin_xattr() {
    local path="$1"
    xattr -w com.apple.fileprovider.pinned#PX $'\x31' "$path" 2>/dev/null || \
    xattr -w com.apple.fileprovider.pinned $'\x31' "$path" 2>/dev/null || true
}

icloud_materialize_tree() {
    local root="$1"
    local count=0
    local failed=0

    if [ ! -d "$root" ]; then
        echo "❌ Not a directory: $root" >&2
        return 1
    fi

    echo "📌 Pinning project (Keep Downloaded)…" >&2
    icloud_pin_xattr "$root"

    echo "📥 Pulling files from iCloud to local disk (may take a few minutes)…" >&2
    echo "   $root" >&2

    while IFS= read -r -d '' f; do
        count=$((count + 1))
        if [ $((count % 250)) -eq 0 ]; then
            echo "   … $count files processed" >&2
        fi
        icloud_pin_xattr "$f"
        # Read one block — triggers download for evicted/dataless files without loading whole file.
        if ! dd if="$f" of=/dev/null bs=65536 count=1 2>/dev/null; then
            failed=$((failed + 1))
        fi
    done < <(find "$root" \
        \( -path '*/.git/*' -o -path '*/node_modules/*' \) -prune -o \
        -type f -print0 2>/dev/null)

    echo "" >&2
    if [ "$failed" -gt 0 ]; then
        echo "⚠️  Processed $count files; $failed could not be read yet." >&2
        echo "   Wait a minute and run this again, or use Finder → right-click folder → Download Now." >&2
        return 1
    fi

    echo "✅ Processed $count files — project should be available offline." >&2
    return 0
}
