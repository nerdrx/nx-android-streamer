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
  → sway (WLR_BACKENDS=headless,libinput, output 1080×2400@90)
  → xdg-desktop-portal-wlr → PipeWire
  → GStreamer: pipewiresrc ! vapostproc ! vah265enc (or AV1) ! webrtcbin
  → phone
phone touch → WebRTC datachannel → nx-streamerd → uinput virtual touchscreen
  → libinput → sway → waydroid surface
```

Known trap, already encoded in `start.sh`: plain `WLR_BACKENDS=headless` gives
sway **no input backend at all**, so injected uinput devices go nowhere. It must
be `headless,libinput` (plus `WLR_LIBINPUT_NO_DEVICES=1` so sway tolerates
starting with zero devices).

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
