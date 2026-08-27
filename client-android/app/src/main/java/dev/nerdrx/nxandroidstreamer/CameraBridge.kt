package dev.nerdrx.nxandroidstreamer

import android.Manifest
import android.content.pm.PackageManager
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import org.json.JSONObject
import org.webrtc.Camera2Enumerator
import org.webrtc.EglBase
import org.webrtc.MediaStreamTrack
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpTransceiver
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoCapturer
import org.webrtc.VideoSource
import org.webrtc.VideoTrack

/**
 * The phone's camera, offered upstream to the remote Android.
 *
 * Path, end to end: Camera2 -> libwebrtc video track -> the WebRTC session's
 * second m=video section -> nx-streamerd decodes it -> v4l2loopback -> the
 * container's external-camera HAL -> apps see "a camera". The daemon half is in
 * server/nx_bridge.py; what it needs from this class is simply *packets on the
 * upstream video m-line*, which is why there is no codec logic here at all.
 *
 * Two things make it work inside a protocol where the server is always the
 * offerer and we can never initiate a renegotiation:
 *
 *  - the server's offer already carries a recvonly m=video whenever the camera
 *    bridge is enabled, so the section exists from the first answer onwards;
 *  - a transceiver created from a remote *recvonly* section defaults to
 *    inactive, which would kill the section for the whole session — so
 *    [prepare] flips it to sendonly in the one window where that is still
 *    possible: after setRemoteDescription, before createAnswer.
 *
 * After that, turning the camera on and off is just setTrack() on a sender.
 * No second offer, no reconnect, and the camera is genuinely released (LED off,
 * other apps can use it) the moment the answer is "off".
 *
 * Privacy: this class never turns the camera on by itself. The user's toggle is
 * OFF by default; we tell the server whether it *may* ask, and only capture when
 * it does. Denying the runtime permission is a normal outcome, not an error —
 * it flips the toggle back off and tells the server so it stops asking.
 */
