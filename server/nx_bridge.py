#!/usr/bin/env python3
"""nx_bridge — the two "native feel" reverse bridges for nx-android-streamer.

Both features hang off the *existing* signaling websocket and the *existing* adb
connection to the Waydroid container.  Neither is allowed to touch the video
path: every entry point below is a no-op when its feature is off or its
prerequisite is missing, and every failure degrades to a log line.

    A) picker bridge   remote app wants a photo
                       -> nx-bridge (in-container app) drops a request json on
                          /sdcard
                       -> we poll it over adb and push {"type":"pick"} at the
                          phone
                       -> the phone's own picker returns the file in ~32 KB
                          base64 chunks on the websocket
                       -> we reassemble, verify, and `adb push` it back into the
                          responses spool, where nx-bridge completes the intent

    B) camera bridge   phone camera -> WebRTC upstream video track
                       -> decode -> v4l2sink -> a v4l2loopback node
                       -> Android's external-camera HAL (it is running in this
                          image; see ARCHITECTURE.md) exposes it as a camera

Kept in its own module on purpose: nx-streamerd.py holds it with six one-line
hooks (arguments, camera receiver, attach, detach, message dispatch), so this
whole file can be deleted and the daemon still streams.

Part of the NX suite.  GPL-3.0 — see LICENSE.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ------------------------------------------------------------------- log ----


def _emit(level: str, msg: str) -> None:
    """Log through the daemon's own logger so bridge lines look like every other
    line.  Resolved lazily on every call for two reasons: this module is imported
    from nx-streamerd's import block, *before* log()/warn() exist, and the same
    code has to run standalone under test with no daemon around it."""
    fn = getattr(sys.modules.get("__main__"), level, None)
    if callable(fn):
        fn(msg)
    else:                                    # standalone / test harness
        sys.stderr.write(f"[{level}] {msg}\n")


def log(msg: str) -> None:
    _emit("log", msg)


def warn(msg: str) -> None:
    _emit("warn", msg)


def err(msg: str) -> None:
    _emit("err", msg)


def dbg(msg: str) -> None:
    _emit("dbg", msg)


# ------------------------------------------------------------ arguments ----

# The companion app's package.  Its private external directory is one of the two
# spool locations we watch (see SPOOL_DIRS).
BRIDGE_PACKAGE = "dev.nerdrx.nxbridge"

# Where nx-bridge drops request json and where we push the answers.
#
# Two roots, watched together, because scoped storage decides which one the app
# can actually use:
#   * /sdcard/nx-bridge is the nice, visible one from ARCHITECTURE.md, but an app
#     targeting API 30+ may only create it with MANAGE_EXTERNAL_STORAGE ("All
#     files access"), which nobody has granted by default.
#   * the app's own external files dir needs no permission at all and adb shell
#     reads it fine (shell is in the ext_data_rw group on Android 11+), so it is
#     the one that always works.
# The app picks whichever it can write and names it in the request, so the reply
# always goes back to the same root.  Watching both costs one extra glob in the
# same `ls`.
SPOOL_DIRS = (
    "/sdcard/nx-bridge",
    f"/sdcard/Android/data/{BRIDGE_PACKAGE}/files/nx-bridge",
)

# The v4l2loopback incantation, quoted verbatim in every message about it so the
# user never has to go looking for the flags.
MODPROBE_HINT = ('sudo modprobe v4l2loopback devices=1 video_nr=42 '
                 'card_label="NX Camera" exclusive_caps=1')


def add_arguments(parser) -> None:
    """The bridge's slice of the daemon command line (one hook in parse_args)."""
    parser.add_argument(
        "--camera", default="none", metavar="auto|none|/dev/videoN",
        help="phone camera -> v4l2loopback bridge. 'none' (default) never "
             "negotiates an upstream video track at all; 'auto' uses the first "
             "v4l2loopback node it finds; an explicit /dev/videoN pins one. "
             f"Needs the module: {MODPROBE_HINT}")
    parser.add_argument(
        "--no-picker", action="store_true",
        help="disable the on-demand file/photo picker bridge (it is on by "
             "default and stays idle unless the nx-bridge companion app is "
             "installed in the container)")


# ------------------------------------------------------------------ adb ----


