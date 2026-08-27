package dev.nerdrx.nxbridge

import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.util.TypedValue
import android.view.Gravity
import android.widget.LinearLayout
import android.widget.TextView

/**
 * Just enough NX design language for two screens, hand-drawn for the same reason
 * the client does it: no XML layouts, no AppCompat, nothing to keep in sync.
 * Palette matches client-android/Ui.kt exactly — deep space void, one violet.
 */
object NxColor {
    const val VOID = 0xFF0A0A12.toInt()
    const val ACCENT = 0xFF7700FF.toInt()
    const val ACCENT_SOFT = 0xFFA45CFF.toInt()
    const val TEXT = 0xFFE8E6F2.toInt()
    const val DIM = 0xFF9C98B4.toInt()
    const val PANEL = 0xF2141222.toInt()
    const val LINE = 0xFF2A2340.toInt()
    const val DANGER = 0xFFFF5470.toInt()
    const val OK = 0xFF4CE0A8.toInt()
}

object NxUi {
    fun dp(ctx: Context, v: Int): Int = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), ctx.resources.displayMetrics
    ).toInt()

    fun cardBg(radius: Float = 18f): GradientDrawable = GradientDrawable().apply {
        cornerRadius = radius
        setColor(NxColor.PANEL)
        setStroke(2, NxColor.LINE)
    }

    fun primaryBg(radius: Float = 14f): GradientDrawable = GradientDrawable(
        GradientDrawable.Orientation.TL_BR,
        intArrayOf(NxColor.ACCENT_SOFT, NxColor.ACCENT)
    ).apply { cornerRadius = radius }

    fun outlineBg(radius: Float = 14f, stroke: Int = NxColor.LINE): GradientDrawable =
        GradientDrawable().apply {
            cornerRadius = radius
            setColor(Color.TRANSPARENT)
            setStroke(3, stroke)
        }

    fun title(ctx: Context, text: String, size: Float = 22f): TextView = TextView(ctx).apply {
        this.text = text
        setTextColor(NxColor.TEXT)
        textSize = size
        setTypeface(typeface, android.graphics.Typeface.BOLD)
    }

    fun body(ctx: Context, text: String, color: Int = NxColor.TEXT, size: Float = 15f): TextView =
        TextView(ctx).apply {
            this.text = text
            setTextColor(color)
            textSize = size
        }

    fun mono(ctx: Context, text: String, color: Int = NxColor.DIM): TextView =
        TextView(ctx).apply {
            this.text = text
            setTextColor(color)
            textSize = 12f
            typeface = android.graphics.Typeface.MONOSPACE
        }

    fun primaryButton(ctx: Context, text: String, onClick: () -> Unit): TextView =
        TextView(ctx).apply {
            this.text = text
            setTextColor(Color.WHITE)
            textSize = 16f
            gravity = Gravity.CENTER
            setTypeface(typeface, android.graphics.Typeface.BOLD)
            background = primaryBg()
            setPadding(dp(ctx, 20), dp(ctx, 14), dp(ctx, 20), dp(ctx, 14))
            isClickable = true
            setOnClickListener { onClick() }
        }

    fun secondaryButton(
        ctx: Context,
        text: String,
        stroke: Int = NxColor.LINE,
        textColor: Int = NxColor.TEXT,
        onClick: () -> Unit
    ): TextView = TextView(ctx).apply {
        this.text = text
        setTextColor(textColor)
        textSize = 15f
        gravity = Gravity.CENTER
        background = outlineBg(stroke = stroke)
        setPadding(dp(ctx, 16), dp(ctx, 12), dp(ctx, 16), dp(ctx, 12))
        isClickable = true
        setOnClickListener { onClick() }
    }

    fun wide(): LinearLayout.LayoutParams = LinearLayout.LayoutParams(
        LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
    )

    fun spacer(ctx: Context, px: Int): android.view.View =
        android.view.View(ctx).apply {
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, px
            )
        }
}
