package dev.nerdrx.nxandroidstreamer

import org.json.JSONArray
import org.json.JSONObject

/** A saved server: friendly name + host + port. Stored as JSON in prefs. */
data class ServerProfile(
    val id: String,
    var name: String,
    var host: String,
    var port: Int
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("name", name)
        .put("host", host)
        .put("port", port)

    companion object {
        fun fromJson(o: JSONObject): ServerProfile = ServerProfile(
            id = o.optString("id"),
            name = o.optString("name"),
            host = o.optString("host"),
            port = o.optInt("port", Prefs.DEFAULT_PORT)
        )

        fun listToJson(list: List<ServerProfile>): String {
            val arr = JSONArray()
            list.forEach { arr.put(it.toJson()) }
            return arr.toString()
        }

        fun listFromJson(raw: String?): MutableList<ServerProfile> {
            val out = mutableListOf<ServerProfile>()
            if (raw.isNullOrBlank()) return out
            try {
                val arr = JSONArray(raw)
                for (i in 0 until arr.length()) out.add(fromJson(arr.getJSONObject(i)))
            } catch (_: Exception) {}
            return out
        }

        fun newId(): String = System.currentTimeMillis().toString(36) + "-" +
            (0..0xffff).random().toString(16)

        /** Parse a pairing URI `nxas://<host>:<port>` (port optional) -> (host, port). */
        fun parseUri(raw: String?): Pair<String, Int>? {
            if (raw == null) return null
            val s = raw.trim()
            val body = when {
                s.startsWith("nxas://") -> s.removePrefix("nxas://")
                s.startsWith("ws://") -> s.removePrefix("ws://")
                s.startsWith("http://") -> s.removePrefix("http://")
                else -> s
            }.substringBefore("/").substringBefore("?")
            if (body.isBlank()) return null
            // host may be an IPv6 literal in [..]; keep it simple for IPv4/hostnames.
            val host: String
            val port: Int
            val colon = body.lastIndexOf(':')
            if (colon > 0 && colon < body.length - 1) {
                host = body.substring(0, colon)
                port = body.substring(colon + 1).toIntOrNull() ?: Prefs.DEFAULT_PORT
            } else {
                host = body
                port = Prefs.DEFAULT_PORT
            }
            if (host.isBlank() || port !in 1..65535) return null
            return host to port
        }
    }
}
