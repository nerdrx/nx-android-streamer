#!/usr/bin/env python3
"""nx-streamerd — v0.1 streaming daemon for nx-android-streamer.

Captures a headless sway output with wf-recorder, encodes H.264 (VAAPI when the
machine has it), and serves exactly one browser client over WebRTC.  The
client's touch events come back on an ordered datachannel and are injected
straight into Android through scrcpy's control socket.

    sway HEADLESS-1 -> wf-recorder (raw bgr0) -> fifo -> gstreamer
                    -> h264 -> webrtcbin -> phone
    phone touch     -> datachannel -> adb tunnel -> scrcpy server -> android

Our headless sway has no seat, hence no libinput backend, so nothing on the
host would consume injected uinput events — that path (--input uinput) is kept
for compositors that do have a seat.  See ARCHITECTURE.md.

Threading: the aiohttp/asyncio loop owns the main thread, GStreamer's GLib
MainLoop runs in a dedicated thread.  There are exactly three handoff points
between them, each marked "THREAD BOUNDARY" below.  Keep it that way.

Part of the NX suite.  GPL-3.0 — see LICENSE.
"""

import argparse
import asyncio
import errno
import fcntl
import json
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

import aiohttp  # noqa: E402
from aiohttp import WSMsgType, web  # noqa: E402

# ------------------------------------------------------------------- log ----

_PURPLE = "\033[38;2;119;0;255m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_OFF = "\033[0m"
_TTY = sys.stdout.isatty()
_log_lock = threading.Lock()


def _emit(colour: str, tag: str, msg: str) -> None:
    stamp = time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)
    thread = threading.current_thread().name
    if _TTY:
        line = f"{colour}[{tag}]{_OFF} {stamp} {thread:<9} {msg}"
    else:
        line = f"[{tag}] {stamp} {thread:<9} {msg}"
    with _log_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log(msg: str) -> None:
    _emit(_PURPLE, "nxas", msg)


def warn(msg: str) -> None:
    _emit(_YELLOW, "warn", msg)


def err(msg: str) -> None:
    _emit(_RED, "fail", msg)


def die(msg: str) -> None:
    """Startup-only: SystemExit is safe on the main thread."""
    err(msg)
    raise SystemExit(1)


# Daemon version, reported to NX Hub in the connector `hello`.
VERSION = "0.1"

# The hub connector is meant to be *silent* — a machine without NX Hub is the
# common case and must produce no warnings.  Anything the connector wants to say
# goes through dbg(), which is off unless NXAS_DEBUG / NX_DEBUG is set.
_DEBUG = bool(os.environ.get("NXAS_DEBUG") or os.environ.get("NX_DEBUG"))


def dbg(msg: str) -> None:
    if _DEBUG:
        _emit(_YELLOW, "dbg ", msg)


class StreamError(RuntimeError):
    """Anything that kills a session. Raised on worker threads, where a bare
    SystemExit would just vanish into an executor future."""


# --------------------------------------------------------------- capture ----

WF_RECORDER = "wf-recorder"


def _wf_supports_overwrite() -> bool:
    """wf-recorder prompts 'file exists, overwrite?' — and our fifo always
    exists.  Newer builds take -y/--overwrite; older ones must be answered on
    stdin (we give them /dev/null, which reads as 'no' and aborts).  Probe."""
    try:
        p = subprocess.run(
            [WF_RECORDER, "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
        )
    except FileNotFoundError:
        raise StreamError(
            f"{WF_RECORDER} not found in PATH — ./start.sh setup installs it")
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        warn(f"could not probe wf-recorder --help ({exc}); assuming no -y")
        return False
    return b"--overwrite" in p.stdout or b"-y," in p.stdout


class Capture:
    """wf-recorder writing raw frames into a fifo that GStreamer reads.

    Fifo instead of /dev/stdout on purpose: wf-recorder and its ffmpeg guts log
    to the same terminal, and a stray line in the pipe would corrupt the raw
    video stream forever (rawvideoparse has no resync).
    """

    def __init__(self, *, output, fifo, fps, wayland_display, on_death):
        self.output = output
        self.fifo = Path(fifo)
        self.fps = fps
        self.wayland_display = wayland_display
        self.on_death = on_death
        self.proc = None
        self._keeper_fd = -1
        self._stopping = False

    # -- fifo ---------------------------------------------------------------
    def _make_fifo(self) -> None:
        try:
            self.fifo.unlink()
        except FileNotFoundError:
            pass
        self.fifo.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.fifo, 0o600)

        # O_NONBLOCK so this open() returns immediately even though nobody is
        # writing yet.  Holding the read end open is what stops wf-recorder's
        # own open()-for-write from blocking forever, and stops filesrc from
        # seeing EOF between frames.  We never read from this fd: all data goes
        # to filesrc, which is the only reader that actually calls read().
        self._keeper_fd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        fl = fcntl.fcntl(self._keeper_fd, fcntl.F_GETFL)
        fcntl.fcntl(self._keeper_fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)

        # A 64 KiB pipe buffer against 10 MB frames means wf-recorder blocks
        # constantly.  Ask for more; the cap is /proc/sys/fs/pipe-max-size.
        F_SETPIPE_SZ = 1031
        for want in (4 << 20, 1 << 20):
            try:
                got = fcntl.fcntl(self._keeper_fd, F_SETPIPE_SZ, want)
                log(f"fifo {self.fifo.name}: pipe buffer {got // 1024} KiB")
                break
            except OSError:
                continue

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._make_fifo()
        cmd = [WF_RECORDER, "-o", self.output, "-x", "bgr0", "-c", "rawvideo",
               "-m", "rawvideo", "-D", "-r", str(self.fps), "-f", str(self.fifo)]
        if _wf_supports_overwrite():
            cmd.insert(1, "-y")
        env = os.environ.copy()
        env["WAYLAND_DISPLAY"] = self.wayland_display
        log(f"capture: WAYLAND_DISPLAY={self.wayland_display} {' '.join(cmd)}")
        try:
            self.proc = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError:
            raise StreamError(
                f"{WF_RECORDER} not found in PATH — ./start.sh setup installs it")
        threading.Thread(target=self._pump_output, name="wf-log", daemon=True).start()
        threading.Thread(target=self._watch, name="wf-watch", daemon=True).start()
        log(f"capture: wf-recorder pid {self.proc.pid} -> {self.fifo}")

    def _pump_output(self) -> None:
        """wf-recorder/ffmpeg chatter, verbatim, into our log."""
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                _emit(_PURPLE, "wf-rec", line)

    def _watch(self) -> None:
        assert self.proc
        rc = self.proc.wait()
        if self._stopping:
            log(f"capture: wf-recorder exited (rc={rc}) during shutdown")
            return
        err(f"capture: wf-recorder DIED unexpectedly (rc={rc}) — no more frames.")
        err("capture: usual causes: session gone, wrong --output name, "
            "WAYLAND_DISPLAY mismatch. Check './start.sh status' and .run/sway.log")
        self.on_death(rc)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        self._stopping = True
        if self.proc and self.proc.poll() is None:
            # SIGINT first (wf-recorder's documented way to stop), then SIGKILL
            # without ceremony: by this point GStreamer has stopped reading, so
            # wf-recorder is parked in write() on a full fifo and never reaches
            # its signal handler. There is nothing to finalize — the "file" is
            # a pipe we are about to delete — so waiting longer buys nothing.
            log("capture: stopping wf-recorder")
            try:
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=0.7)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    warn("capture: wf-recorder would not die")
            except OSError:
                pass
        self.proc = None
        if self._keeper_fd >= 0:
            try:
                os.close(self._keeper_fd)
            except OSError:
                pass
            self._keeper_fd = -1
        try:
            self.fifo.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            warn(f"capture: could not remove {self.fifo}: {exc}")


