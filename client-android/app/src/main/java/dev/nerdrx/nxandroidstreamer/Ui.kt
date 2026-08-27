package dev.nerdrx.nxandroidstreamer

import android.content.Context
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

/** NX design-language colours (deep space void, one violet accent). */
object NxColor {
    const val VOID = 0xFF0A0A12.toInt()
    const val SCRIM = 0xE60A0A12.toInt()
    const val ACCENT = 0xFF7700FF.toInt()
    const val ACCENT_SOFT = 0xFFA45CFF.toInt()
    const val ACCENT_DEEP = 0xFF5E00C7.toInt()
    const val TEXT = 0xFFE8E6F2.toInt()
    const val DIM = 0xFF9C98B4.toInt()
    const val PANEL = 0xF2141222.toInt()
    const val PANEL2 = 0xFF1B1730.toInt()
    const val LINE = 0xFF2A2340.toInt()
    const val WELL = 0xFF0C0A16.toInt()
    const val DANGER = 0xFFFF5470.toInt()
}

object Ui {
    fun dp(ctx: Context, v: Int): Int = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), ctx.resources.displayMetrics
    ).toInt()

    /** A glassy rounded card background. */
    fun cardBg(radius: Float = 18f): GradientDrawable = GradientDrawable().apply {
        cornerRadius = radius
        setColor(NxColor.PANEL)
        setStroke(2, NxColor.LINE)
    }

    fun wellBg(radius: Float = 12f): GradientDrawable = GradientDrawable().apply {
        cornerRadius = radius
        setColor(NxColor.WELL)
        setStroke(2, NxColor.LINE)
    }

    fun primaryBg(radius: Float = 14f): GradientDrawable = GradientDrawable(
        GradientDrawable.Orientation.TL_BR,
        intArrayOf(NxColor.ACCENT_SOFT, NxColor.ACCENT, NxColor.ACCENT_DEEP)
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

    fun sectionHeader(ctx: Context, text: String): TextView = TextView(ctx).apply {
        this.text = text.uppercase()
        setTextColor(NxColor.DIM)
        textSize = 12f
        letterSpacing = 0.12f
        setPadding(dp(ctx, 4), dp(ctx, 18), 0, dp(ctx, 8))
    }

    fun body(ctx: Context, text: String, color: Int = NxColor.TEXT, size: Float = 15f): TextView =
        TextView(ctx).apply {
            this.text = text
            setTextColor(color)
            textSize = size
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

    fun editText(ctx: Context, hint: String, value: String, numeric: Boolean = false): EditText =
        EditText(ctx).apply {
            this.hint = hint
            setText(value)
            setTextColor(NxColor.TEXT)
            setHintTextColor(NxColor.DIM)
            textSize = 15f
            background = wellBg()
            setPadding(dp(ctx, 14), dp(ctx, 12), dp(ctx, 14), dp(ctx, 12))
            inputType = if (numeric) InputType.TYPE_CLASS_NUMBER
            else InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            backgroundTintList = ColorStateList.valueOf(NxColor.LINE)
        }

    fun lp(width: Int, height: Int, topMargin: Int = 0): LinearLayout.LayoutParams =
        LinearLayout.LayoutParams(width, height).apply { this.topMargin = topMargin }

    fun switch(ctx: Context, on: Boolean, onChange: (Boolean) -> Unit): android.widget.Switch =
        android.widget.Switch(ctx).apply {
            isChecked = on
            thumbTintList = ColorStateList(
                arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()),
                intArrayOf(NxColor.ACCENT_SOFT, NxColor.DIM)
            )
            trackTintList = ColorStateList(
                arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf()),
                intArrayOf(NxColor.ACCENT, NxColor.LINE)
            )
            setOnCheckedChangeListener { _, v -> onChange(v) }
        }
}

/**
 * A small segmented control: a row of options, one selected, styled in NX glass.
 */
class Segmented(
    ctx: Context,
    private val options: List<String>,
    private var selectedIndex: Int,
    private val onSelect: (Int) -> Unit
) : LinearLayout(ctx) {

    private val cells = ArrayList<TextView>()

    init {
        orientation = HORIZONTAL
        background = Ui.wellBg(14f)
        val pad = Ui.dp(ctx, 3)
        setPadding(pad, pad, pad, pad)
        options.forEachIndexed { i, label ->
            val cell = TextView(ctx).apply {
                text = label
                gravity = Gravity.CENTER
                textSize = 14f
                setPadding(Ui.dp(ctx, 12), Ui.dp(ctx, 10), Ui.dp(ctx, 12), Ui.dp(ctx, 10))
                isClickable = true
                setOnClickListener { select(i); onSelect(i) }
            }
            val lp = LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
            addView(cell, lp)
            cells.add(cell)
        }
        paint()
    }

    fun select(i: Int) { selectedIndex = i; paint() }

    private fun paint() {
        cells.forEachIndexed { i, cell ->
            if (i == selectedIndex) {
                cell.background = Ui.primaryBg(11f)
                cell.setTextColor(Color.WHITE)
                cell.setTypeface(cell.typeface, android.graphics.Typeface.BOLD)
            } else {
                cell.background = null
                cell.setTextColor(NxColor.DIM)
                cell.setTypeface(android.graphics.Typeface.DEFAULT, android.graphics.Typeface.NORMAL)
            }
        }
    }
}

fun ViewGroup.addSpacer(px: Int) {
    addView(View(context), ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, px))
}
