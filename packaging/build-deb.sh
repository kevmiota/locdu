#!/bin/sh
# Build locdu_<version>_all.deb into OUT_DIR (default: ./deb). Needs only dpkg-deb.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
out="${1:-$here/deb}"
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$here/src/locdu/__init__.py")
[ -n "$version" ] || { echo "no __version__ in src/locdu/__init__.py" >&2; exit 1; }

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
pkg="$stage/locdu_${version}_all"
mkdir -p "$pkg/DEBIAN" "$pkg/usr/bin" "$pkg/usr/lib/python3/dist-packages" "$pkg/usr/share/doc/locdu"

cp -R "$here/src/locdu" "$pkg/usr/lib/python3/dist-packages/"
find "$pkg" -name __pycache__ -prune -exec rm -rf {} +

cat > "$pkg/usr/bin/locdu" <<'EOF'
#!/usr/bin/python3
import sys

from locdu.cli import main

sys.exit(main())
EOF

{
    echo "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"
    echo "Upstream-Name: locdu"
    echo "Source: https://github.com/kevmiota/locdu"
    echo
    echo "Files: *"
    echo "License: MIT"
    echo
    sed 's/^$/./; s/^/ /' "$here/LICENSE"
} > "$pkg/usr/share/doc/locdu/copyright"

cat > "$pkg/DEBIAN/control" <<EOF
Package: locdu
Version: $version
Architecture: all
Maintainer: Kevin Müller <91332197+kevmiota@users.noreply.github.com>
Depends: python3 (>= 3.10)
Section: utils
Priority: optional
Homepage: https://github.com/kevmiota/locdu
Description: ncdu for lines of code
 Scans a directory, counts the lines of every text file and lets you
 browse the tree interactively in the terminal, largest first.
EOF

# Byte-compile on install and clean up on removal, as Debian's Python packages do;
# otherwise a `sudo locdu` leaves a root-owned __pycache__ that `apt remove` keeps.
cat > "$pkg/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && command -v py3compile >/dev/null 2>&1; then
    py3compile -p locdu
fi
EOF
cat > "$pkg/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if command -v py3clean >/dev/null 2>&1; then
    py3clean -p locdu
fi
EOF

find "$pkg" -type d -exec chmod 755 {} +
find "$pkg" -type f -exec chmod 644 {} +
chmod 755 "$pkg/usr/bin/locdu" "$pkg/DEBIAN/postinst" "$pkg/DEBIAN/prerm"

mkdir -p "$out"
dpkg-deb --root-owner-group --build "$pkg" "$out/" >/dev/null
echo "$out/locdu_${version}_all.deb"