# --------------------------------------------------------------- encoder ----

VA_ENCODERS = ("vah264enc", "vah264lpenc")
ALL_ENCODERS = VA_ENCODERS + ("x264enc",)


def pick_encoder(preference: str) -> str:
    """auto: first factory that actually exists, hardware first."""
    if preference == "va":
        candidates = VA_ENCODERS
    elif preference == "x264":
        candidates = ("x264enc",)
    else:
        candidates = ALL_ENCODERS
    for name in candidates:
        if Gst.ElementFactory.find(name) is not None:
            return name
        log(f"encoder: {name} not installed, trying next")
    die(f"no usable H.264 encoder (looked for: {', '.join(candidates)}). "
        "Install gst-plugin-va (hardware) or gst-plugins-ugly (x264enc).")
    raise AssertionError("unreachable")


def configure_encoder(enc: Gst.Element, name: str, bitrate_kbps: int, fps: int) -> None:
    """Everything optional is guarded: property sets differ across GStreamer
    versions and VA drivers, and a missing property must not be fatal."""
    props = {p.name for p in enc.list_properties()}
    gop = 2 * fps

    def maybe(prop, value):
        if prop in props:
            try:
                enc.set_property(prop, value)
                return True
            except (TypeError, ValueError) as exc:
                warn(f"encoder: {name}.{prop}={value!r} rejected ({exc})")
        return False

    def maybe_arg(prop, value):
        if prop in props:
            try:
                Gst.util_set_object_arg(enc, prop, value)
                return True
            except (TypeError, ValueError, GLib.Error) as exc:
                warn(f"encoder: {name}.{prop}={value} rejected ({exc})")
        return False

    if name in VA_ENCODERS:
        maybe("bitrate", bitrate_kbps)      # va encoders take kbps
        maybe("key-int-max", gop)
        maybe("b-frames", 0)                # any B-frame is a frame of latency
        # CBR keeps the pipe predictable for the phone's jitter buffer; not all
        # drivers expose it, and vah264lpenc's enum differs.
        if maybe_arg("rate-control", "cbr"):
            log("encoder: rate-control=cbr")
        else:
            log("encoder: rate-control left at driver default")
        maybe("target-usage", 6)            # 1=quality .. 7=speed
    else:
        maybe_arg("tune", "zerolatency")
        maybe_arg("speed-preset", "superfast")
        maybe("bitrate", bitrate_kbps)      # x264enc takes kbps too
        maybe("key-int-max", gop)
        maybe("byte-stream", False)
    log(f"encoder: {name} @ {bitrate_kbps} kbps, key-int-max={gop} ({fps} fps)")


# ------------------------------------------------------------------ sdp ----

def sdp_from_text(text: str) -> GstSdp.SDPMessage:
    res = GstSdp.SDPMessage.new_from_text(text)
    if isinstance(res, tuple):          # (SDPResult, SDPMessage) on 1.20+
        return res[1]
    return res


def sdp_summary(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.startswith("m=")]
    return " | ".join(lines) if lines else "(no m-lines!)"


# ------------------------------------------------------------- streamer ----

