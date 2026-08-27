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

# Only used to build the upstream force-key-unit event the adaptive-bitrate
# controller sends after a cut.  Optional: without it we simply skip the
# keyframe nudge rather than refusing to start.
try:
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GstVideo  # noqa: E402
except (ValueError, ImportError):       # pragma: no cover - packaging variance
    GstVideo = None

import aiohttp  # noqa: E402
from aiohttp import WSMsgType, web  # noqa: E402

# The picker + camera bridges. Self-contained on purpose: this daemon holds them
# with six one-line hooks (arguments, camera receiver, attach, detach, message
# dispatch) and streams exactly the same without the module.
import nx_bridge  # noqa: E402

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
        # NV12, not BGRx: 1.5 bytes/px instead of 4, so the GPU->CPU readback and
        # the fifo carry 2.7x less data (0.62 GB/s -> 0.23 GB/s at 1080x2400@60),
        # and it is already the encoder's native input so the CPU colour convert
        # disappears entirely. Measured: 82 fps sustained vs 79, at lower CPU.
        cmd = [WF_RECORDER, "-o", self.output, "-x", "nv12", "-c", "rawvideo",
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


# ----------------------------------------------------------------- audio ----

PACTL = "pactl"


class AudioRoute:
    """Where the container's audio can actually be picked up.

    Waydroid has no sink of its own.  Its HAL (`audio.primary.waydroid.so`) is
    plain ALSA — libasound with `PULSE_RUNTIME_PATH=/run/xdg/pulse`, a bind
    mount of the host's PulseAudio socket — so Android arrives on the host as
    one ordinary playback stream called "Waydroid", mixed into whatever the
    default sink happens to be, alongside the browser and the chat client and
    everything else.  There is therefore **no monitor source that means
    "Waydroid and only Waydroid"**, and capturing the default sink's monitor
    would ship the user's entire desktop audio to their phone.

    So `--audio auto` makes one: a private null sink whose monitor carries the
    container and nothing else.  We never touch the default sink, the default
    source, or any stream that is not Waydroid's, we re-home streams that show
    up later (Android opens and closes the PCM as apps come and go), and
    everything we moved goes back where it came from on the way out.

    `--audio <source-name>` skips all of that and captures the named source
    verbatim, for a rig that already routes Waydroid somewhere deliberate.

    Everything here is best-effort by construction: audio that cannot be set up
    is a video-only session, never a failed one.
    """

    SINK_NAME = "nxas_waydroid"
    SINK_DESC = "NX_Android_Streamer"     # no spaces: PA splits module args on them
    STREAM_MATCH = "waydroid"             # matched case-insensitively
    POLL_INTERVAL = 2.0
    TIMEOUT = 10.0

    def __init__(self, spec: str):
        self.spec = (spec or "none").strip()
        self.source = None                # what the pipeline should capture
        self._module = None               # our null-sink module id, if we own one
        self._sink_index = None           # our sink's index, as sink-inputs report it
        self._origin = {}                 # sink-input index -> the sink it came from

    # -- pactl --------------------------------------------------------------
    @classmethod
    def _pactl(cls, *argv, check=True):
        """-> stdout, or None when pactl is missing/failed.

        LC_ALL=C is load-bearing: `pactl list` is translated, and parsing
        "Senkeingang #12" for "Sink Input #12" is exactly the kind of bug that
        only shows up on somebody else's machine.
        """
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        try:
            out = subprocess.run([PACTL, *argv], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=cls.TIMEOUT,
                                 env=env)
        except FileNotFoundError:
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            dbg(f"audio: pactl {' '.join(argv[:2])} failed ({exc!r})")
            return None
        if check and out.returncode != 0:
            dbg(f"audio: pactl {' '.join(argv[:2])} rc={out.returncode}: "
                f"{out.stderr.decode('utf-8', 'replace').strip()}")
            return None
        return out.stdout.decode("utf-8", "replace")

    @classmethod
    def _sources(cls):
        text = cls._pactl("list", "short", "sources")
        if text is None:
            return []
        return [line.split("\t")[1] for line in text.splitlines()
                if len(line.split("\t")) > 1]

    @classmethod
    def _sink_inputs(cls):
        """-> [(index, sink_index, name)] for every playback stream.

        `pactl list short sink-inputs` gives indices but not application names,
        so the long form is the only way to tell Waydroid's stream apart.
        """
        text = cls._pactl("list", "sink-inputs")
        if text is None:
            return []
        found, index, sink, name = [], None, None, ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("Sink Input #"):
                if index is not None:
                    found.append((index, sink, name))
                index, sink, name = line[12:].strip(), None, ""
            elif line.startswith("Sink:"):
                sink = line.split(":", 1)[1].strip()
            elif line.startswith(("application.name = ", "node.name = ")):
                # Waydroid sets both to "Waydroid"; either will do, and taking
                # the first non-empty one keeps us working if one goes away.
                if not name:
                    name = line.split("=", 1)[1].strip().strip('"')
        if index is not None:
            found.append((index, sink, name))
        return found

    @classmethod
    def _sink_index_of(cls, name):
        """-> the index PulseAudio gives our sink, as a string, or None.
        sink-inputs name their sink by index, so this is what poll() compares."""
        text = cls._pactl("list", "short", "sinks")
        if text is None:
            return None
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1] == name:
                return parts[0]
        return None

    @classmethod
    def _our_module(cls):
        """-> the module id of an existing nxas null sink, or None.

        `pactl list short modules` is index<TAB>name<TAB>argument, so the sink
        we own is the module-null-sink whose argument names our sink.
        """
        text = cls._pactl("list", "short", "modules")
        if text is None:
            return None
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1] == "module-null-sink" \
                    and f"sink_name={cls.SINK_NAME}" in parts[2]:
                return parts[0]
        return None

    # -- lifecycle ----------------------------------------------------------
    def resolve(self):
        """Set up (if needed) and return the source name to capture, or None.

        Called once at startup, before any client can connect, so the source
        exists for the very first offer — negotiating an audio transceiver
        against a source that only appears when Android happens to be playing
        would be a coin flip.
        """
        if self.spec in ("", "none"):
            return None
        if self._pactl("info") is None:
            err(f"audio: --audio {self.spec} needs a working 'pactl' and there "
                "is none — streaming video only")
            return None
        if self.spec != "auto":
            if self.spec not in self._sources():
                err(f"audio: source {self.spec!r} does not exist "
                    "(pactl list short sources) — streaming video only")
                return None
            self.source = self.spec
            log(f"audio: capturing {self.source}")
            return self.source
        return self._auto()

    def _auto(self):
        monitor = f"{self.SINK_NAME}.monitor"
        if monitor in self._sources():
            # Left over from a daemon that was killed before release() ran.
            # Adopt it — module id and all — rather than stacking a second
            # identical sink on top of it and leaking both.
            self._module = self._our_module()
            if self._module is None:
                warn(f"audio: adopting the leftover {self.SINK_NAME} sink, but "
                     "its module id is unknown — it will not be removed on exit")
            else:
                warn(f"audio: adopting the leftover {self.SINK_NAME} sink "
                     f"(module {self._module})")
        else:
            out = self._pactl("load-module", "module-null-sink",
                              f"sink_name={self.SINK_NAME}",
                              f"sink_properties=device.description={self.SINK_DESC}")
            if out is None:
                err("audio: could not create the private Waydroid sink — "
                    "streaming video only")
                return None
            self._module = out.strip().split()[-1] if out.strip() else None
            if monitor not in self._sources():
                err(f"audio: {monitor} did not appear after load-module — "
                    "streaming video only")
                self.release()
                return None
        self.source = monitor
        self._sink_index = self._sink_index_of(self.SINK_NAME)
        log(f"audio: private sink {self.SINK_NAME} up; capturing {monitor}")
        self.poll()
        return self.source

    def poll(self) -> None:
        """Re-home any Waydroid stream that is not in our sink yet.

        Android opens and closes the PCM as apps start and stop, so a stream
        that did not exist at startup will land on the default sink like any
        other; this is what catches it.  Cheap enough to run on a timer, and a
        no-op the rest of the time.
        """
        if self.spec != "auto" or self.source is None:
            return
        if self._sink_index is None:
            self._sink_index = self._sink_index_of(self.SINK_NAME)
            if self._sink_index is None:
                return
        live = set()
        for index, sink, name in self._sink_inputs():
            live.add(index)
            if self.STREAM_MATCH not in name.lower():
                continue
            if sink == self._sink_index:
                continue                    # already ours
            # Deliberately keyed on where the stream *is*, not on whether we
            # once moved it: `pactl move-sink-input` reports success as soon as
            # the request is accepted, so a move that did not take gets retried
            # on the next tick instead of being remembered as done.
            if self._pactl("move-sink-input", index, self.SINK_NAME) is None:
                warn(f"audio: could not move stream {index} ({name}) into "
                     f"{self.SINK_NAME}")
                continue
            # setdefault, not assignment: on a retry `sink` is still the real
            # origin, but we must never overwrite it with our own sink.
            if self._origin.setdefault(index, sink) == sink:
                log(f"audio: routed Waydroid stream {index} -> {self.SINK_NAME}")
        # Forget streams that ended; their index will be reused by somebody else.
        for index in [i for i in self._origin if i not in live]:
            self._origin.pop(index, None)

    def release(self) -> None:
        """Put the graph back exactly as we found it.  Idempotent."""
        for index, sink in list(self._origin.items()):
            if sink:
                self._pactl("move-sink-input", index, sink, check=False)
        self._origin.clear()
        if self._module is not None:
            if self._pactl("unload-module", self._module) is None:
                warn(f"audio: could not unload module {self._module} — "
                     f"'pactl unload-module {self._module}' removes the leftover "
                     f"{self.SINK_NAME} sink")
            else:
                log(f"audio: private sink {self.SINK_NAME} removed")
            self._module = None
        self._sink_index = None
        self.source = None


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
        maybe("target-usage", 7)            # 1=quality .. 7=speed; latency wins
        maybe("ref-frames", 1)              # no long refs: fewer stalls, faster recovery
    else:
        maybe_arg("tune", "zerolatency")
        maybe_arg("speed-preset", "superfast")
        maybe("bitrate", bitrate_kbps)      # x264enc takes kbps too
        maybe("key-int-max", gop)
        maybe("byte-stream", False)
    log(f"encoder: {name} @ {bitrate_kbps} kbps, key-int-max={gop} ({fps} fps)")


