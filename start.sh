#!/usr/bin/env bash
# nx-android-streamer — starter script
# Brings up a headless Waydroid session at phone geometry, ready for a streamer.
# Subcommands: setup | arm | up | serve | gui | stream | doctor | down | status | ref
set -euo pipefail

W=${NXAS_WIDTH:-1080}
H=${NXAS_HEIGHT:-2400}
HZ=${NXAS_HZ:-90}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Repo-local scratch (clones, logs) vs machine-wide session state. The state
# must be shared: a hub-installed copy and a git checkout are different ROOTs
# but there is only ONE headless session on the machine — keeping pid/display
# here is what stops copy #2 from starting a second sway on top of the first.
RUN="$ROOT/.run"
STATE="${XDG_RUNTIME_DIR:-/tmp}/nx-android-streamer"
mkdir -p "$RUN" "$STATE"

PROP=/var/lib/waydroid/waydroid_base.prop

log() { printf '\033[38;2;119;0;255m[nxas]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[nxas]\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- setup ----
cmd_setup() {
    log "checking kernel binder support..."
    if zgrep -q 'CONFIG_ANDROID_BINDER_IPC=y' /proc/config.gz 2>/dev/null; then
        log "  binder: built-in ✓"
    else
        die "kernel lacks CONFIG_ANDROID_BINDER_IPC — install a binder-enabled kernel (cachyos/zen) first"
    fi

    local pkgs=()
    need waydroid    || pkgs+=(waydroid)
    need sway        || pkgs+=(sway)
    need wf-recorder || pkgs+=(wf-recorder)
    need grim        || pkgs+=(grim)
    need vainfo      || pkgs+=(libva-utils)
    need adb         || pkgs+=(android-tools)      # touch injection reaches android over adb
    need socat       || pkgs+=(socat)              # doctor's netns adb bridge fallback
    [[ -f /usr/share/scrcpy/scrcpy-server ]] || pkgs+=(scrcpy)  # its server jar, control-only
    # probe actual elements, not gst-launch — the core can be present while
    # the plugin packages that matter (rtph264pay, vah264enc) are not
    gst-inspect-1.0 rtph264pay &>/dev/null || pkgs+=(gst-plugins-good)
    gst-inspect-1.0 webrtcbin  &>/dev/null || pkgs+=(gst-plugins-bad)
    gst-inspect-1.0 vah264enc  &>/dev/null || pkgs+=(gst-plugin-va)
    gst-inspect-1.0 pipewiresrc &>/dev/null || pkgs+=(gst-plugin-pipewire)
    pacman -Q gst-plugins-ugly &>/dev/null || pkgs+=(gst-plugins-ugly)   # x264enc software fallback
    python -c 'import aiohttp' 2>/dev/null || pkgs+=(python-aiohttp)
    python -c 'import evdev'   2>/dev/null || pkgs+=(python-evdev)
    python -c 'import PySide6' 2>/dev/null || pkgs+=(pyside6)        # tray gui
    python -c 'import qrcode'  2>/dev/null || pkgs+=(python-qrcode)  # tray gui QR
    if ((${#pkgs[@]})); then
        log "installing: ${pkgs[*]}"
        sudo pacman -S --needed "${pkgs[@]}" || die "pacman failed — AUR-only package? try paru -S ${pkgs[*]}"
    else
        log "  packages: all present ✓"
    fi

    if [[ ! -f /var/lib/waydroid/waydroid.cfg ]]; then
        log "initializing waydroid with GAPPS image (downloads ~1 GB)..."
        sudo waydroid init -s GAPPS
    else
        log "  waydroid: already initialized ✓"
    fi
    # waydroid init has been observed to exit 0 with the vendor image missing —
    # verify the download actually completed before letting setup "succeed".
    [[ -f /var/lib/waydroid/images/system.img && -f /var/lib/waydroid/images/vendor.img ]] \
        || die "waydroid images incomplete — re-run: sudo waydroid init -s GAPPS"

    log "pinning phone geometry ${W}x${H} in waydroid_base.prop..."
    for kv in "persist.waydroid.width=$W" \
              "persist.waydroid.height=$H" \
              "persist.waydroid.multi_windows=false"; do
        key=${kv%%=*}
        if sudo grep -q "^${key}=" "$PROP" 2>/dev/null; then
            sudo sed -i "s|^${key}=.*|${kv}|" "$PROP"
        else
            echo "$kv" | sudo tee -a "$PROP" >/dev/null
        fi
    done

    sudo systemctl enable --now waydroid-container.service
    log "setup done. next: ./start.sh arm   (ARM app translation), then ./start.sh up"
}

# ------------------------------------------------------------------ arm ----
cmd_arm() {
    # ARM translation + Widevine via casualsnek/waydroid_script (GPL-3.0).
    # Used as a tool, not vendored — see BORROWED.md.
    local ws="$RUN/waydroid_script"
    if [[ ! -d $ws ]]; then
        log "cloning casualsnek/waydroid_script..."
        git clone --depth 1 https://github.com/casualsnek/waydroid_script "$ws"
    fi
    cd "$ws"
    python -m venv venv 2>/dev/null || true
    ./venv/bin/pip install -q -r requirements.txt
    log "installing libndk (ARM translation for AMD) + widevine..."
    sudo ./venv/bin/python main.py install libndk widevine
    log "done. For Play Store login, also run:"
    log "  sudo $ws/venv/bin/python main.py certified"
    log "and register the printed android_id at https://www.google.com/android/uncertified"
}

# ------------------------------------------------------------------- up ----
cmd_up() {
    # Launched from a GUI (hub / .desktop) there is no tty, so a sudo prompt
    # would hang forever with nothing on screen. Ask politely, then explain.
    if ! systemctl is-active -q waydroid-container.service; then
        if sudo -n true 2>/dev/null || [[ -t 0 ]]; then
            sudo systemctl start waydroid-container.service
        elif need pkexec; then
            pkexec systemctl start waydroid-container.service \
                || die "couldn't start waydroid-container (polkit declined)"
        else
            die "waydroid-container isn't running and this shell can't ask for a password — run: sudo systemctl start waydroid-container.service"
        fi
    fi

    if [[ -f $STATE/sway.pid ]] && kill -0 "$(cat "$STATE/sway.pid")" 2>/dev/null; then
        log "already up (sway pid $(cat "$STATE/sway.pid"), display $(cat "$STATE/wayland-display" 2>/dev/null || echo '?'))"
        return
    fi

    cat > "$STATE/sway.conf" <<EOF
# generated by start.sh — headless phone-geometry session for waydroid
output HEADLESS-1 mode --custom ${W}x${H}@${HZ}Hz
default_border none
xwayland disable
exec waydroid show-full-ui
EOF

    log "starting headless sway (${W}x${H}@${HZ})..."
    SOCKS_BEFORE=$(cd "${XDG_RUNTIME_DIR:?}" && ls wayland-* 2>/dev/null | grep -v '\.lock$' | sort || true)
    # Pure headless: the libinput backend needs a seat (logind/seatd) and dies
    # without one. We don't need it — touch is injected inside Android itself
    # via the scrcpy control protocol, so sway is capture-only.
    WLR_BACKENDS=headless \
        sway -c "$STATE/sway.conf" >"$STATE/sway.log" 2>&1 &
    echo $! > "$STATE/sway.pid"

    # sway doesn't reliably log its socket name — detect it as the wayland-*
    # socket that appears in XDG_RUNTIME_DIR after launch.
    local disp=""
    for _ in $(seq 1 50); do
        disp=$(comm -13 <(sort <<<"$SOCKS_BEFORE") \
                        <(cd "${XDG_RUNTIME_DIR:?}" && ls wayland-* 2>/dev/null | grep -v '\.lock$' | sort) | head -1)
        [[ -n $disp ]] && break
        kill -0 "$(cat "$STATE/sway.pid")" 2>/dev/null || { tail -5 "$STATE/sway.log" >&2; die "sway died on startup (see .run/sway.log)"; }
        sleep 0.2
    done
    [[ -n $disp ]] || die "couldn't detect sway's wayland display (see .run/sway.log)"
    echo "$disp" > "$STATE/wayland-display"

    log "up ✓  wayland display: $disp — android is booting inside it"
    log "streamer hookup: WAYLAND_DISPLAY=$disp (nx-streamerd / './start.sh ref' for sunshine baseline)"
}

# ------------------------------------------------------------------ gui ----
cmd_gui() {
    # Tray control panel — the non-CLI front door. Start/stop the stream,
    # live status, pairing QR, close-to-tray. Needs pyside6 (setup installs it).
    python -c 'import PySide6' 2>/dev/null \
        || die "the tray app needs PySide6 — sudo pacman -S pyside6 python-qrcode (or re-run ./start.sh setup)"
    exec python "$ROOT/gui/nx_tray.py"
}

# ---------------------------------------------------------------- serve ----
cmd_serve() {
    # One-shot "bring the whole phone online" — this is what NX Hub's Launch
    # runs. It boots the headless session if it isn't up, optionally repairs
    # the waydroid network/adb path, then runs the streamer in the foreground
    # so the hub tracks THIS process: the daemon connects to the hub's status
    # bus (live state/bitrate/client on the card) and a hub Stop / stack
    # shutdown-request lands on it directly.
    if ! { [[ -f $STATE/sway.pid ]] && kill -0 "$(cat "$STATE/sway.pid" 2>/dev/null)" 2>/dev/null; }; then
        cmd_up
    else
        log "session already up (sway pid $(cat "$STATE/sway.pid"))"
    fi
    # best-effort adb repair so touch works; never fatal — video streams regardless
    if need adb && ! adb devices 2>/dev/null | grep -q "5555[[:space:]]*device"; then
        log "adb not connected to android yet — run './start.sh doctor' if touch is dead"
    fi
    cmd_stream "$@"
}

# --------------------------------------------------------------- stream ----
cmd_stream() {
    [[ -f $STATE/wayland-display ]] \
        || die "no session to capture — run ./start.sh up first (needs .run/wayland-display)"
    local disp fps port
    disp=$(cat "$STATE/wayland-display")
    fps=${NXAS_FPS:-60}
    port=${NXAS_PORT:-8765}

    kill -0 "$(cat "$STATE/sway.pid" 2>/dev/null || echo 0)" 2>/dev/null \
        || die "sway isn't running (stale .run/wayland-display) — ./start.sh up"
    # v0.1 injects touch inside android over scrcpy's control socket, which
    # needs adb to reach the waydroid container. Video streams without it.
    need adb || log "note: adb not found — touch injection is disabled (pacman -S"
    need adb || log "      android-tools). Video still streams; --input none silences it."

    # Best reachable address, in preference order: explicit override, tailscale,
    # first non-loopback LAN IP. This is what the phone connects to — so it is
    # what we put in the QR code and advertise over mDNS.
    local addr
    addr=${NXAS_ADDR:-}
    [[ -z $addr ]] && addr=$(tailscale ip -4 2>/dev/null | head -1)
    [[ -z $addr ]] && addr=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
    [[ -z $addr ]] && addr="<pc-address>"

    # Zero-typing pairing. mDNS lets a same-network phone discover this server
    # with no address at all; the QR carries the address for across-Tailscale,
    # where mDNS may not cross the tailnet. Both encode the same nxas:// URI.
    local avahi_pid=""
    if need avahi-publish-service && systemctl is-active -q avahi-daemon 2>/dev/null; then
        avahi-publish-service "NX Android Streamer @ $(hostname)" _nxstream._tcp "$port" \
            "addr=$addr" "w=$W" "h=$H" >/dev/null 2>&1 &
        avahi_pid=$!
    fi
    trap '[[ -n "$avahi_pid" ]] && kill "$avahi_pid" 2>/dev/null; exit 0' INT TERM

    log "streaming ${W}x${H}@${fps} from $disp on port $port — Ctrl-C to stop"
    if need qrencode && [[ $addr != "<pc-address>" ]]; then
        log "scan to pair the app (or let it find this server over mDNS):"
        qrencode -t ANSIUTF8 "nxas://$addr:$port"
    fi
    log "phone client (browser): http://$addr:$port"
    [[ -n $avahi_pid ]] && log "mDNS: advertising _nxstream._tcp on $port ✓"

    python "$ROOT/server/nx-streamerd.py" \
        --wayland-display "$disp" \
        --width "$W" --height "$H" --fps "$fps" \
        --bitrate "${NXAS_BITRATE:-12000}" \
        --port "$port" "$@"
    [[ -n "$avahi_pid" ]] && kill "$avahi_pid" 2>/dev/null || true
}

# --------------------------------------------------------------- doctor ----
cmd_doctor() {
    # Waydroid network + adb repair. The classic failure: Android never sends a
    # DHCP request (dnsmasq logs zero DISCOVERs), so the container has no IP,
    # no internet, and adb can't reach it. We look inside, fall back to a
    # static config on the waydroid bridge, and pre-authorize the host's adb
    # key so touch injection needs no accept-tap inside an invisible session.
    local wip=192.168.240.112 gw=192.168.240.1
    waydroid status 2>/dev/null | grep -q "Session:.*RUNNING" || die "session not running — ./start.sh up first"
    # everything below is mirrored into .run/doctor.log so the output survives
    # the terminal (and can be read by whoever is debugging remotely)
    exec > >(tee "$RUN/doctor.log") 2>&1

    log "inside view of the network stack:"
    sudo waydroid shell -- /system/bin/sh -c \
        'ip link; echo --; ip addr show eth0 2>/dev/null; echo --; ip route; echo --;
         getprop init.svc.netd; getprop init.svc.zygote; getprop sys.boot_completed' \
        || die "waydroid shell failed"
    log "last ethernet/dhcp lines from logcat:"
    sudo waydroid shell -- /system/bin/sh -c \
        'logcat -d -t 400 2>/dev/null | grep -iE "EthernetTracker|EthernetNetworkFactory|dhcp|eth0" | tail -15' || true

    # The most common cause of "android broadcasts DHCPDISCOVER but dnsmasq
    # never sees one": a host firewall. ufw's default-deny input drops udp/67
    # from the bridge (its counters prove it). Allow the waydroid bridge and
    # DHCP + internet come back permanently.
    if need ufw && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
        if ! sudo ufw status 2>/dev/null | grep -q "on waydroid0"; then
            log "ufw is active with no waydroid0 rules — it is eating android's DHCP. Fixing:"
            sudo ufw allow in on waydroid0
            sudo ufw route allow in on waydroid0
            sudo ufw route allow out on waydroid0
            log "  bouncing eth0 to retry DHCP..."
            sudo waydroid shell -- /system/bin/sh -c 'ip link set eth0 down' || true; sleep 2
            sudo waydroid shell -- /system/bin/sh -c 'ip link set eth0 up' || true; sleep 8
        fi
    fi

    if ! sudo waydroid shell -- /system/bin/sh -c 'ip addr show eth0 2>/dev/null' | grep -q 'inet '; then
        log "eth0 has no IPv4 — applying static config $wip/24 via $gw"
        sudo waydroid shell -- /system/bin/sh -c "
            ip link set eth0 up
            ip addr add $wip/24 dev eth0 2>/dev/null || true
            ip route add default via $gw 2>/dev/null || true
            setprop net.eth0.dns1 $gw"
        sleep 3
        if sudo waydroid shell -- /system/bin/sh -c 'ip addr show eth0' | grep -q "inet $wip"; then
            log "  static config held ✓"
        else
            log "  static config was FLUSHED (android's netd is fighting us) — will bridge adb instead"
        fi
    fi

    log "connectivity from inside:"
    sudo waydroid shell -- /system/bin/sh -c \
        "ping -c1 -W2 $gw >/dev/null 2>&1 && echo '  bridge: OK' || echo '  bridge: FAIL'
         ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 && echo '  wan:    OK' || echo '  wan:    FAIL (host may need masquerade for 192.168.240.0/24)'
         ping -c1 -W2 google.com >/dev/null 2>&1 && echo '  dns:    OK' || echo '  dns:    FAIL'" || true

    log "authorizing host adb key + enabling adbd on tcp/5555..."
    [[ -f $HOME/.android/adbkey.pub ]] || { log "  generating adb key..."; adb keygen "$HOME/.android/adbkey" >/dev/null 2>&1 || true; adb start-server >/dev/null 2>&1 || true; }
    [[ -f $HOME/.android/adbkey.pub ]] || die "no $HOME/.android/adbkey.pub — install android-tools and run 'adb start-server' once"
    sudo mkdir -p /var/lib/waydroid/data/misc/adb
    if ! sudo grep -qsf "$HOME/.android/adbkey.pub" /var/lib/waydroid/data/misc/adb/adb_keys 2>/dev/null; then
        sudo sh -c "cat '$HOME/.android/adbkey.pub' >> /var/lib/waydroid/data/misc/adb/adb_keys"
        sudo chmod 640 /var/lib/waydroid/data/misc/adb/adb_keys
    fi
    sudo waydroid shell -- /system/bin/sh -c 'setprop service.adb.tcp.port 5555; setprop ctl.restart adbd'

    sleep 2
    if adb connect "$wip:5555" 2>/dev/null | grep -q connected; then
        log "adb: connected to $wip:5555 ✓ (boot_completed=$(adb -s "$wip:5555" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r'))"
        return
    fi

    # Plan B: the container's IP stack is broken, but adbd listens inside the
    # container's netns and the container's /data is a host directory. So:
    # one socat with its NETWORK in the container (nsenter -n) but its
    # FILESYSTEM on the host relays a host unix socket to in-container
    # 127.0.0.1:5555; a second, plain socat exposes that as host tcp/15555.
    # adb (and the streamer) then use 127.0.0.1:15555 — no container IP needed.
    log "adb: direct connect failed — building netns adb bridge on 127.0.0.1:15555"
    need socat || die "socat needed for the adb bridge: sudo pacman -S socat, then re-run doctor"
    local apid sock=/run/nxas-adb.sock
    apid=$(pgrep -of '/apex/com.android.adbd/bin/adbd') || die "no adbd process found in container"
    sudo pkill -f "UNIX-LISTEN:$sock" 2>/dev/null || true
    pkill -f "UNIX-CONNECT:$sock" 2>/dev/null || true
    sudo rm -f "$sock"
    sudo nohup nsenter -t "$apid" -n socat "UNIX-LISTEN:$sock,fork,mode=666" TCP:127.0.0.1:5555 >/dev/null 2>&1 &
    sleep 1
    nohup socat TCP-LISTEN:15555,bind=127.0.0.1,reuseaddr,fork "UNIX-CONNECT:$sock" >/dev/null 2>&1 &
    sleep 1
    if adb connect 127.0.0.1:15555 2>/dev/null | grep -q connected; then
        log "adb: bridged ✓ 127.0.0.1:15555 (boot_completed=$(adb -s 127.0.0.1:15555 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r'))"
        log "stream with: ./start.sh stream --adb-serial 127.0.0.1:15555"
    else
        log "adb: bridge failed too — read $RUN/doctor.log and phone a friend"
    fi
}

# ----------------------------------------------------------------- down ----
cmd_down() {
    waydroid session stop 2>/dev/null || true
    if [[ -f $STATE/sway.pid ]]; then
        kill "$(cat "$STATE/sway.pid")" 2>/dev/null || true
        rm -f "$STATE/sway.pid" "$STATE/wayland-display"
    fi
    log "down."
}

# --------------------------------------------------------------- status ----
cmd_status() {
    printf 'container : %s\n' "$(systemctl is-active waydroid-container.service 2>/dev/null || echo inactive)"
    if [[ -f $STATE/sway.pid ]] && kill -0 "$(cat "$STATE/sway.pid")" 2>/dev/null; then
        printf 'sway      : running (pid %s, display %s, %sx%s@%s)\n' \
            "$(cat "$STATE/sway.pid")" "$(cat "$STATE/wayland-display" 2>/dev/null || echo '?')" "$W" "$H" "$HZ"
    else
        printf 'sway      : not running\n'
    fi
    printf 'session   : %s\n' "$(waydroid status 2>/dev/null | sed -n 's/^Session:[[:space:]]*//p' || echo unknown)"
}

# ------------------------------------------------------------------ ref ----
cmd_ref() {
    # Reference rig: Sunshine capturing our headless session, for latency
    # baselines to beat. Pair from the phone with Moonlight, custom res WxH.
    need sunshine || die "sunshine not installed (pacman -S sunshine) — optional, only for baselines"
    [[ -f $STATE/wayland-display ]] || die "session not up — run ./start.sh up first"
    log "starting sunshine against $(cat "$STATE/wayland-display") — Ctrl-C to stop"
    WAYLAND_DISPLAY=$(cat "$STATE/wayland-display") sunshine
}

# ----------------------------------------------------------------- main ----
# Bare invocation means "serve". NX Hub launches the binHint with no arguments,
# and a usage-and-exit-1 there reads to the user as "Launch does nothing".
case "${1:-serve}" in
    setup)  cmd_setup ;;
    arm)    cmd_arm ;;
    up)     cmd_up ;;
    serve)  cmd_serve "${@:2}" ;;
    gui)    cmd_gui ;;
    stream) cmd_stream "${@:2}" ;;
    down)   cmd_down ;;
    status) cmd_status ;;
    doctor) cmd_doctor ;;
    ref)    cmd_ref ;;
    *)      sed -n '2,4p' "$0" | sed 's/^# //'; exit 1 ;;
esac