class Streamer:
    """One capture + encode + webrtcbin pipeline, serving one client."""

    def __init__(self, args, injector, on_fatal):
        self.args = args
        self.injector = injector
        self.on_fatal = on_fatal
        self.encoder_name = pick_encoder(args.encoder)
        self.pipeline = None
        self.webrtc = None
        self.capture = None
        self.channel = None
        self.send_json = None            # set by attach(); GLib thread -> WS
        self._setup_done = False
        self._negotiation_wanted = False
        self._sendonly_done = False
        self._lock = threading.Lock()

    # -- build --------------------------------------------------------------
    def _launch_string(self, fifo: Path) -> str:
        a = self.args
        return (
            f'filesrc location="{fifo}" name=src blocksize=1048576 '
            f"! rawvideoparse width={a.width} height={a.height} format=bgrx "
            f"framerate={a.fps}/1 "
            f"! videoconvert n-threads=4 "
            f"! {self.encoder_name} name=venc "
            f"! h264parse config-interval=-1 "
            f"! rtph264pay name=pay pt=96 "
            f"! application/x-rtp,media=video,encoding-name=H264,payload=96 "
            f"! webrtcbin name=webrtc bundle-policy=max-bundle"
        )

    def start(self, send_json) -> None:
        """Runs in an executor thread (parse_launch + state changes block)."""
        try:
            self._start(send_json)
        except StreamError as exc:
            err(f"session: {exc}")
            self.stop()
            self.on_fatal(1)
            raise

    def _start(self, send_json) -> None:
        with self._lock:
            self.send_json = send_json
            fifo = Path(self.args.run_dir) / f"nxas-cap-{os.getpid()}.raw"

            self.capture = Capture(
                output=self.args.output,
                fifo=fifo,
                fps=self.args.fps,
                wayland_display=self.args.wayland_display,
                on_death=self.on_fatal,
            )
            self.capture.start()

            desc = self._launch_string(fifo)
            log(f"pipeline: {desc}")
            try:
                self.pipeline = Gst.parse_launch(desc)
            except GLib.Error as exc:
                raise StreamError(
                    f"pipeline build failed: {exc.message}. Missing plugin? "
                    "rtph264pay comes from gst-plugins-good, webrtcbin from "
                    "gst-plugins-bad, vah264enc from gst-plugin-va.")

            venc = self.pipeline.get_by_name("venc")
            configure_encoder(venc, self.encoder_name, self.args.bitrate, self.args.fps)

            pay = self.pipeline.get_by_name("pay")
            if pay.find_property("aggregate-mode"):
                Gst.util_set_object_arg(pay, "aggregate-mode", "zero-latency")

            self.webrtc = self.pipeline.get_by_name("webrtc")
            # No STUN/TURN on purpose: LAN or Tailscale/WireGuard only, so host
            # candidates are all we ever need.
            self.webrtc.set_property("stun-server", None)

            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_bus_error)
            bus.connect("message::warning", self._on_bus_warning)
            bus.connect("message::eos", self._on_bus_eos)
            bus.connect("message::state-changed", self._on_state_changed)

            self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
            self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
            for prop in ("ice-connection-state", "ice-gathering-state",
                         "connection-state", "signaling-state"):
                self.webrtc.connect(f"notify::{prop}", self._on_state_notify)

            # READY first: webrtcbin needs to exist properly before we hang a
            # datachannel off it, but must NOT be PLAYING yet or negotiation
            # starts underneath us.
            if self.pipeline.set_state(Gst.State.READY) == Gst.StateChangeReturn.FAILURE:
                raise StreamError("pipeline could not reach READY")

            self._create_channel()
            self._sendonly_done = self._force_sendonly()

            ret = self.pipeline.set_state(Gst.State.PLAYING)
            log(f"pipeline: -> PLAYING ({ret.value_nick})")
            if ret == Gst.StateChangeReturn.FAILURE:
                raise StreamError("pipeline could not reach PLAYING")

            self._setup_done = True
        # Offer only once everything above is wired; on-negotiation-needed may
        # already have fired and parked itself in _negotiation_wanted.
        GLib.idle_add(self._maybe_offer)

    def _create_channel(self) -> None:
        opts = None
        try:
            opts = Gst.Structure.new_empty("options")
            opts.set_value("ordered", True)
        except Exception as exc:  # pragma: no cover - binding differences
            warn(f"datachannel: options structure unsupported ({exc}), using defaults")
            opts = None
        self.channel = self.webrtc.emit("create-data-channel", "input", opts)
        if self.channel is None:
            err("datachannel: create-data-channel returned None — touch input "
                "will not work for this client (video should still flow)")
            return
        self.channel.connect("on-open", lambda _c: log("datachannel: open"))
        self.channel.connect("on-close", lambda _c: log("datachannel: closed"))
        self.channel.connect("on-error", lambda _c, e: err(f"datachannel: {e}"))
        self.channel.connect("on-message-string", self._on_channel_message)
        log("datachannel: 'input' created before negotiation (ordered)")

    def _force_sendonly(self, quiet: bool = False) -> bool:
        """webrtcbin creates the transceiver when its sink pad is requested,
        which parse_launch already did — but on some versions it only
        materializes once the element is PLAYING, so this gets a second try
        just before the offer."""
        tr = self.webrtc.emit("get-transceiver", 0)
        if tr is None:
            if not quiet:
                warn("webrtc: no transceiver at index 0 yet, retrying at offer time")
            return False
        tr.set_property("direction", GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
        log("webrtc: video transceiver direction=sendonly")
        return True

    # -- negotiation (all of this runs on GStreamer/GLib threads) -----------
    def _on_negotiation_needed(self, _element) -> None:
        log("webrtc: on-negotiation-needed")
        self._negotiation_wanted = True
        GLib.idle_add(self._maybe_offer)

    def _maybe_offer(self) -> bool:
        if not (self._setup_done and self._negotiation_wanted):
            return False
        self._negotiation_wanted = False
        if not self._sendonly_done:
            self._sendonly_done = self._force_sendonly(quiet=True)
            if not self._sendonly_done:
                warn("webrtc: still no transceiver — offering whatever "
                     "webrtcbin defaults to (expect sendrecv video)")
        log("webrtc: creating offer")
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None, None)
        self.webrtc.emit("create-offer", None, promise)
        return False

    def _on_offer_created(self, promise, _u1, _u2) -> None:
        promise.wait()
        reply = promise.get_reply()
        if reply is None:
            err("webrtc: create-offer produced no reply")
            return
        offer = reply.get_value("offer")
        self.webrtc.emit("set-local-description", offer, Gst.Promise.new())
        text = offer.sdp.as_text()
        log(f"webrtc: local offer set — {sdp_summary(text)}")
        # THREAD BOUNDARY 1: GStreamer thread -> asyncio (WebSocket send).
        self.send_json({"type": "offer", "sdp": text})

    def _on_ice_candidate(self, _element, mline_index, candidate) -> None:
        # THREAD BOUNDARY 1 (again): GStreamer thread -> asyncio.
        self.send_json({"type": "ice", "candidate": candidate,
                        "sdpMLineIndex": int(mline_index)})

    # THREAD BOUNDARY 2: asyncio -> GStreamer.  Both of these are only ever
    # called via GLib.idle_add() from the WebSocket handler, so they execute on
    # the GLib main-loop thread like every other webrtcbin call.
    def apply_answer(self, sdp_text: str) -> bool:
        if self.webrtc is None:
            return False
        msg = sdp_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, msg)
        self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())
        log(f"webrtc: remote answer set — {sdp_summary(sdp_text)}")
        return False

    def apply_ice(self, mline_index: int, candidate: str) -> bool:
        if self.webrtc is None:
            return False
        self.webrtc.emit("add-ice-candidate", mline_index, candidate)
        return False

    # -- datachannel input --------------------------------------------------
    def _on_channel_message(self, channel, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            warn(f"input: non-JSON datachannel message ({text[:60]!r})")
            return
        kind = msg.get("t")
        if kind == "ping":
            channel.emit("send-string", json.dumps({"t": "pong", "ts": msg.get("ts")}))
            return
        if kind not in ("td", "tm", "tu"):
            warn(f"input: unknown event type {kind!r}")
            return
        if self.injector is None:
            return
        try:
            ident = int(msg["id"])
            x = msg.get("x")
            y = msg.get("y")
            # An injector must never take the stream down with it: a broken
            # touch path is a degraded session, not a dead one.
            self.injector.handle_touch(kind, ident,
                                       None if x is None else float(x),
                                       None if y is None else float(y))
        except Exception as exc:
            warn(f"input: {kind} failed: {exc}")

    # -- bus ----------------------------------------------------------------
    def _on_bus_error(self, _bus, message) -> None:
        gerror, debug = message.parse_error()
        err(f"gst: {message.src.get_name()}: {gerror.message}")
        if debug:
            err(f"gst: debug: {debug.strip()}")
        self.on_fatal(1)

    def _on_bus_warning(self, _bus, message) -> None:
        gerror, debug = message.parse_warning()
        warn(f"gst: {message.src.get_name()}: {gerror.message}")
        if debug:
            warn(f"gst: debug: {debug.strip()}")

    def _on_bus_eos(self, _bus, _message) -> None:
        err("gst: end-of-stream on the capture fifo — wf-recorder stopped writing")
        self.on_fatal(1)

    def _on_state_changed(self, _bus, message) -> None:
        if message.src is not self.pipeline:
            return
        old, new, _pending = message.parse_state_changed()
        log(f"pipeline: {old.value_nick} -> {new.value_nick}")

    def _on_state_notify(self, element, pspec) -> None:
        value = element.get_property(pspec.name)
        nick = getattr(value, "value_nick", value)
        log(f"webrtc: {pspec.name} = {nick}")

    # -- teardown -----------------------------------------------------------
    def stop(self) -> None:
        with self._lock:
            self._setup_done = False
            self._negotiation_wanted = False
            self._sendonly_done = False
            self.channel = None
            if self.pipeline is not None:
                log("pipeline: -> NULL")
                bus = self.pipeline.get_bus()
                try:
                    bus.remove_signal_watch()
                except Exception:
                    pass
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
            self.webrtc = None
            if self.capture is not None:
                self.capture.stop()
                self.capture = None
            self.send_json = None


# ----------------------------------------------------------------- input ----

class Injector:
    """What the datachannel talks to. Two implementations, one contract:

        handle_touch(kind, ident, x, y)   kind in td/tm/tu, x/y normalized
                                          0..1 (None on tu), ident is the
                                          browser's Touch.identifier.

    Called on a GStreamer thread. Implementations must never raise something
    that would take the video pipeline down with them.
    """

    label = "none"

    def handle_touch(self, kind: str, ident: int, x, y) -> None:
        raise NotImplementedError

    def release_all(self) -> None:
        """Client vanished mid-gesture: lift every contact."""

    def close(self) -> None:
        pass

    async def aclose(self) -> None:
        self.close()


class TouchInjector(Injector):
    """Virtual multitouch screen (evdev protocol B) fed by the datachannel.

    NOT the v0.1 default. Our headless sway runs without a seat, so it has no
    libinput backend and nothing consumes these events — this is for seat-ful
    rigs (a real compositor session, or v1.0's nx-compositor before it grows
    wlr_seat_touch_* injection). Reach for it with --input uinput.

    Adapted from Sunshine (src/platform/linux/input.cpp), GPL-3.0
    https://github.com/LizardByte/Sunshine — thank you LizardByte.
    Changes: Python/python-evdev instead of libevdev C, touch-only (no
    gamepad/pen/scroll paths), normalized 0..1 coordinates mapped to a fixed
    portrait panel, slot allocation keyed by the browser's Touch.identifier.
    """

    MAX_SLOTS = 10
    label = "uinput"

    def __init__(self, width: int, height: int):
        # Imported late so --input scrcpy/none never needs python-evdev.
        from evdev import AbsInfo, UInput, ecodes

        self.ecodes = ecodes
        self.width = width
        self.height = height
        self.slots = {}          # client pointer id -> slot
        self.free = list(range(self.MAX_SLOTS))
        self.active_slot = None  # mirrors ABS_MT_SLOT to avoid redundant events
        self.next_tracking_id = 1

        caps = {
            ecodes.EV_KEY: [ecodes.BTN_TOUCH],
            ecodes.EV_ABS: [
                (ecodes.ABS_MT_SLOT, AbsInfo(0, 0, self.MAX_SLOTS - 1, 0, 0, 0)),
                (ecodes.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                (ecodes.ABS_MT_POSITION_X, AbsInfo(0, 0, width - 1, 0, 0, 0)),
                (ecodes.ABS_MT_POSITION_Y, AbsInfo(0, 0, height - 1, 0, 0, 0)),
                (ecodes.ABS_X, AbsInfo(0, 0, width - 1, 0, 0, 0)),
                (ecodes.ABS_Y, AbsInfo(0, 0, height - 1, 0, 0, 0)),
            ],
        }
        name = "nxas-touch"
        try:
            # INPUT_PROP_DIRECT is what tells libinput "this is a touchscreen,
            # not a trackpad" — without it sway treats contacts as pointer
            # gestures and nothing lands where you tapped.
            self.ui = UInput(caps, name=name,
                             input_props=[ecodes.INPUT_PROP_DIRECT])
        except TypeError:
            warn("input: python-evdev too old for input_props; touchscreen may "
                 "be misdetected as a touchpad by libinput")
            self.ui = UInput(caps, name=name)
        except PermissionError as exc:
            raise SystemExit(
                f"input: cannot open /dev/uinput ({exc}). Add yourself to the "
                "'input' group (and a udev rule for /dev/uinput), or use "
                "--input scrcpy / --input none.") from exc
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise SystemExit(
                    "input: /dev/uinput missing — 'sudo modprobe uinput', or "
                    "use --input scrcpy / --input none.") from exc
            raise
        log(f"input: uinput device '{name}' up ({width}x{height}, "
            f"{self.MAX_SLOTS} slots)")

    # -- helpers ------------------------------------------------------------
    def _clamp(self, value, hi):
        n = int(round(value * hi))
        return 0 if n < 0 else (hi if n > hi else n)

    def _select(self, slot):
        if self.active_slot != slot:
            self.ui.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_SLOT, slot)
            self.active_slot = slot

    def _write_position(self, slot, x, y, primary):
        px = self._clamp(x, self.width - 1)
        py = self._clamp(y, self.height - 1)
        self.ui.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_POSITION_X, px)
        self.ui.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_POSITION_Y, py)
        if primary:
            # Single-touch emulation mirrors for anything that ignores MT.
            self.ui.write(self.ecodes.EV_ABS, self.ecodes.ABS_X, px)
            self.ui.write(self.ecodes.EV_ABS, self.ecodes.ABS_Y, py)

    # -- events -------------------------------------------------------------
    def handle_touch(self, kind: str, ident: int, x, y) -> None:
        if kind == "td":
            self._down(ident, x, y)
        elif kind == "tm":
            self._move(ident, x, y)
        elif kind == "tu":
            self._up(ident)

    def _down(self, pid, x, y) -> None:
        if pid in self.slots:                 # duplicate down: treat as move
            self._move(pid, x, y)
            return
        if not self.free:
            warn("input: all touch slots busy, dropping contact")
            return
        first = not self.slots
        slot = self.free.pop(0)
        self.slots[pid] = slot
        tracking = self.next_tracking_id
        self.next_tracking_id = (self.next_tracking_id + 1) % 65535 or 1

        self._select(slot)
        self.ui.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_TRACKING_ID, tracking)
        self._write_position(slot, x, y, primary=first)
        if first:
            self.ui.write(self.ecodes.EV_KEY, self.ecodes.BTN_TOUCH, 1)
        self.ui.syn()

    def _move(self, pid, x, y) -> None:
        slot = self.slots.get(pid)
        if slot is None:                      # move without down: recover
            self._down(pid, x, y)
            return
        self._select(slot)
        self._write_position(slot, x, y, primary=(len(self.slots) == 1))
        self.ui.syn()

    def _up(self, pid) -> None:
        slot = self.slots.pop(pid, None)
        if slot is None:
            return
        self.free.append(slot)
        self.free.sort()
        self._select(slot)
        self.ui.write(self.ecodes.EV_ABS, self.ecodes.ABS_MT_TRACKING_ID, -1)
        if not self.slots:
            self.ui.write(self.ecodes.EV_KEY, self.ecodes.BTN_TOUCH, 0)
        self.ui.syn()

    def release_all(self) -> None:
        if not self.slots:
            return
        log(f"input: releasing {len(self.slots)} stuck contact(s)")
        for pid in list(self.slots):
            self._up(pid)

    def close(self) -> None:
        self.release_all()
        try:
            self.ui.close()
        except Exception:
            pass