# ------------------------------------------------------ adaptive bitrate ----

class AdaptiveBitrate:
    """Feeds RTCP-derived loss/RTT back into the encoder's bitrate.

    This is the shape of the auto-bitrate loop in our WiVRn fork (ARCHITECTURE.md
    "Transport"), ported to webrtcbin's stats: sample once a second, look at what
    changed *in that second*, and move the encoder in one of three ways.

        loss > 5%, or RTT spiking      ->  multiplicative decrease (x0.70)
        loss < 1% and RTT calm, 5x     ->  additive increase (+10% of ceiling)
        anything in between            ->  hold

    Multiplicative-decrease/additive-increase (the TCP shape) is deliberate: the
    expensive mistake is being too high — that is what drops the user's 5G
    session — so we back off fast and crawl back up slowly.

    Why we do this at all when webrtcbin already has GCC: GStreamer's congestion
    control estimates the bandwidth but nothing in this pipeline *listens* to the
    estimate, because our encoder bitrate is a plain property on a `vah264enc`
    that GCC has never heard of.  This class is the missing wire.

    Slow start: a session opens at 60% of the ceiling, not at the ceiling.  The
    failure this whole class exists to fix is the *first* seconds of a mobile
    session — a full-rate H.264 stream punched into a 5G uplink before anybody
    has measured anything.  Probing upward costs ~20 s to reach the ceiling on a
    link that can take it; probing downward costs a dropped connection.

    Threading: `tick()` runs on the GLib main-loop thread (a GLib timeout), and
    the get-stats reply lands on whatever thread webrtcbin completes the promise
    on.  Every element call we make from either (property set, pad send_event) is
    thread-safe in GStreamer.  The one hop out to asyncio is `on_sample`, which
    the caller must make thread-safe.
    """

    INTERVAL_MS = 1000          # sampling period
    SETTLE_S = 3.0              # ignore everything this soon after (re)negotiation
    MIN_APPLY_INTERVAL = 1.0    # hysteresis: at most one change per second

    LOSS_DOWN_PCT = 5.0         # above this, cut
    LOSS_UP_PCT = 1.0           # below this (and calm RTT), a sample is "healthy"
    RTT_SPIKE_FACTOR = 2.0      # "sharply rising" = 2x the session floor...
    RTT_SPIKE_FLOOR_S = 0.150   # ...and only once it is genuinely large
    RTT_EWMA_ALPHA = 0.3        # smoothing for the RTT we act on

    DECREASE_FACTOR = 0.70
    INCREASE_FRACTION = 0.10    # of the ceiling, per step
    HEALTHY_STREAK = 5          # consecutive healthy samples before a step up
    START_FRACTION = 0.60       # of the ceiling, at session start

    def __init__(self, *, webrtc, encoder, encoder_name, max_kbps, min_kbps,
                 on_sample=None, start_kbps=None):
        self.webrtc = webrtc
        self.encoder = encoder
        self.encoder_name = encoder_name
        self.max_kbps = int(max_kbps)
        self.min_kbps = min(int(min_kbps), self.max_kbps)
        self.on_sample = on_sample

        # start_kbps is how a *resumed* controller keeps continuity: switching
        # ABR back on mid-session must not drop the picture to 60% again.
        start = start_kbps if start_kbps else int(self.max_kbps * self.START_FRACTION)
        self.current_kbps = self._clamp(start)

        self._can_set_bitrate = "bitrate" in {p.name for p in encoder.list_properties()}
        self._started_at = time.monotonic()
        self._last_apply = 0.0
        self._healthy = 0
        self._rtt_ewma = None       # seconds
        self._rtt_min = None        # seconds, session floor
        self._prev = None           # (packets_sent, packets_lost, bytes_sent)
        self._in_flight = False     # a get-stats promise is outstanding
        self._stopped = False
        self._failed = False
        self._source_id = None
        self._dumped = False        # NXAS_DEBUG: dump the raw reply once

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Apply the slow-start bitrate and arm the sampler."""
        if not self._can_set_bitrate:
            warn(f"abr: {self.encoder_name} has no 'bitrate' property — "
                 "adaptive bitrate disabled for this session")
            self._failed = True
            return
        self._set_encoder_bitrate(self.current_kbps)
        log(f"abr: on — start {self.current_kbps} kbps, "
            f"range {self.min_kbps}-{self.max_kbps} kbps, "
            f"sampling every {self.INTERVAL_MS} ms")
        self._source_id = GLib.timeout_add(self.INTERVAL_MS, self.tick)
        self._notify()

    def renegotiated(self) -> None:
        """Media (re)starts now: drop the baseline and re-arm the settle window
        so a fresh SSRC's counters are never read as a delta."""
        self._prev = None
        self._healthy = 0
        self._started_at = time.monotonic()

    def stop(self) -> None:
        """Called from Streamer.stop(), i.e. an executor thread.

        We only raise the flag; the timeout removes itself on its next wake-up.
        Calling GLib.source_remove() across threads races with a dispatch that
        already returned False and earns a GLib critical, and one wasted no-op
        callback is cheaper than that.
        """
        self._stopped = True

    # -- sampling -----------------------------------------------------------
    def tick(self) -> bool:
        if self._stopped or self._failed:
            self._source_id = None
            return False                     # unregister the timeout
        try:
            self._request_stats()
        except Exception as exc:             # pragma: no cover - defensive
            self._disable(f"sampling failed: {exc!r}")
            return False
        return True

    def _request_stats(self) -> None:
        if self._in_flight:
            # The previous reply never came back (renegotiation, a stalled
            # transport). Skip rather than queue promises forever.
            return
        webrtc = self.webrtc
        if webrtc is None:
            return
        self._in_flight = True
        promise = Gst.Promise.new_with_change_func(self._on_stats, None, None)
        # None = "every stat", not just one pad's. webrtcbin fills the promise
        # asynchronously; never promise.wait() here, that would deadlock the
        # thread webrtcbin wants to reply on.
        webrtc.emit("get-stats", None, promise)

    def _on_stats(self, promise, _u1, _u2) -> None:
        """Runs on a webrtcbin thread. Must never raise into GStreamer."""
        self._in_flight = False
        if self._stopped or self._failed:
            return
        try:
            reply = promise.get_reply()
            if reply is None:
                return
            if _DEBUG and not self._dumped:
                # The one thing worth having when a field name changes under us
                # in some future GStreamer: the actual shape webrtcbin returned,
                # one entry per line so it is readable in a terminal.
                self._dumped = True
                self._dump(reply)
            sample = self._extract(reply)
            if sample is not None:
                self._evaluate(*sample)
        except Exception as exc:
            self._disable(f"stats handling failed: {exc!r}")

    @staticmethod
    def _dump(reply) -> None:
        """NXAS_DEBUG only: one line per stats entry."""
        try:
            for i in range(reply.n_fields()):
                key = reply.nth_field_name(i)
                entry = reply.get_value(key)
                if isinstance(entry, Gst.Structure):
                    dbg(f"abr: stat {entry.get_name()}: {entry.to_string()[:900]}")
        except Exception as exc:
            dbg(f"abr: could not dump stats ({exc!r})")

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _stat_type(entry) -> int:
        """The `type` field is a GstWebRTCStatsType enum; older bindings hand it
        back as a plain int and some hand back the nick. Take any of them, and
        fall back to the structure name (e.g. 'rtp-outbound-stream-stats')."""
        try:
            raw = entry.get_value("type")
        except Exception:
            raw = None
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                nick = str(getattr(raw, "value_nick", raw))
                for name, value in (("remote-inbound-rtp",
                                     GstWebRTC.WebRTCStatsType.REMOTE_INBOUND_RTP),
                                    ("outbound-rtp",
                                     GstWebRTC.WebRTCStatsType.OUTBOUND_RTP)):
                    if nick == name:
                        return int(value)
        name = entry.get_name() or ""
        if "remote-inbound" in name:
            return int(GstWebRTC.WebRTCStatsType.REMOTE_INBOUND_RTP)
        if "outbound" in name:
            return int(GstWebRTC.WebRTCStatsType.OUTBOUND_RTP)
        return -1

    @staticmethod
    def _num(entry, field):
        """Every field access is optional: entries appear and disappear across
        negotiation, and a missing key must read as 'no data', never a crash."""
        try:
            if not entry.has_field(field):
                return None
            value = entry.get_value(field)
        except Exception:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _extract(self, reply):
        """-> (packets_sent, packets_lost, bytes_sent, rtt_seconds|None), or None
        when the reply does not yet carry an outbound stream."""
        out_sent = out_lost = out_bytes = None
        rtt = None
        lost_remote = None
        try:
            n = reply.n_fields()
        except Exception:
            return None
        for i in range(n):
            try:
                key = reply.nth_field_name(i)
                entry = reply.get_value(key)
            except Exception:
                continue
            if not isinstance(entry, Gst.Structure):
                continue
            kind = self._stat_type(entry)
            if kind == int(GstWebRTC.WebRTCStatsType.OUTBOUND_RTP):
                sent = self._num(entry, "packets-sent")
                if sent is None:
                    continue
                out_sent = sent if out_sent is None else out_sent + sent
                sent_bytes = self._num(entry, "bytes-sent")
                if sent_bytes is not None:
                    out_bytes = sent_bytes if out_bytes is None \
                        else out_bytes + sent_bytes
            elif kind == int(GstWebRTC.WebRTCStatsType.REMOTE_INBOUND_RTP):
                # The receiver's view, arriving in RTCP RR: this is the only
                # place loss and RTT exist at all.
                lost = self._num(entry, "packets-lost")
                if lost is not None:
                    lost_remote = lost if lost_remote is None else lost_remote + lost
                # Seconds, as a double. Reported as 0 until the first RTCP RR
                # that references one of our sender reports comes back, and on
                # a loopback/LAN peer it can stay 0: GStreamer carries it in
                # NTP 16.16 fixed point, so anything under ~15 us truncates to
                # nothing. Policy about that lives in _evaluate, not here.
                value = self._num(entry, "round-trip-time")
                if value is not None and rtt is None:
                    rtt = value
        if out_sent is None:
            return None                      # nothing sent yet
        out_lost = lost_remote if lost_remote is not None else 0.0
        return (out_sent, out_lost, out_bytes or 0.0, rtt)

    # -- control law --------------------------------------------------------
    def _evaluate(self, packets_sent, packets_lost, bytes_sent, rtt) -> None:
        # RTT is an absolute reading, not a delta, so it counts even on the
        # baseline sample — and it *must*, because the session floor is taken
        # from the first reading we keep. Throwing the baseline's RTT away made
        # the second sample define the floor, so a link that went bad right
        # after connecting had its bad RTT enshrined as "normal" and the spike
        # test could never fire again.
        #
        # A zero is "not measured yet" (or a link too fast for the 16.16 fixed
        # point RTCP carries it in), not "zero latency": feeding it in would
        # drag the floor to 0 and make everything after it look like a spike.
        if rtt is not None and rtt > 0:
            self._rtt_ewma = rtt if self._rtt_ewma is None else (
                self.RTT_EWMA_ALPHA * rtt + (1 - self.RTT_EWMA_ALPHA) * self._rtt_ewma)
            self._rtt_min = rtt if self._rtt_min is None else min(self._rtt_min, rtt)

        prev, self._prev = self._prev, (packets_sent, packets_lost, bytes_sent)
        if prev is None:
            return                           # first sample is only a baseline

        d_sent = packets_sent - prev[0]
        d_lost = packets_lost - prev[1]
        if d_sent < 0 or d_lost < -1000:
            # Counters went backwards: the SSRC was replaced (renegotiation).
            # Re-baseline instead of acting on a nonsense delta.
            dbg("abr: stats counters reset, re-baselining")
            self._started_at = time.monotonic()
            return
        # RTCP's cumulative-lost is signed and *can* tick down when duplicates
        # arrive. Negative loss over an interval is not a windfall; it is zero.
        d_lost = max(0.0, d_lost)

        expected = d_sent + d_lost
        loss_pct = (100.0 * d_lost / expected) if expected > 0 else 0.0
        rtt_ms = None if self._rtt_ewma is None else self._rtt_ewma * 1000.0

        self._notify(rtt_ms)
        dbg(f"abr: sample d_sent={int(d_sent)} d_lost={int(d_lost)} "
            f"loss={loss_pct:.1f}% "
            f"rtt={'none' if rtt is None else f'{rtt * 1000.0:.3f}ms raw'}/"
            f"{'?' if rtt_ms is None else f'{rtt_ms:.0f}ms'} "
            f"at {self.current_kbps} kbps")

        now = time.monotonic()
        if now - self._started_at < self.SETTLE_S:
            return                           # ICE/DTLS/first keyframe noise
        if now - self._last_apply < self.MIN_APPLY_INTERVAL:
            return                           # hysteresis

        # "Rising sharply" is relative to what this path has actually shown us:
        # 60 ms on a 5G link is fine, 60 ms on a link whose floor is 8 ms is a
        # queue filling up. The absolute floor keeps a 3 ms LAN from tripping on
        # a 7 ms blip.
        rtt_spiking = (
            self._rtt_ewma is not None
            and self._rtt_min is not None
            and self._rtt_ewma > self.RTT_SPIKE_FLOOR_S
            and self._rtt_ewma > self.RTT_SPIKE_FACTOR * self._rtt_min
        )

        why = f"loss {loss_pct:.1f}%, rtt {'?' if rtt_ms is None else f'{rtt_ms:.0f}ms'}"

        if loss_pct > self.LOSS_DOWN_PCT or rtt_spiking:
            self._healthy = 0
            target = self._clamp(int(self.current_kbps * self.DECREASE_FACTOR))
            if target < self.current_kbps:
                self._apply(target, why)
                # The cut only helps once the client can decode again: whatever
                # the congestion just ate, the phone is now missing reference
                # frames and will stay grey/blocky until the next IDR, which at
                # key-int-max = 2 s is up to two seconds of garbage. Ask for one
                # now, with SPS/PPS attached, so recovery is immediate.
                self._force_keyframe()
            return

        if loss_pct < self.LOSS_UP_PCT and not rtt_spiking:
            self._healthy += 1
            if self._healthy >= self.HEALTHY_STREAK:
                self._healthy = 0
                step = max(1, int(self.max_kbps * self.INCREASE_FRACTION))
                target = self._clamp(self.current_kbps + step)
                if target > self.current_kbps:
                    self._apply(target, why)
            return

        # The grey zone (1%..5% loss): hold, and make the client earn its way
        # back up from a clean streak.
        self._healthy = 0

    # -- actuation ----------------------------------------------------------
    def _clamp(self, kbps: int) -> int:
        return max(self.min_kbps, min(self.max_kbps, int(kbps)))

    def _apply(self, target_kbps: int, why: str) -> None:
        old = self.current_kbps
        if not self._set_encoder_bitrate(target_kbps):
            return
        self.current_kbps = target_kbps
        self._last_apply = time.monotonic()
        log(f"abr: {old} -> {target_kbps} kbps ({why})")
        self._notify(None if self._rtt_ewma is None else self._rtt_ewma * 1000.0)

    def set_ceiling(self, max_kbps: int, min_kbps=None) -> None:
        """Manual control moved the ceiling (a client 'cap it at 6 Mbps').

        Lowering takes effect immediately — a cap the user asked for is a safety
        action.  Raising only grants headroom; the controller still has to earn
        its way up there, which is the whole point of the streak counter.
        """
        self.max_kbps = max(1, int(max_kbps))
        if min_kbps is not None:
            self.min_kbps = max(1, int(min_kbps))
        self.min_kbps = min(self.min_kbps, self.max_kbps)
        target = self._clamp(self.current_kbps)
        if target != self.current_kbps:
            self._apply(target, f"ceiling {self.max_kbps} kbps")

    def _set_encoder_bitrate(self, kbps: int) -> bool:
        enc = self.encoder
        if enc is None or not self._can_set_bitrate:
            return False
        try:
            # vah264enc, vah264lpenc and x264enc all take kbps here, and all
            # three accept the change while PLAYING (see configure_encoder()).
            enc.set_property("bitrate", int(kbps))
            return True
        except Exception as exc:
            self._disable(f"cannot set {self.encoder_name}.bitrate ({exc!r})")
            return False

    def _force_keyframe(self) -> None:
        if GstVideo is None:
            return
        try:
            pad = self.encoder.get_static_pad("src")
            if pad is None:
                return
            # Upstream force-key-unit, sent to the encoder's *source* pad, is
            # how GstVideoEncoder is asked for an IDR from downstream.
            # all_headers=True so SPS/PPS ride along and a client that lost them
            # can start decoding cold.
            event = GstVideo.video_event_new_upstream_force_key_unit(
                Gst.CLOCK_TIME_NONE, True, 0)
            if not pad.send_event(event):
                dbg("abr: force-key-unit not handled by the encoder")
        except Exception as exc:             # never fatal: it is only a nudge
            dbg(f"abr: force-keyframe failed ({exc!r})")

    # -- plumbing -----------------------------------------------------------
    def _notify(self, rtt_ms=None) -> None:
        cb = self.on_sample
        if cb is None:
            return
        try:
            cb(self.current_kbps, rtt_ms)
        except Exception as exc:             # a status hop must not matter
            dbg(f"abr: on_sample raised ({exc!r})")

    def _disable(self, reason: str) -> None:
        """One loud line, then this session streams at whatever it is at now.
        A broken controller is a fixed-bitrate stream, never a dead one."""
        if self._failed:
            return
        self._failed = True
        err(f"abr: disabled for this session — {reason}")
        err(f"abr: streaming continues at {self.current_kbps} kbps")


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

    def __init__(self, args, injector, on_fatal, on_abr=None):
        self.args = args
        self.injector = injector
        self.on_fatal = on_fatal
        self.on_abr = on_abr             # (kbps, rtt_ms) -> None, any thread
        self.encoder_name = pick_encoder(args.encoder)
        self.pipeline = None
        self.webrtc = None
        self.capture = None
        self.channel = None
        self.abr = None
        self.venc = None                 # the live encoder, for manual bitrate
        self.config_snapshot = None      # () -> dict, sent right after the offer
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
            f"! rawvideoparse width={a.width} height={a.height} format=nv12 "
            f"framerate={a.fps}/1 "
            # Freshest-frame-wins. Without a leaky queue any hiccup — a slow
            # encode, a stalled read, a moment of GPU contention — puts a frame
            # backlog in front of the encoder that NEVER drains, so latency
            # ratchets up and stays up. That is what "it gets laggy and stays
            # laggy even on LAN" is. Two buffers deep, drop the oldest.
            f"! queue name=capq max-size-buffers=2 max-size-bytes=0 "
            f"max-size-time=0 leaky=downstream "
            f"! {self.encoder_name} name=venc "
            # Android's libwebrtc only negotiates H.264 constrained-baseline; a
            # High-profile offer (VAAPI's default, profile-level-id=640033) makes
            # it answer m=video 0 and reject the video outright. Browsers accept
            # High, which is why the web client worked and the phone showed a
            # black screen. Force the profile every client can actually decode.
            f"! video/x-h264,profile=constrained-baseline "
            f"! h264parse config-interval=-1 "
            # Tailscale/WireGuard tunnels are 1280-byte MTU. rtph264pay defaults to
            # ~1400, so every RTP packet is oversized and silently dropped inside
            # the tunnel: signaling and ICE succeed, then not one video frame ever
            # arrives. 1100 leaves room for RTP+UDP+IP+WireGuard overhead.
            f"! rtph264pay name=pay pt=96 mtu=1100 "
            f"! application/x-rtp,media=video,encoding-name=H264,payload=96 "
            f"! webrtcbin name=webrtc bundle-policy=max-compat"
            + self._audio_branch()
        )

    def _audio_branch(self) -> str:
        """A second sendonly transceiver on the SAME webrtcbin, or nothing.

        Gated on --audio, which defaults to none: the video path is the working
        one and an audio branch must never be able to cost it a negotiation.
        With no source resolved this returns "" and the launch string is
        byte-identical to the video-only one.

        provide-clock=false is the subtle bit. Our video comes off a filesrc,
        which is not a live source, so the pipeline runs on the system clock.
        pulsesrc *is* live and would otherwise volunteer to be the clock
        provider — which quietly re-paces the encoder against the sound card.
        Video timing is not audio's business.
        """
        src = getattr(self.args, "audio_source", None)
        if not src:
            return ""
        a = self.args
        return (
            f' pulsesrc device="{src}" name=asrc provide-clock=false '
            # convert/resample first: an explicitly named source may be mono,
            # 44.1 kHz, or anything else, and opusenc only takes a shortlist.
            f"! audioconvert ! audioresample "
            f"! audio/x-raw,rate=48000,channels=2 "
            # 20 ms frames match what every WebRTC receiver expects; in-band FEC
            # only actually emits redundancy when the encoder is told to expect
            # loss, hence the pair.
            f"! opusenc bitrate={a.audio_bitrate} frame-size=20 "
            f"inband-fec=true packet-loss-percentage=5 "
            f"! rtpopuspay pt=97 mtu=1100 "
            f"! application/x-rtp,media=audio,encoding-name=OPUS,payload=97 "
            f"! webrtc."
        )

    def start(self, send_json, fatal: bool = True) -> None:
        """Runs in an executor thread (parse_launch + state changes block).

        fatal=False is for a mid-session rebuild (a live fps change): the caller
        wants to roll back and try again, not take the daemon down.
        """
        self._tearing_down = False        # a fault from here on is a real fault
        try:
            self._start(send_json)
        except StreamError as exc:
            err(f"session: {exc}")
            self.stop()
            if fatal:
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
            self.venc = venc
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

            # nx-bridge hook: optional camera receive path (phone camera ->
            # v4l2loopback). Adds a recvonly transceiver before the offer exists,
            # so no renegotiation is ever needed. Returns None — leaving the
            # pipeline exactly as it was — unless --camera is on AND a loopback
            # node is there.
            nx_bridge.attach_camera_receiver(self.args, self.pipeline, self.webrtc)

            ret = self.pipeline.set_state(Gst.State.PLAYING)
            log(f"pipeline: -> PLAYING ({ret.value_nick})")
            if ret == Gst.StateChangeReturn.FAILURE:
                raise StreamError("pipeline could not reach PLAYING")

            # Adaptive bitrate is per-session: a fresh controller every client,
            # so one bad link never leaves the next one throttled. Building it
            # must never be able to stop the stream from starting.
            if self.args.no_abr:
                log(f"abr: off — pinned to {self.args.bitrate} kbps")
                if self.on_abr is not None:
                    self.on_abr(self.args.bitrate, None)
            else:
                self.start_abr()

            self._setup_done = True
        # Offer only once everything above is wired; on-negotiation-needed may
        # already have fired and parked itself in _negotiation_wanted.
        GLib.idle_add(self._maybe_offer)

    # -- live control (manual config channel + ABR) -------------------------
    def start_abr(self, start_kbps=None) -> bool:
        """Arm the adaptive controller on the running pipeline.  Building it
        must never be able to stop or kill the stream."""
        if self.abr is not None or self.webrtc is None or self.venc is None:
            return self.abr is not None
        try:
            self.abr = AdaptiveBitrate(
                webrtc=self.webrtc,
                encoder=self.venc,
                encoder_name=self.encoder_name,
                max_kbps=self.args.bitrate,
                min_kbps=self.args.min_bitrate,
                on_sample=self.on_abr,
                start_kbps=start_kbps,
            )
            self.abr.start()
            return True
        except Exception as exc:
            err(f"abr: could not start ({exc!r}) — fixed bitrate "
                f"{self.args.bitrate} kbps for this session")
            self.abr = None
            return False

    def stop_abr(self) -> None:
        if self.abr is not None:
            self.abr.stop()
            self.abr = None

    def current_bitrate(self):
        """What the encoder is actually running at, kbps, or None if idle."""
        if self.abr is not None:
            return self.abr.current_kbps
        enc = self.venc
        if enc is None:
            return None
        try:
            return int(enc.get_property("bitrate"))
        except Exception:
            return None

    def set_bitrate_now(self, kbps: int) -> bool:
        """Pin the encoder, bypassing the controller (manual mode)."""
        enc = self.venc
        if enc is None:
            return False
        try:
            enc.set_property("bitrate", int(kbps))
            return True
        except Exception as exc:
            warn(f"encoder: manual bitrate {kbps} rejected ({exc})")
            return False

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
        just before the offer.

        One per medium we send, in the order parse_launch requested the pads:
        video is 0, audio (when the audio branch exists) is 1.
        """
        wanted = 2 if getattr(self.args, "audio_source", None) else 1
        done = 0
        for index in range(wanted):
            tr = self.webrtc.emit("get-transceiver", index)
            if tr is None:
                break
            tr.set_property("direction",
                            GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
            done += 1
        if done < wanted:
            if not quiet:
                warn(f"webrtc: only {done}/{wanted} transceiver(s) exist yet, "
                     "retrying at offer time")
            return False
        log(f"webrtc: {done} transceiver(s) direction=sendonly"
            f"{' (video+audio)' if wanted > 1 else ' (video)'}")
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
        # Unsolicited config snapshot right behind the offer: a client that just
        # connected learns the effective bitrate/fps/abr without having to ask,
        # and can render its controls at the real values instead of guessing.
        snapshot = self.config_snapshot
        if snapshot is not None:
            try:
                self.send_json(snapshot())
            except Exception as exc:      # a status frame is never worth a fault
                warn(f"config: could not announce settings ({exc!r})")

    def _on_ice_candidate(self, _element, mline_index, candidate) -> None:
        # THREAD BOUNDARY 1 (again): GStreamer thread -> asyncio.
        # ICE gathering outlives the client: candidates keep arriving for a
        # moment after a disconnect, when send_json is already gone. That is
        # routine, not an error worth a traceback in the log.
        send = self.send_json
        if send is None:
            return
        try:
            send({"type": "ice", "candidate": candidate,
                  "sdpMLineIndex": int(mline_index)})
        except Exception as exc:
            dbg(f"ice: candidate dropped after teardown ({exc!r})")

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
        # Media only starts flowing now, and the SSRCs may be brand new: restart
        # the controller's settle window so it does not judge the link on the
        # first ICE/DTLS-shaped second.
        if self.abr is not None:
            self.abr.renegotiated()
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
        if getattr(self, "_tearing_down", False):
            log(f"gst: {message.src.get_name()}: {gerror.message} (during teardown)")
            return
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
        # A client leaving stops wf-recorder, which EOSes the fifo. That is our
        # own doing: tear the pipeline down and keep serving. Treating it as
        # fatal killed the daemon on the FIRST disconnect, so every client after
        # it found nothing listening and reconnected forever.
        if getattr(self, "_tearing_down", False):
            log("gst: end-of-stream (expected, client left)")
            return
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
            self._tearing_down = True     # EOS/errors below are ours, not faults
            self._setup_done = False
            self._negotiation_wanted = False
            self._sendonly_done = False
            self.channel = None
            # Stop sampling *before* the pipeline goes away, so no get-stats
            # lands on a half-NULL webrtcbin.
            if self.abr is not None:
                self.abr.stop()
                self.abr = None
            if self.pipeline is not None:
                log("pipeline: -> NULL")
                pipeline = self.pipeline
                self.pipeline = None          # nobody else may touch it now
                # THREAD BOUNDARY 5: the state change MUST run on the GLib
                # thread. Tearing the pipeline down from asyncio while webrtcbin
                # was mid-negotiation on the GLib thread (create-offer promise
                # still in flight) freed objects out from under it — glibc
                # aborted the process with "double free or corruption". A client
                # that vanishes during negotiation is completely routine, so
                # this crashed the daemon on ordinary disconnects.
                def _to_null() -> bool:
                    # Drop the bus watch here, not before: removing it while the
                    # GLib thread is dispatching a message on it is itself a
                    # use-after-free.
                    try:
                        pipeline.get_bus().remove_signal_watch()
                    except Exception:
                        pass
                    try:
                        pipeline.set_state(Gst.State.NULL)
                    except Exception as exc:
                        warn(f"pipeline: teardown failed ({exc})")
                    return False               # one-shot

                # A short delay, not idle_add: a client that vanishes mid-offer
                # leaves a create-offer promise still running on the GLib thread,
                # and freeing the pipeline under it aborts the process. Let the
                # in-flight callbacks finish first. We do NOT block asyncio
                # waiting for this — the session is already logically gone.
                GLib.timeout_add(400, _to_null)
            self.webrtc = None
            self.venc = None
            if self.capture is not None:
                self.capture.stop()
                self.capture = None
            self.send_json = None


# ------------------------------------------------------------------- adb ----

class Adb:
    """One adb target, shared by everything that talks into the container.

    Two things need the same device now — the touch path (ScrcpyInjector) and
    the battery mirror — and discovering it twice would mean two `adb connect`
    races against the same Waydroid container, plus two places to keep the
    "never run adb without an explicit serial" rule in.  So resolution happens
    exactly once, behind a lock, and every user holds this same object.

    Loop-affine: `run()` and `resolve()` are coroutines on the asyncio loop.
    """

    LEASES = "/var/lib/misc/dnsmasq.waydroid0.leases"

    def __init__(self, binary, serial=None):
        self.binary = binary or "adb"
        self.serial = serial
        self._resolving = asyncio.Lock()

    async def run(self, *argv, timeout=20.0, check=True):
        """Every adb call goes through here, and every one of them carries
        -s <serial>. A bare `adb shell` would happily target whatever phone
        happens to be plugged into the machine — never do that."""
        if not self.serial:
            raise StreamError("refusing to run adb without an explicit serial")
        cmd = [self.binary, "-s", self.serial, *argv]
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

    def waydroid_ip(self):
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

    async def resolve(self) -> None:
        """Find the container and `adb connect` to it, at most once."""
        if self.serial:
            return
        async with self._resolving:
            if self.serial:                  # somebody else won the race
                return
            ip = self.waydroid_ip()
            if not ip:
                raise StreamError(
                    "cannot find the Waydroid container IP (no DHCP lease in "
                    f"{self.LEASES}, no 'IP address' from waydroid status). "
                    "Android may still be booting, or pass --adb-serial "
                    "explicitly.")
            target = f"{ip}:5555"
            proc = await asyncio.create_subprocess_exec(
                self.binary, "connect", target,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 20.0)
            text = out.decode("utf-8", "replace").strip()
            if "connected to" not in text:
                raise StreamError(f"adb connect {target}: {text}")
            self.serial = target
            log(f"adb: connected to waydroid at {target} ({text})")


# --------------------------------------------------------------- battery ----

class BatteryMirror:
    """The real phone's battery, mirrored onto the container's.

    The point is continuity: the streamed Android is meant to feel like the
    same device you are holding, and a remote stuck at a flat 85% breaks that
    the moment you glance at the status bar.

    `dumpsys battery set ...` is not a report, it is an *override*: the battery
    service stops taking updates from the HAL and keeps whatever we forced,
    for as long as the container lives — long after this daemon is gone.  So
    every way out of here (client disconnects, mirroring switched off, daemon
    exits) has to run `dumpsys battery reset`, and `_forced` is raised before
    the first command rather than after the last, so a half-applied override
    still gets cleaned up.

    `set ac` alone is not enough to look unplugged: Waydroid's health HAL
    reports AC *and* USB online by default, and a container with USB still
    powered keeps the charging bolt in the status bar no matter what `status`
    says.  Both rails go down together.
    """

    LEVEL_MIN, LEVEL_MAX = 0, 100
    STATUS_CHARGING, STATUS_DISCHARGING = 2, 3   # BatteryManager.BATTERY_STATUS_*
    ADB_TIMEOUT = 15.0

    def __init__(self, adb):
        self.adb = adb
        self._applied = None          # last (level, charging) that fully landed
        self._forced = False          # an override of any kind is outstanding
        self._lock = asyncio.Lock()   # serializes the multi-command sequences
        # Bumped by reset(). An apply() that was already in flight when the
        # client went away carries the old epoch and is dropped rather than
        # re-forcing a battery nobody is looking at any more.
        self._epoch = 0

    @property
    def mirroring(self) -> bool:
        """True while the container's battery is under our override."""
        return self._forced

    @classmethod
    def clean_level(cls, raw):
        """-> int in 0..100, or None for 'that was not a battery level'.
        bool is an int in Python, and "level": true is junk, not 1."""
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return max(cls.LEVEL_MIN, min(cls.LEVEL_MAX, value))

    async def apply(self, level: int, charging: bool) -> None:
        state = (int(level), bool(charging))
        epoch = self._epoch
        if state == self._applied:
            return                    # no change: no adb round trip at all
        # Outside the lock on purpose: the first resolve() can sit in an `adb
        # connect` for seconds, and reset() must not have to queue behind it.
        await self.adb.resolve()
        async with self._lock:
            if epoch != self._epoch or state == self._applied:
                return
            first = self._applied is None
            self._forced = True       # before the first command, not after the last
            await self._set("ac", "1" if state[1] else "0")
            await self._set("usb", "1" if state[1] else "0")
            await self._set("status", str(self.STATUS_CHARGING if state[1]
                                          else self.STATUS_DISCHARGING))
            await self._set("level", str(state[0]))
            self._applied = state
        where = "charging" if state[1] else "on battery"
        if first:
            log(f"battery: mirroring the client's battery — {state[0]}%, {where}")
        else:
            dbg(f"battery: {state[0]}%, {where}")

    async def _set(self, key: str, value: str) -> None:
        await self.adb.run("shell", "dumpsys", "battery", "set", key, value,
                           timeout=self.ADB_TIMEOUT)

    async def reset(self) -> None:
        """Hand the battery service back to the HAL.  Idempotent, and safe to
        call from a shutdown path where adb may already be unreachable."""
        async with self._lock:
            self._epoch += 1
            if not self._forced:
                return
            self._forced = False
            self._applied = None
            try:
                await self.adb.run("shell", "dumpsys", "battery", "reset",
                                   timeout=self.ADB_TIMEOUT)
            except (StreamError, OSError) as exc:
                err(f"battery: could not release the override ({exc}) — the "
                    "container will keep reporting the mirrored level until it "
                    "is restarted. Run 'adb shell dumpsys battery reset'.")
                return
        log("battery: override released (dumpsys battery reset)")


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

    def __init__(self, args, loop: asyncio.AbstractEventLoop, adb: Adb):
        self.args = args
        self.loop = loop
        self.width = args.width
        self.height = args.height
        self.adb = adb                         # shared with the battery mirror
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
        return await self.adb.run(*argv, timeout=timeout, check=check)

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
        await self.adb.resolve()
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
            self.adb.binary, "-s", self.adb.serial, *server_cmd,
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
        if self.port is not None and self.adb.serial:
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

