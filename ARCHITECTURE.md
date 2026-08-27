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

## Audio (server half, v0.2, off by default)

Waydroid has **no sink of its own**. Its HAL (`audio.primary.waydroid.so`) is
plain ALSA — libasound with `PULSE_RUNTIME_PATH=/run/xdg/pulse`, a bind mount of
the host's PulseAudio socket — so Android shows up on the host as one ordinary
playback stream named `Waydroid`, mixed into whatever the default sink happens to
be, next to the browser and the chat client. There is therefore no monitor source
that means "Waydroid and only Waydroid", and capturing the default sink's monitor
would ship the user's entire desktop audio to their phone. That is the whole
reason this needs a design rather than a one-liner.

`--audio` picks the capture point, and defaults to `none` — the launch string is
then byte-identical to the video-only one, so audio cannot cost the working video
path a negotiation:

- `none` — no audio branch at all.
- `auto` — make a monitor that *is* Waydroid-only: load a private
  `module-null-sink` (`nxas_waydroid`), move Waydroid's playback streams into it,
  and capture `nxas_waydroid.monitor`. The default sink and default source are
  never touched, only streams named Waydroid are moved, a 2 s poll re-homes
  streams that appear later (Android opens and closes the PCM as apps come and
  go), and everything is moved back and the module unloaded on exit. The sink is
  created at startup, before any client can connect, so the source exists for the
  very first offer.
- `<source-name>` — capture a named PulseAudio source verbatim, for a rig that
  already routes Waydroid somewhere deliberate.

Debugging note: **Discord names its PulseAudio stream "WEBRTC VoiceEngine"**,
which is indistinguishable by name from something a WebRTC streamer would
create. It cost real time chasing a "leak" that was just Discord playing on the
desktop. Ignore names, look at the graph:

```
pw-link -l | grep -A3 '^nxas_waydroid:playback'   # must list ONLY Waydroid:output_*
pw-link -l | grep -A3 'nxas_waydroid:monitor'     # must feed no speaker
ss -tnp | grep :8765                              # who is really attached
```


The branch is a second sendonly transceiver on the *same* `webrtcbin`:
`pulsesrc ! audioconvert ! audioresample ! opusenc ! rtpopuspay ! webrtc.`,
pt=97, 48 kHz stereo, mtu 1100 like the video (Tailscale's 1280-byte MTU).
`provide-clock=false` is load-bearing: video comes off a non-live `filesrc`, so
the pipeline runs on the system clock, and a live `pulsesrc` would otherwise
volunteer as clock provider and quietly re-pace the encoder against the sound
card. Video timing is not audio's business.

The client needs no code for playback — libwebrtc decodes the Opus track and
plays it once negotiated. It does need the right *output*: the default device
module opens `VOICE_COMMUNICATION` (the earpiece-and-AEC call path), so the
Kotlin client installs a `JavaAudioDeviceModule` with `USAGE_MEDIA`, where the
volume keys and the loudspeaker already are.

## Battery mirroring (v0.2)

The remote should read as the device in your hand, and the status bar is the
first place that shows. The client sends
`{"type":"battery","level":<0-100>,"charging":<bool>}` on the **signaling
websocket** (same channel as `config`), once on connect and then only on change;
`{"type":"battery","enabled":false}` is how it says the user switched mirroring
off. There is no polling on the phone: `ACTION_BATTERY_CHANGED` is sticky, so
registering both arms the watch and yields the current value, and only movement
in level or charging state leaves the client (level ticks floored at 30 s apart,
plug/unplug exempt).

The server applies it with `adb shell dumpsys battery set ac|usb|status|level`.
Both power rails go down together for "unplugged": Waydroid's health HAL reports
AC *and* USB online by default, and a container with USB still powered keeps the
charging bolt whatever `status` says.

The sharp edge is that `dumpsys battery set` is not a report but an **override** —
the battery service stops taking updates from the HAL and holds what we forced
for as long as the container lives, long after the daemon is gone. So
`dumpsys battery reset` runs on every way out: client disconnect, mirroring
switched off, daemon shutdown, and defensively on the next client's attach. Two
guards keep a late update from resurrecting a dead session's override — the
daemon drops a battery task that starts after its client left, and `BatteryMirror`
carries an epoch that `reset()` bumps, so an apply already in flight is discarded.

`Adb` is one shared object: the touch path and the battery mirror resolve the
container once, behind a lock, and every call carries `-s <serial>` so a phone
plugged into the PC can never be the target.

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

## Reverse streams — camera and mic

The forward direction makes the PC's Android visible on the phone; feature-
complete means the phone's *hardware* is visible to the PC's Android too.

- **Mic** (planned): upstream WebRTC audio track from the client → daemon → a
  virtual PipeWire source set as the default input for the Waydroid session.
  Android's audio HAL records from the host default source, so apps just see
  "the mic".
- **Camera** (built, v0.2 — see below for exactly how far): upstream WebRTC
  video track → daemon decodes into a **v4l2loopback** device → device passed
  into the LXC container → Android's external-camera HAL (the USB-webcam path,
  Android 9+) exposes it as a real camera. Good enough for video calls; not for
  4K60 vlogging.
