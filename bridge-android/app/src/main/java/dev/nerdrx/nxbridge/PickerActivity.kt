package dev.nerdrx.nxbridge

import android.app.Activity
import android.content.ClipData
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.security.SecureRandom

/**
 * The whole feature, from the remote Android's point of view.
 *
 * A remote app fires ACTION_GET_CONTENT; the chooser offers "Pick on my phone";
 * we land here. From here on it is four steps and no network:
 *
 *   1. drop `<spool>/requests/<id>.json` naming the mime type we were asked for
 *   2. wait, showing a cancellable card, while nx-streamerd notices the file over
 *      adb and pushes {"type":"pick"} at the real phone
 *   3. the phone's own picker returns bytes, the daemon `adb push`es them to
 *      `<spool>/responses/<id>__<name>`
 *   4. copy that into our FileProvider directory and finish with a content:// Uri
 *
 * Why polling and not a FileObserver: the response arrives via `adb push` to a
 * FUSE-backed sdcard, and inotify across that boundary is not reliable on every
 * image. 250 ms of polling for the ~2 seconds a pick actually takes is nothing,
 * and it cannot silently fail to fire.
 *
 * Every exit path is RESULT_CANCELED unless a file genuinely landed and copied:
 * completing the intent with a broken Uri is worse than not completing it.
 */
class PickerActivity : Activity() {

    private lateinit var root: Spool.Root
    private lateinit var id: String
    private var requestFile: File? = null

    private var finished = false
    private var startedAt = 0L

    private val ui = Handler(Looper.getMainLooper())
    private lateinit var worker: HandlerThread
    private lateinit var work: Handler

    private lateinit var statusLine: TextView
    private lateinit var hintLine: TextView

    // ---------------------------------------------------------------------

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setResult(RESULT_CANCELED)          // the default until a file lands

        root = Spool.resolve(this)
        Spool.ensure(root)
        id = newId()
        startedAt = System.currentTimeMillis()

        buildUi()