# The client is served straight off disk and updated often; a stale cached
# app.js means a fixed bug that keeps happening on the phone.
NO_STORE = {"Cache-Control": "no-store, must-revalidate"}


class Daemon:
    """Owns the single-client policy and both event loops' handoffs."""

    # Hard limits for the client-driven config channel. The client is a phone on
    # the other side of a VPN, not a trusted peer: everything it sends is
    # clamped into a range this daemon can actually serve, and anything that is
    # not a number in the first place is dropped.
    # 150 Mbps ceiling: on LAN there is real headroom and 1080x2400 at high fps
    # can eat far more than the old 50 Mbps cap allowed anyone to ask for.
    CONFIG_BITRATE_MIN, CONFIG_BITRATE_MAX = 500, 150000       # kbps
    CONFIG_FPS_MIN, CONFIG_FPS_MAX = 15, 120

    def __init__(self, args, injector, on_fatal, hub=None, battery=None):
        self.args = args
        self.injector = injector
        self.on_fatal = on_fatal
        self.hub = hub
        self.battery = battery
        # Background work spawned off the signaling loop (battery adb calls).
        # Held so the GC cannot collect a running task, and cancellable at
        # shutdown so nothing re-forces state after the final reset.
        self._side_tasks = set()
        self.loop = asyncio.get_running_loop()
        self.streamer = Streamer(args, injector, on_fatal, on_abr=self._on_abr)
        self.client = None
        self.gate = asyncio.Lock()
        # Latest numbers from the adaptive-bitrate controller. Seeded with the
        # ceiling so a session that has not sampled yet still reads sanely.
        self._abr_kbps = args.bitrate
        self._rtt_ms = None
        self._bad_config_fields = set()   # log each junk field once per session
        self.streamer.config_snapshot = self.config_state

    # -- hub status ---------------------------------------------------------
    def _on_abr(self, kbps: int, rtt_ms) -> None:
        """THREAD BOUNDARY 5: GStreamer/GLib thread -> asyncio loop.  The ABR
        controller samples on the GLib thread; the hub connector's status feed
        is loop-affine, so hop before touching it."""
        self.loop.call_soon_threadsafe(self._abr_update, kbps, rtt_ms)

    def _abr_update(self, kbps: int, rtt_ms) -> None:
        """Runs on the asyncio loop."""
        self._abr_kbps = kbps
        if rtt_ms is not None:
            self._rtt_ms = rtt_ms
        self._publish_hub()

    def _publish_hub(self) -> None:
        """Push the current session state onto the hub bus.  Called from the
        attach/detach seams, once at startup to seed `idle`, and on every ABR
        sample.  set_status() only wakes the sender on a real change, and the
        connector still pre-throttles to <= 4 sends/s, so a 1 Hz feed is free."""
        if self.hub is None:
            return
        streaming = self.client is not None
        fields = {
            "state": "streaming" if streaming else "idle",
            "client": streaming,
            # The bitrate the encoder is *currently* running at, in Mbps — the
            # adaptive controller's live value, not the configured ceiling.
            # 0 when idle.
            "bitrate": round(self._abr_kbps / 1000.0, 3) if streaming else 0,
        }
        # `latency` used to be omitted because the server had no RTT at all: the
        # datachannel ping/pong is measured on the *client* and never echoed
        # back.  RTCP receiver reports give us the real network round trip now,
        # so the field is honest — it is the transport RTT, which is the floor
        # of glass-to-glass, not the whole of it.
        if streaming:
            if self._rtt_ms is not None:
                fields["latency"] = round(self._rtt_ms, 1)
        else:
            fields["latency"] = 0          # explicit, so the card does not keep
                                           # showing the last session's RTT
        self.hub.set_status(**fields)

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
            # Fresh session, fresh numbers: never report the previous client's
            # RTT or throttled bitrate against this one.
            self._abr_kbps = self.args.bitrate
            self._rtt_ms = None
            self._bad_config_fields.clear()
            # The battery override belongs to whoever is holding the phone. A
            # no-op unless a previous session left one behind (takeover, or a
            # detach whose reset could not reach adb).
            await self._reset_battery()
            log(f"ws: client {peer} connected — building pipeline")
            try:
                await self.loop.run_in_executor(
                    None, self.streamer.start, self._sender(ws))
            except StreamError as exc:
                await self._safe_send(ws, {"type": "error", "message": str(exc)})
                await ws.close(code=1011, message=b"pipeline failed")
                self.client = None
        # nx-bridge hook: start watching the container's picker spool for this
        # client and announce the camera bridge's state. No-op with --no-picker
        # and when the nx-bridge companion app is not installed.
        if self.client is ws:
            nx_bridge.client_attached(self.args, ws,
                                      lambda payload: self._safe_send(ws, payload))
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
            nx_bridge.client_detached(ws)  # nx-bridge hook: stop polling adb
            self._publish_hub()            # back to idle
        # Outside the gate: an adb round trip must not hold up the next client's
        # attach. The client is gone, so a mirrored battery is a lie now.
        await self._reset_battery()

    # -- battery mirror -----------------------------------------------------
    async def _reset_battery(self) -> None:
        """Hand the container's battery back to its own HAL. Never raises."""
        if self.battery is None:
            return
        try:
            await self.battery.reset()
        except Exception as exc:            # pragma: no cover - defensive
            warn(f"battery: reset failed ({exc!r})")

    async def on_battery(self, msg) -> None:
        """{"type":"battery","level":0..100,"charging":bool} from the phone —
        the real device's battery, mirrored so the remote reads as the same
        one.  {"type":"battery","enabled":false} is how the client says the
        user switched mirroring off, and releases the override.

        Everything is validated here rather than trusted: the client is a phone
        on the other side of a VPN, and `dumpsys battery set level` will happily
        accept a number that leaves the container looking broken.
        """
        if self.battery is None:
            if "battery" not in self._bad_config_fields:
                self._bad_config_fields.add("battery")
                warn("battery: no adb device configured — cannot mirror the "
                     "client's battery (video and touch are unaffected)")
            return
        if msg.get("enabled") is False:
            await self._reset_battery()
            return
        if self.client is None:
            # This runs off the signaling loop, so a message can be spawned and
            # the client can vanish before the task ever starts — and detach's
            # reset would then be undone by an apply for a phone that is no
            # longer there. BatteryMirror's epoch guard catches the mirror image
            # of this (a task already in flight); between them nothing outlives
            # its session.
            return
        level = BatteryMirror.clean_level(msg.get("level"))
        if level is None:
            self._config_junk("battery level", msg.get("level"))
            return
        charging = msg.get("charging")
        if not isinstance(charging, bool):
            if charging is not None:
                self._config_junk("battery charging", charging)
            charging = False
        try:
            await self.battery.apply(level, charging)
        except (StreamError, OSError) as exc:
            warn(f"battery: could not mirror {level}% ({exc})")

    def _spawn(self, coro) -> None:
        """Run something off the signaling loop, held so the GC cannot collect
        a task that is still running."""
        task = asyncio.ensure_future(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    async def aclose(self) -> None:
        """Shutdown: stop anything that could still be changing the container,
        then release the battery override."""
        for task in list(self._side_tasks):
            task.cancel()
        for task in list(self._side_tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._side_tasks.clear()
        await self._reset_battery()

    # -- config channel -----------------------------------------------------
    def config_state(self, note=None) -> dict:
        """The effective settings, as the client should render them.

        Called both from the asyncio loop (replies) and from a GStreamer thread
        (the unsolicited announce after an offer); it only reads plain ints off
        `args` and the controller, so there is nothing here to race on.
        """
        current = self.streamer.current_bitrate()
        if current is None:
            current = self._abr_kbps
        state = {
            "type": "config",
            "bitrate": int(current),          # what is being encoded right now
            "fps": int(self.args.fps),
            "abr": not self.args.no_abr,
            "min_bitrate": int(self.args.min_bitrate),
            "max_bitrate": int(self.args.bitrate),   # the ceiling / pinned rate
        }
        if note:
            state["note"] = note
        return state

    def _config_number(self, msg, key, lo, hi):
        """-> clamped int, or None for 'absent' / 'junk, already complained'."""
        if key not in msg:
            return None
        raw = msg.get(key)
        if raw is None:
            return None                       # explicit null = leave alone
        # bool is an int in Python; "bitrate": true is junk, not 1.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            self._config_junk(key, raw)
            return None
        value = int(raw)
        clamped = max(lo, min(hi, value))
        if clamped != value:
            log(f"config: {key}={value} out of range, clamped to {clamped}")
        return clamped

    def _config_junk(self, key, raw) -> None:
        if key in self._bad_config_fields:
            return                            # once per session, then silence
        self._bad_config_fields.add(key)
        warn(f"config: ignoring bad {key!r} from the client ({raw!r})")

    async def on_config(self, ws, msg) -> None:
        """{"type":"config", bitrate?, fps?, abr?} from the phone.

        Manual control is the point: ABR is the *default*, not the law.  Whatever
        the client asks for, the reply is always the effective state, so its UI
        reflects reality rather than its own optimism.
        """
        bitrate = self._config_number(msg, "bitrate",
                                      self.CONFIG_BITRATE_MIN, self.CONFIG_BITRATE_MAX)
        fps = self._config_number(msg, "fps",
                                  self.CONFIG_FPS_MIN, self.CONFIG_FPS_MAX)
        abr = msg.get("abr")
        if abr is not None and not isinstance(abr, bool):
            self._config_junk("abr", abr)
            abr = None

        note = None
        if bitrate is not None or abr is not None:
            self._apply_bitrate_config(bitrate, abr)
        if fps is not None and fps != self.args.fps:
            note = await self._apply_fps_config(ws, fps)

        self._publish_hub()
        await self._safe_send(ws, self.config_state(note))

    def _apply_bitrate_config(self, bitrate, abr) -> None:
        """Bitrate and the abr switch, applied to the live encoder.

        A manual bitrate always moves the *ceiling* (`--bitrate`), not just the
        instantaneous value: "cap it at 6 Mbps" has to survive the controller's
        next probe upward, and has to survive into the next session's
        configure_encoder() too.
        """
        st = self.streamer
        if bitrate is not None:
            self.args.bitrate = bitrate
            if self.args.min_bitrate > bitrate:
                self.args.min_bitrate = bitrate

        if abr is False:
            # Pin: stop adapting and hold whatever was asked for (or whatever we
            # happen to be at, if the client only said "stop adapting").
            target = bitrate
            if target is None:
                target = st.current_bitrate() or self.args.bitrate
                self.args.bitrate = int(target)
            st.stop_abr()
            self.args.no_abr = True
            st.set_bitrate_now(target)
            self._abr_kbps = int(target)
            log(f"config: abr off — pinned to {int(target)} kbps")
            return

        if abr is True and self.args.no_abr:
            # Resume from where we are, not from slow start: the picture must
            # not visibly drop just because the user re-armed the controller.
            resume = st.current_bitrate() or self.args.bitrate
            self.args.no_abr = False
            if st.start_abr(start_kbps=resume):
                log(f"config: abr on — resuming at {int(resume)} kbps, "
                    f"ceiling {self.args.bitrate} kbps")
            else:
                self._abr_kbps = int(resume)
            return

        # ABR already running (or no client yet): move its ceiling.
        if bitrate is not None:
            if st.abr is not None:
                st.abr.set_ceiling(self.args.bitrate, self.args.min_bitrate)
                log(f"config: ceiling now {self.args.bitrate} kbps "
                    f"(floor {self.args.min_bitrate})")
            elif self.args.no_abr:
                st.set_bitrate_now(bitrate)
                self._abr_kbps = bitrate
                log(f"config: pinned bitrate now {bitrate} kbps")
            else:
                self._abr_kbps = bitrate

    async def _apply_fps_config(self, ws, fps: int):
        """Framerate lives in the wf-recorder command line and the rawvideoparse
        caps, so it cannot be twiddled on a running pipeline.  Rebuild capture +
        pipeline and re-offer to the SAME websocket: the client only ever
        answers, never offers, so a fresh offer is a legal thing to hand it and
        the session survives without a reconnect.

        Returns a note for the reply, or None when the change went through.
        """
        old = self.args.fps
        self.args.fps = fps
        if self.client is not ws:
            return "fps applies to the next session"
        log(f"config: fps {old} -> {fps}, rebuilding capture + pipeline")
        if await self._rebuild(ws):
            return None
        # Rolling back is worth one try: the client asked for a framerate this
        # machine cannot capture, and dropping it into a dead session over that
        # would be a worse answer than "no".
        err(f"config: fps {fps} would not start — rolling back to {old}")
        self.args.fps = old
        if await self._rebuild(ws):
            return f"fps {fps} failed, still at {old}"
        err("config: rollback failed too — closing the client so it reconnects")
        try:
            await ws.close(code=1011, message=b"pipeline failed")
        except Exception:
            pass
        return f"fps {fps} failed and the pipeline did not recover"

    async def _rebuild(self, ws) -> bool:
        """Stop and restart the pipeline for the client that is already here."""
        async with self.gate:
            if self.client is not ws:
                return False
            await self.loop.run_in_executor(None, self.streamer.stop)
            if self.injector:
                self.injector.release_all()
            self._abr_kbps = self.args.bitrate
            self._rtt_ms = None
            sender = self._sender(ws)
            try:
                # fatal=False: a rebuild that fails is a failed *setting*, and
                # the caller still gets to roll it back. Only the initial attach
                # is allowed to take the daemon down.
                await self.loop.run_in_executor(
                    None, lambda: self.streamer.start(sender, fatal=False))
            except StreamError as exc:
                err(f"config: rebuild failed: {exc}")
                return False
        return True

    async def on_message(self, ws, msg) -> None:
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
        elif kind == "config":
            # Manual control from the phone. Deliberately on the signaling
            # socket rather than the input datachannel: the client must be able
            # to set bitrate/fps before (or without) the datachannel ever
            # opening, and the datachannel stays input-only.
            await self.on_config(ws, msg)
        elif kind == "battery":
            # Fire-and-forget on purpose. Mirroring costs three adb round trips
            # and the first one can sit in an `adb connect` for seconds; an
            # answer or an ICE candidate queued behind a slow `dumpsys` is a
            # session that never connects. BatteryMirror serializes internally.
            self._spawn(self.on_battery(msg))
        # nx-bridge hook: pick-data / pick-cancel / camera. Returns False for
        # anything it does not own, so the unknown-type warning stays honest.
        elif not await nx_bridge.on_message(ws, msg):
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
                    await self.on_message(ws, payload)
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
            # `bitrate` stays the live one (Mbps) so existing readers keep
            # meaning the same thing; the ceiling is alongside it.
            "bitrate": round(self._abr_kbps / 1000.0, 3),
            "bitrate_max": self.args.bitrate / 1000.0,
            "bitrate_min": self.args.min_bitrate / 1000.0,
            "abr": not self.args.no_abr,
            "rtt_ms": None if self._rtt_ms is None else round(self._rtt_ms, 1),
            "input": self.injector.label if self.injector else "none",
            "input_ready": bool(getattr(self.injector, "connected",
                                        self.injector is not None)),
            # What is actually being captured, not what was asked for: --audio
            # degrades to video-only rather than failing, and this is where you
            # see which of the two happened.
            "audio": self.args.audio_source,
            "battery_mirrored": bool(self.battery and self.battery.mirroring),
        })

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/health", self.health)

        root = Path(self.args.web_root)
        index = root / "index.html"
        if index.is_file():
            async def serve_index(_request):
                return web.FileResponse(index, headers=NO_STORE)

            async def serve_file(request):
                # Manual resolve so a crafted path can't escape the web root.
                target = (root / request.match_info["path"]).resolve()
                if root not in target.parents or not target.is_file():
                    raise web.HTTPNotFound()
                return web.FileResponse(target, headers=NO_STORE)

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
    # The *ceiling*, not a fixed rate: adaptive bitrate starts below it and
    # never exceeds it. 8000 rather than 12000 because 12 Mbps of 1080x2400
    # demonstrably overruns a 5G uplink and takes the whole session down.
    p.add_argument("--bitrate", type=int, default=8000,
                   help="kbps ceiling (default 8000)")
    p.add_argument("--min-bitrate", type=int, default=1500,
                   help="kbps floor the adaptive controller will not go below "
                        "(default 1500)")
    p.add_argument("--no-abr", action="store_true",
                   help="disable adaptive bitrate and pin the encoder to "
                        "--bitrate (the pre-0.2 behaviour)")
    p.add_argument("--audio", default="none", metavar="auto|none|SOURCE",
                   help="stream the container's audio as a second WebRTC track: "
                        "'none' (default, video only); 'auto' to route Waydroid "
                        "into a private null sink and capture its monitor — "
                        "Waydroid has no monitor source of its own, see "
                        "AudioRoute; or the name of a PulseAudio source to "
                        "capture verbatim (pactl list short sources)")
    p.add_argument("--audio-bitrate", type=int, default=96000,
                   help="opus bitrate in bits/s (default 96000)")
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
    nx_bridge.add_arguments(p)     # nx-bridge hook: --camera, --no-picker
    args = p.parse_args(argv)
    if args.no_input:
        args.input = "none"
    if args.min_bitrate < 1:
        args.min_bitrate = 1
    if args.min_bitrate > args.bitrate:
        warn(f"--min-bitrate {args.min_bitrate} is above --bitrate "
             f"{args.bitrate}; clamping the floor to the ceiling")
        args.min_bitrate = args.bitrate
    if args.adb is None:
        args.adb = shutil.which("adb")
    if args.audio_bitrate < 6000:
        args.audio_bitrate = 6000          # opusenc's own floor
    # Filled in by serve() once AudioRoute has resolved (or refused to): the
    # pipeline reads this, never --audio itself, so "asked for audio" and "has
    # a source to capture" can never be confused.
    args.audio_source = None

    here = Path(__file__).resolve().parent
    args.web_root = str(Path(args.web_root).resolve()) if args.web_root \
        else str((here.parent / "web").resolve())
    args.run_dir = str(here.parent / ".run")
    return args