- **Prerequisite**: browsers only grant `getUserMedia` on secure origins, so
  the PWA path needs https (Tailscale can issue real certs for its hostnames) —
  the native Kotlin client has no such restriction and is the carrier for both.

### Camera bridge: what is built, and what it needs

Server half in `server/nx_bridge.py` (`CameraReceiver`), phone half in
`client-android/.../CameraBridge.kt`. Off by default at both ends: the daemon
needs `--camera auto|/dev/videoN` and the client needs "Allow remote camera
access", which ships OFF because handing a machine across a VPN a live camera
is the most invasive thing this app can do.

**No renegotiation, by construction.** In this protocol the server is always the
offerer and the client can never initiate an offer, so a track that appears
mid-session has nowhere to be negotiated. Instead, when `--camera` resolves to a
real node, the daemon adds a **recvonly** H.264 transceiver to `webrtcbin`
*before* the first offer, so every offer carries a second `m=video`. The client
finds it by mid (the section marked `a=recvonly`), flips its own direction to
`sendonly` in the one window where that is still possible — after
`setRemoteDescription`, before `createAnswer` — and from then on turning the
camera on and off is just `setTrack()` on a sender. Idle cost is one dormant
m-line: no packets, no encoder, no camera LED, and the camera is genuinely
released when the answer is "off".

**Verified on this machine (2026-08-27):**

- The container **does** have the external camera HAL. LineageOS 20 /
  Android 13, `ro.hardware.camera=v4l2`, with
  `android.hardware.camera.provider@2.7-external-service` *running* and
  `/vendor/etc/external_camera_config.xml` present. This is the piece most
  likely to have been missing, and it is not missing.
- Waydroid already binds every `/dev/video*` it finds into the container
  (`lxc.mount.entry` lines in `/var/lib/waydroid/lxc/waydroid/config`).
- The offer really does grow the extra section: with `--camera` pointed at a
  node, the SDP is `m=video sendonly | m=video recvonly | m=application`, and
  the mid parser in `CameraBridge.kt` picks the right one out of it.
- `v4l2loopback` is **not loaded** here, so the daemon takes the degraded path:
  it says so, prints the modprobe line, and leaves the pipeline byte-for-byte
  unchanged (still one `m=video`). Loading it needs root, which nothing in this
  project has or wants.

**What has therefore NOT been proven end to end:** actual frames arriving in an
Android app. The two untested links are `v4l2sink` writing into a loopback node
and the HAL enumerating that node as a camera. Both are the well-trodden
OBS-virtual-camera path, and the HAL is running — but "should work" is not
"works", and it is not claimed here.

To finish it:

```sh
sudo modprobe v4l2loopback devices=1 video_nr=42 card_label="NX Camera" exclusive_caps=1
```

then **restart the Waydroid session**. That second step is not optional and is
easy to lose an hour to: waydroid globs `/dev/video*` into its LXC config only
while *generating* it at session start, so a node created afterwards simply does
not exist inside the container. `exclusive_caps=1` is what makes the node
present itself as a capture device once something is writing to it, which is
what the HAL looks for.

## Shared storage — one gallery

The container's `/sdcard` is a plain host directory, which turns "shared
gallery" into a file-sync problem with an existing best-in-class answer:
**Syncthing** (phone `DCIM`/`Pictures` ↔ a host folder inside Waydroid's
storage), plus an adb `media scan` poke so MediaStore indexes arrivals
immediately. Photos taken on the real phone appear in the remote gallery;
remote screenshots and downloads flow back. A network drive/SMB share can't do
this — Android galleries only index local MediaStore storage, which is exactly
what the sync target is. (Still planned.)

