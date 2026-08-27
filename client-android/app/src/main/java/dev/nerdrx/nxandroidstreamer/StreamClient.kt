package dev.nerdrx.nxandroidstreamer

import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import org.webrtc.CandidatePairChangeEvent
import org.webrtc.DataChannel
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpReceiver
import org.webrtc.RtpTransceiver
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.SurfaceViewRenderer
import org.webrtc.VideoTrack
import java.nio.ByteBuffer
import java.nio.charset.StandardCharsets
import java.util.concurrent.TimeUnit

/**
 * The WebRTC + WebSocket signaling client and its reconnect state machine.
 *
 * This is a byte-for-byte behavioural port of web/app.js — the server-verified,
 * FROZEN protocol. The server is the offerer; we always answer, never offer.
 * The whole machine runs on the main looper, exactly like the browser's single
 * JS thread: every OkHttp and WebRTC callback re-posts onto [main] before it
 * touches state, so a late callback from a torn-down session can never race the
 * new one. Each connection attempt carries a generation number [gen]; stale
 * callbacks compare against it and bail (app.js does the same).
 *
 * Going native buys us the lifecycle the PWA could not have: this survives the
 * screen turning off, doze, and a Wi-Fi<->cellular handoff without a manual
 * reload — the WiVRn Pico-standby lesson. A dead link that never closes cleanly
 * (phone slept, NAT rebind) shows up as pongs going missing long before the
 * socket notices, so a heartbeat watchdog forces the reconnect.
 */
