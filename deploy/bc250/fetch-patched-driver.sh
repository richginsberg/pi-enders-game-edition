#!/usr/bin/env bash
# Fetch the BC-250-patched RADV driver into the build context so the Dockerfile can
# bake it in (making the image self-contained — no runtime host bind-mount).
#
# The patched libvulkan_radeon.so is a local rpmbuild of Mesa 24.1.5 with the BC-250
# chip (0x13FE / gfx1013 "Cyan Skillfish") mapped to NAVI10 — stock upstream Mesa
# reports it "unknown (143,132)" and winsys init fails. It's a ~11MB binary and is
# gitignored (never committed); supply it here before building.
#
# Usage:
#   ./fetch-patched-driver.sh <user>@<bc250-host>       # scp from a node that has it
# or place your own patched /usr/lib64/libvulkan_radeon.so at deploy/bc250/libvulkan_radeon.so
set -euo pipefail

DEST="$(dirname "$0")/libvulkan_radeon.so"
SRC_HOST="${1:-}"

if [ -z "$SRC_HOST" ]; then
    echo "usage: $0 <user>@<bc250-host>   (copies /usr/lib64/libvulkan_radeon.so here)" >&2
    exit 2
fi

scp "${SRC_HOST}:/usr/lib64/libvulkan_radeon.so" "$DEST"
echo "fetched -> $DEST ($(du -h "$DEST" | cut -f1))"
echo "verify it's the patched build: sha256sum $DEST  (patched != stock upstream)"
