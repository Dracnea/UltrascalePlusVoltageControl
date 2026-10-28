#!/usr/bin/env bash
# Build upvc_<version>_all.deb with dpkg-deb. Nothing is installed.
#
#   packaging/deb/build-deb.sh [OUTDIR]        OUTDIR defaults to ./dist
#
# Layout: /opt/upvc/{host,rtl/prebuilt,boards/u55c,packaging/python/upvc,docs}
# (the same shape as a checkout, so the package finds host/ and the bridges the
# same way), /usr/bin/upvc, and the udev rule in /lib/udev/rules.d.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$(realpath -m "${1:-$ROOT/dist}")"
mkdir -p "$OUT"
LOG="$OUT/build-deb.log"
exec > >(tee -a "$LOG") 2>&1
echo "== build-deb $(date -u +%FT%TZ) =="

command -v dpkg-deb >/dev/null || { echo "build-deb: dpkg-deb not found" >&2; exit 1; }
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
[ -n "$VERSION" ] || { echo "build-deb: no version in pyproject.toml" >&2; exit 1; }
PKG="upvc_${VERSION}_all"

STAGE="$(mktemp -d "$OUT/stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
D="$STAGE/$PKG"
APP="$D/opt/upvc"
mkdir -p "$APP/host" "$APP/rtl/prebuilt" "$APP/boards/u55c" "$APP/packaging/python" \
         "$APP/docs" "$D/usr/bin" "$D/lib/udev/rules.d" "$D/DEBIAN" "$D/usr/share/doc/upvc"

# the bridges must match their checksums before they go into a package
( cd "$ROOT/rtl/prebuilt" && sha256sum -c SHA256SUMS )

cp "$ROOT"/host/*.py "$APP/host/"
cp "$ROOT"/rtl/prebuilt/*.bit.xz "$ROOT/rtl/prebuilt/SHA256SUMS" "$APP/rtl/prebuilt/"
cp "$ROOT"/boards/u55c/*.bit.xz "$APP/boards/u55c/"
cp -r "$ROOT/packaging/python/upvc" "$APP/packaging/python/"
cp "$ROOT"/docs/*.md "$APP/docs/"
cp "$ROOT/README.md" "$ROOT/LICENSE" "$APP/"
cp "$ROOT/LICENSE" "$D/usr/share/doc/upvc/copyright"
cp "$ROOT/packaging/udev/99-upvc-ftdi.rules" "$D/lib/udev/rules.d/"
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +

cat > "$D/usr/bin/upvc" <<'WRAP'
#!/bin/sh
PYTHONPATH="/opt/upvc/packaging/python${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m upvc "$@"
WRAP
chmod 0755 "$D/usr/bin/upvc"

SIZE="$(du -sk --exclude=DEBIAN "$D" | cut -f1)"
cat > "$D/DEBIAN/control" <<CTL
Package: upvc
Version: $VERSION
Architecture: all
Maintainer: UPVC maintainers <noreply@invalid>
Section: electronics
Priority: optional
Depends: python3 (>= 3.8), libftdi1-2
Installed-Size: $SIZE
Description: Set UltraScale+ FPGA card rails over an FTDI cable, no Vivado
 upvc reads a card's IDCODE over its FTDI JTAG cable, loads a small prebuilt
 bridge bitstream, and sets the core, BRAM and memory rails. Pure Python
 (standard library) on top of the system libftdi1. Vivado is not needed.
CTL
cat > "$D/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules || true
    udevadm trigger --subsystem-match=usb --attr-match=idVendor=0403 || true
fi
echo "upvc: add yourself to the plugdev group to use a cable without root:"
echo "      sudo usermod -aG plugdev \$USER   (then log out and in)"
exit 0
POST
cat > "$D/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if command -v udevadm >/dev/null 2>&1; then udevadm control --reload-rules || true; fi
exit 0
POSTRM
chmod 0755 "$D/DEBIAN/postinst" "$D/DEBIAN/postrm"
( cd "$D" && find . -path ./DEBIAN -prune -o -type f -print | sed 's|^\./||' | sort \
    | xargs md5sum > DEBIAN/md5sums )

find "$D" -type d -exec chmod 0755 {} +
find "$D/opt" "$D/lib" "$D/usr/share" -type f -exec chmod 0644 {} +
dpkg-deb --root-owner-group --build "$D" "$OUT/$PKG.deb"
echo "built $OUT/$PKG.deb"