        worker = HandlerThread("nx-picker").apply { start() }
        work = Handler(worker.looper)
        work.post { postRequest() }
        work.postDelayed(pollRunnable, POLL_INTERVAL_MS)
        ui.postDelayed(tickRunnable, 1000)
    }

    /** 16 hex chars. Must match nx_bridge.py's `[A-Za-z0-9_-]{1,64}` id guard. */
    private fun newId(): String {
        val bytes = ByteArray(8)
        SecureRandom().nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it) }
    }

    // ---- step 1: the doorbell -------------------------------------------

    private fun postRequest() {
        val mime = requestedMime()
        val json = JSONObject()
            .put("id", id)
            .put("mime", mime)
            .put("action", intent?.action ?: Intent.ACTION_GET_CONTENT)
            .put("multiple", intent?.getBooleanExtra(Intent.EXTRA_ALLOW_MULTIPLE, false) ?: false)
            // The daemon pushes the answer back to whichever root we could write;
            // without this it would have to guess, and it would guess wrong the
            // moment All-files-access changes under it.
            .put("spool", root.remote)
            .put("ts", System.currentTimeMillis())
            .put("caller", callingPackage ?: "")
        val file = File(root.requests, "$id.json")
        try {
            // Written under a .part name and renamed, so the daemon's `ls` can
            // never catch a half-written json — the same trick it uses on us.
            val staging = File(root.requests, "$id.json.part")
            staging.writeText(json.toString())
            if (!staging.renameTo(file)) {
                staging.copyTo(file, overwrite = true)
                staging.delete()
            }
            requestFile = file
            Log.i(TAG, "request $id ($mime) -> ${file.absolutePath}")
        } catch (e: Exception) {
            Log.e(TAG, "could not write the request: ${e.message}")
            ui.post { failOut("Cannot write to ${root.remote}") }
        }
    }

    /**
     * What the caller actually asked for. `intent.type` is the reliable field;
     * EXTRA_MIME_TYPES narrows it further and the phone's picker can use it, so
     * pass the first entry through when the plain type is the useless "* / *".
     */
    private fun requestedMime(): String {
        val declared = intent?.type
        val extras = intent?.getStringArrayExtra(Intent.EXTRA_MIME_TYPES)
        if ((declared == null || declared == "*/*") && !extras.isNullOrEmpty()) {
            return extras[0]
        }
        return declared ?: "*/*"
    }

    // ---- step 2/3: wait for the answer ----------------------------------

    private val pollRunnable = object : Runnable {
        override fun run() {
            if (finished) return
            val outcome = scan()
            if (outcome == null) {
                if (System.currentTimeMillis() - startedAt > TIMEOUT_MS) {
                    ui.post { failOut("The host did not answer in time") }
                    return
                }
                work.postDelayed(this, POLL_INTERVAL_MS)
                return
            }
            ui.post { settle(outcome) }
        }
    }

    /** -> the delivered file, or a marker, or null while nothing has arrived. */
    private fun scan(): Outcome? {
        val entries = root.responses.listFiles() ?: return null
        for (f in entries) {
            val name = f.name
            when {
                name == "$id.cancel" -> { f.delete(); return Outcome.Cancelled }
                name == "$id.error" -> { f.delete(); return Outcome.Failed("The host could not send the file") }
                // ".part" is the daemon mid-push; it renames when the bytes are
                // all there, so anything without the suffix is complete.
                name.startsWith("$id$SEP") && !name.endsWith(".part") -> return Outcome.File(f)
            }
        }
        return null
    }

    private sealed class Outcome {
        object Cancelled : Outcome()
        data class Failed(val why: String) : Outcome()
        data class File(val file: java.io.File) : Outcome()
    }

    private fun settle(outcome: Outcome) {
        when (outcome) {
            is Outcome.Cancelled -> failOut("Cancelled on your phone", quiet = true)
            is Outcome.Failed -> failOut(outcome.why)
            is Outcome.File -> {
                statusLine.text = "Receiving…"
                work.post { deliver(outcome.file) }
            }
        }
    }

    // ---- step 4: hand it to the caller ----------------------------------

    private fun deliver(delivered: File) {
        try {
            val display = delivered.name.substringAfter(SEP).ifBlank { "$id.bin" }
            val shared = File(filesDir, "shared").apply { mkdirs() }
            sweep(shared)
            val out = File(shared, "$id-$display")
            delivered.copyTo(out, overwrite = true)
            delivered.delete()

            val uri = FileProvider.getUriForFile(this, AUTHORITY, out)
            val result = Intent().apply {
                data = uri
                // Both, because callers disagree about which they read: a share
                // sheet reads clipData, a plain openInputStream reads getData().
                clipData = ClipData.newUri(contentResolver, display, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            Log.i(TAG, "delivering $display (${out.length()} bytes) as $uri")
            ui.post {
                if (finished) return@post
                finished = true
                setResult(RESULT_OK, result)
                cleanup()
                finish()
            }
        } catch (e: Exception) {
            Log.e(TAG, "delivery failed: ${e.message}")
            ui.post { failOut("Could not hand the file over") }
        }
    }

    /** Keep files/shared from growing forever; a delivered Uri is short-lived. */
    private fun sweep(dir: File) {
        val cutoff = System.currentTimeMillis() - SHARED_TTL_MS
        dir.listFiles()?.forEach { if (it.lastModified() < cutoff) it.delete() }
    }

    // ---- exits ----------------------------------------------------------

    private fun failOut(why: String, quiet: Boolean = false) {
        if (finished) return
        finished = true
        if (!quiet) Log.w(TAG, "pick $id ended: $why")
        setResult(RESULT_CANCELED)
        cleanup()
        finish()
    }

    /** Never leave a doorbell ringing for a pick nobody is waiting on. */
    private fun cleanup() {
        val file = requestFile
        val handler = if (::work.isInitialized) work else null
        val job = Runnable {
            try {
                file?.delete()
                File(root.requests, "$id.json.part").delete()
                root.responses.listFiles()?.forEach {
                    if (it.name.startsWith(id)) it.delete()
                }
            } catch (e: Exception) {
                Log.w(TAG, "cleanup: ${e.message}")
            }
        }
        if (handler != null) handler.post(job) else job.run()
    }

    override fun onDestroy() {
        super.onDestroy()
        ui.removeCallbacksAndMessages(null)
        if (::worker.isInitialized) {
            work.removeCallbacksAndMessages(null)
            worker.quitSafely()
        }
    }

    // ---- UI --------------------------------------------------------------

    private fun buildUi() {
        val pad = NxUi.dp(this, 24)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(NxColor.VOID)
            setPadding(pad, pad, pad, pad)
        }
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = NxUi.cardBg()
            setPadding(NxUi.dp(context, 22), NxUi.dp(context, 22),
                NxUi.dp(context, 22), NxUi.dp(context, 22))
        }
        card.addView(NxUi.title(this, "Picking on your phone", 20f))
        card.addView(NxUi.spacer(this, NxUi.dp(this, 6)))
        statusLine = NxUi.body(this, "Waiting for the host…", NxColor.ACCENT_SOFT, 15f)
        card.addView(statusLine)
        card.addView(NxUi.spacer(this, NxUi.dp(this, 10)))
        card.addView(NxUi.body(this,
            "The real phone's photo picker is opening. Choose a file there and it " +
                "will arrive here.", NxColor.DIM, 13f))
        card.addView(NxUi.spacer(this, NxUi.dp(this, 6)))
        hintLine = NxUi.body(this, "", NxColor.DIM, 12f)
        card.addView(hintLine)
        card.addView(NxUi.spacer(this, NxUi.dp(this, 18)))
        card.addView(
            NxUi.secondaryButton(this, "Cancel", stroke = NxColor.DANGER,
                textColor = NxColor.DANGER) { failOut("Cancelled here", quiet = true) },
            NxUi.wide()
        )
        col.addView(card, NxUi.wide())
        setContentView(col)
    }

    private val tickRunnable = object : Runnable {
        override fun run() {
            if (finished) return
            val elapsed = (System.currentTimeMillis() - startedAt) / 1000
            statusLine.text = "Waiting for the host…  ${TIMEOUT_MS / 1000 - elapsed}s"
            // The single most likely failure is "nothing is attached", and the
            // user cannot see that from in here — so say it out loud rather than
            // spinning silently for two minutes.
            if (elapsed >= 8 && hintLine.text.isEmpty()) {
                hintLine.text = "No answer yet — check that the NX client is " +
                    "connected on your phone."
            }
            ui.postDelayed(this, 1000)
        }
    }

    companion object {
        private const val TAG = "nx-bridge"
        private const val AUTHORITY = "dev.nerdrx.nxbridge.files"
        /** Separator between the request id and the original filename. */
        const val SEP = "__"
        private const val POLL_INTERVAL_MS = 250L
        private const val TIMEOUT_MS = 120_000L
        private const val SHARED_TTL_MS = 60 * 60 * 1000L
    }
}
