#!/bin/sh
# Install locdu without pip: copy the package to ~/.local/share/locdu and put a launcher in ~/.local/bin.
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove both again
set -eu

here=$(cd "$(dirname "$0")" && pwd)
bin_dir="${BIN_DIR:-$HOME/.local/bin}"
lib_dir="${LIB_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/locdu}"
python="${PYTHON:-python3}"

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$bin_dir/locdu"
    rm -rf "$lib_dir"
    echo "removed $bin_dir/locdu and $lib_dir"
    exit 0
fi

command -v "$python" >/dev/null || { echo "locdu needs $python" >&2; exit 1; }
"$python" -c 'import curses' 2>/dev/null || { echo "locdu needs Python with curses" >&2; exit 1; }

rm -rf "$lib_dir"
mkdir -p "$lib_dir" "$bin_dir"
cp -R "$here/src/locdu" "$lib_dir/locdu"
find "$lib_dir" -name __pycache__ -prune -exec rm -rf {} +

cat > "$bin_dir/locdu" <<EOF
#!/bin/sh
PYTHONPATH="$lib_dir\${PYTHONPATH:+:\$PYTHONPATH}" exec "$(command -v "$python")" -m locdu "\$@"
EOF
chmod +x "$bin_dir/locdu"

echo "installed $bin_dir/locdu ($("$bin_dir/locdu" --version))"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "note: $bin_dir is not on your PATH" ;;
esac
