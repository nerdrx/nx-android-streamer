package dev.nerdrx.nxandroidstreamer

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.view.MotionEvent
import android.widget.FrameLayout
import org.webrtc.EglBase
import org.webrtc.RendererCommon
import org.webrtc.SurfaceViewRenderer
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * The stage: a SurfaceViewRenderer that letterboxes portrait content on the
 * #0a0a12 void, plus the touch pipeline. Touch normalization is a direct port of
 * web/app.js contentRect(): compute the picture rect from the rotated frame size
 * vs the view bounds, normalize against the PICTURE (not the element), clamp to
 * [0,1] so a finger dragged into a dead bar reports the edge instead of vanishing.
 *
 * The one addition over the web math is Fill/crop: in FILL the content covers the
 * view (scale = max instead of min), so the offsets go negative and clamp handles
 * the cropped edges — same formula, different scale pick.
 *
 * Browsers hand out large pointer ids; the wire wants small slot ids 0..9 mapped
 * to Android touch slots. We keep a free list exactly like app.js.
 */
class StreamView(context: Context) : FrameLayout(context) {

    interface Callbacks {
        fun onTouchDown(id: Int, x: Float, y: Float, eventTimeMs: Long = 0L)
        fun onTouchMove(id: Int, x: Float, y: Float)
        fun onTouchUp(id: Int)
        fun onResolution(width: Int, height: Int)
        fun onOpenSettings()
    }

    val renderer = SurfaceViewRenderer(context)
    private var callbacks: Callbacks? = null
    private val main = Handler(Looper.getMainLooper())

    // rotation-applied frame dimensions (what the eye sees), set from RendererEvents
    private var contentW = 0
    private var contentH = 0

    private var scaling = Scaling.FIT

    // pointerId -> wire slot (0..9)
    private val pointerSlots = HashMap<Int, Int>()
    private val freeSlots = ArrayDeque<Int>().apply { for (i in MAX_TOUCH_IDS - 1 downTo 0) addLast(i) }

    // long-press to open settings mid-stream
    private var downX = 0f
    private var downY = 0f
    // NOTE: there is deliberately no long-press-to-open-settings here. Long
    // press is a first-class Android gesture — icons, text selection, context
    // menus — and swallowing it means the remote device can never receive one.
    // Settings live behind the edge handle instead, which costs the stream
    // nothing.