class CameraBridge(
    private val activity: ComponentActivity,
    private val factory: PeerConnectionFactory,
    private val eglBase: EglBase,
    private val prefs: Prefs,
    private val client: () -> StreamClient?,
    private val onStatus: (String?) -> Unit
) {

    private val permission = activity.registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            startCapture()
        } else {
            // Graceful denial: remember it, stop asking, and say why in the UI.
            Log.d(TAG, "camera permission denied — turning the bridge off")
            prefs.allowRemoteCamera = false
            status = "Camera permission denied"
            publish()
            sendAllow(false)
        }
    }

    // ---- server-announced capability ------------------------------------
    private var available = false
    private var wanted = false                 // the server's latest on/off
    private var width = 1280
    private var height = 720
    private var fps = 30
    private var status: String? = null

    // ---- live capture ----------------------------------------------------
    private var sender: RtpTransceiver? = null
    private var capturer: VideoCapturer? = null
    private var source: VideoSource? = null
    private var track: VideoTrack? = null
    private var helper: SurfaceTextureHelper? = null

    /** What the settings screen shows under the toggle. */
    fun statusText(): String? = status

    // ---------------------------------------------------------------------
    // Negotiation
    // ---------------------------------------------------------------------

    /**
     * Called from StreamClient the instant the offer has been applied and before
     * the answer is created. Finds the recvonly m=video the server added for us
     * and claims it as a sender.
     *
     * Matching is by mid, parsed out of the offer, rather than by transceiver
     * index: the daemon's own video is also an m=video and both transceivers
     * arrive with the same default direction, so position is the only thing that
     * separates them and position is exactly what a future extra m-line would
     * change.
     */
    fun prepare(pc: PeerConnection, offerSdp: String) {
        sender = null
        val mid = recvonlyVideoMid(offerSdp) ?: return
        val transceiver = try {
            pc.transceivers.firstOrNull { it.mid == mid }
        } catch (e: Exception) {
            Log.w(TAG, "could not enumerate transceivers: ${e.message}")
            null
        } ?: return
        try {
            // Without this the answer is a=inactive and the section is dead for
            // the rest of the session, whatever we do with tracks later.
            transceiver.direction = RtpTransceiver.RtpTransceiverDirection.SEND_ONLY
            sender = transceiver
            Log.d(TAG, "camera transceiver mid=$mid claimed as sendonly")
        } catch (e: Exception) {
            Log.w(TAG, "could not claim the camera transceiver: ${e.message}")
        }
        // A fresh peer connection means the old track is gone with it.
        if (wanted && prefs.allowRemoteCamera) ensureCapture()
    }

    /**
     * -> the mid of the first `m=video` section the offer marks `a=recvonly`.
     *
     * Hand-rolled rather than pulled from a library because it is six lines and
     * the alternative is a dependency that parses all of SDP to answer one
     * question. Attributes are per-section, so we simply track which section we
     * are inside.
     */
    private fun recvonlyVideoMid(sdp: String): String? {
        var inVideo = false
        var mid: String? = null
        var recvonly = false
        for (raw in sdp.split("\r\n", "\n")) {
            val line = raw.trim()
            if (line.startsWith("m=")) {
                if (inVideo && recvonly && mid != null) return mid
                inVideo = line.startsWith("m=video")
                mid = null
                recvonly = false
                continue
            }
            if (!inVideo) continue
            if (line.startsWith("a=mid:")) mid = line.removePrefix("a=mid:")
            if (line == "a=recvonly") recvonly = true
        }
        return if (inVideo && recvonly) mid else null
    }

    // ---------------------------------------------------------------------
    // Signaling
    // ---------------------------------------------------------------------

    /** `{"type":"camera", available, on, device, width, height, fps, note?}`. */
    fun onCameraMessage(msg: JSONObject) {
        available = msg.optBoolean("available", false)
        wanted = msg.optBoolean("on", false)
        width = msg.optInt("width", width)
        height = msg.optInt("height", height)
        fps = msg.optInt("fps", fps)

        status = when {
            !available -> msg.optString("note").ifBlank { "Not available on the host" }
            wanted -> "Streaming to the remote Android"
            prefs.allowRemoteCamera -> "Allowed — the host is not asking for it"
            else -> "Off"
        }
        publish()

        if (!available) {
            stopCapture()
            return
        }
        // The server computes `on` from the allow flag we sent it, so an
        // announce that disagrees with our preference means it has not heard the
        // preference yet (fresh connection). Tell it, once.
        if (prefs.allowRemoteCamera != wanted) {
            sendAllow(prefs.allowRemoteCamera)
            if (!prefs.allowRemoteCamera) stopCapture()
            return
        }
        if (wanted) ensureCapture() else stopCapture()
    }

    /** The settings toggle moved. */
    fun onAllowChanged(allow: Boolean) {
        if (!allow) {
            stopCapture()
            status = "Off"
            publish()
        }
        sendAllow(allow)
    }

    /** Session ended or dropped: release the camera, do not hold it across a
     *  reconnect the user may never complete. */
    fun onSessionEnded() {
        stopCapture()
        sender = null
        wanted = false
        if (available) {
            status = if (prefs.allowRemoteCamera) "Waiting for the host" else "Off"
            publish()
        }
    }

    private fun sendAllow(allow: Boolean) {
        client()?.sendBridgeFrame(JSONObject().put("type", "camera").put("allow", allow))
    }

    private fun publish() = onStatus(status)

    // ---------------------------------------------------------------------
    // Capture
    // ---------------------------------------------------------------------

    private fun ensureCapture() {
        if (track != null) {
            attach()
            return
        }
        if (ContextCompat.checkSelfPermission(activity, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        ) {
            Log.d(TAG, "camera requested — asking for the runtime permission")
            status = "Waiting for camera permission"
            publish()
            permission.launch(Manifest.permission.CAMERA)
            return
        }
        startCapture()
    }

    private fun startCapture() {
        if (track != null) { attach(); return }
        if (!prefs.allowRemoteCamera) return
        val enumerator = Camera2Enumerator(activity)
        val names = try { enumerator.deviceNames } catch (e: Exception) { emptyArray<String>() }
        // Front camera first: the thing this feature exists for is a video call
        // inside the remote Android, and that always wants the selfie camera.
        val chosen = names.firstOrNull { enumerator.isFrontFacing(it) }
            ?: names.firstOrNull()
        if (chosen == null) {
            Log.w(TAG, "no camera on this device")
            status = "No camera on this phone"
            publish()
            sendAllow(false)
            return
        }
        try {
            val cap = enumerator.createCapturer(chosen, null)
                ?: throw IllegalStateException("createCapturer returned null")
            val hlp = SurfaceTextureHelper.create("nx-cam", eglBase.eglBaseContext)
            val src = factory.createVideoSource(false)
            cap.initialize(hlp, activity, src.capturerObserver)
            cap.startCapture(width, height, fps)
            val trk = factory.createVideoTrack("nx-camera", src)
            trk.setEnabled(true)
            capturer = cap
            helper = hlp
            source = src
            track = trk
            Log.d(TAG, "camera $chosen capturing at ${width}x$height@$fps")
            status = "Streaming to the remote Android"
            publish()
            attach()
        } catch (e: Exception) {
            Log.e(TAG, "camera start failed: ${e.message}")
            releaseCapture()
            status = "Camera failed to start"
            publish()
            sendAllow(false)
        }
    }

    private fun attach() {
        val t = track ?: return
        val s = sender ?: return
        try {
            // setTrack(track, false): do NOT take ownership — this class disposes
            // the track itself, and a double dispose inside libwebrtc is a native
            // crash, not an exception.
            s.sender.setTrack(t, false)
            Log.d(TAG, "camera track attached to mid=${s.mid}")
        } catch (e: Exception) {
            Log.w(TAG, "could not attach the camera track: ${e.message}")
        }
    }

    private fun stopCapture() {
        val s = sender
        if (s != null) {
            try { s.sender.setTrack(null, false) } catch (e: Exception) { /* gone */ }
        }
        releaseCapture()
    }

    private fun releaseCapture() {
        // Order matters: stop the frames, then tear down what produced them.
        try { capturer?.stopCapture() } catch (e: Exception) { /* already stopped */ }
        try { capturer?.dispose() } catch (e: Exception) {}
        try { track?.dispose() } catch (e: Exception) {}
        try { source?.dispose() } catch (e: Exception) {}
        try { helper?.dispose() } catch (e: Exception) {}
        capturer = null
        track = null
        source = null
        helper = null
    }

    /** Activity teardown. */
    fun release() {
        stopCapture()
        sender = null
    }

    companion object {
        private const val TAG = "nx"
        /** Kept for callers that want to know what a video track looks like. */
        val VIDEO_KIND: String = MediaStreamTrack.VIDEO_TRACK_KIND
    }
}