class Adb:
    """The handful of adb calls the picker needs, with the same discipline as
    ScrcpyInjector: never run adb without an explicit serial, never let a hung
    adb wedge the loop.

    Deliberately *not* sharing ScrcpyInjector's connection — the injector may be
    absent (--input none/uinput) and the picker still has to work."""

    LEASES = "/var/lib/misc/dnsmasq.waydroid0.leases"

    def __init__(self, binary, serial):
        self.binary = binary or shutil.which("adb")
        self.serial = serial
        self._resolved = False

    # -- discovery ----------------------------------------------------------
    def _waydroid_ip(self):
        """Container IP, cheapest source first (same order as ScrcpyInjector)."""
        try:
            with open(self.LEASES, "r") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 3 and parts[2].count(".") == 3:
                        return parts[2]
        except OSError:
            pass
        try:
            out = subprocess.run(["waydroid", "status"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=10)
            for line in out.stdout.decode("utf-8", "replace").splitlines():
                if "IP address" in line:
                    value = line.split(":", 1)[1].strip()
                    if value and value != "UNKNOWN":
                        return value
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    async def resolve(self) -> bool:
        """-> True once we have a serial we can talk to."""
        if self.binary is None:
            return False
        if self.serial:
            return True
        if self._resolved:
            return False                     # already tried and failed
        self._resolved = True
        ip = self._waydroid_ip()
        if not ip:
            return False
        target = f"{ip}:5555"
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "connect", target,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 20.0)
        except (OSError, asyncio.TimeoutError):
            return False
        if b"connected to" not in out:
            return False
        self.serial = target
        log(f"bridge: adb connected to waydroid at {target}")
        return True

    # -- calls --------------------------------------------------------------
    async def run(self, *argv, timeout=20.0):
        """-> (rc, text).  Never raises on a non-zero rc: every caller here has
        a sane 'it did not work' branch, and a bridge fault must stay a bridge
        fault."""
        if self.binary is None or not self.serial:
            return (127, "")
        cmd = [self.binary, "-s", self.serial, *argv]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
        except OSError as exc:
            return (127, str(exc))
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except OSError:
                pass
            return (124, f"adb {argv[0]} timed out after {timeout}s")
        return (proc.returncode, out.decode("utf-8", "replace"))

    async def shell(self, script: str, timeout=20.0):
        return await self.run("shell", script, timeout=timeout)


# --------------------------------------------------------------- picker ----


