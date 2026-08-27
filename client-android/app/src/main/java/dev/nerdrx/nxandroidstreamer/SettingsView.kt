package dev.nerdrx.nxandroidstreamer

import android.annotation.SuppressLint
import android.content.Context
import android.view.Gravity
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * The server list + settings screen. One view, two lives:
 *   - cold (home): the launcher screen. Discovered (mDNS) servers on top, saved
 *     profiles next, then [Scan to pair] / [Add manually], then the settings.
 *   - warm (overlay): the same screen floated over a running stream via the
 *     long-press / edge-handle affordance, with Close + Disconnect and live
 *     diagnostics, so the user can switch servers or flip scaling without leaving.
 *
 * Everything writes straight to Prefs and takes effect with no rebuild; the live
 * surfaces (scaling, pill, keep-awake, reconnect policy) re-read on change.
 */
@SuppressLint("ViewConstructor")
class SettingsView(context: Context, private val host: Host) : ScrollView(context) {

    interface Host {
        val prefs: Prefs
        val streaming: Boolean
        val disconnected: Boolean
        fun discoveredServers(): List<Discovery.Server>
        fun onConnect(profile: ServerProfile)
        fun onConnectDiscovered(s: Discovery.Server)
        fun onReconnect()
        fun onDisconnect()
        fun onCloseOverlay()
        fun onScanQr()
        fun currentRttMs(): Int?
        fun currentResolution(): String?
        fun onSettingsChanged()
        /** Push bitrate/fps/abr to the server for the live session. */
        fun onQualityChanged()

        // nx-bridge: the remote-camera opt-in and whatever the server last said
        // about it.
        fun onCameraAllowChanged(allow: Boolean)
        fun cameraStatus(): String?
        /** The battery-mirror toggle flipped; start or stop it on the live session. */
        fun onBatteryMirrorChanged()
    }

    private val col = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
    private var editing: ServerProfile? = null
    private var mode = Mode.LIST

    private var rttView: TextView? = null
    private var resView: TextView? = null

    private enum class Mode { LIST, EDIT }