    init {
        setBackgroundColor(0xFF0A0A12.toInt())
        renderer.layoutParams = LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT)
        addView(renderer)
        isClickable = true
        isFocusable = true
    }

    fun init(eglContext: EglBase.Context) {
        renderer.init(eglContext, object : RendererCommon.RendererEvents {
            override fun onFirstFrameRendered() {}
            override fun onFrameResolutionChanged(videoWidth: Int, videoHeight: Int, rotation: Int) {
                val rot = ((rotation % 360) + 360) % 360
                val rw = if (rot == 90 || rot == 270) videoHeight else videoWidth
                val rh = if (rot == 90 || rot == 270) videoWidth else videoHeight
                main.post {
                    contentW = rw
                    contentH = rh
                    callbacks?.onResolution(rw, rh)
                }
            }
        })
        renderer.setEnableHardwareScaler(true)
        applyScaling()
    }

    fun setCallbacks(cb: Callbacks?) { callbacks = cb }

    fun setScaling(s: Scaling) {
        scaling = s
        applyScaling()
    }

    private fun applyScaling() {
        renderer.setScalingType(
            if (scaling == Scaling.FILL) RendererCommon.ScalingType.SCALE_ASPECT_FILL
            else RendererCommon.ScalingType.SCALE_ASPECT_FIT
        )
    }

    fun release() {
        renderer.release()
    }

    // ---- letterbox math (port of app.js contentRect + normX/normY) ------

    private fun clamp01(v: Float): Float = if (v < 0f) 0f else if (v > 1f) 1f else v

    private fun normX(vx: Float): Float {
        val viewW = width.toFloat()
        val fw = contentW.toFloat()
        val fh = contentH.toFloat()
        val viewH = height.toFloat()
        if (fw <= 0f || fh <= 0f || viewW <= 0f || viewH <= 0f) {
            return clamp01(vx / max(1f, viewW))
        }
        val scale = if (scaling == Scaling.FILL) max(viewW / fw, viewH / fh) else min(viewW / fw, viewH / fh)
        val cw = fw * scale
        val left = (viewW - cw) / 2f
        return clamp01((vx - left) / cw)
    }

    private fun normY(vy: Float): Float {
        val viewW = width.toFloat()
        val fw = contentW.toFloat()
        val fh = contentH.toFloat()
        val viewH = height.toFloat()
        if (fw <= 0f || fh <= 0f || viewW <= 0f || viewH <= 0f) {
            return clamp01(vy / max(1f, viewH))
        }
        val scale = if (scaling == Scaling.FILL) max(viewW / fw, viewH / fh) else min(viewW / fw, viewH / fh)
        val ch = fh * scale
        val top = (viewH - ch) / 2f
        return clamp01((vy - top) / ch)
    }

    // ---- touch slots ----------------------------------------------------

    private fun acquireSlot(pointerId: Int): Int {
        pointerSlots[pointerId]?.let { return it }
        if (freeSlots.isEmpty()) return -1        // >10 fingers: ignore the extras
        val id = freeSlots.removeLast()
        pointerSlots[pointerId] = id
        return id
    }

    private fun releaseSlot(pointerId: Int): Int {
        val id = pointerSlots.remove(pointerId) ?: return -1
        freeSlots.addLast(id)
        return id
    }

    private fun liftEverything() {
        val cb = callbacks
        pointerSlots.values.forEach { cb?.onTouchUp(it) }
        pointerSlots.clear()
        freeSlots.clear()
        for (i in MAX_TOUCH_IDS - 1 downTo 0) freeSlots.addLast(i)
    }

    // ---- touch handling (mirrors app.js pointer* handlers) --------------

    override fun onTouchEvent(event: MotionEvent): Boolean {
        val cb = callbacks ?: return true
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                val idx = event.actionIndex
                downX = event.getX(idx); downY = event.getY(idx)
                val pid = event.getPointerId(idx)
                val slot = acquireSlot(pid)
                if (slot >= 0) cb.onTouchDown(slot, normX(event.getX(idx)), normY(event.getY(idx)), event.eventTime)
            }
            MotionEvent.ACTION_POINTER_DOWN -> {                 // multi-touch is not a long-press
                val idx = event.actionIndex
                val pid = event.getPointerId(idx)
                val slot = acquireSlot(pid)
                if (slot >= 0) cb.onTouchDown(slot, normX(event.getX(idx)), normY(event.getY(idx)), event.eventTime)
            }
            MotionEvent.ACTION_MOVE -> {
                // Historical samples carry every OS-captured point between frames, so a
                // fast swipe arrives as a real curve (app.js getCoalescedEvents).
                val history = event.historySize
                for (p in 0 until event.pointerCount) {
                    val pid = event.getPointerId(p)
                    val slot = pointerSlots[pid] ?: continue
                    for (h in 0 until history) {
                        cb.onTouchMove(slot, normX(event.getHistoricalX(p, h)), normY(event.getHistoricalY(p, h)))
                    }
                    cb.onTouchMove(slot, normX(event.getX(p)), normY(event.getY(p)))
                }
            }
            MotionEvent.ACTION_POINTER_UP -> {
                val idx = event.actionIndex
                val pid = event.getPointerId(idx)
                val slot = releaseSlot(pid)
                if (slot >= 0) cb.onTouchUp(slot)
            }
            MotionEvent.ACTION_UP -> {
                val idx = event.actionIndex
                val pid = event.getPointerId(idx)
                val slot = releaseSlot(pid)
                if (slot >= 0) cb.onTouchUp(slot)
            }
            MotionEvent.ACTION_CANCEL -> {
                // The system stole the gesture (back swipe, call UI): lift on the
                // remote side too, or a finger stays stuck down forever.
                liftEverything()
            }
        }
        return true
    }

    /** Called by the activity when the app backgrounds: never leave fingers down. */
    fun liftAllPointers() = liftEverything()

    companion object {
        private const val MAX_TOUCH_IDS = 10
        private const val TOUCH_SLOP = 24f
    }
}
