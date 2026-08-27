package dev.nerdrx.nxandroidstreamer

import android.net.Uri
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.provider.OpenableColumns
import android.util.Base64
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import org.json.JSONObject
import java.security.MessageDigest

/**
 * The phone half of the on-demand picker.
 *
 * When an app inside the *remote* Android asks for a picture, the nx-bridge
 * companion app in the container drops a request the daemon sees over adb, and
 * the daemon pushes `{"type":"pick"}` down the signaling socket to here. We open
 * the phone's own picker, and the file the user chooses rides back up the same
 * socket into the container's spool.
 *
 * Chunked, not one frame: a photo off a Pixel is 3-8 MB and the signaling socket
 * is also carrying SDP, ICE and config. One giant frame would stall negotiation
 * behind it for as long as the upload takes — and reconnects happen mid-upload.
 * [CHUNK_BYTES] of raw data per frame (~44 KB once base64'd) interleaves finely
 * and still amortises the JSON overhead away.
 *
 * Everything expensive happens on [worker]; every StreamClient call is posted
 * back to the main looper, because that class's whole contract is that it runs
 * on one thread.
 */
class PickerBridge(
    private val activity: ComponentActivity,
    private val client: () -> StreamClient?
) {

    // Registered at construction time, in the activity's field initializers —
    // ActivityResultLauncher registration is illegal after onStart().
    //
    // Two contracts because they are genuinely different pickers: PickVisualMedia
    // is the modern photo picker (no storage permission, no access to anything
    // but what the user taps), GetContent is the document chooser and the only
    // one that can serve an arbitrary mime type.
    private val pickImage = activity.registerForActivityResult(
        ActivityResultContracts.PickVisualMedia()
    ) { uri -> onPicked(uri) }

    private val pickAny = activity.registerForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri -> onPicked(uri) }

    private val main = Handler(Looper.getMainLooper())
    private var worker: HandlerThread? = null

    /** The request we are currently serving. One at a time, by construction: the
     *  picker is a modal activity and the container is waiting on exactly one. */
    private var pendingId: String? = null
    private var pendingSpool: String? = null

    // ---------------------------------------------------------------------

    /** `{"type":"pick","id":…,"mime":…}` arrived. Runs on the main looper. */
    fun onPickRequest(msg: JSONObject) {
        val id = msg.optString("id")
        if (id.isNullOrEmpty()) return
        if (pendingId != null) {
            // The container should never have two picks open, but if it does,
            // answer the loser rather than leaving its activity hanging until
            // its own timeout.
            Log.w(TAG, "pick $id arrived while $pendingId is in flight; declining")
            sendCancel(id, msg.optString("spool"))
            return
        }
        pendingId = id
        pendingSpool = msg.optString("spool").ifBlank { null }
        val mime = msg.optString("mime").ifBlank { "*/*" }
        Log.d(TAG, "pick $id for $mime — opening the picker")
        try {
            if (mime.startsWith("image/")) {
                pickImage.launch(
                    androidx.activity.result.PickVisualMediaRequest.Builder()
                        .setMediaType(ActivityResultContracts.PickVisualMedia.ImageOnly)
                        .build()
                )
            } else {
                pickAny.launch(mime)
            }
        } catch (e: Exception) {
            Log.e(TAG, "could not open a picker: ${e.message}")
            finishPending(cancelled = true)
        }
    }

    /** The stream went away mid-pick: stop, and let the container stop waiting. */
    fun abort() {
        val id = pendingId ?: return
        Log.d(TAG, "pick $id aborted (session ended)")
        sendCancel(id, pendingSpool)
        pendingId = null
        pendingSpool = null
        worker?.quitSafely()
        worker = null
    }

    // ---------------------------------------------------------------------

    private fun onPicked(uri: Uri?) {
        val id = pendingId ?: return
        if (uri == null) {
            Log.d(TAG, "pick $id cancelled by the user")
            finishPending(cancelled = true)
            return
        }
        val thread = HandlerThread("nx-pick").apply { start() }
        worker = thread
        Handler(thread.looper).post { upload(id, uri) }
    }

    private fun finishPending(cancelled: Boolean) {
        val id = pendingId ?: return
        if (cancelled) sendCancel(id, pendingSpool)
        pendingId = null
        pendingSpool = null
        worker?.quitSafely()
        worker = null
    }

    private fun sendCancel(id: String, spool: String?) {
        val msg = JSONObject().put("type", "pick-cancel").put("id", id)
        if (!spool.isNullOrEmpty()) msg.put("spool", spool)
        main.post { client()?.sendBridgeFrame(msg) }
    }

    // ---- the upload, on the worker thread -------------------------------

    private fun upload(id: String, uri: Uri) {
        val resolver = activity.contentResolver
        val name = displayName(uri) ?: "photo.jpg"
        val declared = declaredSize(uri)
        val mime = resolver.getType(uri) ?: "application/octet-stream"
        Log.d(TAG, "uploading $name ($mime, ${declared ?: -1} bytes) for pick $id")

        val digest = MessageDigest.getInstance("SHA-256")
        var sent = 0L
        var seq = 0
        try {
            resolver.openInputStream(uri).use { input ->
                if (input == null) throw IllegalStateException("openInputStream returned null")
                val buf = ByteArray(CHUNK_BYTES)
                while (true) {
                    val n = input.read(buf)
                    val eof = n <= 0
                    val piece = if (eof) ByteArray(0) else buf.copyOf(n)
                    if (!eof) {
                        digest.update(piece)
                        sent += n
                    }
                    val frame = JSONObject()
                        .put("type", "pick-data")
                        .put("id", id)
                        .put("seq", seq)
                        .put("eof", eof)
                        .put("b64", Base64.encodeToString(piece, Base64.NO_WRAP))
                    if (seq == 0) {
                        frame.put("name", name)
                        frame.put("mime", mime)
                        // Only when the resolver actually told us; a guess here
                        // would trip the daemon's truncation check for no reason.
                        if (declared != null) frame.put("size", declared)
                        pendingSpool?.let { frame.put("spool", it) }
                    }
                    if (eof) {
                        // The daemon checks byte count and this independently.
                        // A photo that arrives *almost* intact is the failure
                        // nobody notices until the upload they just made is half
                        // grey, so make it impossible.
                        frame.put("size", sent)
                        frame.put("sha256", digest.digest().joinToString("") { "%02x".format(it) })
                    }
                    awaitBacklog()
                    main.post { client()?.sendBridgeFrame(frame) }
                    seq++
                    if (eof) break
                }
            }
            Log.d(TAG, "pick $id uploaded: $sent bytes in $seq frames")
        } catch (e: Exception) {
            Log.e(TAG, "pick $id upload failed: ${e.message}")
            sendCancel(id, pendingSpool)
        } finally {
            main.post {
                if (pendingId == id) {
                    pendingId = null
                    pendingSpool = null
                }
            }
            worker?.quitSafely()
            worker = null
        }
    }

    /**
     * Backpressure. OkHttp buffers whatever we hand it, so a 60 MB video with no
     * throttle is 80 MB of queued base64 in the heap and an OOM on the phone —
     * and it would also bury the ICE/SDP frames the reconnect needs. Wait for the
     * socket to drain instead of racing it.
     */
    private fun awaitBacklog() {
        var waited = 0
        while (true) {
            val backlog = client()?.signalBacklogBytes() ?: return
            if (backlog < MAX_BACKLOG_BYTES) return
            if (waited > BACKLOG_TIMEOUT_MS) {
                Log.w(TAG, "signaling socket is not draining ($backlog bytes queued)")
                return
            }
            try {
                Thread.sleep(BACKLOG_POLL_MS)
            } catch (e: InterruptedException) {
                return
            }
            waited += BACKLOG_POLL_MS.toInt()
        }
    }

    private fun displayName(uri: Uri): String? = try {
        activity.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME),
            null, null, null)?.use { c ->
            if (c.moveToFirst() && !c.isNull(0)) c.getString(0) else null
        }
    } catch (e: Exception) {
        null
    }

    private fun declaredSize(uri: Uri): Long? = try {
        activity.contentResolver.query(uri, arrayOf(OpenableColumns.SIZE),
            null, null, null)?.use { c ->
            if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else null
        }
    } catch (e: Exception) {
        null
    }

    companion object {
        private const val TAG = "nx"
        /** Raw bytes per frame; ~44 KB once base64 and JSON are on top. */
        private const val CHUNK_BYTES = 32 * 1024
        private const val MAX_BACKLOG_BYTES = 512 * 1024L
        private const val BACKLOG_POLL_MS = 20L
        private const val BACKLOG_TIMEOUT_MS = 30_000
    }
}
