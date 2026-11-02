package com.example.jupiter

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * Pure sampling decisions for Live screen sharing (no Android types, unit-tested on the JVM). Mirrors the web
 * sampler in live_media.js so both platforms send the same kind of frames: ≤1 fps, only when the screen visibly
 * changed (32×18 luma signature) or as a 5 s keyframe, text-readable resolution, and never a blank/secure capture.
 */
object ScreenFrameMath {
    const val INTERVAL_MS = 1000L
    const val KEYFRAME_MS = 5000L
    const val MAX_EDGE = 1280
    const val SIG_W = 32
    const val SIG_H = 18
    const val CHANGE_THRESHOLD = 1.2
    const val BLANK_FRAMES = 3
    const val MAX_FRAME_BYTES = 240 * 1024
    val JPEG_QUALITIES = intArrayOf(72, 62, 52)

    /** Capture size: the longest edge at most [MAX_EDGE], even dimensions, aspect ratio kept. */
    fun captureSize(width: Int, height: Int, maxEdge: Int = MAX_EDGE): Pair<Int, Int> {
        if (width <= 0 || height <= 0) return 0 to 0
        val scale = minOf(1.0, maxEdge.toDouble() / max(width, height))
        val w = max(2, (width * scale).roundToInt() and 1.inv())
        val h = max(2, (height * scale).roundToInt() and 1.inv())
        return w to h
    }

    /** Luma signature from ARGB pixels already scaled to [SIG_W]×[SIG_H]. */
    fun signature(argb: IntArray): IntArray = IntArray(argb.size) { index ->
        val p = argb[index]
        val r = (p shr 16) and 0xFF
        val g = (p shr 8) and 0xFF
        val b = p and 0xFF
        (r * 0.299 + g * 0.587 + b * 0.114).toInt()
    }

    fun delta(a: IntArray?, b: IntArray?): Double {
        if (a == null || b == null || a.size != b.size || a.isEmpty()) return Double.POSITIVE_INFINITY
        var diff = 0L
        for (i in a.indices) diff += abs(a[i] - b[i])
        return diff.toDouble() / a.size
    }

    /** A FLAG_SECURE / DRM window is captured as a flat dark rectangle. A white document is NOT blank. */
    fun isBlank(sig: IntArray?): Boolean {
        if (sig == null || sig.isEmpty()) return false
        var lo = 255
        var hi = 0
        var sum = 0L
        for (v in sig) { if (v < lo) lo = v; if (v > hi) hi = v; sum += v }
        return hi - lo <= 6 && sum.toDouble() / sig.size < 24
    }

    /** Send this sample? Changed screens go out at once; unchanged ones only as a keyframe. */
    fun shouldSend(sig: IntArray, last: IntArray?, nowMs: Long, lastSentMs: Long): Boolean =
        delta(sig, last) > CHANGE_THRESHOLD || nowMs - lastSentMs >= KEYFRAME_MS

    /** Base64 length of [bytes] raw bytes (what the WebView and the server actually receive). */
    fun base64Length(bytes: Int): Int = ((bytes + 2) / 3) * 4
}