    init {
        setBackgroundColor(if (host.streaming) NxColor.SCRIM else NxColor.VOID)
        isFillViewport = true
        val pad = Ui.dp(context, 20)
        col.setPadding(pad, Ui.dp(context, 44), pad, Ui.dp(context, 40))
        addView(col, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.WRAP_CONTENT))
        render()
    }

    fun refresh() = render()

    fun updateDiagnostics() {
        rttView?.text = host.currentRttMs()?.let { "$it ms" } ?: "—"
        resView?.text = host.currentResolution() ?: "—"
    }

    private fun render() {
        col.removeAllViews()
        rttView = null
        resView = null
        when (mode) {
            Mode.LIST -> renderList()
            Mode.EDIT -> renderEdit()
        }
    }

    // ---- top bar / header ----------------------------------------------

    private fun renderList() {
        val ctx = context

        if (host.streaming) {
            // Overlay top bar: Close (resume stream) + Disconnect.
            val bar = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
            bar.addView(Ui.title(ctx, "Settings", 20f), LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            bar.addView(Ui.secondaryButton(ctx, "Close") { host.onCloseOverlay() })
            col.addView(bar)
            col.addSpacer(Ui.dp(ctx, 10))
            if (host.disconnected) {
                col.addView(Ui.primaryButton(ctx, "Reconnect") { host.onReconnect() }, wide())
                col.addSpacer(Ui.dp(ctx, 6))
            }
            col.addView(
                Ui.secondaryButton(ctx, "Disconnect", stroke = NxColor.DANGER, textColor = NxColor.DANGER) { host.onDisconnect() },
                wide()
            )
        } else {
            col.addView(Ui.title(ctx, "NX Android Streamer", 24f))
            col.addSpacer(Ui.dp(ctx, 4))
            col.addView(Ui.body(ctx, "Your PC is the phone, your phone is the glass.", NxColor.DIM, 14f))
        }

        // DISCOVERED (mDNS)
        val discovered = host.discoveredServers()
        if (discovered.isNotEmpty()) {
            col.addView(Ui.sectionHeader(ctx, "On this network"))
            discovered.forEach { s ->
                col.addView(discoveredCard(s), wide())
                col.addSpacer(Ui.dp(ctx, 8))
            }
        }

        // SAVED profiles
        val servers = host.prefs.servers()
        col.addView(Ui.sectionHeader(ctx, "Saved servers"))
        if (servers.isEmpty()) {
            col.addView(Ui.body(ctx, "No saved servers yet. Scan a pairing QR, or add one manually.", NxColor.DIM, 14f))
        } else {
            servers.forEachIndexed { i, p ->
                col.addView(savedCard(p, i, servers.size), wide())
                col.addSpacer(Ui.dp(ctx, 8))
            }
        }

        // Actions
        col.addSpacer(Ui.dp(ctx, 6))
        val actions = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL }
        actions.addView(Ui.primaryButton(ctx, "Scan to pair") { host.onScanQr() }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        actions.addView(View(ctx), LinearLayout.LayoutParams(Ui.dp(ctx, 10), 1))
        actions.addView(Ui.secondaryButton(ctx, "Add manually") { openEditor(null) }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        col.addView(actions, wide())

        // Settings sections
        renderDisplaySection()
        renderQualitySection()
        renderBehaviorSection()
        renderHardwareSection()
        renderAboutSection()
    }

    private fun discoveredCard(s: Discovery.Server): View {
        val ctx = context
        val card = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            background = Ui.cardBg()
            setPadding(Ui.dp(ctx, 16), Ui.dp(ctx, 14), Ui.dp(ctx, 16), Ui.dp(ctx, 14))
            isClickable = true
            setOnClickListener { host.onConnectDiscovered(s) }
        }
        val txt = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL }
        txt.addView(Ui.body(ctx, s.name, NxColor.TEXT, 16f))
        val sub = if (s.w > 0 && s.h > 0) "${s.host}:${s.port}  ·  ${s.w}×${s.h}" else "${s.host}:${s.port}"
        txt.addView(Ui.body(ctx, sub, NxColor.DIM, 13f))
        card.addView(txt, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        val dot = TextView(ctx).apply {
            text = "Tap to pair"
            setTextColor(NxColor.ACCENT_SOFT)
            textSize = 13f
        }
        card.addView(dot)
        return card
    }

    private fun savedCard(p: ServerProfile, index: Int, count: Int): View {
        val ctx = context
        val card = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            background = Ui.cardBg()
            setPadding(Ui.dp(ctx, 16), Ui.dp(ctx, 12), Ui.dp(ctx, 10), Ui.dp(ctx, 12))
        }
        val txt = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            isClickable = true
            setOnClickListener { host.onConnect(p) }
        }
        val name = p.name.ifBlank { p.host }
        txt.addView(Ui.body(ctx, name, NxColor.TEXT, 16f))
        txt.addView(Ui.body(ctx, "${p.host}:${p.port}", NxColor.DIM, 13f))
        card.addView(txt, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))

        // reorder up/down
        card.addView(iconButton(ctx, "▲") { move(index, -1) }.apply { alpha = if (index == 0) 0.3f else 1f })
        card.addView(iconButton(ctx, "▼") { move(index, +1) }.apply { alpha = if (index == count - 1) 0.3f else 1f })
        card.addView(iconButton(ctx, "✎") { openEditor(p) })
        return card
    }

    private fun iconButton(ctx: Context, glyph: String, onClick: () -> Unit): TextView =
        TextView(ctx).apply {
            text = glyph
            setTextColor(NxColor.DIM)
            textSize = 16f
            gravity = Gravity.CENTER
            setPadding(Ui.dp(ctx, 10), Ui.dp(ctx, 6), Ui.dp(ctx, 10), Ui.dp(ctx, 6))
            isClickable = true
            setOnClickListener { onClick() }
        }

    private fun move(index: Int, delta: Int) {
        val list = host.prefs.servers()
        val target = index + delta
        if (target < 0 || target >= list.size) return
        val tmp = list[index]; list[index] = list[target]; list[target] = tmp
        host.prefs.saveServers(list)
        render()
    }

    // ---- editor ---------------------------------------------------------

    private fun openEditor(p: ServerProfile?) {
        editing = p
        mode = Mode.EDIT
        render()
    }

    private lateinit var nameField: EditText
    private lateinit var hostField: EditText
    private lateinit var portField: EditText

    private fun renderEdit() {
        val ctx = context
        val p = editing
        col.addView(Ui.title(ctx, if (p == null) "Add server" else "Edit server", 22f))
        col.addSpacer(Ui.dp(ctx, 16))

        col.addView(Ui.body(ctx, "Name", NxColor.DIM, 13f))
        nameField = Ui.editText(ctx, "Living room PC", p?.name ?: "")
        nameField.inputType = android.text.InputType.TYPE_CLASS_TEXT
        col.addView(nameField, wide())
        col.addSpacer(Ui.dp(ctx, 12))

        col.addView(Ui.body(ctx, "Host / IP", NxColor.DIM, 13f))
        hostField = Ui.editText(ctx, "100.x.y.z or hostname", p?.host ?: "")
        col.addView(hostField, wide())
        col.addSpacer(Ui.dp(ctx, 12))

        col.addView(Ui.body(ctx, "Port", NxColor.DIM, 13f))
        portField = Ui.editText(ctx, Prefs.DEFAULT_PORT.toString(), (p?.port ?: Prefs.DEFAULT_PORT).toString(), numeric = true)
        col.addView(portField, wide())
        col.addSpacer(Ui.dp(ctx, 20))

        col.addView(Ui.primaryButton(ctx, "Save & connect") { saveEditor(connect = true) }, wide())
        col.addSpacer(Ui.dp(ctx, 8))
        col.addView(Ui.secondaryButton(ctx, "Save") { saveEditor(connect = false) }, wide())
        col.addSpacer(Ui.dp(ctx, 8))
        if (p != null) {
            col.addView(Ui.secondaryButton(ctx, "Delete", stroke = NxColor.DANGER, textColor = NxColor.DANGER) {
                host.prefs.deleteServer(p.id)
                backToList()
            }, wide())
            col.addSpacer(Ui.dp(ctx, 8))
        }
        col.addView(Ui.secondaryButton(ctx, "Cancel") { backToList() }, wide())
    }

    private fun saveEditor(connect: Boolean) {
        val host0 = hostField.text.toString().trim()
        if (host0.isBlank()) { hostField.error = "Required"; return }
        val port = portField.text.toString().trim().toIntOrNull() ?: Prefs.DEFAULT_PORT
        val name = nameField.text.toString().trim().ifBlank { host0 }
        val existing = editing
        val profile = if (existing == null)
            ServerProfile(ServerProfile.newId(), name, host0, port)
        else existing.copy(name = name, host = host0, port = port)
        host.prefs.upsertServer(profile)
        if (connect) {
            host.onConnect(profile)
        } else {
            backToList()
        }
    }

    private fun backToList() {
        editing = null
        mode = Mode.LIST
        render()
    }

    // ---- settings sections ---------------------------------------------

    private fun renderDisplaySection() {
        val ctx = context
        val prefs = host.prefs
        col.addView(Ui.sectionHeader(ctx, "Display"))

        col.addView(settingRow(ctx, "Video scaling", "Fit letterboxes portrait; Fill crops to edges."))
        val scaleSeg = Segmented(ctx, listOf("Fit", "Fill"), if (prefs.scaling == Scaling.FILL) 1 else 0) { i ->
            prefs.scaling = if (i == 1) Scaling.FILL else Scaling.FIT
            host.onSettingsChanged()
        }
        col.addView(scaleSeg, wide(Ui.dp(ctx, 8)))

        col.addSpacer(Ui.dp(ctx, 14))
        col.addView(settingRow(ctx, "Status pill", "Connection + RTT readout."))
        val pillIdx = when (prefs.pillMode) { PillMode.ALWAYS -> 0; PillMode.AUTO -> 1; PillMode.OFF -> 2 }
        val pillSeg = Segmented(ctx, listOf("Always", "Auto-hide", "Off"), pillIdx) { i ->
            prefs.pillMode = when (i) { 0 -> PillMode.ALWAYS; 2 -> PillMode.OFF; else -> PillMode.AUTO }
            host.onSettingsChanged()
        }
        col.addView(pillSeg, wide(Ui.dp(ctx, 8)))

        col.addSpacer(Ui.dp(ctx, 6))
        col.addView(toggleRow(ctx, "Keep screen awake while streaming", prefs.keepAwake) {
            prefs.keepAwake = it; host.onSettingsChanged()
        })
    }

    /**
     * Stream quality. These are the knobs that actually cost bandwidth, so they
     * live on the phone: the server is told over the signaling socket and applies
     * them live. The server clamps everything and echoes back what it really did.
     */
    private fun renderQualitySection() {
        val ctx = context
        val prefs = host.prefs
        col.addView(Ui.sectionHeader(ctx, "Stream quality"))

        col.addView(toggleRow(ctx, "Adaptive bitrate", prefs.adaptiveBitrate) {
            prefs.adaptiveBitrate = it
            host.onQualityChanged()
            render()                                  // relabel the bitrate row
        })
        col.addView(settingRow(ctx, if (prefs.adaptiveBitrate) "Bitrate ceiling" else "Bitrate",
            if (prefs.adaptiveBitrate)
                "Adapts to the link, never above this."
            else "Fixed rate — no adaptation."))
        val rates = listOf(2000, 4000, 6000, 8000, 12000, 20000)
        val rateIdx = rates.indexOfFirst { it >= prefs.bitrateKbps }.let { if (it < 0) rates.lastIndex else it }
        col.addView(Segmented(ctx, rates.map { "${it / 1000}M" }, rateIdx) { i ->
            prefs.bitrateKbps = rates[i]
            host.onQualityChanged()
        }, wide(Ui.dp(ctx, 8)))

        col.addSpacer(Ui.dp(ctx, 14))
        col.addView(settingRow(ctx, "Frame rate", "Lower costs less bandwidth; the session runs at 90Hz."))
        val fpsOpts = listOf(30, 45, 60, 90)
        val fpsIdx = fpsOpts.indexOfFirst { it >= prefs.fps }.let { if (it < 0) fpsOpts.lastIndex else it }
        col.addView(Segmented(ctx, fpsOpts.map { "$it" }, fpsIdx) { i ->
            prefs.fps = fpsOpts[i]
            host.onQualityChanged()
        }, wide(Ui.dp(ctx, 8)))
    }

    private fun renderBehaviorSection() {
        val ctx = context
        val prefs = host.prefs
        col.addView(Ui.sectionHeader(ctx, "Input & behaviour"))
        col.addView(toggleRow(ctx, "Auto-connect to last server on launch", prefs.autoConnect) { prefs.autoConnect = it })
        col.addView(toggleRow(ctx, "Reconnect automatically", prefs.autoReconnect) { prefs.autoReconnect = it })
        col.addView(toggleRow(ctx, "Haptic tick on connect", prefs.hapticOnConnect) { prefs.hapticOnConnect = it })
        col.addView(toggleRow(ctx, "Mirror my battery to the remote", prefs.mirrorBattery) {
            prefs.mirrorBattery = it
            host.onBatteryMirrorChanged()
        })
    }

    /**
     * Reverse streams: the phone's own hardware, offered to the remote Android.
     *
     * Its own section rather than a line in "Input & behaviour" because it is
     * the one setting here that gives the machine on the other end of the VPN
     * something it could not otherwise have. Default OFF, and the row always
     * says what the server currently thinks, so "allowed" and "actually
     * streaming" are never confused for each other.
     */
    private fun renderHardwareSection() {
        val ctx = context
        val prefs = host.prefs
        col.addView(Ui.sectionHeader(ctx, "Phone hardware"))
        col.addView(toggleRow(ctx, "Allow remote camera access", prefs.allowRemoteCamera) {
            prefs.allowRemoteCamera = it
            host.onCameraAllowChanged(it)
            render()
        })
        val note = host.cameraStatus()
            ?: if (prefs.allowRemoteCamera)
                "The host will ask when an app in the remote Android wants a camera."
            else "Apps in the remote Android cannot see this phone's camera."
        col.addView(Ui.body(ctx, note, NxColor.DIM, 12f))
    }

    private fun renderAboutSection() {
        val ctx = context
        col.addView(Ui.sectionHeader(ctx, "About"))
        val card = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            background = Ui.cardBg()
            setPadding(Ui.dp(ctx, 16), Ui.dp(ctx, 14), Ui.dp(ctx, 16), Ui.dp(ctx, 14))
        }
        card.addView(Ui.body(ctx, "NX Android Streamer  v${BuildConfig.VERSION_NAME}", NxColor.TEXT, 15f))
        card.addView(Ui.body(ctx, "Part of the NX suite", NxColor.DIM, 13f))
        if (host.streaming) {
            card.addView(diagRow(ctx, "Glass-to-glass RTT") { rttView = it })
            card.addView(diagRow(ctx, "Video resolution") { resView = it })
            updateDiagnostics()
        }
        col.addView(card, wide(Ui.dp(ctx, 8)))
    }

    private fun diagRow(ctx: Context, label: String, capture: (TextView) -> Unit): View {
        val row = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, Ui.dp(ctx, 8), 0, 0)
        }
        row.addView(Ui.body(ctx, label, NxColor.DIM, 14f), LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        val value = Ui.body(ctx, "—", NxColor.ACCENT_SOFT, 14f)
        capture(value)
        row.addView(value)
        return row
    }

    private fun settingRow(ctx: Context, title: String, subtitle: String): View {
        val box = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL }
        box.addView(Ui.body(ctx, title, NxColor.TEXT, 15f))
        box.addView(Ui.body(ctx, subtitle, NxColor.DIM, 12f))
        return box
    }

    private fun toggleRow(ctx: Context, label: String, on: Boolean, onChange: (Boolean) -> Unit): View {
        val row = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, Ui.dp(ctx, 10), 0, Ui.dp(ctx, 10))
        }
        row.addView(Ui.body(ctx, label, NxColor.TEXT, 15f), LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        row.addView(Ui.switch(ctx, on, onChange))
        return row
    }

    private fun wide(top: Int = 0): LinearLayout.LayoutParams =
        LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = top }
}
