#!/usr/bin/env bash
# One-time setup, in one place — this is what NX Hub's "One more step" button
# runs. It opens a terminal (setup needs an interactive sudo password and
# downloads ~1 GB, neither of which works in a GUI-spawned shell) and walks
# through setup -> arm -> doctor.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Re-exec inside a terminal emulator when we have no tty (hub button case).
if [[ ! -t 0 ]]; then
    for t in konsole gnome-terminal alacritty kitty foot xfce4-terminal xterm; do
        command -v "$t" >/dev/null 2>&1 || continue
        case $t in
            gnome-terminal) exec "$t" -- bash -lc "'$0'" ;;
            *)              exec "$t" -e bash -lc "'$0'" ;;
        esac
    done
    echo "No terminal emulator found. Run this by hand:  $0" >&2
    exit 1
fi

cd "$HERE"
printf '\033[38;2;119;0;255m'
cat <<'BANNER'
  NX Android Streamer — first run
  setup (packages + android image) -> arm (ARM apps) -> doctor (network + adb)
  You will be asked for your password. This takes a while the first time.
BANNER
printf '\033[0m\n'

./start.sh setup
./start.sh arm
./start.sh doctor || true   # doctor is best-effort; video works without adb

echo
echo "Done. Launch from the hub, or run: ./start.sh gui"
read -rp "[enter] to close "
