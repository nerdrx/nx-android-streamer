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

        // nx-bridge: two seams for the picker/camera bridges, so neither has to
        // live inside this class.
        /** A signaling frame this class does not own (pick, camera, …). */
        fun onServerMessage(msg: JSONObject) {}
        /** The offer is applied and the answer has NOT been created yet — the
         *  only window in which a transceiver's direction can still be chosen. */
        fun onRemoteOfferApplied(pc: PeerConnection, sdp: String) {}
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
    // Last battery reading we mirrored, re-sent on every (re)connect so a fresh
    // session starts matched instead of waiting for the next percent to tick.
    // Null while mirroring is off.
    private var pendingBattery: Pair<Int, Boolean>? = null
    private var connectStartedAt = 0L
    private var pendingCameraWant: Boolean? = null


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
        // Startup fires three of these in a row (onCreate autoconnect, onResume,
        // and the network-available callback). Each one opened a NEW websocket,
        // and the server keeps a single client — so every attempt evicted the
        // one before it and the survivor was torn down mid-negotiation. Skip a
        // redundant reconnect while a young attempt is still in flight.
        val inFlight = System.currentTimeMillis() - connectStartedAt
        // The grace has to cover RECONNECTING as well: phase is only CONNECTING on
        // the very first attempt, so after any failure a wake-up would preempt the
        // in-flight attempt again — pong watchdog, then onResume, then the network
        // callback, each opening a socket that evicts the last one server-side.
        // A machine parked in backoff (retryScheduled) SHOULD be preempted now.
        val attemptInFlight = !retryScheduled &&
            (lastPhase == Phase.CONNECTING || lastPhase == Phase.RECONNECTING)
        if (isHealthy() || (attemptInFlight && inFlight < ATTEMPT_GRACE_MS)) {
            Log.d(TAG, "reconnectNow ignored (attempt in flight)")
            return
        }
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
        sendSignal(
            JSONObject()
                .put("type", "config")
                .put("bitrate", bitrateKbps)
                .put("fps", fps)
                .put("abr", abr),
            "config"
        )
    }

    /**
     * Mirror this phone's battery onto the remote Android, so the streamed
     * device reads as the one in your hand. Same rules as [sendConfig]: safe
     * before the socket exists, and re-sent on every reconnect.
     */
    /**
     * Ask the host to add (or drop) the camera m-section. It is NOT in the
     * first offer on purpose: android's libwebrtc fails the whole answer on an
     * offered recvonly video section, so the host only adds one — via a
     * re-offer — once the user has actually allowed the camera.
     */
    fun sendCameraWant(want: Boolean) {
        pendingCameraWant = want
        sendSignal(JSONObject().put("type", "camera").put("want", want), "camera")
    }

    fun sendBattery(level: Int, charging: Boolean) {
        val state = Pair(level.coerceIn(0, 100), charging)
        pendingBattery = state
        sendSignal(
            JSONObject()
                .put("type", "battery")
                .put("level", state.first)
                .put("charging", state.second),
            "battery"
        )
    }

    /**
     * Mirroring was switched off: tell the server to hand the container's
     * battery back to its own driver. The server's override outlives our
     * process, so this has to be said out loud rather than just stopping.
     */
    fun sendBatteryOff() {
        pendingBattery = null
        sendSignal(JSONObject().put("type", "battery").put("enabled", false), "battery")
    }

    /** Best-effort signaling send: no socket yet is normal, not an error. */
    private fun sendSignal(msg: JSONObject, what: String) {
        val sock = ws ?: return
        try {
            sock.send(msg.toString())
        } catch (e: Exception) {
            Log.w(TAG, "$what send failed: ${e.message}")
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
        connectStartedAt = System.currentTimeMillis()

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
                    pendingCameraWant?.let { sendCameraWant(it) }
                    pendingBattery?.let { (l, c) -> sendBattery(l, c) }
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
            // nx-bridge: pick / camera and anything else the daemon adds later.
            else -> listener.onServerMessage(msg)
        }
    }

    private fun handleOffer(sdp: String, myGen: Int) {
        val peer = pc ?: return
        val offer = SessionDescription(SessionDescription.Type.OFFER, sdp)
        peer.setRemoteDescription(object : SdpObserver {
            override fun onSetSuccess() {
                main.post {
                    if (myGen != gen) return@post
                    // nx-bridge: last chance to set a transceiver's direction —
                    // createAnswer() below freezes it.
                    listener.onRemoteOfferApplied(peer, sdp)
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

    // nx-bridge: the picker and camera bridges own their own message types but
    // share this socket. Both call these from the main looper, like everything
    // else in this class.
    /** Put one bridge frame on the signaling socket. No-op while it is down. */
    fun sendBridgeFrame(obj: JSONObject) = sendSignal(obj, obj.optString("type"))

    /** Bytes OkHttp still has queued for this socket. The picker paces a
     *  multi-megabyte upload on it so the transfer cannot bury SDP/ICE frames
     *  (or the phone's heap) while a reconnect is trying to happen. */
    fun signalBacklogBytes(): Long = try {
        ws?.queueSize() ?: 0L
    } catch (e: Exception) {
        0L
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
                // The server creates this channel BEFORE negotiation, so it is
                // frequently already OPEN by the time we get here — and an
                // observer registered on an already-open channel never receives
                // onStateChange. Missing that edge meant ping/pong never started
                // (no RTT, no health signal) and the session never flipped to
                // LIVE, so the pill sat on "connecting…" over working video.
                if (channel.state() == DataChannel.State.OPEN) {
                    Log.d(TAG, "input channel already open")
                    lastPongAt = System.currentTimeMillis()
                    startPing(myGen)
                    markLiveIfReady()
                }
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

    fun sendTouchDown(id: Int, x: Float, y: Float, eventTimeMs: Long = 0L) =
        sendInput(JSONObject().put("t", "td").put("id", id).put("x", x.toDouble()).put("y", y.toDouble()),
                  eventTimeMs)

    fun sendTouchMove(id: Int, x: Float, y: Float, eventTimeMs: Long = 0L) =
        sendInput(JSONObject().put("t", "tm").put("id", id).put("x", x.toDouble()).put("y", y.toDouble()),
                  eventTimeMs)

    fun sendTouchUp(id: Int) =
        sendInput(JSONObject().put("t", "tu").put("id", id))

    /**
     * Set NXAS_TRACE_INPUT to log how long each touch spends between the
     * MotionEvent and the wire, and how much is queued in the datachannel.
     * A growing bufferedAmount means SCTP is pacing us, which shows up as
     * touch lag with perfectly healthy video.
     */
    private fun traceInput(eventTimeMs: Long, d: DataChannel) {
        val age = android.os.SystemClock.uptimeMillis() - eventTimeMs
        val queued = try { d.bufferedAmount() } catch (e: Exception) { -1L }
        if (!Log.isLoggable(TAG, Log.VERBOSE)) return
        Log.v(TAG, "input trace: event->send ${age}ms, dc queued ${queued}B")
    }

    private fun sendInput(obj: JSONObject, eventTimeMs: Long = 0L) {
        val d = dc ?: return
        if (d.state() != DataChannel.State.OPEN) return
        try {
            if (eventTimeMs > 0L) traceInput(eventTimeMs, d)
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
        /** How long one connection attempt is given before another may preempt it. */
        private const val ATTEMPT_GRACE_MS = 8000L
        private const val TAG = "nx"
        private const val RECONNECT_MIN_MS = 500L
        private const val RECONNECT_MAX_MS = 5000L
        private const val PING_INTERVAL_MS = 2000L
        private const val PONG_TIMEOUT_MS = 8000L
    }
}