class StreamClient(
    private val factory: PeerConnectionFactory,
    private val renderer: SurfaceViewRenderer,
    private val host: String,
    private val port: Int,
    // Read fresh each time so a settings toggle takes effect on the next drop.
    private val autoReconnect: () -> Boolean,
    private val listener: Listener
) {
    // DISCONNECTED is the resting state when auto-reconnect is off and the link
    // dropped: the machine parks instead of looping, and the UI offers a manual
    // Reconnect button.
    enum class Phase { CONNECTING, RECONNECTING, LIVE, DISCONNECTED }

    interface Listener {
        fun onPhase(phase: Phase)
        fun onRtt(rttMs: Int?)
    }

    private val main = Handler(Looper.getMainLooper())

    // OkHttp with no read timeout: the ws stays open idle between signaling frames.
    private val httpClient = OkHttpClient.Builder()
        .pingInterval(0, TimeUnit.SECONDS)
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)
        .build()

    // ---- session state (mirrors app.js) --------------------------------
    private var gen = 0
    private var ws: WebSocket? = null
    private var pc: PeerConnection? = null
    private var dc: DataChannel? = null            // "input" channel, server-created
    private var remoteVideo: VideoTrack? = null

    private var retryDelayMs = RECONNECT_MIN_MS
    private var retryScheduled = false
    private var lastPongAt = 0L
    private var rtt: Int? = null
    private var live = false
    private var stopped = false
    private var attempts = 0

    // readiness signals for markLiveIfReady()
    private var haveVideo = false
    private var pcConnected = false

    private var lastPhase: Phase? = null
    // Last quality settings the user chose; re-sent on every (re)connect so the
    // server always matches the phone's UI, including after a drop.
    private var pendingConfig: Triple<Int, Int, Boolean>? = null

    private val pingRunnable = object : Runnable {
        override fun run() {
            pingTick(gen)
        }
    }

    // ---------------------------------------------------------------------
    // Public control surface
    // ---------------------------------------------------------------------

    fun start() {
        stopped = false
        attempts = 0
        lastPhase = null
        connect()
    }

    /** Reconnect right now, skipping the backoff wait (foreground return, network
     *  came back). Mirrors app.js reconnectNow(). */
    fun reconnectNow() {
        if (stopped) return
        main.removeCallbacks(retryDelayed)
        retryScheduled = false
        retryDelayMs = RECONNECT_MIN_MS
        connect()
    }

    /** True if the pipe looks healthy enough to skip a wake-up reconnect. */
    fun isHealthy(): Boolean {
        val d = dc
        val p = pc
        return live && d != null && d.state() == DataChannel.State.OPEN &&
            p != null && p.connectionState() != PeerConnection.PeerConnectionState.FAILED &&
            p.connectionState() != PeerConnection.PeerConnectionState.CLOSED
    }

    /** Suppress the pong watchdog tripping on time the phone spent asleep. */
    fun noteAwake() {
        lastPongAt = System.currentTimeMillis()
    }

    /**
     * Push stream-quality settings to the server over the signaling socket.
     * Safe to call any time: if the socket isn't open yet the settings ride
     * along on the next connect (sendConfig is called again once ws opens).
     */
    fun sendConfig(bitrateKbps: Int, fps: Int, abr: Boolean) {
        pendingConfig = Triple(bitrateKbps, fps, abr)
        val sock = ws ?: return
        val msg = JSONObject()
            .put("type", "config")
            .put("bitrate", bitrateKbps)
            .put("fps", fps)
            .put("abr", abr)
        try {
            sock.send(msg.toString())
        } catch (e: Exception) {
            Log.w(TAG, "config send failed: ${e.message}")
        }
    }

    fun stop() {
        stopped = true
        main.removeCallbacks(retryDelayed)
        retryScheduled = false
        teardown()
    }

    // ---------------------------------------------------------------------
    // Connection lifecycle
    // ---------------------------------------------------------------------

    private fun connect() {
        if (stopped) return
        teardown()                       // paranoia: never run two sessions
        val myGen = ++gen
        attempts++

        // First attempt reads "connecting…"; anything after a failure "reconnecting…".
        setPhase(if (attempts == 1) Phase.CONNECTING else Phase.RECONNECTING)
        Log.d(TAG, "connecting gen=$myGen")

        // No iceServers: host candidates over the VPN are all we need.
        val rtcConfig = PeerConnection.RTCConfiguration(emptyList()).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_CONTINUALLY
            // GStreamer's webrtcbin offers the datachannel as a bundle-only
            // m=application section, and that section carries no a=rtcp-mux (SCTP
            // has no RTCP). libwebrtc with RtcpMuxPolicy.REQUIRE applies the mux
            // check to EVERY bundled section, so applying our own answer fails with
            // "Failed to setup RTCP mux" on mid=application1. Browsers don't do this
            // check, which is why the web client works against the same offer.
            // NEGOTIATE relaxes it; MAXBUNDLE requires REQUIRE, so drop to BALANCED.
            bundlePolicy = PeerConnection.BundlePolicy.BALANCED
            rtcpMuxPolicy = PeerConnection.RtcpMuxPolicy.NEGOTIATE
        }

        val peer = factory.createPeerConnection(rtcConfig, PcObserver(myGen))
        if (peer == null) {
            Log.e(TAG, "createPeerConnection returned null")
            scheduleReconnect(myGen)
            return
        }
        pc = peer

        openWebSocket(myGen)
    }

    private fun openWebSocket(myGen: Int) {
        val url = "ws://$host:$port/ws"
        val request = Request.Builder().url(url).build()
        ws = httpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                main.post {
                    if (myGen != gen) return@post
                    Log.d(TAG, "ws open, waiting for offer")
                    pendingConfig?.let { (b, f, a) -> sendConfig(b, f, a) }
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                main.post { if (myGen == gen) onSignal(text, myGen) }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                main.post { if (myGen == gen) { Log.d(TAG, "ws closed"); scheduleReconnect(myGen) } }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                main.post { if (myGen == gen) { Log.d(TAG, "ws failure: ${t.message}"); scheduleReconnect(myGen) } }
            }
        })
    }

    private fun onSignal(raw: String, myGen: Int) {
        if (myGen != gen) return
        val msg = try { JSONObject(raw) } catch (e: Exception) { return }
        when (msg.optString("type")) {
            "offer" -> handleOffer(msg.optString("sdp"), myGen)
            "ice" -> handleRemoteIce(msg, myGen)
            "answer" -> { /* we are never the offerer */ }
            "config" -> {
                // Server's effective settings — it clamps, so this is the truth.
                Log.d(TAG, "server config: ${msg.optInt("bitrate")}kbps " +
                    "${msg.optInt("fps")}fps abr=${msg.optBoolean("abr")}")
            }
            else -> {}
        }
    }

    private fun handleOffer(sdp: String, myGen: Int) {
        val peer = pc ?: return
        val offer = SessionDescription(SessionDescription.Type.OFFER, sdp)
        peer.setRemoteDescription(object : SdpObserver {
            override fun onSetSuccess() {
                main.post {
                    if (myGen != gen) return@post
                    peer.createAnswer(object : SdpObserver {
                        override fun onCreateSuccess(answer: SessionDescription) {
                            main.post {
                                if (myGen != gen) return@post
                                peer.setLocalDescription(object : SdpObserver {
                                    override fun onSetSuccess() {
                                        main.post {
                                            if (myGen != gen) return@post
                                            val local = peer.localDescription ?: return@post
                                            send(JSONObject().put("type", "answer").put("sdp", local.description))
                                            Log.d(TAG, "answer sent")
                                        }
                                    }
                                    override fun onSetFailure(error: String?) = failNegotiation(myGen, "setLocal: $error")
                                    override fun onCreateSuccess(p0: SessionDescription?) {}
                                    override fun onCreateFailure(p0: String?) {}
                                }, answer)
                            }
                        }
                        override fun onCreateFailure(error: String?) = failNegotiation(myGen, "createAnswer: $error")
                        override fun onSetSuccess() {}
                        override fun onSetFailure(p0: String?) {}
                    }, MediaConstraints())
                }
            }
            override fun onSetFailure(error: String?) = failNegotiation(myGen, "setRemote: $error")
            override fun onCreateSuccess(p0: SessionDescription?) {}
            override fun onCreateFailure(p0: String?) {}
        }, offer)
    }

    private fun failNegotiation(myGen: Int, why: String) {
        main.post {
            if (myGen != gen) return@post
            Log.e(TAG, "negotiation failed: $why")
            scheduleReconnect(myGen)
        }
    }

    private fun handleRemoteIce(msg: JSONObject, myGen: Int) {
        val candidate = msg.optString("candidate")
        if (candidate.isNullOrEmpty()) return          // tolerate end-of-candidates
        val mlineIndex = msg.optInt("sdpMLineIndex", 0)
        // The frozen protocol carries no sdpMid, and libwebrtc resolves by mline
        // index — but the sdpMid MUST NOT be null: it goes straight into JNI as a
        // String, and a null there aborts the whole process (SIGABRT inside
        // nativeAddIceCandidate). Empty string is the correct "no mid".
        val ice = IceCandidate("", mlineIndex, candidate)
        // Candidates can arrive before setRemoteDescription resolves; libwebrtc queues them.
        try {
            pc?.addIceCandidate(ice)
        } catch (e: Exception) {
            Log.w(TAG, "addIceCandidate failed: ${e.message}")
        }
    }

    private fun send(obj: JSONObject) {
        ws?.send(obj.toString())
    }

    // ---------------------------------------------------------------------
    // PeerConnection observer
    // ---------------------------------------------------------------------

    private inner class PcObserver(private val myGen: Int) : PeerConnection.Observer {
        override fun onIceCandidate(candidate: IceCandidate) {
            // null candidate string = end-of-candidates; protocol has no frame for it.
            if (candidate.sdp.isNullOrEmpty()) return
            main.post {
                if (myGen != gen) return@post
                send(
                    JSONObject()
                        .put("type", "ice")
                        .put("candidate", candidate.sdp)
                        .put("sdpMLineIndex", candidate.sdpMLineIndex)
                )
            }
        }

        override fun onDataChannel(channel: DataChannel) {
            main.post {
                if (myGen != gen) return@post
                if (channel.label() != "input") return@post
                dc = channel
                channel.registerObserver(InputChannelObserver(myGen, channel))
            }
        }

        override fun onAddTrack(receiver: RtpReceiver, streams: Array<out MediaStream>?) {
            attachTrack(receiver.track(), myGen)
        }

        override fun onTrack(transceiver: RtpTransceiver) {
            attachTrack(transceiver.receiver?.track(), myGen)
        }

        override fun onConnectionChange(newState: PeerConnection.PeerConnectionState) {
            main.post {
                if (myGen != gen) return@post
                Log.d(TAG, "pc $newState")
                when (newState) {
                    PeerConnection.PeerConnectionState.CONNECTED -> {
                        pcConnected = true
                        markLiveIfReady()
                    }
                    PeerConnection.PeerConnectionState.FAILED,
                    PeerConnection.PeerConnectionState.CLOSED -> scheduleReconnect(myGen)
                    PeerConnection.PeerConnectionState.DISCONNECTED -> {
                        // 'disconnected' can self-heal; give ICE a moment before we nuke it.
                        main.postDelayed({
                            if (myGen == gen && pc?.connectionState() == PeerConnection.PeerConnectionState.DISCONNECTED) {
                                scheduleReconnect(myGen)
                            }
                        }, 2000)
                    }
                    else -> {}
                }
            }
        }

        override fun onIceConnectionChange(newState: PeerConnection.IceConnectionState) {
            main.post {
                if (myGen != gen) return@post
                if (newState == PeerConnection.IceConnectionState.CONNECTED ||
                    newState == PeerConnection.IceConnectionState.COMPLETED
                ) {
                    pcConnected = true
                    markLiveIfReady()
                }
            }
        }

        // Unused observer surface (kept explicit; libwebrtc calls these off-thread).
        override fun onSignalingChange(newState: PeerConnection.SignalingState) {}
        override fun onIceConnectionReceivingChange(receiving: Boolean) {}
        override fun onIceGatheringChange(newState: PeerConnection.IceGatheringState) {}
        override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>?) {}
        override fun onAddStream(stream: MediaStream) {}
        override fun onRemoveStream(stream: MediaStream) {}
        override fun onRenegotiationNeeded() {}
        override fun onRemoveTrack(receiver: RtpReceiver) {}
        override fun onSelectedCandidatePairChanged(event: CandidatePairChangeEvent?) {}
        override fun onStandardizedIceConnectionChange(newState: PeerConnection.IceConnectionState?) {}
    }

    private fun attachTrack(track: org.webrtc.MediaStreamTrack?, myGen: Int) {
        if (track !is VideoTrack) return
        main.post {
            if (myGen != gen) return@post
            if (remoteVideo === track) return@post
            remoteVideo?.let { try { it.removeSink(renderer) } catch (_: Exception) {} }
            remoteVideo = track
            try {
                track.setEnabled(true)
                track.addSink(renderer)
            } catch (e: Exception) {
                Log.w(TAG, "addSink failed: ${e.message}")
            }
            haveVideo = true
            markLiveIfReady()
        }
    }

    // ---------------------------------------------------------------------
    // Input datachannel
    // ---------------------------------------------------------------------

    private inner class InputChannelObserver(
        private val myGen: Int,
        private val channel: DataChannel
    ) : DataChannel.Observer {
        override fun onStateChange() {
            main.post {
                if (myGen != gen) return@post
                when (channel.state()) {
                    DataChannel.State.OPEN -> {
                        Log.d(TAG, "input channel open")
                        lastPongAt = System.currentTimeMillis()
                        startPing(myGen)
                        markLiveIfReady()
                    }
                    DataChannel.State.CLOSED -> {
                        Log.d(TAG, "input channel closed")
                        scheduleReconnect(myGen)
                    }
                    else -> {}
                }
            }
        }

        override fun onMessage(buffer: DataChannel.Buffer) {
            // Copy out before leaving the callback thread: the native buffer is reused.
            val data = buffer.data
            val bytes = ByteArray(data.remaining())
            data.get(bytes)
            val text = String(bytes, StandardCharsets.UTF_8)
            main.post {
                if (myGen != gen) return@post
                onInputMessage(text)
            }
        }

        override fun onBufferedAmountChange(previousAmount: Long) {}
    }

    private fun onInputMessage(text: String) {
        val msg = try { JSONObject(text) } catch (e: Exception) { return }
        if (msg.optString("t") == "pong") {
            lastPongAt = System.currentTimeMillis()
            if (msg.has("ts")) {
                val ts = msg.optLong("ts")
                rtt = maxOf(0L, System.currentTimeMillis() - ts).toInt()
                if (live) listener.onRtt(rtt)
            }
        }
    }

    // ---------------------------------------------------------------------
    // Touch -> datachannel (called from the UI thread; DataChannel.send is safe)
    // ---------------------------------------------------------------------

    fun sendTouchDown(id: Int, x: Float, y: Float) =
        sendInput(JSONObject().put("t", "td").put("id", id).put("x", x.toDouble()).put("y", y.toDouble()))

    fun sendTouchMove(id: Int, x: Float, y: Float) =
        sendInput(JSONObject().put("t", "tm").put("id", id).put("x", x.toDouble()).put("y", y.toDouble()))

    fun sendTouchUp(id: Int) =
        sendInput(JSONObject().put("t", "tu").put("id", id))

    private fun sendInput(obj: JSONObject) {
        val d = dc ?: return
        if (d.state() != DataChannel.State.OPEN) return
        try {
            val bytes = obj.toString().toByteArray(StandardCharsets.UTF_8)
            d.send(DataChannel.Buffer(ByteBuffer.wrap(bytes), false))  // false = text frame
        } catch (e: Exception) {
            // A send on a channel that died between check and here is not worth a log
            // line; the close handler reconnects.
        }
    }

    // ---------------------------------------------------------------------
    // Heartbeat + liveness
    // ---------------------------------------------------------------------

    private fun startPing(myGen: Int) {
        main.removeCallbacks(pingRunnable)
        pingTick(myGen)                  // seed the RTT readout immediately
        main.postDelayed(pingRunnable, PING_INTERVAL_MS)
    }

    private fun pingTick(myGen: Int) {
        if (myGen != gen) return
        val d = dc
        if (d == null || d.state() != DataChannel.State.OPEN) return
        // A dead link that never closes cleanly shows up as pongs going missing.
        if (live && System.currentTimeMillis() - lastPongAt > PONG_TIMEOUT_MS) {
            Log.d(TAG, "pong timeout")
            scheduleReconnect(myGen)
            return
        }
        try {
            val json = JSONObject().put("t", "ping").put("ts", System.currentTimeMillis()).toString()
            d.send(DataChannel.Buffer(ByteBuffer.wrap(json.toByteArray(StandardCharsets.UTF_8)), false))
        } catch (e: Exception) {
            scheduleReconnect(myGen)
            return
        }
        main.postDelayed(pingRunnable, PING_INTERVAL_MS)
    }

    private fun markLiveIfReady() {
        if (live) return
        if (haveVideo && dc?.state() == DataChannel.State.OPEN && pcConnected) {
            live = true
            retryDelayMs = RECONNECT_MIN_MS   // a good connection resets the backoff
            setPhase(Phase.LIVE)
            listener.onRtt(rtt)
            Log.d(TAG, "live")
        }
    }

    // ---------------------------------------------------------------------
    // Teardown + reconnect
    // ---------------------------------------------------------------------

    private fun teardown() {
        main.removeCallbacks(pingRunnable)
        live = false
        rtt = null
        haveVideo = false
        pcConnected = false

        remoteVideo?.let { try { it.removeSink(renderer) } catch (_: Exception) {} }
        remoteVideo = null

        dc?.let {
            try { it.unregisterObserver() } catch (_: Exception) {}
            try { it.close() } catch (_: Exception) {}
            try { it.dispose() } catch (_: Exception) {}
        }
        dc = null

        ws?.let { try { it.close(1000, null) } catch (_: Exception) {} }
        ws = null

        pc?.let {
            try { it.dispose() } catch (_: Exception) {}
        }
        pc = null
        // The renderer keeps its last frame rather than flashing to void mid-reconnect.
    }

    private val retryDelayed = Runnable {
        retryScheduled = false
        connect()
    }

    private fun scheduleReconnect(myGen: Int) {
        if (myGen != gen) return
        if (stopped) return
        if (retryScheduled) return       // one retry in flight is enough

        teardown()
        gen++                            // invalidate every callback from the dead session

        // Auto-reconnect off: park in DISCONNECTED and wait for a manual Reconnect
        // (which calls reconnectNow()). No backoff loop.
        if (!autoReconnect()) {
            retryDelayMs = RECONNECT_MIN_MS
            setPhase(Phase.DISCONNECTED)
            return
        }

        setPhase(Phase.RECONNECTING)

        val delay = retryDelayMs
        retryDelayMs = minOf(retryDelayMs * 2, RECONNECT_MAX_MS)
        Log.d(TAG, "reconnect in $delay ms")

        retryScheduled = true
        main.postDelayed(retryDelayed, delay)
    }

    private fun setPhase(phase: Phase) {
        if (phase != lastPhase) {
            lastPhase = phase
        }
        listener.onPhase(phase)
    }

    companion object {
        private const val TAG = "nx"
        private const val RECONNECT_MIN_MS = 500L
        private const val RECONNECT_MAX_MS = 5000L
        private const val PING_INTERVAL_MS = 2000L
        private const val PONG_TIMEOUT_MS = 8000L
    }
}
