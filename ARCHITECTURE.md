# Architecture

## The one-sentence version

Waydroid renders a full Android at phone geometry inside a headless Wayland
session on the PC; `nx-streamerd` hardware-encodes it and ships it over WebRTC;
a thin client on the phone decodes and displays it and sends touch/audio/keys
back; every layer is tuned so the result is indistinguishable from local.

## Server pipeline

### v0.1 (off-the-shelf capture)

```
waydroid show-full-ui
  → sway (WLR_BACKENDS=headless, output 1080×2400@90)
  → wf-recorder -c rawvideo → fifo
  → GStreamer: filesrc ! rawvideoparse ! vah264enc ! rtph264pay ! webrtcbin
  → phone
phone touch → WebRTC datachannel → nx-streamerd → adb tunnel
  → scrcpy server (control-only) → android InputManager
```

### Why touch goes around the compositor

The obvious path is a uinput virtual touchscreen: kernel → libinput → sway →
waydroid surface. It doesn't work here, and the reason is worth writing down.

A libinput backend needs a **seat** (logind or seatd). Our sway is started
headless from a shell with no seat of its own, so `WLR_BACKENDS=headless,libinput`
makes sway die at startup; plain `WLR_BACKENDS=headless` starts fine but has no
input backend at all, so uinput devices are created, appear in `/dev/input`, and
are read by nobody. Either way the tap never lands.

So v0.1 skips the host input stack entirely and injects **inside Android**,
through the same control socket scrcpy uses:

- `adb push` scrcpy's server jar, start it control-only (`video=false
  audio=false control=true tunnel_forward=true`), `adb forward` a TCP port onto
  its `localabstract:scrcpy_<scid>` socket, and write 32-byte
  `INJECT_TOUCH_EVENT` messages at it (see BORROWED.md).
- With video disabled the server does no coordinate mapping, so we send device
  pixels directly — our capture geometry and Android's display are the same
  1080×2400 by construction.
- The tradeoff: it depends on adb reaching the container (Waydroid needs its
  network up, and `service.adb.tcp.port` set for adbd to listen on TCP), and it
  bypasses the compositor, so a future non-Waydroid client would need its own
  path.

`--input uinput` keeps the evdev multitouch device for rigs that *do* have a
seat — a normal desktop session, or a nested compositor with seatd — and
`--input none` streams video only.

### v1.0 (nx-compositor)

sway + portal get replaced by a purpose-built wlroots compositor (~tinywl-sized):

- Waydroid's surface is the only client; no window management at all.
- Frames leave as dmabufs straight into the VAAPI encoder — no PipeWire hop,
  no extra copy.
- Touch is injected at the compositor seat (`wlr_seat_touch_*`) — no uinput,
  no libinput, one fewer context switch and no udev permissions dance.
- Damage tracking → encode only when Android actually drew (idle Android
  costs ~0 bandwidth).

## Transport

WebRTC via GStreamer `webrtcbin`. We keep it because it gives us congestion
control (GCC), FEC, DTLS-SRTP encryption, and NAT traversal for free — the
person-decades we don't want to spend. Runs over Tailscale/WireGuard; the
signaling server is a small piece of nx-streamerd and never leaves the VPN.

If measured glass-to-glass latency hits WebRTC's jitter-buffer floor and it
matters in practice, v1.x swaps in a raw UDP transport modeled on WiVRn's.

Adaptive bitrate (done, v0.1): the shape of the auto-bitrate work from our WiVRn
fork, ported to `webrtcbin`'s `get-stats`. `AdaptiveBitrate` in
`server/nx-streamerd.py` samples once a second while a client is attached, takes
per-interval deltas of `packets-sent` / `packets-lost` off the `outbound-rtp` and
`remote-inbound-rtp` entries plus a smoothed `round-trip-time`, and drives the
encoder's `bitrate` property:

- loss > 5%, or smoothed RTT above both 150 ms and 2x the session floor →
  multiplicative decrease (x0.70) plus an upstream force-key-unit, so the client
  recovers on an IDR instead of waiting out the 2 s GOP;
- loss < 1% with calm RTT for 5 consecutive samples → additive increase of 10%
  of the ceiling;
- anything between → hold.

Clamped to `[--min-bitrate, --bitrate]`, at most one change per second, and a 3 s
settle window after every (re)negotiation. A session starts at 60% of the ceiling
and probes upward: the failure this fixes is the *first* seconds of a mobile
session, where a full-rate stream punched into an unmeasured 5G uplink takes the
connection down with it. `--no-abr` pins the encoder to `--bitrate`.

GCC exists inside `webrtcbin`, but nothing in this pipeline listened to it — the
encoder is a `vah264enc` with a plain `bitrate` property that GCC has never heard
of. This class is that missing wire.

Manual control (config channel): the client may send
`{"type":"config","bitrate":<kbps>,"fps":<n>,"abr":<bool>}` on the **signaling
websocket** — not the input datachannel, which stays input-only, so config works
before the datachannel opens. Any field may be absent or null ("leave alone").
The server clamps hard (bitrate 500..50000 kbps, fps 15..120), ignores junk, and
always answers with the effective state:
`{"type":"config","bitrate":N,"fps":N,"abr":b,"min_bitrate":N,"max_bitrate":N}`.
The same frame is pushed unsolicited right behind every offer, so a fresh client
never has to ask. A manual bitrate moves the *ceiling*, not just the
instantaneous value; `abr:false` pins the encoder there. Framerate is baked into
the wf-recorder command line and the `rawvideoparse` caps, so an fps change tears
down capture + pipeline and re-offers to the same websocket — the client only
ever answers, never offers, so a fresh offer is always legal. Nothing is
persisted server-side; the client owns its preferences.

## Client

- **v0.1 — PWA.** RTCPeerConnection playback (works over plain http on the VPN;
  no secure-context requirement for receiving), fullscreen API, touch events →
  datachannel. Exists to prove the pipe, not to be good.
- **v0.2+ — Kotlin.** The actual product:
  - MediaCodec in low-latency mode, AV1 (Tensor G2 hw decode) with HEVC fallback
  - SurfaceView, 90 Hz frame pacing (moonlight-android's renderer is the
    reference here)
  - sticky immersive fullscreen; phone gestures land inside the stream
  - Opus audio both ways (stream audio out; mic bridge later)
  - reconnect state machine that survives doze, network handoff (Wi-Fi↔5G),
    and lock/unlock — the Pico-standby lesson from WiVRn

## Reverse streams — camera and mic (planned, v0.2/v0.3)

The forward direction makes the PC's Android visible on the phone; feature-
complete means the phone's *hardware* is visible to the PC's Android too.

- **Mic**: upstream WebRTC audio track from the client → daemon → a virtual
  PipeWire source set as the default input for the Waydroid session. Android's
  audio HAL records from the host default source, so apps just see "the mic".
- **Camera**: upstream WebRTC video track → daemon decodes into a
  **v4l2loopback** device → device passed into the LXC container → Android's
  external-camera HAL (the USB-webcam path, Android 9+) exposes it as a real
  camera. Good enough for video calls; not for 4K60 vlogging.
- **Prerequisite**: browsers only grant `getUserMedia` on secure origins, so
  the PWA path needs https (Tailscale can issue real certs for its hostnames) —
  the native Kotlin client has no such restriction and is the intended carrier
  for both. Idle cost is zero: reverse tracks are negotiated only while an
  Android app actually holds the camera/mic open.

## Shared storage — one gallery (planned, v0.3)

The container's `/sdcard` is a plain host directory, which turns "shared
gallery" into a file-sync problem with an existing best-in-class answer:
**Syncthing** (phone `DCIM`/`Pictures` ↔ a host folder inside Waydroid's
storage), plus an adb `media scan` poke so MediaStore indexes arrivals
immediately. Photos taken on the real phone appear in the remote gallery;
remote screenshots and downloads flow back. A network drive/SMB share can't do
this — Android galleries only index local MediaStore storage, which is exactly
what the sync target is.

**On-demand picker bridge**: the sharper version. A small companion app inside
the remote Android (`nx-bridge`, planned) registers as a photo/file picker
(`ACTION_GET_CONTENT` / DocumentsProvider). When a remote app requests a
picture, the *real* phone's picker opens (native in the Kotlin client; a plain
file input in the PWA — no secure-context needed), the chosen file rides the
existing connection into `/sdcard`, gets media-scanned, and completes the
intent as if picked locally. The same companion app is the intended home for
the notification bridge and clipboard sync — one in-container helper, many
native-feel features.

## Native-feel checklist

The bar: someone picks up the Pixel and doesn't notice it's remote.

- [ ] geometry & refresh identical to panel (1080×2400@90)
- [ ] touch-to-photon under ~70 ms on 5G, ~40 ms on LAN
- [ ] gesture nav (back/home/recents) works inside the stream
- [ ] unlock → stream instantly (launcher mode / boot-to-stream)
- [ ] stream survives sleep, reconnects silently
- [ ] audio in sync
- [ ] hardware volume keys control remote Android
- [ ] clipboard sync both directions
- [ ] Waydroid notifications bridged to real phone notifications
- [ ] phone camera available to remote Android apps (v4l2loopback bridge)
- [ ] phone mic available to remote Android apps (virtual PipeWire source)
- [ ] one gallery: photos sync both ways (Syncthing into /sdcard + media scan)
- [ ] battery drain comparable to video playback

## Latency budget (target, 5G)

| Stage | Budget |
|---|---|
| Android render + compositor | ~2 ms |
| Capture + encode (VAAPI, RDNA3) | ~5 ms |
| Network (Tailscale over 5G) | 20–40 ms |
| Jitter buffer | 0–10 ms |
| Decode + display (Tensor G2, 90 Hz) | ~8 ms |
| **Glass to glass** | **~40–70 ms** |

## Not in scope (be honest)

No SIM/calls/SMS/NFC/real GPS/camera on the remote side — this is a second,
stronger Android, not a remote control for the Pixel's own identity. Play
Integrity fails (uncertified container): banking apps and Wallet stay on the
local phone. Widevine L3 only.