def _sanitize_name(name: str) -> str:
    """A display name from the phone becomes a filename inside the container, so
    it is attacker-adjacent input even though the attacker is the user's own
    gallery: strip anything that could climb out of the responses directory."""
    name = (name or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = name.lstrip(".") or "file"
    return name[:96]


class _Transfer:
    """One in-flight pick: the phone is streaming a chosen file at us."""

    def __init__(self, ident, spool, tmp_path):
        self.id = ident
        self.spool = spool
        self.tmp_path = tmp_path
        self.fh = open(tmp_path, "wb")
        self.bytes = 0
        self.next_seq = 0
        self.name = None
        self.mime = None
        self.size = None                     # declared total, if the client said
        self.sha = hashlib.sha256()
        self.started = time.monotonic()

    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass

    def discard(self) -> None:
        self.close()
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass


class PickerBridge:
    """Polls the container's request spool and brokers files back into it.

    Polling over `adb shell ls` twice a second sounds crude and is exactly right
    here: there is no inotify across the container boundary that does not cost a
    persistent socket and a second protocol, a pick happens maybe once an hour,
    and two `ls` globs are cheaper than the touch events we already send.  It
    also only runs while a client is attached *and* the companion app is
    installed, so the common case (no nx-bridge) costs one `pm path` per session
    and nothing after that.
    """

    POLL_INTERVAL = 0.5
    MAX_BYTES = 64 << 20                     # a pick is a photo, not a disk image
    TRANSFER_TIMEOUT = 180.0                 # phone went away mid-upload

    def __init__(self, args, adb, send):
        self.args = args
        self.adb = adb
        self.send = send                     # async (payload) -> None
        self.run_dir = Path(getattr(args, "run_dir", "."))
        self._task = None
        self._transfers = {}                 # id -> _Transfer
        self._seen = set()                   # request paths already handled
        self._installed = None               # None = not checked yet
        self._closing = False

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def aclose(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        # A client that vanished mid-upload leaves the in-container app waiting
        # on a response that will never come; tell it so instead of letting it
        # sit out its own timeout.
        for transfer in list(self._transfers.values()):
            transfer.discard()
            await self._write_marker(transfer.spool, transfer.id, "cancel")
        self._transfers.clear()

    # -- polling ------------------------------------------------------------
    async def _loop(self) -> None:
        try:
            if not await self.adb.resolve():
                log("bridge: no adb route to the container — picker idle")
                return
            if not await self._check_installed():
                return
            log(f"bridge: watching {len(SPOOL_DIRS)} request spool(s) "
                f"every {self.POLL_INTERVAL:.1f}s")
            while not self._closing:
                await self._poll_once()
                self._expire_transfers()
                await asyncio.sleep(self.POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:             # never take the session with us
            err(f"bridge: picker poll stopped ({exc!r}) — video unaffected")

    async def _check_installed(self) -> bool:
        rc, out = await self.adb.shell(f"pm path {BRIDGE_PACKAGE}", timeout=15.0)
        if rc != 0 or "package:" not in out:
            self._installed = False
            log(f"bridge: {BRIDGE_PACKAGE} is not installed in the container — "
                "picker idle (build/install bridge-android/ to enable it)")
            return False
        self._installed = True
        return True

    def _globs(self) -> str:
        return " ".join(f"{d}/requests/*.json" for d in SPOOL_DIRS)

    async def _poll_once(self) -> None:
        # -d so directories are not descended, and unmatched globs come back
        # literally (with a '*' in them) rather than as an error — filtered below.
        rc, out = await self.adb.shell(f"ls -1d {self._globs()} 2>/dev/null",
                                       timeout=10.0)
        if rc not in (0, 1):                 # 1 == "no such file", i.e. idle
            dbg(f"bridge: spool ls rc={rc} ({out.strip()[:120]!r})")
            return
        for line in out.splitlines():
            path = line.strip()
            if not path or "*" in path or not path.endswith(".json"):
                continue
            if path in self._seen:
                continue
            self._seen.add(path)
            await self._take_request(path)

    async def _take_request(self, path: str) -> None:
        """Read the request and delete it in one shell round trip: the file is a
        one-shot doorbell, and leaving it there would re-fire it forever if
        anything below throws."""
        rc, out = await self.adb.shell(f"cat {path}; rm -f {path}", timeout=15.0)
        self._seen.discard(path)             # it is gone now; forget the name
        if rc != 0:
            warn(f"bridge: could not read request {path} (rc={rc})")
            return
        try:
            req = json.loads(out.strip() or "{}")
        except ValueError:
            warn(f"bridge: request {path} is not JSON ({out.strip()[:80]!r})")
            return
        ident = str(req.get("id") or "").strip()
        if not ident or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
            warn(f"bridge: request {path} has no usable id ({req.get('id')!r})")
            return
        # The app tells us which spool root it could actually write, so the
        # answer lands where it is watching.  Anything outside the known roots is
        # refused: this string becomes an adb push destination.
        spool = str(req.get("spool") or "")
        if spool not in SPOOL_DIRS:
            spool = path.rsplit("/requests/", 1)[0]
            if spool not in SPOOL_DIRS:
                warn(f"bridge: request {ident} names an unknown spool "
                     f"{req.get('spool')!r} — ignoring")
                return
        mime = str(req.get("mime") or "*/*")
        log(f"bridge: pick request {ident} ({mime}) from the container")
        await self.send({
            "type": "pick",
            "id": ident,
            "mime": mime,
            "multiple": bool(req.get("multiple")),
        })

    def _expire_transfers(self) -> None:
        now = time.monotonic()
        for ident, transfer in list(self._transfers.items()):
            if now - transfer.started > self.TRANSFER_TIMEOUT:
                warn(f"bridge: pick {ident} timed out mid-transfer "
                     f"({transfer.bytes} bytes) — cancelling")
                transfer.discard()
                self._transfers.pop(ident, None)
                asyncio.ensure_future(
                    self._write_marker(transfer.spool, ident, "cancel"))

    # -- client -> container ------------------------------------------------
    async def on_pick_data(self, msg) -> None:
        """{"type":"pick-data","id","seq","eof","b64", name?/mime?/size?/sha256?}

        Chunked because a photo is megabytes and the signaling socket is also
        carrying SDP and ICE: one 8 MB frame would stall negotiation behind it
        (and blow past aiohttp's max_msg_size).  ~32 KB of payload per frame is
        small enough to interleave and big enough that the base64 and JSON
        overhead does not dominate.
        """
        ident = str(msg.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
            return
        transfer = self._transfers.get(ident)
        seq = msg.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            warn(f"bridge: pick {ident} sent a bad seq ({seq!r})")
            return

        if transfer is None:
            if seq != 0:
                # A late chunk from a transfer we already gave up on.
                dbg(f"bridge: ignoring orphan chunk {ident}#{seq}")
                return
            spool = str(msg.get("spool") or "")
            if spool not in SPOOL_DIRS:
                spool = SPOOL_DIRS[1]        # the always-writable one
            tmp = self.run_dir / f"nxas-pick-{os.getpid()}-{ident}.bin"
            try:
                self.run_dir.mkdir(parents=True, exist_ok=True)
                transfer = _Transfer(ident, spool, tmp)
            except OSError as exc:
                err(f"bridge: cannot buffer pick {ident} ({exc})")
                await self._fail(spool, ident, "server could not buffer the file")
                return
            transfer.name = _sanitize_name(msg.get("name") or f"{ident}.bin")
            transfer.mime = str(msg.get("mime") or "application/octet-stream")
            size = msg.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                transfer.size = size
            self._transfers[ident] = transfer
            log(f"bridge: receiving {transfer.name!r} for pick {ident}"
                + (f" ({transfer.size} bytes)" if transfer.size is not None else ""))

        if seq != transfer.next_seq:
            # The websocket is ordered and reliable, so a gap means the client is
            # confused, not that the network dropped something. Fail loudly
            # rather than writing a corrupt file into the user's gallery.
            err(f"bridge: pick {ident} chunk out of order "
                f"(got {seq}, wanted {transfer.next_seq}) — aborting")
            await self._abort(ident, "chunks arrived out of order")
            return
        transfer.next_seq += 1

        raw = msg.get("b64") or ""
        if raw:
            try:
                chunk = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                err(f"bridge: pick {ident} chunk {seq} is not valid base64")
                await self._abort(ident, "bad base64 in the transfer")
                return
            if transfer.bytes + len(chunk) > self.MAX_BYTES:
                err(f"bridge: pick {ident} exceeds {self.MAX_BYTES >> 20} MiB — "
                    "aborting")
                await self._abort(ident, "file too large")
                return
            transfer.fh.write(chunk)
            transfer.sha.update(chunk)
            transfer.bytes += len(chunk)

        if not msg.get("eof"):
            return

        transfer.close()
        self._transfers.pop(ident, None)
        digest = transfer.sha.hexdigest()

        # Two independent checks, because a truncated photo that *looks* fine is
        # the worst outcome here: it completes the intent and the user only finds
        # out when the upload they just made is half grey.
        if transfer.size is not None and transfer.size != transfer.bytes:
            err(f"bridge: pick {ident} size mismatch — client said "
                f"{transfer.size}, received {transfer.bytes}")
            transfer.discard()
            await self._fail(transfer.spool, ident, "transfer was truncated")
            return
        claimed = msg.get("sha256")
        if isinstance(claimed, str) and claimed and claimed.lower() != digest:
            err(f"bridge: pick {ident} sha256 mismatch "
                f"(client {claimed[:16]}…, received {digest[:16]}…)")
            transfer.discard()
            await self._fail(transfer.spool, ident, "transfer was corrupted")
            return

        log(f"bridge: pick {ident} complete — {transfer.bytes} bytes, "
            f"sha256 {digest[:16]}…")
        await self._deliver(transfer)

    async def _deliver(self, transfer: _Transfer) -> None:
        """Push the file in, then rename it into place.

        The rename is the whole protocol: nx-bridge only ever looks for finished
        names, so it can never open a half-pushed file.
        """
        dest_dir = f"{transfer.spool}/responses"
        final = f"{dest_dir}/{transfer.id}__{transfer.name}"
        staging = f"{final}.part"
        rc, out = await self.adb.shell(f"mkdir -p {dest_dir}", timeout=15.0)
        if rc != 0:
            err(f"bridge: cannot create {dest_dir} (rc={rc}) — pick lost")
            transfer.discard()
            return
        rc, out = await self.adb.run("push", str(transfer.tmp_path), staging,
                                     timeout=180.0)
        if rc != 0:
            err(f"bridge: adb push failed for pick {transfer.id}: "
                f"{out.strip()[:160]}")
            transfer.discard()
            await self._fail(transfer.spool, transfer.id, "could not push the file")
            return
        rc, out = await self.adb.shell(f"mv {staging} {final}", timeout=15.0)
        transfer.discard()
        if rc != 0:
            err(f"bridge: could not finalize {final} (rc={rc})")
            await self._fail(transfer.spool, transfer.id, "could not place the file")
            return
        log(f"bridge: delivered pick {transfer.id} -> {final}")
        # Poke MediaStore so the file is also visible in the container's gallery
        # (ARCHITECTURE.md "one gallery"): the intent completes either way, this
        # only decides whether the picture also *exists* to other apps.
        await self.adb.shell(
            f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            f"-d file://{final} >/dev/null 2>&1", timeout=15.0)

    async def on_pick_cancel(self, msg) -> None:
        ident = str(msg.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
            return
        transfer = self._transfers.pop(ident, None)
        spool = transfer.spool if transfer else str(msg.get("spool") or "")
        if transfer:
            transfer.discard()
        if spool not in SPOOL_DIRS:
            spool = SPOOL_DIRS[1]
        log(f"bridge: pick {ident} cancelled by the phone")
        await self._write_marker(spool, ident, "cancel")

    async def _abort(self, ident: str, why: str) -> None:
        transfer = self._transfers.pop(ident, None)
        if transfer is None:
            return
        transfer.discard()
        await self._fail(transfer.spool, ident, why)

    async def _fail(self, spool: str, ident: str, why: str) -> None:
        if spool not in SPOOL_DIRS:
            spool = SPOOL_DIRS[1]
        await self._write_marker(spool, ident, "error")
        await self.send({"type": "pick-error", "id": ident, "message": why})

    async def _write_marker(self, spool: str, ident: str, kind: str) -> None:
        """An empty `<id>.cancel` / `<id>.error` next to the responses: the
        companion app stops waiting the moment it sees one."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
            return
        dest = f"{spool}/responses/{ident}.{kind}"
        await self.adb.shell(f"mkdir -p {spool}/responses && : > {dest}",
                             timeout=15.0)


# --------------------------------------------------------------- camera ----


def find_loopback(spec: str):
    """-> (device_path, why_not).  Exactly one of the two is None.

    v4l2loopback nodes are *virtual* v4l2 devices, so they are the ones under
    /sys/devices/virtual/video4linux — a real UVC webcam hangs off a PCI/USB
    parent instead.  That is a sturdier test than parsing lsmod, and it also
    tells us the node number without guessing.
    """
    if not spec or spec == "none":
        return (None, None)                  # not asked for; say nothing

    loaded = False
    try:
        with open("/proc/modules", "r") as fh:
            loaded = any(line.startswith("v4l2loopback ") for line in fh)
    except OSError:
        pass

    if spec != "auto":
        path = Path(spec)
        if not path.exists():
            return (None, f"{spec} does not exist")
        return (str(path), None)

    candidates = []
    virtual = Path("/sys/devices/virtual/video4linux")
    if virtual.is_dir():
        for entry in sorted(virtual.glob("video*")):
            node = Path("/dev") / entry.name
            if not node.exists():
                continue
            try:
                label = (entry / "name").read_text().strip()
            except OSError:
                label = ""
            candidates.append((str(node), label))

    if not candidates:
        if loaded:
            return (None, "v4l2loopback is loaded but exposes no /dev/video node")
        return (None, "v4l2loopback is not loaded")
    # Prefer the node we told the user to create; otherwise the lowest virtual one.
    for node, label in candidates:
        if "nx camera" in label.lower():
            return (node, None)
    return (candidates[0][0], None)


class CameraReceiver:
    """The upstream half of the WebRTC session: the phone's camera, decoded on
    the host and written into a v4l2loopback node.

    Shape of it, and why it needs no renegotiation: webrtcbin gets a *recvonly*
    H.264 transceiver before the offer is created, so every offer this daemon
    sends already carries a second m=video section.  The client answers it
    sendonly and simply attaches (or detaches) a track on that sender when we ask
    it to — no second offer, which matters because in this protocol the server is
    always the offerer and the client can never initiate one.

    Cost when the phone is not sending: an idle m-line.  No packets, no encoder,
    no camera LED.
    """

    # 720p30 is what the external-camera HAL's config file lists as a sane cap
    # (see /vendor/etc/external_camera_config.xml in the container) and what a
    # video call actually wants.  Anything bigger just costs uplink.
    WIDTH, HEIGHT, FPS = 1280, 720, 30

    def __init__(self, device, pipeline, webrtc):
        self.device = device
        self.pipeline = pipeline
        self.webrtc = webrtc
        self.chain = []                      # elements we added, for teardown

    def attach(self) -> None:
        """Add the recvonly transceiver and arm the pad handler.  Runs on the
        thread that built the pipeline, before it goes PLAYING."""
        from gi.repository import Gst, GstWebRTC          # noqa: E402  (lazy)

        caps = Gst.Caps.from_string(
            "application/x-rtp,media=video,encoding-name=H264,"
            "clock-rate=90000,payload=97")
        try:
            self.webrtc.emit(
                "add-transceiver",
                GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY, caps)
        except Exception as exc:
            err(f"camera: could not add the recvonly transceiver ({exc!r}) — "
                "camera bridge off for this session, video unaffected")
            return
        self.webrtc.connect("pad-added", self._on_pad)
        log(f"camera: recvonly H.264 transceiver offered -> {self.device} "
            f"({self.WIDTH}x{self.HEIGHT}@{self.FPS})")

    def _on_pad(self, _element, pad) -> None:
        """webrtcbin produced an incoming stream.  Runs on a GStreamer thread;
        everything here is either guarded or logged, never raised."""
        from gi.repository import Gst                     # noqa: E402  (lazy)

        try:
            if pad.get_direction() != Gst.PadDirection.SRC:
                return
            if self.chain:
                dbg("camera: a second incoming pad appeared; ignoring it")
                return
            caps = pad.get_current_caps() or pad.query_caps(None)
            encoding = ""
            if caps and caps.get_size():
                encoding = str(caps.get_structure(0).get_string("encoding-name")
                               or "").upper()
            depay = {"H264": "rtph264depay", "VP8": "rtpvp8depay",
                     "H265": "rtph265depay"}.get(encoding)
            if depay is None:
                warn(f"camera: incoming track is {encoding or 'unknown'}, which "
                     "this build does not depayload — ignoring it")
                return
            log(f"camera: incoming {encoding} track -> {self.device}")
            self._build(depay)
            sink_pad = self.chain[0].get_static_pad("sink")
            if pad.link(sink_pad) != Gst.PadLinkReturn.OK:
                err("camera: could not link the incoming track — camera bridge "
                    "inactive, video unaffected")
        except Exception as exc:
            err(f"camera: incoming track setup failed ({exc!r}) — video "
                "unaffected")

    def _build(self, depay_name: str) -> None:
        """depay -> decode -> YUY2 at a size the external-camera HAL lists.

        v4l2sink with sync=false on purpose: a webcam has no timeline to respect,
        and honouring one would just add a frame of latency to a video call.
        """
        from gi.repository import Gst                     # noqa: E402  (lazy)

        desc = (f"{depay_name} ! decodebin ! videoconvert ! videoscale ! "
                f"videorate ! video/x-raw,format=YUY2,"
                f"width={self.WIDTH},height={self.HEIGHT},framerate={self.FPS}/1 "
                f"! v4l2sink device={self.device} sync=false")
        bin_ = Gst.parse_bin_from_description(desc, True)
        bin_.set_name("nx-camera-sink")
        self.pipeline.add(bin_)
        bin_.sync_state_with_parent()
        self.chain = [bin_]


def attach_camera_receiver(args, pipeline, webrtc):
    """Hook called from Streamer._start().  Returns the receiver, or None when
    the feature is off or its prerequisite is missing — in which case the
    pipeline is left byte-for-byte as it was."""
    device, why_not = find_loopback(getattr(args, "camera", "none"))
    if device is None:
        if why_not:
            _explain_missing_loopback(why_not)
        return None
    receiver = CameraReceiver(device, pipeline, webrtc)
    receiver.attach()
    return receiver


def _explain_missing_loopback(why_not: str) -> None:
    """One block, everything the user needs, no hunting."""
    warn(f"camera: {why_not} — the phone-camera bridge is OFF for this session "
         "(video and touch are unaffected)")
    warn(f"camera: load it with:  {MODPROBE_HINT}")
    warn("camera: then restart the Waydroid session — waydroid re-globs "
         "/dev/video* into its LXC config only at session start, so a node "
         "created afterwards is not visible inside the container")


def camera_state(args) -> dict:
    """The snapshot the client renders its 'remote camera' row from."""
    spec = getattr(args, "camera", "none")
    device, why_not = find_loopback(spec)
    state = {
        "type": "camera",
        "available": device is not None,
        "device": device,
        "width": CameraReceiver.WIDTH,
        "height": CameraReceiver.HEIGHT,
        "fps": CameraReceiver.FPS,
        "on": False,
    }
    if device is None:
        state["note"] = ("disabled (--camera none)" if spec in (None, "", "none")
                         else f"unavailable: {why_not}")
    return state


# ---------------------------------------------------------------- wiring ----
#
# Module-level singleton rather than an object the Daemon owns, so the daemon
# needs no constructor change: the three lifecycle hooks below are the entire
# surface nx-streamerd.py touches.

_session = None                              # the live _Session, or None

# Message types this module claims off the signaling socket.
_KINDS = frozenset(("pick-data", "pick-cancel", "camera"))


class _Session:
    """Everything the bridge owns for exactly one attached client."""

    def __init__(self, args, ws, send):
        self.args = args
        self.ws = ws
        self.send = send
        self.adb = Adb(getattr(args, "adb", None), getattr(args, "adb_serial", None))
        self.picker = None
        self.camera_allowed = False

    def start(self) -> None:
        if not getattr(self.args, "no_picker", False):
            self.picker = PickerBridge(self.args, self.adb, self.send)
            self.picker.start()

    async def aclose(self) -> None:
        if self.picker is not None:
            await self.picker.aclose()
            self.picker = None


def client_attached(args, ws, send) -> None:
    """Hook: a client just finished attaching.  `send` is an async callable that
    puts one JSON payload on that client's signaling socket."""
    global _session
    if _session is not None:
        asyncio.ensure_future(_session.aclose())
    _session = _Session(args, ws, send)
    _session.start()
    # Tell the phone what the camera bridge can do before it asks, exactly like
    # the config snapshot behind every offer: a fresh client renders the real
    # state instead of guessing.
    asyncio.ensure_future(_announce_camera(_session))


async def _announce_camera(session) -> None:
    try:
        await session.send(camera_state(session.args))
    except Exception as exc:                 # a status frame is never fatal
        dbg(f"bridge: camera announce failed ({exc!r})")


def client_detached(ws) -> None:
    """Hook: that client is gone.  Stops polling and releases the camera."""
    global _session
    session = _session
    if session is None or session.ws is not ws:
        return                               # already replaced; not ours to stop
    _session = None
    asyncio.ensure_future(session.aclose())


async def on_message(ws, msg) -> bool:
    """Hook in the websocket dispatch.  -> True when this module took the
    message, so the daemon's 'unknown message type' branch stays honest."""
    kind = msg.get("type")
    if kind not in _KINDS:
        return False
    session = _session
    if session is None or session.ws is not ws:
        return True                          # ours by type, but a stale client
    try:
        if kind == "camera":
            await _on_camera(session, msg)
        elif session.picker is None:
            dbg(f"bridge: {kind} arrived with the picker disabled")
        elif kind == "pick-data":
            await session.picker.on_pick_data(msg)
        elif kind == "pick-cancel":
            await session.picker.on_pick_cancel(msg)
    except Exception as exc:                 # a bridge fault is a bridge fault
        err(f"bridge: {kind} failed ({exc!r}) — session unaffected")
    return True


async def _on_camera(session, msg) -> None:
    """{"type":"camera","allow":bool} — the phone's privacy toggle.

    The client never turns its own camera on: it only tells us whether it *may*,
    and we answer with whether it should.  That keeps one authority over a
    privacy-sensitive capability, and it means a server with no loopback node
    simply never asks.
    """
    allow = msg.get("allow")
    if allow is not None and not isinstance(allow, bool):
        warn(f"bridge: ignoring bad camera allow flag ({allow!r})")
        allow = None
    if allow is not None:
        session.camera_allowed = allow
    state = camera_state(session.args)
    state["on"] = bool(state["available"] and session.camera_allowed)
    if state["on"]:
        log(f"camera: phone camera requested ON -> {state['device']}")
    elif allow is False:
        log("camera: phone camera requested OFF")
    await session.send(state)
