# Borrowed code & prior art

House rule: **we don't invent wheels, we credit them.** Every chunk of code
adapted from another project carries a header comment naming the source file,
project, and license, and gets a row here. If a diff looks clever and has no
attribution header, that's a review failure.

## Planned / active borrows

| Project | License | What we take |
|---|---|---|
| [Sunshine](https://github.com/LizardByte/Sunshine) | GPL-3.0 | VAAPI encoder configuration, wlroots screencopy capture reference, uinput touch-injection patterns |
| [moonlight-android](https://github.com/moonlight-stream/moonlight-android) | GPL-3.0 | MediaCodec low-latency decoder setup, frame pacing / render-time tricks for the Kotlin client |
| [WiVRn](https://github.com/WiVRn/WiVRn) (incl. our NX fork) | GPL-3.0 | Adaptive bitrate logic, client session lifecycle handling (sleep/reconnect) |
| [scrcpy](https://github.com/Genymobile/scrcpy) | Apache-2.0 | Touch event encoding ideas, Android-side input semantics |
| [wlroots](https://gitlab.freedesktop.org/wlroots/wlroots) / tinywl | MIT | Compositor skeleton for v1.0 `nx-compositor` |
| GStreamer examples (`webrtcbin`) | LGPL | WebRTC pipeline wiring, signaling patterns |
| [waydroid_script](https://github.com/casualsnek/waydroid_script) | GPL-3.0 | Used as a tool (not vendored): libndk ARM translation, Widevine, Play certification |

## Used as-is, not vendored

- **Waydroid** — installed from the distro like a normal package. We configure
  it; we don't fork it.
- **sway** — interim headless compositor until `nx-compositor` exists.

## Header template

```c
/* Adapted from Sunshine (src/platform/linux/input.cpp), GPL-3.0
 * https://github.com/LizardByte/Sunshine — thank you LizardByte.
 * Changes: stripped gamepad paths, portrait touch mapping. */
```
