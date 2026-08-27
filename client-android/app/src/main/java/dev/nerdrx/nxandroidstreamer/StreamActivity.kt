package dev.nerdrx.nxandroidstreamer

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.drawable.GradientDrawable
import android.media.AudioAttributes
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import org.json.JSONObject
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.audio.JavaAudioDeviceModule

/**
 * The single activity. Portrait-locked, sticky-immersive, the video is the app.
 *
 * The lifecycle here is the whole point of going native — the PWA could not do it.
 * We connect on resume; on any drop the StreamClient retries with backoff; and we
 * survive the screen turning off and back on and a Wi-Fi<->cellular handoff WITHOUT
 * a manual reload. That is the WiVRn Pico-standby lesson: a device that sleeps and
 * wakes must re-establish the pipe by itself, silently, or it isn't a product.
 * The server re-offers on every new WebSocket, so we always answer, never offer.
 */
class StreamActivity : AppCompatActivity(), StreamClient.Listener, StreamView.Callbacks, SettingsView.Host {

    private lateinit var prefsImpl: Prefs
    private lateinit var eglBase: EglBase
    private lateinit var factory: PeerConnectionFactory
    private lateinit var audioModule: JavaAudioDeviceModule
    private lateinit var battery: BatteryMonitor

    private lateinit var root: FrameLayout
    private lateinit var streamView: StreamView
    private lateinit var pill: TextView
    private lateinit var edgeHandle: TextView

    private var settingsView: SettingsView? = null
    private var client: StreamClient? = null

    // nx-bridge: the two reverse bridges. Both own ActivityResultLaunchers, so
    // both are built in onCreate() and never later — registration after the
    // activity is STARTED throws.
    private lateinit var pickerBridge: PickerBridge
    private lateinit var cameraBridge: CameraBridge

    private lateinit var discovery: Discovery
    private var discovered: List<Discovery.Server> = emptyList()
    private var discovering = false

    private var connectedProfile: ServerProfile? = null
    private var currentRtt: Int? = null
    private var currentRes: String? = null
    private var lastPhase: StreamClient.Phase? = null

    private var wakeLock: PowerManager.WakeLock? = null
    private val ui = Handler(Looper.getMainLooper())
    private var insetTop = 0

    // ---- QR + camera permission plumbing --------------------------------

    private val scanLauncher = registerForActivityResult(ScanContract()) { result ->
        val contents = result?.contents ?: return@registerForActivityResult
        val parsed = ServerProfile.parseUri(contents)
        if (parsed == null) {
            toast("Not an NX pairing code")
            return@registerForActivityResult
        }
        val (host, port) = parsed
        val existing = prefsImpl.findByHostPort(host, port)
        val profile = existing ?: prefsImpl.upsertServer(
            ServerProfile(ServerProfile.newId(), host, host, port)
        )
        onConnect(profile)
    }

