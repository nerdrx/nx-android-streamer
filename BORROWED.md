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

## In tree today

| Our file | Source | License | What we took |
|---|---|---|---|
| `server/nx-streamerd.py` (`TouchInjector`) | [Sunshine](https://github.com/LizardByte/Sunshine) `src/platform/linux/input.cpp` | GPL-3.0 | Shape of the evdev protocol-B multitouch device: slot/tracking-id bookkeeping, `INPUT_PROP_DIRECT`, ABS_X/ABS_Y single-touch mirrors, BTN_TOUCH on first contact / off on last lift. Rewritten in Python with python-evdev, touch-only, normalized 0..1 coordinates. |
| `server/nx-streamerd.py` (`ScrcpyInjector`) | [scrcpy](https://github.com/Genymobile/scrcpy) v4.1: `app/src/control_msg.{c,h}`, `app/src/server.c`, `server/src/main/java/com/genymobile/scrcpy/control/Controller.java` | Apache-2.0 | The control protocol, not the code: the 32-byte `INJECT_TOUCH_EVENT` layout (type 2, big-endian, pressure as u16 fixed point), the `CLASSPATH=… app_process … com.genymobile.scrcpy.Server <version> scid=…` launch line with `tunnel_forward`, the `localabstract:scrcpy_<scid>` tunnel and its handshake byte, and the fact that a `video=false` server maps no coordinates so the client must send device pixels. Reimplemented in Python/asyncio, touch-only, with restart supervision. Wire format verified byte-for-byte against upstream's own `test_serialize_inject_touch_event` vector. |

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