def build_injector(args, loop, adb):
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
    if adb is None:
        err("input: no adb binary found (pass --adb) — touch disabled, "
            "video unaffected")
        return None
    injector = ScrcpyInjector(args, loop, adb)
    injector.start_background()
    return injector


async def audio_watch(route: AudioRoute, stop: asyncio.Event) -> None:
    """Keep re-homing Waydroid's playback streams while we are up.

    Android opens the PCM when an app plays and closes it when it stops, so the
    stream we routed at startup is not the stream that exists ten minutes later.
    Never allowed to raise: audio housekeeping cannot be a reason to lose video.
    """
    while not stop.is_set():
        try:
            await asyncio.sleep(route.POLL_INTERVAL)
            route.poll()
        except asyncio.CancelledError:
            raise
        except Exception as exc:              # pragma: no cover - defensive
            dbg(f"audio: poll failed ({exc!r})")


async def serve(args, glib_loop) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    exit_code = {"code": 0}

    def fatal(code: int) -> None:
        """Called from GStreamer/watchdog threads when the stream is dead."""
        exit_code["code"] = code
        # THREAD BOUNDARY 3: any thread -> asyncio, shutdown only.
        loop.call_soon_threadsafe(stop.set)

    # One adb target for everything that talks into the container: the touch
    # path and the battery mirror share it, so the device is discovered once.
    adb = Adb(args.adb, args.adb_serial) if args.adb else None
    injector = build_injector(args, loop, adb)
    battery = BatteryMirror(adb) if adb is not None else None

    # Audio is resolved before anything can connect, so the source exists for
    # the very first offer. Defaults to none and degrades to none: this must
    # not be able to cost the working video path a negotiation.
    audio = AudioRoute(args.audio)
    args.audio_source = audio.resolve()

    # NX Hub connector: optional, silent, and isolated. A shutdown-request on
    # the bus must trigger exactly the same clean exit as SIGTERM — set `stop`.
    hub = None
    if not args.no_hub:
        def _hub_shutdown() -> None:
            log("shutdown-request from NX Hub — shutting down")
            stop.set()
        hub = HubConnector(version=VERSION, on_shutdown_request=_hub_shutdown)

    daemon = Daemon(args, injector, fatal, hub=hub, battery=battery)
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
    mode = (f"fixed {args.bitrate} kbps" if args.no_abr else
            f"adaptive {args.min_bitrate}-{args.bitrate} kbps")
    log(f"capturing {args.output} on WAYLAND_DISPLAY={args.wayland_display} "
        f"at {args.width}x{args.height}@{args.fps}, {mode}")
    log("audio: " + (f"opus from {args.audio_source} @ {args.audio_bitrate} bps"
                     if args.audio_source else "off (--audio none)"))

    audio_task = None
    if args.audio_source and args.audio == "auto":
        audio_task = asyncio.ensure_future(audio_watch(audio, stop))

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

    if audio_task is not None:
        audio_task.cancel()
        try:
            await audio_task
        except (asyncio.CancelledError, Exception):
            pass

    # Order matters: stop the injector retrying and hang up on the client
    # first, so neither can stretch the shutdown out.
    if injector is not None:
        injector.close()                  # sync: just sets the closing flag
    await daemon.close_client()
    await runner.cleanup()
    await loop.run_in_executor(None, daemon.streamer.stop)
    # Before adb goes away: cancel anything still in flight and release the
    # battery override. A container left pinned at the last mirrored level
    # would stay that way until it is restarted.
    await daemon.aclose()
    if injector is not None:
        try:
            await injector.aclose()
        except Exception as exc:
            warn(f"input: shutdown: {exc}")
    # Last, once nothing is capturing the monitor any more: put the audio graph
    # back exactly as we found it.
    audio.release()
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