class ScrcpyInjector(Injector):
    """Touch injection *inside* Android, over scrcpy's control socket.

    This is the v0.1 default. Our headless sway has no seat and therefore no
    libinput backend, so uinput events would land nowhere; going in through
    Android's own input pipeline sidesteps the compositor entirely.

    Protocol knowledge adapted from scrcpy v4.1 (Apache-2.0)
    https://github.com/Genymobile/scrcpy — thank you Genymobile:
      app/src/control_msg.h  — SC_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT == 2
      app/src/control_msg.c  — the 32-byte touch serialization below
      app/src/server.c       — CLASSPATH/app_process launch, scid, the
                               localabstract:scrcpy_<scid> tunnel
      server/src/main/java/com/genymobile/scrcpy/control/Controller.java
                             — with video=false the server has no
                               PositionMapper and uses our raw coordinates
    Changes: Python/asyncio instead of C, touch-only (no keyboard, clipboard,
    UHID or device messages), no video/audio sockets at all, and a supervisor
    that restarts the server at most once a minute instead of aborting.
    """

    label = "scrcpy"

    REMOTE_JAR = "/data/local/tmp/nxas-scrcpy-server.jar"
    SOCKET_PREFIX = "scrcpy_"
    TYPE_INJECT_TOUCH = 2
    ACTION_DOWN, ACTION_UP, ACTION_MOVE = 0, 1, 2   # AMOTION_EVENT_ACTION_*
    PRESSURE_DOWN = 0xFFFF                          # sc_float_to_u16fp(1.0f)
    TOUCH_STRUCT = struct.Struct(">BBQiiHHHII")     # 32 bytes, big-endian
    RESTART_COOLDOWN = 60.0
    LEASES = "/var/lib/misc/dnsmasq.waydroid0.leases"

    def __init__(self, args, loop: asyncio.AbstractEventLoop):
        self.args = args
        self.loop = loop
        self.width = args.width
        self.height = args.height
        self.adb = args.adb or "adb"
        self.serial = args.adb_serial          # never run adb without this
        self.scid = random.randrange(1, 0x7FFFFFFF)
        self.port = None
        self.proc = None                       # the `adb shell app_process` pid
        self.reader = None
        self.writer = None
        self.connected = False
        self.last_pos = {}                     # pointer id -> (x, y) pixels
        self._closing = False
        self._tasks = []
        self._last_attempt = 0.0
        self._drop_logged_at = 0.0
        self._dropped = 0

    # -- adb plumbing -------------------------------------------------------
    async def _run(self, *argv, timeout=20.0, check=True):
        """Every adb call goes through here, and every one of them carries
        -s <serial>. A bare `adb shell` would happily target whatever phone
        happens to be plugged into the machine — never do that."""
        if not self.serial:
            raise StreamError("refusing to run adb without an explicit serial")
        cmd = [self.adb, "-s", self.serial, *argv]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise StreamError(f"adb {' '.join(argv[:2])} timed out after {timeout}s")
        text = out.decode("utf-8", "replace").strip()
        if check and proc.returncode != 0:
            raise StreamError(f"adb {' '.join(argv[:2])} failed: {text or proc.returncode}")
        return text

    def _waydroid_ip(self):
        """Waydroid's container IP, cheapest source first."""
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
        try:
            out = subprocess.run(["ip", "-4", "neigh", "show", "dev", "waydroid0"],
                                 stdout=subprocess.PIPE, timeout=5)
            for line in out.stdout.decode().splitlines():
                if line.split():
                    return line.split()[0]
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    async def _resolve_device(self) -> None:
        if self.serial:
            return
        ip = self._waydroid_ip()
        if not ip:
            raise StreamError(
                "cannot find the Waydroid container IP (no DHCP lease in "
                f"{self.LEASES}, no 'IP address' from waydroid status). Android "
                "may still be booting, or pass --adb-serial explicitly.")
        target = f"{ip}:5555"
        proc = await asyncio.create_subprocess_exec(
            self.adb, "connect", target,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), 20.0)
        text = out.decode("utf-8", "replace").strip()
        if "connected to" not in text:
            raise StreamError(f"adb connect {target}: {text}")
        self.serial = target
        log(f"input: adb connected to waydroid at {target} ({text})")

    async def _wait_boot(self) -> None:
        deadline = time.monotonic() + self.args.adb_timeout
        announced = False
        while not self._closing and time.monotonic() < deadline:
            state = await self._run("shell", "getprop", "sys.boot_completed",
                                    timeout=15.0, check=False)
            if state.strip() == "1":
                if announced:
                    log("input: android finished booting")
                return
            if not announced:
                log("input: waiting for android to finish booting "
                    f"(sys.boot_completed={state.strip()!r})")
                announced = True
            await asyncio.sleep(3.0)
        if self._closing:
            raise StreamError("shutting down")
        raise StreamError(
            f"android never reported sys.boot_completed=1 within "
            f"{self.args.adb_timeout}s")

    def _server_version(self) -> str:
        """The server jar refuses a version string that isn't its own, so ask
        the installed client binary rather than hardcoding one."""
        if self.args.scrcpy_version:
            return self.args.scrcpy_version
        try:
            out = subprocess.run(["scrcpy", "--version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=10)
            first = out.stdout.decode("utf-8", "replace").splitlines()[0]
            parts = first.split()
            if len(parts) >= 2 and parts[0] == "scrcpy":
                return parts[1]
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
        raise StreamError(
            "could not determine the scrcpy version ('scrcpy --version' "
            "failed) — pass --scrcpy-version to match "
            f"{self.args.scrcpy_server}")

    # -- bring-up -----------------------------------------------------------
    async def _bringup(self) -> None:
        jar = Path(self.args.scrcpy_server)
        if not jar.is_file():
            raise StreamError(f"scrcpy server jar not found at {jar} "
                              "(pacman -S scrcpy, or pass --scrcpy-server)")
        await self._resolve_device()
        await self._wait_boot()

        version = self._server_version()
        await self._run("push", str(jar), self.REMOTE_JAR, timeout=60.0)
        log(f"input: pushed {jar.name} -> {self.REMOTE_JAR} (scrcpy {version})")

        socket_name = f"{self.SOCKET_PREFIX}{self.scid:08x}"
        # Arg names verified against the installed build: every key here also
        # appears in scrcpy-server's Options parser, and unknown keys make the
        # server abort. tunnel_forward is load-bearing — without it the server
        # tries to connect out instead of listening on the abstract socket.
        server_cmd = [
            "shell",
            f"CLASSPATH={self.REMOTE_JAR}",
            "app_process", "/", "com.genymobile.scrcpy.Server", version,
            f"scid={self.scid:08x}",
            "log_level=info",
            "video=false", "audio=false", "control=true",
            "cleanup=true", "tunnel_forward=true",
        ]
        self.proc = await asyncio.create_subprocess_exec(
            self.adb, "-s", self.serial, *server_cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        log(f"input: scrcpy server started (pid {self.proc.pid}, "
            f"scid={self.scid:08x}, control-only)")
        self._tasks.append(asyncio.ensure_future(self._pump_server_log()))

        forward = await self._run("forward", "tcp:0", f"localabstract:{socket_name}")
        try:
            self.port = int(forward.split()[-1])
        except (ValueError, IndexError):
            raise StreamError(f"adb forward returned no port: {forward!r}")
        log(f"input: adb forward tcp:{self.port} -> localabstract:{socket_name}")

        await self._connect()
        self.connected = True
        self._tasks.append(asyncio.ensure_future(self._drain()))
        log(f"input: control socket up on 127.0.0.1:{self.port} — "
            f"touch goes to android at {self.width}x{self.height}")

    async def _connect(self) -> None:
        """adb accepts the TCP connection even when the abstract socket isn't
        there yet and then hangs up, so a connect that succeeds proves nothing:
        wait for the server's handshake byte(s) instead."""
        deadline = time.monotonic() + 15.0
        last = "?"
        while not self._closing and time.monotonic() < deadline:
            if self.proc and self.proc.returncode is not None:
                raise StreamError(
                    f"scrcpy server exited (rc={self.proc.returncode}) before "
                    "the control socket was ready")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            except OSError as exc:
                last = str(exc)
                await asyncio.sleep(0.2)
                continue
            try:
                # Whatever the build prepends (dummy byte, 64-byte device
                # name), it is not a control message: swallow it. An immediate
                # EOF means adb could not reach the abstract socket yet.
                head = await asyncio.wait_for(reader.read(256), 2.0)
            except asyncio.TimeoutError:
                warn("input: no scrcpy handshake byte; proceeding anyway")
                self.reader, self.writer = reader, writer
                return
            if not head:
                writer.close()
                last = "server closed the connection immediately"
                await asyncio.sleep(0.2)
                continue
            log(f"input: scrcpy handshake ok ({len(head)} byte(s) drained)")
            self.reader, self.writer = reader, writer
            return
        raise StreamError(f"could not reach the scrcpy control socket: {last}")

    async def _pump_server_log(self) -> None:
        """The server's own logs, verbatim — the only window into what Android
        thinks of our events."""
        proc = self.proc
        try:
            while proc.stdout is not None:
                line = await proc.stdout.readline()
                if not line:
                    break
                _emit(_PURPLE, "scrcpy", line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warn(f"input: server log pump stopped: {exc}")
        finally:
            self._mark_dead(f"scrcpy server exited (rc={proc.returncode})")

    async def _drain(self) -> None:
        """We never read control replies, but the read side is how we learn the
        socket died."""
        try:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warn(f"input: control socket read error: {exc}")
        self._mark_dead("control socket closed")

    def _mark_dead(self, reason: str = "") -> None:
        """Idempotent: the socket reader and the process watcher both notice a
        death, and one loud line about it is plenty."""
        if self.connected and reason and not self._closing:
            err(f"input: {reason} — TOUCH IS DEAD, video keeps running; "
                f"retrying within {int(self.RESTART_COOLDOWN)}s")
        self.connected = False
        self.last_pos.clear()
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
            self.writer = None

    # -- supervision --------------------------------------------------------
    def start_background(self) -> None:
        self._tasks.append(asyncio.ensure_future(self._supervise()))

    async def _supervise(self) -> None:
        while not self._closing:
            if not self.connected:
                waited = time.monotonic() - self._last_attempt
                if self._last_attempt == 0.0 or waited >= self.RESTART_COOLDOWN:
                    self._last_attempt = time.monotonic()
                    try:
                        await self._teardown_server()
                        await self._bringup()
                    except StreamError as exc:
                        err(f"input: scrcpy bring-up failed: {exc}")
                        err("input: video is unaffected; retrying in "
                            f"{int(self.RESTART_COOLDOWN)}s")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        err(f"input: scrcpy bring-up crashed: {exc!r}")
            await asyncio.sleep(2.0)

    async def _teardown_server(self) -> None:
        self._mark_dead()
        for task in list(self._tasks):
            if task is not asyncio.current_task() and task.done():
                self._tasks.remove(task)
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3.0)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
        if self.port is not None and self.serial:
            try:
                await self._run("forward", "--remove", f"tcp:{self.port}",
                                timeout=10.0, check=False)
            except StreamError:
                pass
            self.port = None
        # A fresh scid every attempt: the old abstract socket may still linger.
        self.scid = random.randrange(1, 0x7FFFFFFF)

    # -- events -------------------------------------------------------------
    def _clamp(self, value, hi):
        n = int(round(value * hi))
        return 0 if n < 0 else (hi if n > hi else n)

    def handle_touch(self, kind: str, ident: int, x, y) -> None:
        if kind == "tu":
            pos = self.last_pos.pop(ident, None)
            if pos is None:
                return                       # up for a contact we never saw
            px, py = pos
            action, pressure = self.ACTION_UP, 0
        else:
            if x is None or y is None:
                return
            px = self._clamp(x, self.width - 1)
            py = self._clamp(y, self.height - 1)
            known = ident in self.last_pos
            self.last_pos[ident] = (px, py)
            # A move for an untracked contact becomes a down, and a second down
            # for a tracked one becomes a move — Android dislikes both.
            action = self.ACTION_MOVE if known else self.ACTION_DOWN
            pressure = self.PRESSURE_DOWN
        payload = self.TOUCH_STRUCT.pack(
            self.TYPE_INJECT_TOUCH,
            action,
            ident & 0xFFFFFFFFFFFFFFFF,      # pointer id, the client's own
            px, py,                          # raw device pixels (video=false)
            self.width, self.height,
            pressure,
            0,                               # action_button: mouse only
            0,                               # buttons: mouse only
        )
        # THREAD BOUNDARY 4: GStreamer thread -> asyncio. call_soon_threadsafe
        # keeps the event order and never blocks the streaming thread.
        self.loop.call_soon_threadsafe(self._write, payload)

    def _write(self, payload: bytes) -> None:
        """Runs on the asyncio loop thread."""
        writer = self.writer
        if writer is None or not self.connected:
            self._note_drop()
            return
        try:
            transport = writer.transport
            if transport is not None and transport.get_write_buffer_size() > 65536:
                self._note_drop()            # socket wedged; don't grow forever
                return
            writer.write(payload)
        except Exception as exc:
            self._mark_dead(f"control write failed ({exc})")

    def _note_drop(self) -> None:
        self._dropped += 1
        now = time.monotonic()
        if now - self._drop_logged_at >= 5.0:
            self._drop_logged_at = now
            warn(f"input: dropping touch events ({self._dropped} so far) — "
                 "scrcpy control channel is down")

    def release_all(self) -> None:
        if not self.last_pos:
            return
        log(f"input: releasing {len(self.last_pos)} contact(s) into android")
        for ident in list(self.last_pos):
            self.handle_touch("tu", ident, None, None)

    async def aclose(self) -> None:
        self._closing = True
        self.release_all()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self._teardown_server()
        log("input: scrcpy injector closed")

    def close(self) -> None:
        self._closing = True


# ------------------------------------------------------------ hub bus ----

_MISSING = object()


class HubConnector:
    """Streams live daemon status onto NX Hub's local connector bus.

    Wholly optional and wholly silent.  On a machine where NX Hub is not
    installed the token file never appears and nothing answers on port 9021 —
    that is the common case, so we never log, warn, or fail our own startup over
    it (§8 of PROTOCOL.md).  The whole lifecycle is wrapped so an exception just
    retries; it must never block or crash the streamer.

    We lean on aiohttp's WebSocket *client*, which is already a dependency: it
    masks every client->hub frame (§1) and verifies the server's
    Sec-WebSocket-Accept during the handshake (§1) for us, so there is no
    hand-rolled RFC 6455 to get wrong here.
    """

    URL = "ws://127.0.0.1:9021"          # loopback only, per §1
    APP_ID = "nx-android-streamer"        # repo name, lowercased — attaches the
                                          # status strip to the right hub card
    MAX_FRAME = 16384                     # §1: 16 KB reassembled cap
    BACKOFF_START = 1.0
    BACKOFF_MAX = 30.0
    MIN_SEND_INTERVAL = 0.25              # §4: pre-throttle to <= 4 status/s

    def __init__(self, *, version, on_shutdown_request, data_dir=None):
        self.version = str(version)
        self._on_shutdown_request = on_shutdown_request
        self._data_dir = data_dir         # explicit override (tests); otherwise
                                          # $NX_HUB_DATA_DIR / the default path
        self._fields = {}                 # last-known full merged status
        self._dirty = asyncio.Event()     # created on the loop thread
        self._ws = None                   # live client ws, for a best-effort bye
        self._closing = False
        self._last_send = 0.0

    # -- token --------------------------------------------------------------
    def _token_path(self) -> Path:
        base = self._data_dir or os.environ.get("NX_HUB_DATA_DIR")
        if base:
            return Path(base) / "connector.token"
        return Path.home() / ".local" / "share" / "nx-hub" / "connector.token"

    def _read_token(self):
        """Re-read on every attempt (§2): the file may not exist yet if we
        started before NX Hub ever ran.  Missing == 'no hub', stay silent."""
        try:
            raw = self._token_path().read_text()
        except (OSError, ValueError):
            return None
        tok = raw.rstrip("\r\n")           # §2: trim the trailing newline
        return tok or None

    # -- status feed --------------------------------------------------------
    def set_status(self, **fields) -> None:
        """Called from the daemon's lifecycle points (on the asyncio loop).
        Merges into the full status and wakes the sender only on a real change.
        Never raises — a status update must not be able to disturb the stream."""
        changed = False
        for key, value in fields.items():
            if self._fields.get(key, _MISSING) != value:
                self._fields[key] = value
                changed = True
        if changed:
            self._dirty.set()

    # -- lifecycle ----------------------------------------------------------
    async def run(self) -> None:
        """Reconnect forever with exponential backoff (§8), silently.  Wrapped
        so no failure here ever escapes into the daemon."""
        backoff = self.BACKOFF_START
        while not self._closing:
            reached_welcome = False
            try:
                reached_welcome = await self._session_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:       # pragma: no cover - defensive
                dbg(f"hub: session crashed ({exc!r})")
            if self._closing:
                break
            if reached_welcome:
                backoff = self.BACKOFF_START   # §8: reset after a good session
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, self.BACKOFF_MAX)

    async def _session_once(self) -> bool:
        """One connect->hello->welcome->serve cycle.  Returns True if we ever
        reached `welcome` (which resets the backoff)."""
        tok = self._read_token()
        if tok is None:
            return False                   # no hub / no token yet — silent
        reached_welcome = False
        welcome = asyncio.Event()
        session = aiohttp.ClientSession()
        try:
            try:
                ws = await session.ws_connect(self.URL, max_msg_size=self.MAX_FRAME)
            except Exception as exc:       # refused == no bus is up; be silent
                dbg(f"hub: connect failed ({exc!r})")
                return False
            self._ws = ws
            try:
                await ws.send_json(self._hello(tok))   # §3: hello comes first
            except Exception as exc:
                dbg(f"hub: hello failed ({exc!r})")
                return False
            sender = asyncio.ensure_future(self._sender_loop(ws, welcome))
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                        except ValueError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        kind = data.get("type")
                        if kind == "welcome":
                            reached_welcome = True
                            welcome.set()
                        elif kind == "ping":
                            # §5: liveness — a silent daemon is reaped at 90 s.
                            try:
                                await ws.send_json({"type": "pong"})
                            except Exception:
                                break
                        elif kind == "shutdown-request":
                            # §7: a polite request; exit exactly like SIGTERM.
                            self._request_shutdown()
                        elif kind == "error":
                            dbg(f"hub: error frame: {data.get('message')!r}")
                        # unknown types are ignored (§3/§6): forward compatible
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                                      WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
            finally:
                # Awaiting the sender we just cancelled re-raises its
                # CancelledError here; that is the *child's* cancellation, not
                # ours, so swallow it — otherwise it escapes and kills the whole
                # reconnect loop on the first disconnect.  A genuine shutdown
                # cancels the run() task and is handled by the _closing check.
                sender.cancel()
                try:
                    await sender
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            self._ws = None
            try:
                await session.close()
            except Exception:
                pass
        return reached_welcome

    async def _sender_loop(self, ws, welcome: asyncio.Event) -> None:
        """Waits for welcome, resends the full status once (§8: the hub starts
        our slot empty), then pushes on change, pre-throttled to <= 4/s."""
        try:
            await welcome.wait()
            loop = asyncio.get_running_loop()
            await self._flush(ws)          # §8: full resend right after connect
            while True:
                await self._dirty.wait()
                self._dirty.clear()
                gap = self._last_send + self.MIN_SEND_INTERVAL - loop.time()
                if gap > 0:
                    # Coalesce a burst into one send of the latest value, so we
                    # never rely on the hub silently dropping our excess (§4).
                    await asyncio.sleep(gap)
                    self._dirty.clear()
                await self._flush(ws)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            dbg(f"hub: sender stopped ({exc!r})")

    async def _flush(self, ws) -> None:
        fields = dict(self._fields)        # send the full merged set every time
        if not fields:
            return
        await ws.send_json({"type": "status", "fields": fields})
        self._last_send = asyncio.get_running_loop().time()

    def _hello(self, tok: str) -> dict:
        return {
            "type": "hello",
            "app": self.APP_ID,
            "version": self.version,
            "pid": os.getpid(),
            "token": tok,
            "caps": ["status"],
        }

    def _request_shutdown(self) -> None:
        cb = self._on_shutdown_request
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:           # pragma: no cover - defensive
            dbg(f"hub: shutdown callback failed ({exc!r})")

    async def aclose(self) -> None:
        """Daemon is shutting down: send a best-effort `bye` (§8) and stop."""
        self._closing = True
        ws = self._ws
        if ws is not None and not ws.closed:
            try:
                await ws.send_json({"type": "bye"})
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass


# ------------------------------------------------------------- signaling ----

class Daemon:
    """Owns the single-client policy and both event loops' handoffs."""

    def __init__(self, args, injector, on_fatal, hub=None):
        self.args = args
        self.injector = injector
        self.on_fatal = on_fatal
        self.hub = hub
        self.loop = asyncio.get_running_loop()
        self.streamer = Streamer(args, injector, on_fatal)
        self.client = None
        self.gate = asyncio.Lock()

    # -- hub status ---------------------------------------------------------
    def _publish_hub(self) -> None:
        """Push the current session state onto the hub bus.  Called from the
        attach/detach seams (and once at startup to seed `idle`)."""
        if self.hub is None:
            return
        streaming = self.client is not None
        self.hub.set_status(
            state="streaming" if streaming else "idle",
            client=streaming,
            # Configured encoder bitrate in Mbps while streaming, 0 when idle.
            # Real *measured* bitrate is future work — this is the target we ask
            # the encoder for (args.bitrate is kbps).
            bitrate=round(self.args.bitrate / 1000.0, 3) if streaming else 0,
            # latency (glass-to-glass) is deliberately OMITTED: the server does
            # not currently see the client<->server RTT.  The datachannel
            # ping/pong is measured on the *client* and never echoed back here,
            # so sending anything now would be fabricated.  This key lands once
            # the client reports its measured RTT to the server.
        )

    # -- WS <-> GStreamer ---------------------------------------------------
    def _sender(self, ws):
        """Returns a thread-safe send used from GStreamer threads."""
        def send(payload):
            # THREAD BOUNDARY 1: GStreamer thread -> asyncio loop.
            if ws.closed:
                return
            asyncio.run_coroutine_threadsafe(self._safe_send(ws, payload), self.loop)
        return send

    async def _safe_send(self, ws, payload) -> None:
        try:
            await ws.send_json(payload)
        except (ConnectionResetError, RuntimeError) as exc:
            warn(f"ws: send failed ({exc})")

    async def attach(self, ws, peer: str) -> None:
        async with self.gate:
            if self.client is not None:
                log(f"ws: {peer} takes over; dropping previous client")
                old, self.client = self.client, None
                try:
                    await old.close(code=4000, message=b"replaced by newer client")
                except Exception:
                    pass
                await self.loop.run_in_executor(None, self.streamer.stop)
                if self.injector:
                    self.injector.release_all()
            self.client = ws
            log(f"ws: client {peer} connected — building pipeline")
            try:
                await self.loop.run_in_executor(
                    None, self.streamer.start, self._sender(ws))
            except StreamError as exc:
                await self._safe_send(ws, {"type": "error", "message": str(exc)})
                await ws.close(code=1011, message=b"pipeline failed")
                self.client = None
        # Reflect the outcome to the hub: streaming on success, idle if the
        # pipeline failed above (self.client tells which).
        self._publish_hub()

    async def close_client(self) -> None:
        """Shutdown: hang up on the client ourselves, otherwise aiohttp's
        cleanup sits waiting for the websocket handler to return (measured at
        ~15s with a browser attached)."""
        ws = self.client
        if ws is None:
            return
        try:
            await ws.close(code=1001, message=b"server shutting down")
        except Exception:
            pass

    async def detach(self, ws, peer: str) -> None:
        async with self.gate:
            if self.client is not ws:
                return                      # already replaced; not ours to stop
            self.client = None
            log(f"ws: client {peer} gone — tearing down pipeline")
            await self.loop.run_in_executor(None, self.streamer.stop)
            if self.injector:
                self.injector.release_all()
            self._publish_hub()            # back to idle

    async def on_message(self, msg) -> None:
        kind = msg.get("type")
        if kind == "answer":
            sdp = msg.get("sdp") or ""
            # THREAD BOUNDARY 2: asyncio -> GStreamer, via the GLib main loop.
            GLib.idle_add(self.streamer.apply_answer, sdp)
        elif kind == "ice":
            candidate = msg.get("candidate")
            if not candidate:
                return                      # end-of-candidates marker
            index = int(msg.get("sdpMLineIndex") or 0)
            # THREAD BOUNDARY 2 (again).
            GLib.idle_add(self.streamer.apply_ice, index, candidate)
        else:
            warn(f"ws: unknown message type {kind!r}")

    # -- http ---------------------------------------------------------------
    async def ws_handler(self, request):
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)
        peer = request.remote or "?"
        await self.attach(ws, peer)
        try:
            async for raw in ws:
                if raw.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(raw.data)
                    except ValueError:
                        warn(f"ws: non-JSON message from {peer}")
                        continue
                    await self.on_message(payload)
                elif raw.type == WSMsgType.ERROR:
                    err(f"ws: connection error: {ws.exception()}")
        finally:
            await self.detach(ws, peer)
        return ws

    async def health(self, _request):
        cap = self.streamer.capture
        return web.json_response({
            "state": "streaming" if self.client is not None else "idle",
            "client": self.client is not None,
            "encoder": self.streamer.encoder_name,
            "capture": bool(cap and cap.alive()),
            "geometry": f"{self.args.width}x{self.args.height}@{self.args.fps}",
            "bitrate": self.args.bitrate / 1000.0,
            "input": self.injector.label if self.injector else "none",
            "input_ready": bool(getattr(self.injector, "connected",
                                        self.injector is not None)),
        })

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/health", self.health)

        root = Path(self.args.web_root)
        index = root / "index.html"
        if index.is_file():
            async def serve_index(_request):
                return web.FileResponse(index)

            async def serve_file(request):
                # Manual resolve so a crafted path can't escape the web root.
                target = (root / request.match_info["path"]).resolve()
                if root not in target.parents or not target.is_file():
                    raise web.HTTPNotFound()
                return web.FileResponse(target)

            app.router.add_get("/", serve_index)
            # Registered last: both of these are catch-alls.
            try:
                app.router.add_static("/", str(root), show_index=False)
            except (ValueError, AssertionError):  # aiohttp dislikes a "/" prefix
                app.router.add_get("/{path:.*}", serve_file)
            log(f"http: serving {root}")
        else:
            warn(f"http: no index.html under {root} — serving a placeholder. "
                 "The v0.1 PWA client lives in web/.")

            async def placeholder(_request):
                return web.Response(
                    status=503,
                    content_type="text/plain",
                    text=f"nx-streamerd is up, but there is no client at "
                         f"{root}/index.html.\nSignaling WebSocket: /ws\n")
            app.router.add_get("/", placeholder)
        return app


# ------------------------------------------------------------------ main ----

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="nx-streamerd",
        description="Stream a headless sway output to one browser client over "
                    "WebRTC, and inject its touches back into android.")
    p.add_argument("--width", type=int, default=1080)
    p.add_argument("--height", type=int, default=2400)
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--bitrate", type=int, default=12000, help="kbps")
    p.add_argument("--wayland-display", required=True,
                   help="wayland socket of the headless session, e.g. wayland-1")
    p.add_argument("--output", default="HEADLESS-1", help="wlr output to capture")
    p.add_argument("--web-root", default=None,
                   help="static client directory (default: ../web)")
    p.add_argument("--encoder", choices=("auto", "va", "x264"), default="auto")
    p.add_argument("--input", choices=("scrcpy", "uinput", "none"),
                   default="scrcpy",
                   help="touch path: scrcpy = inject inside android over the "
                        "scrcpy control socket (default); uinput = virtual "
                        "touchscreen, only useful on a compositor with a seat; "
                        "none = video only")
    p.add_argument("--no-input", action="store_true",
                   help=argparse.SUPPRESS)          # old spelling of --input none
    p.add_argument("--adb", default=None,
                   help="adb binary (default: first on PATH)")
    p.add_argument("--adb-serial", default=None,
                   help="adb target, e.g. 192.168.240.112:5555. Default: "
                        "discover the waydroid container and connect to it. "
                        "Set this if other devices are attached.")
    p.add_argument("--adb-timeout", type=float, default=180.0,
                   help="seconds to wait for sys.boot_completed=1")
    p.add_argument("--scrcpy-server", default="/usr/share/scrcpy/scrcpy-server",
                   help="path to the scrcpy server jar to push")
    p.add_argument("--scrcpy-version", default=None,
                   help="version string the jar expects (default: ask "
                        "'scrcpy --version')")
    p.add_argument("--bind", default="0.0.0.0", help="listen address")
    p.add_argument("--no-hub", action="store_true",
                   help="disable the NX Hub connector (status bus). It is "
                        "enabled by default and stays silent when no hub is "
                        "running; honours $NX_HUB_DATA_DIR for the token path.")
    args = p.parse_args(argv)
    if args.no_input:
        args.input = "none"
    if args.adb is None:
        args.adb = shutil.which("adb")

    here = Path(__file__).resolve().parent
    args.web_root = str(Path(args.web_root).resolve()) if args.web_root \
        else str((here.parent / "web").resolve())
    args.run_dir = str(here.parent / ".run")
    return args