**On-demand picker bridge** (built, v0.2): the sharper version, and the one that
needs no sync at all. A companion app inside the remote Android
(`bridge-android/`, `dev.nerdrx.nxbridge`) registers for `ACTION_GET_CONTENT`
and `ACTION_PICK`, so it shows up in the remote picker as **"Pick on my
phone"**. Choosing it opens the *real* phone's picker; the chosen file rides the
existing signaling socket into `/sdcard`, gets media-scanned, and completes the
intent as if picked locally. The same companion app is the intended home for the
notification bridge and clipboard sync — one in-container helper, many
native-feel features.

### Picker bridge: the wire

```
remote app: ACTION_GET_CONTENT
  → nx-bridge writes <spool>/requests/<id>.json   (mime, action, spool root)
  → nx-streamerd polls that dir over adb, 2 Hz, only while a client is attached
  → {"type":"pick","id","mime"} on the signaling websocket
  → phone opens PickVisualMedia (images) or GetContent (anything)
  → {"type":"pick-data","id","seq","eof","b64"} × N, ~32 KB of payload each
  → daemon reassembles, checks byte count AND sha256, adb push
  → <spool>/responses/<id>__<name>  (pushed as .part, then renamed)
  → nx-bridge sees it, copies into its FileProvider dir, returns a content:// Uri
```

Four decisions worth writing down:

- **A file spool, not a socket.** The daemon already has adb into the container
  and nothing else. A spool needs no port, no service, no permission, and the
  whole protocol stays inspectable with `ls`. The companion app has no
  `INTERNET` permission at all, which makes it a much smaller thing to trust.
- **Polling, not inotify.** A pick happens maybe once an hour; two `ls` globs at
  2 Hz cost less than the touch events already in flight, and inotify across the
  container's FUSE `/sdcard` is not dependable on every image. Polling also
  stops entirely when no client is attached, and never starts at all if
  `pm path dev.nerdrx.nxbridge` says the app is not installed.
- **Two spool roots, watched together.** `/sdcard/nx-bridge` is the readable one,
  but an app targeting API 30+ can only create it with All-files-access, which
  nobody has granted. The fallback is the app's own external files dir
  (`/sdcard/Android/data/dev.nerdrx.nxbridge/files/nx-bridge`), which needs no
  permission and which adb's `shell` user can still read — it is in the
  `ext_data_rw` group on Android 11+. The app picks whichever it can write and
  *names it in the request*, so the answer always goes back to the same root and
  granting the permission later changes nothing on either side.
- **Chunked, and checked twice.** A Pixel photo is 3–8 MB and the signaling
  socket also carries SDP, ICE and config; one giant frame would stall
  negotiation behind it for the length of the upload — and reconnects happen
  mid-upload. ~32 KB per frame interleaves finely, the client paces itself on
  OkHttp's queue depth so a big file cannot bury an ICE frame (or the phone's
  heap), and the daemon verifies both the declared byte count and a sha256
  before it pushes. A photo that arrives *almost* intact is the failure nobody
  notices until the upload they just made is half grey.

Both ends fail closed: a cancel, an error, a truncated transfer or a vanished
client all end as `RESULT_CANCELED` in the remote app rather than a broken Uri,
and the daemon writes a `<id>.cancel` marker so the in-container activity stops
waiting instead of sitting out its 120 s timeout.

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
- [ ] audio in sync — server half built (`--audio auto|none|<source>`, default
      `none`); the Opus track negotiates as a second sendonly transceiver and the
      client plays it with no extra code. Sync against real video is unmeasured.
- [ ] hardware volume keys control remote Android
- [ ] clipboard sync both directions
- [ ] Waydroid notifications bridged to real phone notifications
- [ ] phone camera available to remote Android apps (v4l2loopback bridge) —
      both halves built and negotiating; blocked on `modprobe v4l2loopback`,
      which needs root. Frames into an app are unproven, see above.
- [ ] phone mic available to remote Android apps (virtual PipeWire source)
- [x] pick a file on the real phone from inside a remote app (nx-bridge)
- [ ] one gallery: photos sync both ways (Syncthing into /sdcard + media scan)
- [x] the remote wears the real phone's battery (level + charging state)
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
