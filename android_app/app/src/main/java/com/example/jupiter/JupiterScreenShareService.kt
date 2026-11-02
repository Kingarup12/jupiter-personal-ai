package com.example.jupiter

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.SystemClock
import android.util.Base64
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.io.ByteArrayOutputStream

/**
 * Live screen sharing (visual input only). Starts only after the user accepted Android's own screen-capture
 * consent dialog, shows an ongoing "Jupiter is seeing your screen" notification with a Stop action, samples at
 * most one frame per second, sends a frame only when the screen visibly changed (or a 5 s keyframe), keeps the
 * frame under the server's size cap and never sends a blank/secure capture. Frames live in memory only and are
 * handed straight to the WebView; nothing is written to storage. It never captures audio. The foreground service
 * also carries the microphone type so the user can keep talking to Jupiter while sharing another app.
 */
class JupiterScreenShareService : Service() {

    interface Listener {
        fun onFrame(json: String)
        fun onState(json: String)
    }

    companion object {
        const val ACTION_START = "com.arupbairagya.jupiter.SCREEN_SHARE_START"
        const val ACTION_STOP = "com.arupbairagya.jupiter.SCREEN_SHARE_STOP"
        const val EXTRA_RESULT_CODE = "screen_share_result_code"
        const val EXTRA_RESULT_DATA = "screen_share_result_data"
        private const val CHANNEL_ID = "jupiter_live_screen_share"
        private const val NOTIFICATION_ID = 4111

        @Volatile var listener: Listener? = null
        @Volatile var active: Boolean = false
            private set

        fun state(status: String, kind: String = "", reason: String = ""): String = JSONObject().apply {
            put("status", status)
            if (kind.isNotEmpty()) put("kind", kind)
            if (reason.isNotEmpty()) put("reason", reason)
        }.toString()
    }

