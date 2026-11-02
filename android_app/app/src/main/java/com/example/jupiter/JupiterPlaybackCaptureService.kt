package com.example.jupiter

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.IBinder
import android.os.Process
import android.util.Base64

/**
 * Captures only Android playback that the source application and platform
 * explicitly allow. Jupiter's own UID is excluded, so its TTS can never be
 * routed back into Live Voice through this digital input.
 */
class JupiterPlaybackCaptureService : Service() {

    interface Listener {
        fun onPcmFrame(base64Pcm: String, sampleRate: Int)
        fun onState(state: String, reason: String = "")
    }

    companion object {
        const val ACTION_START = "com.arupbairagya.jupiter.SYSTEM_AUDIO_START"
        const val ACTION_STOP = "com.arupbairagya.jupiter.SYSTEM_AUDIO_STOP"
        const val EXTRA_RESULT_CODE = "media_projection_result_code"
        const val EXTRA_RESULT_DATA = "media_projection_result_data"
        const val EXTRA_SAMPLE_RATE = "system_audio_sample_rate"
        private const val CHANNEL_ID = "jupiter_live_system_audio"
        private const val NOTIFICATION_ID = 4107

        @Volatile var listener: Listener? = null
        @Volatile var active: Boolean = false
            private set
        /** True until the playback AudioRecord has actually been released. */
        @Volatile var ownsAudioRecord: Boolean = false
            private set
    }

    private val captureLock = Any()
    private var mediaProjection: MediaProjection? = null
    private var audioRecord: AudioRecord? = null
    private var captureThread: Thread? = null
    @Volatile private var captureGeneration = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                "Jupiter Live system audio",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Shows while Jupiter listens to supported audio playing on this device."
                setSound(null, null)
            }
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopCapture("stopped", "")
            stopSelf()
            return START_NOT_STICKY
        }
        if (intent?.action != ACTION_START || Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            listener?.onState("unsupported", "playback_capture_unavailable")
            stopSelf()
            return START_NOT_STICKY
        }

        val notification = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("Jupiter Live Voice")
            .setContentText("Listening to supported device audio")
            .setCategory(Notification.CATEGORY_SERVICE)
            .setOngoing(true)
            .build()
        startForeground(
            NOTIFICATION_ID,
            notification,
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
        )

        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, 0)
        @Suppress("DEPRECATION")
        val resultData = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(EXTRA_RESULT_DATA, Intent::class.java)
        } else {
            intent.getParcelableExtra(EXTRA_RESULT_DATA)
        }
        val requestedRate = intent.getIntExtra(EXTRA_SAMPLE_RATE, 16000)
            .takeIf { it in 8000..48000 } ?: 16000
        if (resultCode == 0 || resultData == null) {
            listener?.onState("error", "permission_denied")
            stopSelf()
            return START_NOT_STICKY
        }

        startCapture(resultCode, resultData, requestedRate)
        return START_NOT_STICKY
    }

    private fun startCapture(resultCode: Int, resultData: Intent, sampleRate: Int) = synchronized(captureLock) {
        stopCaptureLocked()
        if (!JupiterAudioSessionOwner.claimPlaybackCapture()) {
            listener?.onState("error", "capture_busy")
            stopSelf()
            return@synchronized
        }
        try {
            val projectionManager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            val projection = projectionManager.getMediaProjection(resultCode, resultData)
                ?: throw IllegalStateException("media_projection_unavailable")
            projection.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    stopCapture("stopped", "projection_ended")
                    stopSelf()
                }
            }, android.os.Handler(mainLooper))

            val configuration = AudioPlaybackCaptureConfiguration.Builder(projection)
                .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                .addMatchingUsage(AudioAttributes.USAGE_GAME)
                .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                .excludeUid(Process.myUid())
                .build()
            val format = AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(sampleRate)
                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                .build()
            val minimum = AudioRecord.getMinBufferSize(
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            if (minimum <= 0) throw IllegalStateException("audio_record_init_failed")
            val record = AudioRecord.Builder()
                .setAudioFormat(format)
                .setBufferSizeInBytes(maxOf(minimum * 2, 4096))
                .setAudioPlaybackCaptureConfig(configuration)
                .build()
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                record.release()
                throw IllegalStateException("audio_record_init_failed")
            }
            record.startRecording()
            if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                record.release()
                throw IllegalStateException("audio_record_start_failed")
            }

            mediaProjection = projection
            audioRecord = record
            ownsAudioRecord = true
            active = true
            val generation = ++captureGeneration
            listener?.onState("active")
            captureThread = Thread({
                val buffer = ByteArray(4096)
                while (active && captureGeneration == generation) {
                    val count = record.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
                    if (count > 0) {
                        listener?.onPcmFrame(
                            Base64.encodeToString(buffer, 0, count, Base64.NO_WRAP),
                            sampleRate
                        )
                    } else if (count < 0) {
                        listener?.onState("error", "playback_capture_read_failed")
                        break
                    }
                }
            }, "JupiterPlaybackCaptureThread").apply {
                priority = Thread.MAX_PRIORITY
                start()
            }
        } catch (_: SecurityException) {
            listener?.onState("error", "permission_denied")
            stopCaptureLocked()
            stopSelf()
        } catch (_: Exception) {
            listener?.onState("error", "source_not_capturable")
            stopCaptureLocked()
            stopSelf()
        }
    }

    private fun stopCapture(state: String, reason: String) = synchronized(captureLock) {
        val wasActive = active
        stopCaptureLocked()
        if (wasActive || reason.isNotEmpty()) listener?.onState(state, reason)
    }

    private fun stopCaptureLocked() {
        active = false
        captureGeneration += 1
        val staleRecord = audioRecord
        val staleProjection = mediaProjection
        val staleThread = captureThread
        audioRecord = null
        mediaProjection = null
        captureThread = null
        try { if (staleRecord?.recordingState == AudioRecord.RECORDSTATE_RECORDING) staleRecord.stop() } catch (_: Exception) {}
        try { staleThread?.interrupt() } catch (_: Exception) {}
        if (staleThread != null && staleThread !== Thread.currentThread()) {
            try { staleThread.join(500) } catch (_: InterruptedException) { Thread.currentThread().interrupt() }
        }
        try { staleRecord?.release() } catch (_: Exception) {}
        try { staleProjection?.stop() } catch (_: Exception) {}
        ownsAudioRecord = false
        JupiterAudioSessionOwner.releasePlaybackCapture()
    }

    override fun onDestroy() {
        synchronized(captureLock) { stopCaptureLocked() }
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }
}
