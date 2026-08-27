package dev.nerdrx.nxbridge

import android.content.Context
import android.os.Build
import android.os.Environment
import java.io.File

/**
 * The one shared surface between this app and nx-streamerd: a directory on
 * /sdcard that we write requests into and the daemon writes answers into.
 *
 * A file spool instead of a socket because the daemon already has adb into this
 * container and nothing else — no port to open, no service to keep alive, no
 * permission to ask for, and the whole protocol stays inspectable with `ls`.
 *
 * Two candidate roots, tried in order, because scoped storage decides which one
 * we may actually use:
 *
 *  1. [PUBLIC_REMOTE] — `/sdcard/nx-bridge`. The readable one, and the one
 *     ARCHITECTURE.md names. On API 30+ creating it needs "All files access"
 *     (MANAGE_EXTERNAL_STORAGE), which nobody has granted by default.
 *  2. [PRIVATE_REMOTE] — this app's own external files dir. Needs no permission
 *     whatsoever, and adb's `shell` user is in `ext_data_rw`, so the daemon can
 *     still read and write it. This is the path that always works.
 *
 * The daemon watches both and the request names the one in use, so a machine
 * that later gets All-files-access flips over without either side reconfiguring.
 * The *remote* strings below are what the daemon sees; the local [File] may well
 * be `/storage/emulated/0/...` for the same directory, which is why both exist.
 */
object Spool {

    const val PUBLIC_REMOTE = "/sdcard/nx-bridge"
    const val PRIVATE_REMOTE = "/sdcard/Android/data/dev.nerdrx.nxbridge/files/nx-bridge"

    /** A usable spool root: where to read/write locally, and what to call it. */
    data class Root(val dir: File, val remote: String) {
        val requests: File get() = File(dir, "requests")
        val responses: File get() = File(dir, "responses")
        /** True when we are on the fallback because All files access is missing. */
        val isPrivate: Boolean get() = remote == PRIVATE_REMOTE
    }

    /** True when this app currently holds "All files access". */
    fun hasAllFilesAccess(): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && Environment.isExternalStorageManager()

    /**
     * Pick the best root we can actually write. Probes rather than trusting the
     * permission bits: some images (this one included) are more permissive than
     * the API says, and the only answer that matters is whether a file lands.
     */
    fun resolve(context: Context): Root {
        val public = File(Environment.getExternalStorageDirectory(), "nx-bridge")
        if (writable(public)) return Root(public, PUBLIC_REMOTE)
        // getExternalFilesDir(null) is /sdcard/Android/data/<pkg>/files. It can be
        // null when external storage is not mounted, which never happens in a
        // Waydroid container but is cheap to survive.
        val base = context.getExternalFilesDir(null) ?: context.filesDir
        val private = File(base, "nx-bridge")
        private.mkdirs()
        return Root(private, PRIVATE_REMOTE)
    }

    private fun writable(dir: File): Boolean = try {
        dir.mkdirs()
        val probe = File(dir, ".nxprobe")
        probe.writeText("x")
        probe.delete()
        true
    } catch (e: Exception) {
        false
    }

    /** Create both halves of the spool; harmless if they already exist. */
    fun ensure(root: Root) {
        root.requests.mkdirs()
        root.responses.mkdirs()
    }
}