def build_injector(args, loop):
    """The touch path. Failing to build one is never fatal to the video."""
    if args.input == "none":
        warn("input: --input none — no touch injection at all "
             "(ping/pong still answered, so the client can measure RTT)")
        return None
    if args.input == "uinput":
        warn("input: --input uinput — only reaches the compositor if it has a "
             "seat/libinput backend; our headless sway does not (see "
             "ARCHITECTURE.md)")
        return TouchInjector(args.width, args.height)
    if not args.adb:
        err("input: no adb binary found (pass --adb) — touch disabled, "
            "video unaffected")
        return None
    injector = ScrcpyInjector(args, loop)
    injector.start_background()
    return injector


async def serve(args, glib_loop) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    exit_code = {"code": 0}

    def fatal(code: int) -> None:
        """Called from GStreamer/watchdog threads when the stream is dead."""
        exit_code["code"] = code
        # THREAD BOUNDARY 3: any thread -> asyncio, shutdown only.
        loop.call_soon_threadsafe(stop.set)

    injector = build_injector(args, loop)

    # NX Hub connector: optional, silent, and isolated. A shutdown-request on
    # the bus must trigger exactly the same clean exit as SIGTERM — set `stop`.
    hub = None
    if not args.no_hub:
        def _hub_shutdown() -> None:
            log("shutdown-request from NX Hub — shutting down")
            stop.set()
        hub = HubConnector(version=VERSION, on_shutdown_request=_hub_shutdown)

    daemon = Daemon(args, injector, fatal, hub=hub)
    try:
        runner = web.AppRunner(daemon.build_app(), access_log=None,
                               shutdown_timeout=5.0)
    except TypeError:                     # aiohttp without shutdown_timeout
        runner = web.AppRunner(daemon.build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.bind, args.port)
    await site.start()

    def on_signal(sig: signal.Signals) -> None:
        log(f"{sig.name} — shutting down")
        stop.set()

    for _sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(_sig, on_signal, _sig)

    log(f"listening on http://{args.bind}:{args.port}  (client: / , signaling: /ws)")
    log(f"capturing {args.output} on WAYLAND_DISPLAY={args.wayland_display} "
        f"at {args.width}x{args.height}@{args.fps}, {args.bitrate} kbps")

    # Seed the hub with our initial state (idle), then let it reconnect forever
    # in the background. It never blocks or crashes the streamer if absent.
    hub_task = None
    if hub is not None:
        daemon._publish_hub()
        hub_task = asyncio.ensure_future(hub.run())

    await stop.wait()

    # Say goodbye to the hub first (best-effort) and stop its reconnect loop.
    if hub is not None:
        try:
            await hub.aclose()
        except Exception as exc:          # pragma: no cover - defensive
            dbg(f"hub: aclose failed ({exc!r})")
        if hub_task is not None:
            hub_task.cancel()
            try:
                await hub_task
            except (asyncio.CancelledError, Exception):
                pass

    # Order matters: stop the injector retrying and hang up on the client
    # first, so neither can stretch the shutdown out.
    if injector is not None:
        injector.close()                  # sync: just sets the closing flag
    await daemon.close_client()
    await runner.cleanup()
    await loop.run_in_executor(None, daemon.streamer.stop)
    if injector is not None:
        try:
            await injector.aclose()
        except Exception as exc:
            warn(f"input: shutdown: {exc}")
    glib_loop.quit()
    return exit_code["code"]


def main() -> int:
    args = parse_args()
    Gst.init(None)
    log(f"nx-streamerd starting (GStreamer {Gst.version_string()}, "
        f"input={args.input})")

    # GStreamer's signals (bus watch, webrtcbin callbacks, GLib.idle_add) are
    # dispatched from the default main context; nobody else may iterate it.
    glib_loop = GLib.MainLoop()
    threading.Thread(target=glib_loop.run, name="glib", daemon=True).start()

    code = asyncio.run(serve(args, glib_loop))
    log(f"stopped (exit {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())
