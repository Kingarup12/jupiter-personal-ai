package com.example.jupiter

import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.PixelFormat
import android.graphics.RectF
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.view.animation.DecelerateInterpolator

/**
 * Marker 197 (Jarvis HUD on the phone): a small floating Jupiter mascot that follows the phone agent's taps while a
 * cross-app task runs, so the user sees where Jupiter is acting.
 *
 * Driven ONLY by the Jupiter desktop host over ADB (`am broadcast -a com.arupbairagya.jupiter.MASCOT`); the receiver
 * requires android.permission.DUMP, which only the ADB shell and the system hold, so no app can show or move it.
 * It needs "Display over other apps" for Jupiter; without it every signal is ignored. The overlay never takes touches
 * (FLAG_NOT_TOUCHABLE) or focus, and it hides itself after a while without signals.
 */
object JupiterMascotOverlay {
    const val ACTION = "com.arupbairagya.jupiter.MASCOT"
    private const val SIZE_DP = 56
    private const val IDLE_HIDE_MS = 45_000L

    private val main = Handler(Looper.getMainLooper())
    private var view: MascotView? = null
    private var params: WindowManager.LayoutParams? = null
    private var animator: ValueAnimator? = null
    private val autoHide = Runnable { hide() }

    fun canShow(context: Context): Boolean = Settings.canDrawOverlays(context)

    fun signal(context: Context, state: String, x: Int, y: Int) {
        val app = context.applicationContext
        main.post {
            if (!canShow(app)) return@post
            when (state) {
                "hide" -> hide()
                "done" -> { ensure(app); view?.mood = MascotView.Mood.DONE; view?.invalidate(); scheduleHide(2_500L) }
                "tap" -> { ensure(app); view?.mood = MascotView.Mood.WORKING; moveTo(app, x, y); view?.pulse(); scheduleHide(IDLE_HIDE_MS) }
                else -> { ensure(app); view?.mood = MascotView.Mood.WORKING; view?.invalidate(); scheduleHide(IDLE_HIDE_MS) }
            }
        }
    }

    private fun scheduleHide(delay: Long) {
        main.removeCallbacks(autoHide)
        main.postDelayed(autoHide, delay)
    }

    @SuppressLint("RtlHardcoded")
    private fun ensure(context: Context) {
        if (view != null) return
        val size = (SIZE_DP * context.resources.displayMetrics.density).toInt()
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY else @Suppress("DEPRECATION") WindowManager.LayoutParams.TYPE_PHONE
        val layout = WindowManager.LayoutParams(size, size, type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT).apply {
            gravity = Gravity.TOP or Gravity.LEFT
            val metrics = context.resources.displayMetrics
            x = metrics.widthPixels - size - size / 3          // starts top-right, out of the way
            y = metrics.heightPixels / 5
            title = "Jupiter mascot"
        }
        val mascot = MascotView(context)
        try {
            (context.getSystemService(Context.WINDOW_SERVICE) as WindowManager).addView(mascot, layout)
            view = mascot
            params = layout
        } catch (_: Exception) {
            view = null
            params = null
        }
    }

    /** Glide so the mascot's lower-left "hand" lands next to the tap point (screen pixels from ARTEMIS). */
    private fun moveTo(context: Context, x: Int, y: Int) {
        val target = view ?: return
        val layout = params ?: return
        val manager = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val metrics = context.resources.displayMetrics
        val size = layout.width
        val toX = (x + size / 6).coerceIn(0, metrics.widthPixels - size)
        val toY = (y - size + size / 6).coerceIn(0, metrics.heightPixels - size)
        val fromX = layout.x
        val fromY = layout.y
        animator?.cancel()
        animator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 380
            interpolator = DecelerateInterpolator()
            addUpdateListener {
                val f = it.animatedValue as Float
                layout.x = (fromX + (toX - fromX) * f).toInt()
                layout.y = (fromY + (toY - fromY) * f).toInt()
                try { manager.updateViewLayout(target, layout) } catch (_: Exception) { cancel() }
            }
            start()
        }
    }

    fun hide() {
        main.removeCallbacks(autoHide)
        animator?.cancel()
        animator = null
        val current = view ?: return
        try {
            (current.context.getSystemService(Context.WINDOW_SERVICE) as WindowManager).removeView(current)
        } catch (_: Exception) {
        }
        view = null
        params = null
    }

    /** Jupiter's orb with two eyes (the desktop notch's face), a soft glow and a tap ripple. */
    class MascotView(context: Context) : View(context) {
        enum class Mood { WORKING, DONE }

        var mood = Mood.WORKING
        private var ripple = 0f
        private var blinkAt = System.currentTimeMillis() + 2_500
        private val body = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFF0B0F1A.toInt() }
        private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; color = 0xFF4FACFE.toInt() }
        private val eye = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFFFFFFFF.toInt() }
        private val wave = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; color = 0xFFFFB340.toInt() }

        fun pulse() {
            ValueAnimator.ofFloat(0f, 1f).apply {
                duration = 520
                addUpdateListener { ripple = it.animatedValue as Float; invalidate() }
                start()
            }
        }

        override fun onDraw(canvas: Canvas) {
            val w = width.toFloat()
            val r = w * 0.36f
            val cx = w / 2
            val cy = w / 2
            ring.strokeWidth = w * 0.05f
            ring.color = if (mood == Mood.DONE) 0xFF3DDC84.toInt() else 0xFF4FACFE.toInt()
            if (ripple in 0.01f..0.99f) {
                wave.strokeWidth = w * 0.04f
                wave.alpha = ((1f - ripple) * 255).toInt()
                canvas.drawCircle(w * 0.12f, w * 0.88f, w * 0.1f + ripple * w * 0.3f, wave)
            }
            canvas.drawCircle(cx, cy, r, body)
            canvas.drawCircle(cx, cy, r, ring)
            val now = System.currentTimeMillis()
            val blinking = now in blinkAt..(blinkAt + 140)
            if (now > blinkAt + 140) blinkAt = now + 2_500 + (now % 1_500)
            val eyeW = w * 0.09f
            val eyeH = when {
                blinking -> w * 0.025f
                mood == Mood.DONE -> w * 0.08f
                else -> w * 0.2f
            }
            for (dx in listOf(-0.11f, 0.11f)) {
                val ex = cx + dx * w
                canvas.drawRoundRect(RectF(ex - eyeW / 2, cy - eyeH / 2, ex + eyeW / 2, cy + eyeH / 2), eyeW / 2, eyeW / 2, eye)
            }
            postInvalidateDelayed(120)                      // gentle blink loop while visible
        }
    }
}

/** Shell/system-only signal from the Jupiter desktop host (see JupiterMascotOverlay). */
class JupiterMascotReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != JupiterMascotOverlay.ACTION) return
        val state = intent.getStringExtra("state")?.take(16) ?: return
        if (state !in setOf("working", "tap", "done", "hide")) return
        JupiterMascotOverlay.signal(context, state, intent.getIntExtra("x", -1), intent.getIntExtra("y", -1))
    }
}