    private val cameraPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) launchScanner()
            else {
                // Graceful fallback to manual entry when camera is denied.
                toast("Camera denied — add the server manually")
                openHome()
            }
        }

    // ---------------------------------------------------------------------

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefsImpl = Prefs(this)
        discovery = Discovery(this)

        // WebRTC init. DefaultVideoDecoderFactory prefers MediaCodec H.264 hardware
        // decode (the whole point on Tensor G2) but falls back to software, so the
        // client also works on devices/emulators without an H.264 decoder.
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(applicationContext)
                .createInitializationOptions()
        )
        eglBase = EglBase.create()
        // Audio needs no code of ours to play: libwebrtc decodes an incoming
        // Opus track and hands it to the device module by itself. What it does
        // need is the right output — the default module opens the track as
        // VOICE_COMMUNICATION, which is the earpiece-and-echo-canceller path
        // meant for calls, not for a phone's whole sound output. USAGE_MEDIA
        // puts the remote Android on the media stream, where the volume keys
        // and the loudspeaker already are. Inert while the server runs
        // --audio none: no audio track is negotiated, so nothing opens.
        audioModule = JavaAudioDeviceModule.builder(applicationContext)
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MOVIE)
                    .build()
            )
            // We never send audio upstream, so nothing here should reach for
            // the mic (and nothing should want RECORD_AUDIO).
            .setUseHardwareAcousticEchoCanceler(false)
            .setUseHardwareNoiseSuppressor(false)
            .createAudioDeviceModule()
        factory = PeerConnectionFactory.builder()
            .setAudioDeviceModule(audioModule)
            .setVideoDecoderFactory(org.webrtc.NxLowLatencyDecoderFactory(eglBase.eglBaseContext))
            .setVideoEncoderFactory(DefaultVideoEncoderFactory(eglBase.eglBaseContext, true, true))
            .setOptions(PeerConnectionFactory.Options())
            .createPeerConnectionFactory()

        // No permission needed, and nothing is registered until a stream is up.
        battery = BatteryMonitor(applicationContext) { level, charging ->
            client?.sendBattery(level, charging)
        }

        // nx-bridge: on-demand picker + the phone-camera upstream. Neither does
        // anything until the server asks; the camera additionally needs the
        // opt-in in Settings, which is off by default.
        pickerBridge = PickerBridge(this) { client }
        cameraBridge = CameraBridge(this, factory, eglBase, prefsImpl, { client }) {
            settingsView?.refresh()          // the status line under the toggle
        }

        buildUi()
        enterImmersive()
        registerNetworkCallback()

        // Autoconnect: after first pairing, launch straight into the stream with the
        // pill showing "connecting…". Only fall back to the home screen when there is
        // no saved server (or the user backs out).
        val last = prefsImpl.lastUsedServer()
        if (prefsImpl.autoConnect && last != null) {
            startStream(last)
        } else {
            openHome()
        }
    }

    private fun buildUi() {
        root = FrameLayout(this).apply { setBackgroundColor(NxColor.VOID) }

        streamView = StreamView(this)
        streamView.init(eglBase.eglBaseContext)
        streamView.setCallbacks(this)
        streamView.setScaling(prefsImpl.scaling)
        root.addView(streamView, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))

        pill = TextView(this).apply {
            text = "connecting…"
            setTextColor(NxColor.TEXT)
            textSize = 13f
            gravity = Gravity.CENTER
            background = pillBg()
            setPadding(Ui.dp(context, 14), Ui.dp(context, 7), Ui.dp(context, 14), Ui.dp(context, 7))
            visibility = View.GONE
        }
        val pillLp = FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            topMargin = Ui.dp(this@StreamActivity, 12)
        }
        root.addView(pill, pillLp)

        // Tiny edge handle (top-left) to reopen settings mid-stream, in addition to
        // the long-press affordance in StreamView. Out of the way of stream gestures.
        edgeHandle = TextView(this).apply {
            text = "≡"
            setTextColor(NxColor.TEXT)
            textSize = 16f
            gravity = Gravity.CENTER
            background = handleBg()
            setPadding(Ui.dp(context, 12), Ui.dp(context, 6), Ui.dp(context, 12), Ui.dp(context, 6))
            alpha = 0.5f
            visibility = View.GONE
            setOnClickListener { openOverlay() }
        }
        val handleLp = FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT).apply {
            gravity = Gravity.TOP or Gravity.START
            topMargin = Ui.dp(this@StreamActivity, 10)
            leftMargin = Ui.dp(this@StreamActivity, 10)
        }
        root.addView(edgeHandle, handleLp)

        setContentView(root)

        // Safe-area: keep the pill + handle clear of the cutout / status region.
        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            insetTop = bars.top
            pillLp.topMargin = insetTop + Ui.dp(this, 8)
            pill.layoutParams = pillLp
            handleLp.topMargin = insetTop + Ui.dp(this, 6)
            edgeHandle.layoutParams = handleLp
            insets
        }
    }

    private fun pillBg(): GradientDrawable = GradientDrawable().apply {
        cornerRadius = Ui.dp(this@StreamActivity, 999).toFloat()
        setColor(0x8C141222.toInt())
        setStroke(2, 0x14FFFFFF)
    }

    private fun handleBg(): GradientDrawable = GradientDrawable().apply {
        cornerRadius = Ui.dp(this@StreamActivity, 14).toFloat()
        setColor(0x66141222)
        setStroke(2, NxColor.LINE)
    }

    // ---------------------------------------------------------------------
    // Home / overlay
    // ---------------------------------------------------------------------

    private fun openHome() {
        // Cold home: no active stream. Discovery runs while this screen is up.
        connectedProfile = null
        pill.visibility = View.GONE
        edgeHandle.visibility = View.GONE
        showSettings()
        startDiscovery()
    }

    private fun openOverlay() {
        // Warm overlay: floated over a running stream.
        showSettings()
        startDiscovery()
    }

    private fun showSettings() {
        if (settingsView == null) {
            val sv = SettingsView(this, this)
            settingsView = sv
            root.addView(sv, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        } else {
            settingsView?.refresh()
        }
        settingsView?.visibility = View.VISIBLE
        settingsView?.bringToFront()
    }

    private fun closeSettings() {
        settingsView?.let { root.removeView(it) }
        settingsView = null
        stopDiscovery()
        pill.bringToFront()
        edgeHandle.bringToFront()
    }

    // ---------------------------------------------------------------------
    // Stream lifecycle
    // ---------------------------------------------------------------------

    private fun startStream(profile: ServerProfile) {
        closeSettings()
        stopDiscovery()
        connectedProfile = profile
        prefsImpl.lastUsedId = profile.id
        currentRtt = null
        currentRes = null
        lastPhase = null

        streamView.setScaling(prefsImpl.scaling)
        pill.visibility = if (prefsImpl.pillMode == PillMode.OFF) View.GONE else View.VISIBLE
        pill.alpha = 1f
        setPillText("connecting…")
        edgeHandle.visibility = View.VISIBLE
        edgeHandle.bringToFront()

        applyKeepAwake()
        acquireWake()

        client?.stop()
        client = StreamClient(
            factory = factory,
            renderer = streamView.renderer,
            host = profile.host,
            port = profile.port,
            autoReconnect = { prefsImpl.autoReconnect },
            listener = this
        ).also {
            // Seed the user's quality choices before connecting; the client
            // re-sends them whenever the socket (re)opens.
            it.sendConfig(prefsImpl.bitrateKbps, prefsImpl.fps, prefsImpl.adaptiveBitrate)
            it.start()
        }
        // After the client exists, so the first reading has somewhere to go —
        // it rides along on the websocket's open, like the quality config.
        applyBatteryMirror()
    }

    private fun stopStream() {
        // nx-bridge: let the container stop waiting on a pick, and put the
        // camera down before the socket goes.
        pickerBridge.abort()
        cameraBridge.onSessionEnded()
        battery.stop()
        client?.sendBatteryOff()
        client?.stop()
        client = null
        connectedProfile = null
        currentRtt = null
        currentRes = null
        lastPhase = null
        pill.visibility = View.GONE
        edgeHandle.visibility = View.GONE
        clearKeepAwake()
        releaseWake()
    }

    // ---- StreamClient.Listener -----------------------------------------

    override fun onPhase(phase: StreamClient.Phase) {
        val was = lastPhase
        lastPhase = phase
        when (phase) {
            StreamClient.Phase.CONNECTING -> setWaitingPill("connecting…")
            // nx-bridge: the socket is gone, so an in-flight upload can never
            // finish and the camera should not stay held over a reconnect the
            // user may never complete. Both calls are no-ops when idle.
            StreamClient.Phase.RECONNECTING -> {
                setWaitingPill("reconnecting…")
                pickerBridge.abort()
                cameraBridge.onSessionEnded()
            }
            StreamClient.Phase.DISCONNECTED -> {
                setWaitingPill("disconnected")
                pickerBridge.abort()
                cameraBridge.onSessionEnded()
            }
            StreamClient.Phase.LIVE -> {
                if (was != StreamClient.Phase.LIVE) {
                    if (prefsImpl.hapticOnConnect) haptic()
                    revealPillThenMaybeFade()
                }
                setPillText(currentRtt?.let { "$it ms" } ?: "live")
            }
        }
        // Keep the overlay's Reconnect/Disconnect + diagnostics in step with phase.
        settingsView?.refresh()
    }

    override fun onRtt(rttMs: Int?) {
        currentRtt = rttMs
        if (lastPhase == StreamClient.Phase.LIVE && prefsImpl.pillMode != PillMode.OFF) {
            setPillText(rttMs?.let { "$it ms" } ?: "live")
        }
        settingsView?.updateDiagnostics()
    }

    // ---- nx-bridge seams -----------------------------------------------

    /** Signaling frames StreamClient does not own: route them to their bridge. */
    override fun onServerMessage(msg: JSONObject) {
        when (msg.optString("type")) {
            "pick" -> pickerBridge.onPickRequest(msg)
            "camera" -> cameraBridge.onCameraMessage(msg)
            "pick-error" -> toast("The host could not take the file")
        }
    }

    /** Offer applied, answer not yet built — the camera bridge's one chance to
     *  claim the recvonly m=video the daemon put there for it. */
    override fun onRemoteOfferApplied(pc: PeerConnection, sdp: String) {
        cameraBridge.prepare(pc, sdp)
    }

    // ---- StreamView.Callbacks ------------------------------------------

    override fun onTouchDown(id: Int, x: Float, y: Float) { client?.sendTouchDown(id, x, y) }
    override fun onTouchMove(id: Int, x: Float, y: Float) { client?.sendTouchMove(id, x, y) }
    override fun onTouchUp(id: Int) { client?.sendTouchUp(id) }
    override fun onResolution(width: Int, height: Int) {
        currentRes = "${width}×${height}"
        settingsView?.updateDiagnostics()
    }
    override fun onOpenSettings() { openOverlay() }

    // ---- SettingsView.Host ---------------------------------------------

    override val prefs: Prefs get() = prefsImpl
    override val streaming: Boolean get() = client != null
    override val disconnected: Boolean get() = lastPhase == StreamClient.Phase.DISCONNECTED
    override fun discoveredServers(): List<Discovery.Server> = discovered

    override fun onConnect(profile: ServerProfile) {
        prefsImpl.upsertServer(profile)
        startStream(profile)
    }

    override fun onConnectDiscovered(s: Discovery.Server) {
        val existing = prefsImpl.findByHostPort(s.host, s.port)
        val profile = existing ?: prefsImpl.upsertServer(
            ServerProfile(ServerProfile.newId(), s.name, s.host, s.port)
        )
        startStream(profile)
    }

    override fun onReconnect() { client?.reconnectNow() }
    override fun onDisconnect() { stopStream(); openHome() }
    override fun onCloseOverlay() { closeSettings() }
    override fun onScanQr() { requestScan() }
    override fun currentRttMs(): Int? = currentRtt
    override fun currentResolution(): String? = currentRes

    override fun onQualityChanged() {
        // Apply live to the running session; StreamClient re-sends on reconnect.
        client?.sendConfig(prefsImpl.bitrateKbps, prefsImpl.fps, prefsImpl.adaptiveBitrate)
    }

    // nx-bridge: the remote-camera opt-in. The server is told whether it *may*
    // ask; it answers with whether it is asking, and only then do we capture.
    override fun onCameraAllowChanged(allow: Boolean) = cameraBridge.onAllowChanged(allow)
    override fun cameraStatus(): String? = cameraBridge.statusText()

    override fun onBatteryMirrorChanged() = applyBatteryMirror()

    /**
     * Match the monitor to the setting and the session. Switching mirroring off
     * has to be said out loud: the server's `dumpsys battery` override outlives
     * both the socket and this process, so "stop sending" is not enough.
     */
    private fun applyBatteryMirror() {
        if (client != null && prefsImpl.mirrorBattery) {
            battery.start()
        } else {
            battery.stop()
            client?.sendBatteryOff()
        }
    }

    override fun onSettingsChanged() {
        streamView.setScaling(prefsImpl.scaling)
        applyKeepAwake()
        // Pill visibility follows the mode live.
        if (client != null) {
            when (prefsImpl.pillMode) {
                PillMode.OFF -> pill.visibility = View.GONE
                PillMode.ALWAYS -> { pill.visibility = View.VISIBLE; pill.alpha = 1f; ui.removeCallbacks(fadePill) }
                PillMode.AUTO -> { pill.visibility = View.VISIBLE; revealPillThenMaybeFade() }
            }
        }
    }

    // ---------------------------------------------------------------------
    // Pill
    // ---------------------------------------------------------------------

    private fun setPillText(text: String) {
        if (prefsImpl.pillMode == PillMode.OFF) { pill.visibility = View.GONE; return }
        pill.text = text
    }

    private fun setWaitingPill(text: String) {
        if (prefsImpl.pillMode == PillMode.OFF) { pill.visibility = View.GONE; return }
        ui.removeCallbacks(fadePill)
        pill.visibility = View.VISIBLE
        pill.animate().alpha(1f).setDuration(200).start()
        pill.text = text
    }

    private val fadePill = Runnable {
        if (lastPhase == StreamClient.Phase.LIVE && prefsImpl.pillMode == PillMode.AUTO) {
            pill.animate().alpha(0f).setDuration(500).start()
        }
    }

    private fun revealPillThenMaybeFade() {
        if (prefsImpl.pillMode == PillMode.OFF) { pill.visibility = View.GONE; return }
        ui.removeCallbacks(fadePill)
        pill.visibility = View.VISIBLE
        pill.animate().alpha(1f).setDuration(200).start()
        if (prefsImpl.pillMode == PillMode.AUTO) ui.postDelayed(fadePill, 3000)
    }

    // ---------------------------------------------------------------------
    // Discovery
    // ---------------------------------------------------------------------

    private fun startDiscovery() {
        if (discovering) return
        discovering = true
        discovery.start(object : Discovery.Listener {
            override fun onServersChanged(servers: List<Discovery.Server>) {
                discovered = servers
                settingsView?.refresh()
            }
        })
    }

    private fun stopDiscovery() {
        if (!discovering) return
        discovering = false
        discovery.stop()
        discovered = emptyList()
    }

    // ---------------------------------------------------------------------
    // QR
    // ---------------------------------------------------------------------

    private fun requestScan() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            launchScanner()
        } else {
            cameraPermLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private fun launchScanner() {
        val options = ScanOptions().apply {
            setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            setPrompt("Point at the NX pairing code")
            setBeepEnabled(false)
            setOrientationLocked(true)
        }
        scanLauncher.launch(options)
    }

    // ---------------------------------------------------------------------
    // Immersive + wake + network
    // ---------------------------------------------------------------------

    private fun enterImmersive() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        // Draw into the cutout area too, so the stream is truly edge-to-edge
        // instead of stopping short of the camera notch.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            window.attributes.layoutInDisplayCutoutMode =
                WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
        }
        val controller = WindowInsetsControllerCompat(window, window.decorView)
        controller.hide(WindowInsetsCompat.Type.systemBars())
        controller.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) enterImmersive()      // re-hide the bars after any transient reveal
    }

    private fun applyKeepAwake() {
        if (client != null && prefsImpl.keepAwake) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            clearKeepAwake()
        }
    }

    private fun clearKeepAwake() {
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    @Suppress("WakelockTimeout")
    private fun acquireWake() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        // PARTIAL keeps the CPU (and the WebRTC pipe) alive if the user powers the
        // screen off mid-stream; the stream resumes on wake without a reload.
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "nxstreamer:stream").apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun releaseWake() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    private var netCallback: ConnectivityManager.NetworkCallback? = null

    private fun registerNetworkCallback() {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                // Wi-Fi <-> cellular handoff: the old path dies, a new one appears.
                // Force a reconnect if the stream is up but the pipe went stale.
                ui.post {
                    val c = client
                    if (c != null && !c.isHealthy()) c.reconnectNow()
                }
            }
        }
        netCallback = cb
        try { cm.registerDefaultNetworkCallback(cb) } catch (_: Exception) {}
    }

    private fun haptic() {
        try {
            val vib: Vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                (getSystemService(VIBRATOR_MANAGER_SERVICE) as VibratorManager).defaultVibrator
            } else {
                @Suppress("DEPRECATION")
                getSystemService(VIBRATOR_SERVICE) as Vibrator
            }
            vib.vibrate(VibrationEffect.createOneShot(18, VibrationEffect.DEFAULT_AMPLITUDE))
        } catch (_: Exception) {}
    }

    private fun toast(msg: String) {
        android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_SHORT).show()
    }

    // ---------------------------------------------------------------------
    // Activity lifecycle — the reconnect state machine's outer loop
    // ---------------------------------------------------------------------

    override fun onResume() {
        super.onResume()
        enterImmersive()
        val c = client
        if (c != null) {
            applyKeepAwake()
            acquireWake()
            c.noteAwake()               // don't trip the pong watchdog on sleep time
            if (!c.isHealthy()) c.reconnectNow()   // woke into a dead session: rebuild now
        } else if (settingsView != null) {
            startDiscovery()
        }
    }

    override fun onPause() {
        super.onPause()
        // Never leave fingers down across a background/sleep.
        streamView.liftAllPointers()
        client?.let { for (i in 0..9) it.sendTouchUp(i) }
        stopDiscovery()
        // The client stays alive on purpose: the stream must survive the screen
        // going off and resume on its own (WiVRn Pico-standby lesson).
    }

    override fun onBackPressed() {
        // Back from an overlay resumes the stream; back from the home screen (or a
        // live stream with no overlay) exits.
        if (settingsView != null && client != null) {
            closeSettings()
        } else {
            @Suppress("DEPRECATION")
            super.onBackPressed()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        netCallback?.let {
            try { (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).unregisterNetworkCallback(it) } catch (_: Exception) {}
        }
        battery.stop()
        cameraBridge.release()          // nx-bridge: hand the camera back
        client?.stop()
        client = null
        stopDiscovery()
        releaseWake()
        streamView.release()
        try { factory.dispose() } catch (_: Exception) {}
        try { audioModule.release() } catch (_: Exception) {}
        try { eglBase.release() } catch (_: Exception) {}
    }
}
