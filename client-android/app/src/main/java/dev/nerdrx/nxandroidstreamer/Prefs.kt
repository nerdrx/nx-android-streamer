package dev.nerdrx.nxandroidstreamer

import android.content.Context

enum class Scaling { FIT, FILL }
enum class PillMode { ALWAYS, AUTO, OFF }

/**
 * Everything the client persists. Server profiles are a JSON list; the rest are
 * plain flags. All of it takes effect without an APK rebuild — the UI writes here
 * and the live surfaces (StreamView scaling, the pill, the reconnect policy) read
 * back immediately.
 */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("nx_streamer", Context.MODE_PRIVATE)

    // ---- server profiles ------------------------------------------------

    fun servers(): MutableList<ServerProfile> =
        ServerProfile.listFromJson(sp.getString(KEY_SERVERS, null))

    fun saveServers(list: List<ServerProfile>) {
        sp.edit().putString(KEY_SERVERS, ServerProfile.listToJson(list)).apply()
    }

    /** Insert or update by id; returns the stored profile. */
    fun upsertServer(p: ServerProfile): ServerProfile {
        val list = servers()
        val idx = list.indexOfFirst { it.id == p.id }
        if (idx >= 0) list[idx] = p else list.add(p)
        saveServers(list)
        return p
    }

    fun deleteServer(id: String) {
        val list = servers().filterNot { it.id == id }
        saveServers(list)
        if (lastUsedId == id) lastUsedId = ""
    }

    /** Find an existing profile matching host+port, else null. */
    fun findByHostPort(host: String, port: Int): ServerProfile? =
        servers().firstOrNull { it.host.equals(host, true) && it.port == port }

    var lastUsedId: String
        get() = sp.getString(KEY_LAST_USED, "") ?: ""
        set(v) = sp.edit().putString(KEY_LAST_USED, v).apply()

    fun lastUsedServer(): ServerProfile? {
        val id = lastUsedId
        if (id.isBlank()) return null
        return servers().firstOrNull { it.id == id }
    }

    // ---- behaviour ------------------------------------------------------

    var autoConnect: Boolean
        get() = sp.getBoolean(KEY_AUTOCONNECT, true)
        set(v) = sp.edit().putBoolean(KEY_AUTOCONNECT, v).apply()

    var autoReconnect: Boolean
        get() = sp.getBoolean(KEY_AUTORECONNECT, true)
        set(v) = sp.edit().putBoolean(KEY_AUTORECONNECT, v).apply()

    var hapticOnConnect: Boolean
        get() = sp.getBoolean(KEY_HAPTIC, true)
        set(v) = sp.edit().putBoolean(KEY_HAPTIC, v).apply()

    // ---- display --------------------------------------------------------

    var scaling: Scaling
        // FILL by default: the remote is the same 1080x2400 as the phone, so "fill"
        // is an exact 1:1 with nothing cropped, while FIT letterboxes it smaller.
        get() = if (sp.getString(KEY_SCALING, "FILL") == "FILL") Scaling.FILL else Scaling.FIT
        set(v) = sp.edit().putString(KEY_SCALING, v.name).apply()

    var pillMode: PillMode
        get() = when (sp.getString(KEY_PILL, "AUTO")) {
            "ALWAYS" -> PillMode.ALWAYS
            "OFF" -> PillMode.OFF
            else -> PillMode.AUTO
        }
        set(v) = sp.edit().putString(KEY_PILL, v.name).apply()

    var keepAwake: Boolean
        get() = sp.getBoolean(KEY_KEEP_AWAKE, true)
        set(v) = sp.edit().putBoolean(KEY_KEEP_AWAKE, v).apply()

    // ---- stream quality (sent to the server as a config message) --------
    // The server is authoritative and clamps these; we only remember what the
    // user asked for so a reconnect re-applies it.

    var adaptiveBitrate: Boolean
        get() = sp.getBoolean(KEY_ABR, true)
        set(v) = sp.edit().putBoolean(KEY_ABR, v).apply()

    /** Ceiling in kbps when ABR is on; the fixed rate when it is off. */
    var bitrateKbps: Int
        get() = sp.getInt(KEY_BITRATE, 8000)
        set(v) = sp.edit().putInt(KEY_BITRATE, v.coerceIn(500, 50000)).apply()

    var fps: Int
        get() = sp.getInt(KEY_FPS, 60)
        set(v) = sp.edit().putInt(KEY_FPS, v.coerceIn(15, 120)).apply()

    companion object {
        const val DEFAULT_PORT = 8765
        private const val KEY_ABR = "abr"
        private const val KEY_BITRATE = "bitrate_kbps"
        private const val KEY_FPS = "fps"
        private const val KEY_SERVERS = "servers"
        private const val KEY_LAST_USED = "last_used"
        private const val KEY_AUTOCONNECT = "autoconnect"
        private const val KEY_AUTORECONNECT = "autoreconnect"
        private const val KEY_HAPTIC = "haptic"
        private const val KEY_SCALING = "scaling"
        private const val KEY_PILL = "pill_mode"
        private const val KEY_KEEP_AWAKE = "keep_awake"
    }
}
