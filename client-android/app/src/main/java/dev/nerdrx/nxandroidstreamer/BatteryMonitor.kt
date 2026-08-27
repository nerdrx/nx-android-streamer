package dev.nerdrx.nxandroidstreamer

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Handler
import android.os.Looper

/**
 * The real phone's battery, watched so the remote Android can wear it.
 *
 * The remote is meant to feel like the device in your hand, and a status bar
 * frozen at whatever the container invented breaks that the first time you
 * glance at it. So we read the level and the charging state here and the server
 * forces them onto the container's battery service.
 *
 * There is no polling. ACTION_BATTERY_CHANGED is sticky, so registering both
 * arms the watch and hands back the current value in one call; the framework
 * then re-broadcasts on its own. A poll loop over a battery reading is exactly
 * the kind of thing that turns up later as "why does this app cost 4%/hour".
 *
 * The framework fires that broadcast for temperature and voltage wobble as
 * well, several times per percent, so only real movement in *our* two fields
 * leaves this class. Level ticks are additionally floored at [MIN_INTERVAL_MS]
 * apart; a plug or unplug jumps the queue, because that is the change the user
 * is looking at the status bar to see.
 *
 * Needs no permission: battery state is public to every app.
 */
class BatteryMonitor(
    private val context: Context,
    private val onChange: (level: Int, charging: Boolean) -> Unit
) {
    private val main = Handler(Looper.getMainLooper())

    private var receiver: BroadcastReceiver? = null
    private var lastSent: Pair<Int, Boolean>? = null
    private var lastSentAt = 0L
    private var pending: Pair<Int, Boolean>? = null

    private val flush = Runnable {
        pending?.let { emit(it) }
        pending = null
    }

    /**
     * Begin watching, and surface the current reading immediately. Calling this
     * on an already-running monitor re-announces instead — that is the
     * new-session case, where the reading has not changed but the peer holding
     * it has.
     */
    fun start() {
        val existing = receiver
        if (existing != null) {
            resend()
            return
        }
        val r = object : BroadcastReceiver() {
            override fun onReceive(c: Context?, intent: Intent?) {
                intent?.let { offer(read(it)) }
            }
        }
        receiver = r
        // No RECEIVER_EXPORTED/NOT_EXPORTED flag: ACTION_BATTERY_CHANGED is a
        // protected system broadcast, which targetSdk 34+ exempts from the
        // flag requirement.
        val sticky = context.registerReceiver(r, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        offer(read(sticky))
    }

    fun stop() {
        main.removeCallbacks(flush)
        pending = null
        receiver?.let { try { context.unregisterReceiver(it) } catch (_: Exception) {} }
        receiver = null
        // Forget what was sent: the next start() is a new peer that has never
        // been told anything, and must not be deduplicated against the old one.
        lastSent = null
        lastSentAt = 0L
    }

    /** Re-announce the current reading even though nothing changed. */
    fun resend() {
        lastSent = null
        lastSentAt = 0L
        // A null receiver reads the sticky intent without registering anything.
        offer(read(context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))))
    }

    // ---------------------------------------------------------------------

    private fun read(intent: Intent?): Pair<Int, Boolean>? {
        if (intent == null) return null
        val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        if (level < 0 || scale <= 0) return null
        val status = intent.getIntExtra(
            BatteryManager.EXTRA_STATUS, BatteryManager.BATTERY_STATUS_UNKNOWN
        )
        // FULL-while-plugged still reads as "charging" to anyone looking at the
        // icon, and EXTRA_PLUGGED covers the gap where status has not caught up
        // with the cable yet.
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
            status == BatteryManager.BATTERY_STATUS_FULL ||
            intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0
        // scale is 100 on every shipping device, but it is documented as a
        // scale, so treat it as one instead of assuming.
        return Pair((level * 100 / scale).coerceIn(0, 100), charging)
    }

    private fun offer(state: Pair<Int, Boolean>?) {
        if (state == null) return
        val previous = lastSent
        if (state == previous) return
        val flipped = previous == null || previous.second != state.second
        val since = System.currentTimeMillis() - lastSentAt
        if (flipped || since >= MIN_INTERVAL_MS) {
            main.removeCallbacks(flush)
            pending = null
            emit(state)
            return
        }
        // Too soon for a level tick: hold the newest value and let the floor
        // expire. Only the last one in a burst ever goes out.
        pending = state
        main.removeCallbacks(flush)
        main.postDelayed(flush, MIN_INTERVAL_MS - since)
    }

    private fun emit(state: Pair<Int, Boolean>) {
        lastSent = state
        lastSentAt = System.currentTimeMillis()
        onChange(state.first, state.second)
    }

    companion object {
        /** Floor between level updates, whatever the framework does upstream. */
        private const val MIN_INTERVAL_MS = 30_000L
    }
}