    private val lock = Any()
    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var captureWidth = 0
    private var captureHeight = 0
    private var lastSignature: IntArray? = null
    private var lastSentAt = 0L
    private var blankRun = 0
    private var protectedShown = false
    @Volatile private var generation = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Jupiter Live screen sharing", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shows while Jupiter can see your screen during Live."
                setSound(null, null)
            }
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSharing("user_off")
            return START_NOT_STICKY
        }
        if (intent?.action != ACTION_START || Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            listener?.onState(state("error", kind = "unsupported"))
            stopSelf()
            return START_NOT_STICKY
        }
        val stopIntent = PendingIntent.getService(
            this, 0, Intent(this, JupiterScreenShareService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val notification = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("Jupiter can see your screen")
            .setContentText("Live screen sharing is on. Tap Stop to end it.")
            .setCategory(Notification.CATEGORY_SERVICE)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Stop sharing", stopIntent).build())
            .build()
        // The microphone type keeps Live listening while the user is in the app being shared. It is added only when
        // the microphone permission is already granted (the type cannot be used without it).
        val micGranted = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        val type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or
            (if (micGranted && Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0)
        try {
            startForeground(NOTIFICATION_ID, notification, type)
        } catch (_: Exception) {
            listener?.onState(state("error", kind = "screen_error"))
            stopSelf()
            return START_NOT_STICKY
        }
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, 0)
        @Suppress("DEPRECATION")
        val resultData = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(EXTRA_RESULT_DATA, Intent::class.java)
        } else {
            intent.getParcelableExtra(EXTRA_RESULT_DATA)
        }
        if (resultCode == 0 || resultData == null) {
            listener?.onState(state("error", kind = "permission_denied"))
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }
        startSharing(resultCode, resultData)
        return START_NOT_STICKY
    }

    private fun startSharing(resultCode: Int, resultData: Intent) = synchronized(lock) {
        releaseLocked()
        val gen = ++generation
        try {
            val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            val mp = manager.getMediaProjection(resultCode, resultData) ?: throw IllegalStateException("projection_unavailable")
            val worker = HandlerThread("JupiterScreenShare").apply { start() }
            val workerHandler = Handler(worker.looper)
            thread = worker
            handler = workerHandler
            projection = mp
            // Android 14+: the callback must be registered before the virtual display is created.
            mp.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() { stopSharing("system") }
                override fun onCapturedContentResize(width: Int, height: Int) {
                    workerHandler.post { resize(gen, width, height) }
                }
            }, workerHandler)
            val metrics = resources.displayMetrics
            val (w, h) = ScreenFrameMath.captureSize(metrics.widthPixels, metrics.heightPixels)
            captureWidth = w
            captureHeight = h
            val imageReader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
            reader = imageReader
            virtualDisplay = mp.createVirtualDisplay(
                "JupiterLiveScreenShare", w, h, metrics.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, imageReader.surface, null, workerHandler
            )
            active = true
            lastSignature = null; lastSentAt = 0L; blankRun = 0; protectedShown = false
            listener?.onState(state("on"))
            workerHandler.post(object : Runnable {
                override fun run() {
                    if (gen != generation || !active) return
                    sample()
                    workerHandler.postDelayed(this, ScreenFrameMath.INTERVAL_MS)
                }
            })
        } catch (_: SecurityException) {
            releaseLocked()
            listener?.onState(state("error", kind = "permission_denied"))
            stopSelf()
        } catch (_: Exception) {
            releaseLocked()
            listener?.onState(state("error", kind = "screen_error"))
            stopSelf()
        }
    }

    private fun resize(gen: Long, width: Int, height: Int) = synchronized(lock) {
        if (gen != generation || !active) return@synchronized
        val (w, h) = ScreenFrameMath.captureSize(width, height)
        if (w <= 0 || (w == captureWidth && h == captureHeight)) return@synchronized
        val next = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        virtualDisplay?.resize(w, h, resources.displayMetrics.densityDpi)
        virtualDisplay?.surface = next.surface
        reader?.close()
        reader = next
        captureWidth = w
        captureHeight = h
        lastSignature = null
    }

    private fun sample() {
        val image = try { reader?.acquireLatestImage() } catch (_: Exception) { null } ?: return
        val bitmap: Bitmap
        try {
            val plane = image.planes[0]
            val pixelStride = plane.pixelStride
            val rowPadding = plane.rowStride - pixelStride * image.width
            val padded = Bitmap.createBitmap(image.width + rowPadding / pixelStride, image.height, Bitmap.Config.ARGB_8888)
            padded.copyPixelsFromBuffer(plane.buffer)
            bitmap = if (rowPadding == 0) padded else Bitmap.createBitmap(padded, 0, 0, image.width, image.height).also { padded.recycle() }
        } catch (_: Exception) {
            return
        } finally {
            image.close()
        }
        try {
            val small = Bitmap.createScaledBitmap(bitmap, ScreenFrameMath.SIG_W, ScreenFrameMath.SIG_H, true)
            val pixels = IntArray(ScreenFrameMath.SIG_W * ScreenFrameMath.SIG_H)
            small.getPixels(pixels, 0, ScreenFrameMath.SIG_W, 0, 0, ScreenFrameMath.SIG_W, ScreenFrameMath.SIG_H)
            small.recycle()
            val sig = ScreenFrameMath.signature(pixels)
            if (ScreenFrameMath.isBlank(sig)) {
                blankRun += 1
                lastSignature = null
                if (blankRun >= ScreenFrameMath.BLANK_FRAMES && !protectedShown) {
                    protectedShown = true
                    listener?.onState(state("notice", kind = "protected"))   // secure content stays private
                }
                return
            }
            if (protectedShown) listener?.onState(state("notice", kind = "visible"))
            blankRun = 0; protectedShown = false
            val now = SystemClock.elapsedRealtime()
            if (!ScreenFrameMath.shouldSend(sig, lastSignature, now, lastSentAt)) return
            val encoded = encodeWithinBudget(bitmap) ?: return
            lastSignature = sig
            lastSentAt = now
            listener?.onFrame(JSONObject().apply {
                put("data", encoded.first)
                put("width", encoded.second)
                put("height", encoded.third)
            }.toString())
        } finally {
            bitmap.recycle()
        }
    }

    /** Quality steps down first (text geometry is kept), then the size; never an over-budget frame. */
    private fun encodeWithinBudget(source: Bitmap): Triple<String, Int, Int>? {
        var target = source
        try {
            repeat(4) {
                for (quality in ScreenFrameMath.JPEG_QUALITIES) {
                    val out = ByteArrayOutputStream()
                    target.compress(Bitmap.CompressFormat.JPEG, quality, out)
                    if (out.size() <= ScreenFrameMath.MAX_FRAME_BYTES) {
                        return Triple(Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP), target.width, target.height)
                    }
                }
                val next = Bitmap.createScaledBitmap(target, (target.width * 0.8).toInt().coerceAtLeast(2),
                    (target.height * 0.8).toInt().coerceAtLeast(2), true)
                if (target !== source) target.recycle()
                target = next
            }
            return null
        } finally {
            if (target !== source) target.recycle()
        }
    }

    private fun stopSharing(reason: String) {
        val wasActive = synchronized(lock) {
            val was = active
            releaseLocked()
            was
        }
        if (wasActive) listener?.onState(state("off", reason = reason))
        try { stopForeground(STOP_FOREGROUND_REMOVE) } catch (_: Exception) {}
        stopSelf()
    }

    private fun releaseLocked() {
        generation += 1
        active = false
        try { virtualDisplay?.release() } catch (_: Exception) {}
        virtualDisplay = null
        try { reader?.close() } catch (_: Exception) {}
        reader = null
        val mp = projection
        projection = null
        try { mp?.stop() } catch (_: Exception) {}
        handler?.removeCallbacksAndMessages(null)
        thread?.quitSafely()
        thread = null
        handler = null
        lastSignature = null
    }

    override fun onDestroy() {
        stopSharing("destroyed")
        super.onDestroy()
    }
}
