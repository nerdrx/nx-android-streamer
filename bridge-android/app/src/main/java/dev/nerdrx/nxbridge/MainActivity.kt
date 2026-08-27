package dev.nerdrx.nxbridge

import android.app.Activity
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.io.File

/**
 * The launcher face. It does nothing the feature needs — [PickerActivity] is
 * started by an intent, not by a user — and exists so the whole thing can be
 * verified by hand: open it and it tells you which spool root is live, whether
 * anything is queued, and the exact command to fix the one thing that can be
 * wrong (All files access).
 *
 * It also doubles as the self-test: "Send a test request" writes a real request
 * json, so you can watch the daemon pick it up without involving another app.
 */
class MainActivity : Activity() {

    private lateinit var root: Spool.Root
    private lateinit var statusView: TextView
    private lateinit var countsView: TextView
    private val ui = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        root = Spool.resolve(this)
        Spool.ensure(root)
        build()
    }

    override fun onResume() {
        super.onResume()
        // The spool root can change under us the moment All files access is
        // granted, so re-resolve rather than trusting what onCreate saw.
        root = Spool.resolve(this)
        Spool.ensure(root)
        ui.post(refresh)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(refresh)
    }

    private val refresh = object : Runnable {
        override fun run() {
            val queued = root.requests.listFiles()?.count { it.name.endsWith(".json") } ?: 0
            val waiting = root.responses.listFiles()?.size ?: 0
            countsView.text = "$queued request(s) queued · $waiting response file(s)"
            statusView.text = if (queued > 0) "Handing off a pick…" else "Waiting for requests"
            ui.postDelayed(this, 1000)
        }
    }

    private fun build() {
        val pad = NxUi.dp(this, 22)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, NxUi.dp(context, 46), pad, pad)
        }

        col.addView(NxUi.title(this, "NX Bridge", 26f))
        col.addView(NxUi.body(this,
            "The remote half of the on-demand picker.", NxColor.DIM, 14f))
        col.addView(NxUi.spacer(this, NxUi.dp(this, 20)))

        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = NxUi.cardBg()
            setPadding(NxUi.dp(context, 18), NxUi.dp(context, 16),
                NxUi.dp(context, 18), NxUi.dp(context, 16))
        }
        statusView = NxUi.body(this, "Waiting for requests", NxColor.OK, 17f)
        card.addView(statusView)
        card.addView(NxUi.spacer(this, NxUi.dp(this, 4)))
        countsView = NxUi.body(this, "—", NxColor.DIM, 13f)
        card.addView(countsView)
        card.addView(NxUi.spacer(this, NxUi.dp(this, 14)))
        card.addView(NxUi.body(this, "SPOOL", NxColor.DIM, 11f))
        card.addView(NxUi.mono(this, root.remote, NxColor.ACCENT_SOFT))
        card.addView(NxUi.spacer(this, NxUi.dp(this, 10)))
        card.addView(NxUi.body(this, "STORAGE MODE", NxColor.DIM, 11f))
        card.addView(NxUi.body(this,
            if (root.isPrivate)
                "App-private (no permission needed). This works; the daemon " +
                    "watches this path too."
            else "All files access — using the readable /sdcard/nx-bridge.",
            if (root.isPrivate) NxColor.DIM else NxColor.OK, 13f))
        col.addView(card, NxUi.wide())

        // Only worth offering when it would actually change something.
        if (root.isPrivate && Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            col.addView(NxUi.spacer(this, NxUi.dp(this, 12)))
            val hint = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                background = NxUi.cardBg()
                setPadding(NxUi.dp(context, 18), NxUi.dp(context, 16),
                    NxUi.dp(context, 18), NxUi.dp(context, 16))
            }
            hint.addView(NxUi.body(this, "Optional: readable spool", NxColor.TEXT, 15f))
            hint.addView(NxUi.body(this,
                "Grant All files access to move the spool to /sdcard/nx-bridge, " +
                    "where it is easy to inspect. Nothing needs it.",
                NxColor.DIM, 13f))
            hint.addView(NxUi.spacer(this, NxUi.dp(this, 8)))
            hint.addView(NxUi.mono(this,
                "adb shell appops set $packageName MANAGE_EXTERNAL_STORAGE allow"))
            hint.addView(NxUi.spacer(this, NxUi.dp(this, 10)))
            hint.addView(NxUi.secondaryButton(this, "Open the setting") {
                try {
                    startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
                } catch (e: Exception) {
                    // Some AOSP images ship no such settings screen; the adb line
                    // above is the fallback and is printed right here.
                }
            }, NxUi.wide())
            col.addView(hint, NxUi.wide())
        }

        col.addView(NxUi.spacer(this, NxUi.dp(this, 16)))
        col.addView(NxUi.primaryButton(this, "Send a test request") { testRequest() },
            NxUi.wide())
        col.addView(NxUi.spacer(this, NxUi.dp(this, 8)))
        col.addView(NxUi.secondaryButton(this, "Try the picker (image/*)") {
            // Exactly what a remote app does. Ends up back in PickerActivity if
            // the intent filters are registered correctly — the fastest way to
            // confirm the chooser lists us.
            val get = Intent(Intent.ACTION_GET_CONTENT).apply {
                type = "image/*"
                addCategory(Intent.CATEGORY_OPENABLE)
            }
            startActivityForResult(Intent.createChooser(get, "Pick an image"), 1)
        }, NxUi.wide())

        col.addView(NxUi.spacer(this, NxUi.dp(this, 18)))
        col.addView(NxUi.body(this,
            "v${BuildConfig.VERSION_NAME} · part of the NX suite · no network " +
                "permission, no background service.", NxColor.DIM, 12f))

        setContentView(ScrollView(this).apply {
            setBackgroundColor(NxColor.VOID)
            isFillViewport = true
            addView(col, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT))
        })
    }

    /** Drop a request json by hand, so the daemon side can be watched in isolation. */
    private fun testRequest() {
        val id = "test" + java.lang.Long.toHexString(System.currentTimeMillis())
        val json = org.json.JSONObject()
            .put("id", id)
            .put("mime", "image/*")
            .put("action", Intent.ACTION_GET_CONTENT)
            .put("multiple", false)
            .put("spool", root.remote)
            .put("ts", System.currentTimeMillis())
            .put("caller", packageName)
        try {
            File(root.requests, "$id.json").writeText(json.toString())
            toast("Wrote $id.json — watch the daemon log")
        } catch (e: Exception) {
            toast("Could not write: ${e.message}")
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != 1) return
        val uri = data?.data
        if (resultCode != RESULT_OK || uri == null) {
            toast("Picker returned nothing")
            return
        }
        // Read it back through the resolver, exactly like a real caller would:
        // a Uri we cannot open is a bug we want to see here, not in someone's app.
        val bytes = try {
            contentResolver.openInputStream(uri)?.use { it.readBytes().size } ?: -1
        } catch (e: Exception) {
            -1
        }
        toast(if (bytes >= 0) "Got $bytes bytes from $uri" else "Could not read $uri")
    }

    private fun toast(msg: String) =
        android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_LONG).show()
}
