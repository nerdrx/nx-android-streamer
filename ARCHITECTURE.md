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

Adaptive bitrate: port the shape of the auto-bitrate work from our WiVRn fork —
observe RTT/loss from RTCP, feed encoder bitrate/keyframe decisions.

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
