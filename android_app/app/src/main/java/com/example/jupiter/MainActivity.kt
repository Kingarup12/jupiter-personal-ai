package com.example.jupiter

import android.Manifest
import android.content.ContentValues
import android.content.Intent
import android.content.res.Configuration
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.SystemClock
import android.provider.MediaStore
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.RenderProcessGoneDetail
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.enableEdgeToEdge
import androidx.activity.SystemBarStyle
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

/**
 * Jupiter's Android shell.
 *
 * Security contract for this Activity:
 *  - Jupiter has no package-install capability at all. There is deliberately no
 *    DownloadListener, no DownloadManager use, no FileProvider, and no
 *    ACTION_INSTALL_PACKAGE intent. The only file-save bridge is a bounded,
 *    authenticated custom-TTS MP3 save into the user's Downloads collection. Updates are
 *    delivered by the user opening the trusted versioned Jupiter APK URL in their
 *    browser; the Android Package Installer performs the install.
 *  - Only the official Jupiter deployment is trusted, and only over HTTPS.
 *  - Web content can never navigate the shell to a non-HTTPS scheme, so
 *    intent://, file://, content://, market:// and package: install vectors are
 *    unreachable from remote pages (including iframes).
 */
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.RingtoneManager
import android.media.MediaRecorder
import android.media.projection.MediaProjectionManager
import android.util.Base64
import android.util.Log
import org.json.JSONObject
import java.util.Locale
import java.security.MessageDigest

internal enum class JupiterCaptureKind { NORMAL_STT, LIVE_MIC, PLAYBACK_CAPTURE }

/**
 * The single ownership gate for every Jupiter-owned Android audio resource.
 * Live may intentionally own the microphone and playback capture together;
 * normal STT is exclusive and must wait for both to be released.
 */
internal object JupiterAudioSessionOwner {
    private val lock = Any()
    @Volatile private var microphoneOwner: JupiterCaptureKind? = null
    @Volatile private var playbackCaptureOwned = false
    @Volatile private var generation = 0L

    fun claimMicrophone(kind: JupiterCaptureKind): Boolean = synchronized(lock) {
        if (kind == JupiterCaptureKind.PLAYBACK_CAPTURE) return false
        val current = microphoneOwner
        if (current != null && current != kind) return false
        microphoneOwner = kind
        generation += 1
        true
    }

    fun releaseMicrophone(kind: JupiterCaptureKind): Boolean = synchronized(lock) {
        if (microphoneOwner != kind) return false
        microphoneOwner = null
        generation += 1
        true
    }

    fun claimPlaybackCapture(): Boolean = synchronized(lock) {
        // Playback capture may coexist only with Live's microphone. It must
        // never overlap a normal dictation AudioRecord.
        if (microphoneOwner != null && microphoneOwner != JupiterCaptureKind.LIVE_MIC) return false
        playbackCaptureOwned = true
        generation += 1
        true
    }

    fun releasePlaybackCapture(): Boolean = synchronized(lock) {
        if (!playbackCaptureOwned) return false
        playbackCaptureOwned = false
        generation += 1
        true
    }

    fun ownsPlaybackCapture(): Boolean = playbackCaptureOwned

    fun hasMicrophoneOwner(): Boolean = microphoneOwner != null

    fun state(): String = synchronized(lock) {
        when {
            microphoneOwner == JupiterCaptureKind.LIVE_MIC && playbackCaptureOwned -> "LIVE_MIC+PLAYBACK_CAPTURE"
            microphoneOwner == JupiterCaptureKind.NORMAL_STT -> "NORMAL_STT"
            microphoneOwner == JupiterCaptureKind.LIVE_MIC -> "LIVE_MIC"
            playbackCaptureOwned -> "PLAYBACK_CAPTURE"
            else -> "NONE"
        }
    }
}

class MainActivity : ComponentActivity() {

    companion object {
        /** The only origin Jupiter grants device capabilities to. */
        private const val TRUSTED_HOST = "jupiter.example.invalid"

        /** Remote entry point that the app loads on launch. */
        // DEBUG ONLY: localhost (routed to the host WT backend with `adb reverse tcp:8765 tcp:8765`) is a secure
        // context, so browser APIs such as crypto.randomUUID exist exactly as on the HTTPS production origin.
        // http://10.0.2.2 is an insecure origin and the frontend failed at startup there.
        private val BOOTSTRAP_URL = if (BuildConfig.DEBUG) {
            "http://localhost:8765/mobile/?client=android"
        } else {
            "https://jupiter.example.invalid/mobile/?client=android"
        }

        private fun isTrustedOrigin(uri: Uri?): Boolean {
            if (uri == null) return false
            val originStr = uri.toString().trim()
            if (BuildConfig.DEBUG && uri.scheme == "http" && uri.port == 8765
                && uri.host in setOf("10.0.2.2", "localhost", "127.0.0.1") && uri.userInfo == null) {
                return true
            }
            val scheme = uri.scheme
            if (!"https".equals(scheme, ignoreCase = true)) return false
            val host = uri.host ?: Uri.parse(originStr).host ?: ""
            return host.equals(TRUSTED_HOST, ignoreCase = true) || host.endsWith(".$TRUSTED_HOST", ignoreCase = true)
        }
    }

    private lateinit var webView: WebView
    private val profileImageTypes = setOf("image/jpeg", "image/png", "image/webp")
    private var pendingProfileImage: ValueCallback<Array<Uri>>? = null
    private var profileImagePage: String? = null
    private var profileImagePickerInFlight = false
    private val profileImagePicker = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        // Detach before delivery so a cancelled picker cannot block the next request.
        val callback = pendingProfileImage
        val page = profileImagePage
        pendingProfileImage = null
        profileImagePage = null
        profileImagePickerInFlight = false
        if (callback != null) {
            val samePage = ::webView.isInitialized && page == webView.url && isTrustedProfilePage(page)
            val uri = result.data?.data
            val cancelled = result.resultCode != RESULT_OK
            val valid = samePage && !cancelled && uri != null
                && (result.data?.clipData?.itemCount ?: 1) == 1 && isValidProfileImageUri(uri)
            callback.onReceiveValue(if (valid) arrayOf(uri!!) else null)
            if (samePage && !cancelled && !valid) profilePickerError("Choose a JPEG, PNG, or WebP image up to 5 MB.")
        }
    }

    private fun isTrustedProfilePage(page: String?): Boolean {
        val uri = page?.let(Uri::parse) ?: return false
        if (!isTrustedOrigin(uri)) return false
        // Use parsed origins, not the older debug prefix match, for this new capability.
        return uri.scheme.equals("https", ignoreCase = true) || (BuildConfig.DEBUG
            && uri.scheme == "http" && uri.host in setOf("10.0.2.2", "localhost", "127.0.0.1") && uri.port == 8765)
    }

    private fun isValidProfileImageUri(uri: Uri): Boolean = try {
        val provider = uri.authority?.let { packageManager.resolveContentProvider(it, 0) }
        if (uri.scheme != "content" || provider == null || provider.applicationInfo.uid == applicationInfo.uid
            || contentResolver.getType(uri)?.lowercase(Locale.ROOT) !in profileImageTypes) {
            false
        } else {
            // The system picker grants this one document. Never resolve raw paths,
            // persist the grant, copy private files, or request storage permissions.
            contentResolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { cursor ->
                val column = cursor.getColumnIndex(OpenableColumns.SIZE)
                !cursor.moveToFirst() || column < 0 || cursor.isNull(column)
                    || cursor.getLong(column) in 1..(5L * 1024 * 1024)
            } ?: true // Cloud-backed providers may not know the size; JS + server validate it.
        }
    } catch (_: Exception) { false }

    private fun cancelProfileImageCallback() {
        val callback = pendingProfileImage
        pendingProfileImage = null
        profileImagePage = null
        callback?.onReceiveValue(null)
        // Keep the in-flight guard until its result arrives; never route an old
        // picker's result to a newer request after navigation/cancellation.
    }

    private fun profilePickerError(message: String) {
        android.widget.Toast.makeText(this, message, android.widget.Toast.LENGTH_LONG).show()
    }

    private data class DocumentRequest(
        val callback: ValueCallback<Array<Uri>>, val page: String?,
        val mimeTypes: Set<String>, val extensions: Set<String>, val multiple: Boolean,
    )
    private var pendingDocument: DocumentRequest? = null
    private var documentPickerInFlight = false
    private val documentPicker = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val request = pendingDocument
        pendingDocument = null
        documentPickerInFlight = false
        if (request != null) {
            val samePage = ::webView.isInitialized && request.page == webView.url && isTrustedProfilePage(request.page)
            val uris = mutableListOf<Uri>()
            result.data?.clipData?.let { clip ->
                for (index in 0 until minOf(clip.itemCount, 9)) uris.add(clip.getItemAt(index).uri)
            } ?: result.data?.data?.let { uris.add(it) }
            val selected = uris.distinct()
            val valid = samePage && result.resultCode == RESULT_OK && selected.isNotEmpty()
                && selected.size <= (if (request.multiple) 8 else 1)
                && selected.all { isValidDocumentUri(it, request) }
            request.callback.onReceiveValue(if (valid) selected.toTypedArray() else null)
            if (samePage && result.resultCode == RESULT_OK && !valid) {
                profilePickerError("Choose supported files within Jupiter's attachment limits.")
            }
        }
    }

    private fun isValidDocumentUri(uri: Uri, request: DocumentRequest): Boolean = try {
        val provider = uri.authority?.let { packageManager.resolveContentProvider(it, 0) }
        if (uri.scheme != "content" || provider == null || provider.applicationInfo.uid == applicationInfo.uid) false
        else {
            val mime = contentResolver.getType(uri)?.lowercase(Locale.ROOT).orEmpty()
            contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { cursor ->
                if (!cursor.moveToFirst()) return@use false
                val nameColumn = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                val name = if (nameColumn >= 0) cursor.getString(nameColumn).orEmpty() else ""
                val extension = "." + name.substringAfterLast('.', "").lowercase(Locale.ROOT)
                val allowed = request.mimeTypes.any { it == "*/*" || it == mime || (it.endsWith("/*") && mime.startsWith(it.substringBefore('/') + "/")) }
                    || extension in request.extensions
                val sizeColumn = cursor.getColumnIndex(OpenableColumns.SIZE)
                val maximum = if (mime in setOf("video/mp4", "video/webm", "video/quicktime", "video/x-m4v")) 100L else 10L
                allowed && (sizeColumn < 0 || cursor.isNull(sizeColumn) || cursor.getLong(sizeColumn) in 1..(maximum * 1024 * 1024))
            } ?: false
        }
    } catch (_: Exception) { false }

    private fun cancelDocumentCallback() {
        val request = pendingDocument
        pendingDocument = null
        request?.callback?.onReceiveValue(null)
        // Do not reset in-flight until the original OS result arrives.
    }

    private var pendingAudioRequest: PermissionRequest? = null
    // Live camera: a separate, video-only grant path. The microphone keeps its own unchanged path above.
    private var pendingVideoRequest: PermissionRequest? = null
    private var pendingSystemAudioSampleRate = 16000
    private val audioBridge = JupiterAudioBridge()
    private val downloadBridge = JupiterDownloadBridge()
    private data class PendingDownload(
        val filename: String,
        val mimeType: String,
        val bytes: ByteArray,
        val callback: String,
    )
    private data class PendingDownloadLocation(val accountKey: String, val callback: String)
    @Volatile private var pendingDownload: PendingDownload? = null
    @Volatile private var pendingDownloadLocation: PendingDownloadLocation? = null
    private val systemAudioListener = object : JupiterPlaybackCaptureService.Listener {
        override fun onPcmFrame(base64Pcm: String, sampleRate: Int) {
            runOnUiThread {
                if (::webView.isInitialized) {
                    webView.evaluateJavascript(
                        "window.onJupiterNativeSystemAudioPcmFrame?.('$base64Pcm', $sampleRate);",
                        null
                    )
                }
            }
        }

        override fun onState(state: String, reason: String) {
            runOnUiThread {
                if (::webView.isInitialized) {
                    webView.evaluateJavascript(
                        "window.onJupiterNativeSystemAudioState?.(${JSONObject.quote(state)}, ${JSONObject.quote(reason)});",
                        null
                    )
                }
            }
        }
    }

    inner class JupiterAudioBridge {
        private val audioLock = Any()
        private var audioRecord: AudioRecord? = null
        private var recordingThread: Thread? = null
        @Volatile private var recording = false
        @Volatile private var recordingGeneration = 0L
        @Volatile private var activeSampleRate = 0
        @Volatile private var resumeSampleRate = 0
        @Volatile private var lastErrorCode = "none"
        @Volatile private var pcmFramesDelivered = 0L

        @JavascriptInterface
        fun hasMicrophonePermission(): Boolean {
            return ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        }

        @JavascriptInterface
        fun requestMicrophonePermission() {
            runOnUiThread {
                if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                    webView.evaluateJavascript("window.onJupiterMicrophonePermissionResult?.(true);", null)
                } else {
                    ActivityCompat.requestPermissions(this@MainActivity, arrayOf(Manifest.permission.RECORD_AUDIO), 2)
                }
            }
        }

        @JavascriptInterface
        fun isNativeAudioSupported(): Boolean = true

        @JavascriptInterface
        fun claimAudioSession(kind: String): Boolean {
            return when (kind.trim().uppercase()) {
                "NORMAL_STT" -> JupiterAudioSessionOwner.claimMicrophone(JupiterCaptureKind.NORMAL_STT)
                "LIVE_MIC" -> JupiterAudioSessionOwner.claimMicrophone(JupiterCaptureKind.LIVE_MIC)
                else -> false
            }
        }

        @JavascriptInterface
        fun releaseAudioSession(kind: String): Boolean {
            return when (kind.trim().uppercase()) {
                "NORMAL_STT" -> JupiterAudioSessionOwner.releaseMicrophone(JupiterCaptureKind.NORMAL_STT)
                "LIVE_MIC" -> JupiterAudioSessionOwner.releaseMicrophone(JupiterCaptureKind.LIVE_MIC)
                else -> false
            }
        }

        @JavascriptInterface
        fun getAudioSessionState(): String = JupiterAudioSessionOwner.state()

        @JavascriptInterface
        fun getLastErrorCode(): String = lastErrorCode

        @JavascriptInterface
        fun isRecording(): Boolean = recording && audioRecord?.recordingState == AudioRecord.RECORDSTATE_RECORDING

        @JavascriptInterface
        fun getPcmFramesDelivered(): Long = pcmFramesDelivered

        @JavascriptInterface
        fun getSystemAudioCapability(): String = JSONObject().apply {
            val supported = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
            put("available", supported)
            put("source", if (supported) "android_playback_capture" else "none")
            put(
                "reason",
                if (supported) "" else "System audio from this app can't be captured on this device."
            )
        }.toString()

        @JavascriptInterface
        fun startSystemAudioCapture(targetSampleRate: Int): Boolean {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
                systemAudioListener.onState("unsupported", "playback_capture_unavailable")
                return false
            }
            if (JupiterPlaybackCaptureService.active) {
                systemAudioListener.onState("active")
                return true
            }
            if (JupiterScreenShareService.active) {                     // one MediaProjection at a time
                systemAudioListener.onState("error", "screen_share_active")
                return false
            }
            pendingSystemAudioSampleRate = targetSampleRate.takeIf { it in 8000..48000 } ?: 16000
            runOnUiThread {
                val projectionManager = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
                startActivityForResult(projectionManager.createScreenCaptureIntent(), 3)
            }
            return true
        }

        @JavascriptInterface
        fun stopSystemAudioCapture(): Boolean {
            val intent = Intent(this@MainActivity, JupiterPlaybackCaptureService::class.java).apply {
                action = JupiterPlaybackCaptureService.ACTION_STOP
            }
            try { startService(intent) } catch (_: Exception) { stopService(intent) }
            return true
        }

        @JavascriptInterface
        fun startRecording(targetSampleRate: Int): Boolean {
            if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                lastErrorCode = "permission_denied"
                return false
            }
            if (!JupiterAudioSessionOwner.hasMicrophoneOwner()) {
                lastErrorCode = "audio_session_not_claimed"
                return false
            }
            // ACTION_STOP is handled asynchronously by the Service. Starting a
            // fresh microphone AudioRecord immediately after sending that intent
            // can therefore succeed only nominally while the stale playback
            // AudioRecord still owns the device. Wait for the Jupiter-owned
            // record's real release; the Web tick never participates in startup.
            if (!releasePlaybackCaptureBeforeMicrophone()) {
                lastErrorCode = "audio_capture_busy"
                return false
            }

            return synchronized(audioLock) {
                stopRecordingLocked(clearResumeRequest = true)

                val sampleRate = if (targetSampleRate in 8000..48000) targetSampleRate else 16000
                val channelConfig = AudioFormat.CHANNEL_IN_MONO
                val audioFormat = AudioFormat.ENCODING_PCM_16BIT
                val minBufSize = AudioRecord.getMinBufferSize(sampleRate, channelConfig, audioFormat)
                if (minBufSize <= 0) {
                    lastErrorCode = "audio_record_init_failed"
                    return@synchronized false
                }

                val bufferSize = maxOf(minBufSize * 2, 4096)
                val sources = intArrayOf(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    MediaRecorder.AudioSource.MIC,
                    MediaRecorder.AudioSource.DEFAULT
                )

                var record: AudioRecord? = null
                var sawInitializedSource = false
                for (source in sources) {
                    var candidate: AudioRecord? = null
                    try {
                        candidate = AudioRecord(source, sampleRate, channelConfig, audioFormat, bufferSize)
                        if (candidate.state != AudioRecord.STATE_INITIALIZED) {
                            candidate.release()
                            continue
                        }
                        sawInitializedSource = true
                        candidate.startRecording()
                        if (candidate.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
                            record = candidate
                            break
                        }
                    } catch (_: SecurityException) {
                        lastErrorCode = "permission_denied"
                        try { candidate?.release() } catch (_: Exception) {}
                        return@synchronized false
                    } catch (_: Exception) {
                        // Some Android devices initialize VOICE_RECOGNITION but reject
                        // it when recording begins. Release it and try MIC/DEFAULT.
                    } finally {
                        if (candidate !== record) {
                            try {
                                if (candidate?.recordingState == AudioRecord.RECORDSTATE_RECORDING) candidate.stop()
                            } catch (_: Exception) {}
                            try { candidate?.release() } catch (_: Exception) {}
                        }
                    }
                }

                if (record == null) {
                    lastErrorCode = if (sawInitializedSource) "audio_record_start_failed" else "audio_record_init_failed"
                    Log.w("JupiterNativeAudio", "Native microphone failed: $lastErrorCode")
                    return@synchronized false
                }

                audioRecord = record
                recording = true
                activeSampleRate = sampleRate
                resumeSampleRate = sampleRate
                lastErrorCode = "none"
                pcmFramesDelivered = 0
                val generation = ++recordingGeneration

                recordingThread = Thread({
                    val chunkSamples = 1024
                    val buffer = ShortArray(chunkSamples)
                    val byteBuffer = ByteArray(chunkSamples * 2)

                    while (recording && recordingGeneration == generation && record.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
                        val readCount = record.read(buffer, 0, chunkSamples)
                        if (readCount > 0) {
                            pcmFramesDelivered += 1
                            for (i in 0 until readCount) {
                                val v = buffer[i].toInt()
                                byteBuffer[i * 2] = (v and 0xFF).toByte()
                                byteBuffer[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
                            }
                            val base64Data = Base64.encodeToString(byteBuffer, 0, readCount * 2, Base64.NO_WRAP)
                            runOnUiThread {
                                if (recording && recordingGeneration == generation && audioRecord === record) {
                                    webView.evaluateJavascript("window.onJupiterNativePcmFrame?.('$base64Data', $sampleRate);", null)
                                }
                            }
                        } else if (readCount < 0) {
                            if (recordingGeneration == generation) {
                                lastErrorCode = "native_pcm_read_failed"
                                recording = false
                            }
                            break
                        }
                    }
                }, "JupiterAudioRecordThread").apply {
                    priority = Thread.MAX_PRIORITY
                    start()
                }

                true
            }
        }

        private fun releasePlaybackCaptureBeforeMicrophone(): Boolean {
            if (!JupiterPlaybackCaptureService.ownsAudioRecord && !JupiterAudioSessionOwner.ownsPlaybackCapture()) return true
            val intent = Intent(this@MainActivity, JupiterPlaybackCaptureService::class.java).apply {
                action = JupiterPlaybackCaptureService.ACTION_STOP
            }
            try { startService(intent) } catch (_: Exception) { stopService(intent) }
            val deadline = SystemClock.elapsedRealtime() + 1_200L
            while ((JupiterPlaybackCaptureService.ownsAudioRecord || JupiterAudioSessionOwner.ownsPlaybackCapture()) && SystemClock.elapsedRealtime() < deadline) {
                try { Thread.sleep(20L) } catch (_: InterruptedException) {
                    Thread.currentThread().interrupt()
                    return false
                }
            }
            return !JupiterPlaybackCaptureService.ownsAudioRecord && !JupiterAudioSessionOwner.ownsPlaybackCapture()
        }

        @JavascriptInterface
        fun stopRecording(): Boolean = synchronized(audioLock) {
            stopRecordingLocked(clearResumeRequest = true)
            true
        }

        private fun stopRecordingLocked(clearResumeRequest: Boolean) {
            recording = false
            recordingGeneration += 1
            activeSampleRate = 0
            if (clearResumeRequest) resumeSampleRate = 0

            val staleRecord = audioRecord
            val staleThread = recordingThread
            audioRecord = null
            recordingThread = null

            try {
                if (staleRecord?.recordingState == AudioRecord.RECORDSTATE_RECORDING) staleRecord.stop()
            } catch (_: Exception) {}
            try { staleThread?.interrupt() } catch (_: Exception) {}
            if (staleThread != null && staleThread !== Thread.currentThread()) {
                try { staleThread.join(500) } catch (_: InterruptedException) { Thread.currentThread().interrupt() }
            }
            try { staleRecord?.release() } catch (_: Exception) {}
        }

        fun suspendForLifecycle() = synchronized(audioLock) {
            val rateToResume = if (recording) activeSampleRate else 0
            stopRecordingLocked(clearResumeRequest = false)
            resumeSampleRate = rateToResume
        }

        fun resumeAfterLifecycle() {
            val rate = synchronized(audioLock) { resumeSampleRate }
            if (rate <= 0 || !::webView.isInitialized) return
            val resumed = startRecording(rate)
            runOnUiThread {
                val state = if (resumed) "resumed" else "resume_failed"
                webView.evaluateJavascript(
                    "window.onJupiterNativeAudioState?.('$state', '${getLastErrorCode()}');",
                    null
                )
            }
        }

        fun destroy() {
            synchronized(audioLock) { stopRecordingLocked(clearResumeRequest = true) }
            JupiterAudioSessionOwner.releaseMicrophone(JupiterCaptureKind.NORMAL_STT)
            JupiterAudioSessionOwner.releaseMicrophone(JupiterCaptureKind.LIVE_MIC)
        }
    }

    /**
     * Saves only the already-authenticated custom-TTS MP3 bytes supplied by the
     * frontend. It never reads a path or accepts a URL, so an attachment ID or
     * arbitrary filesystem path cannot be smuggled through this bridge.
     */
    /** Updated on the UI thread for every page load; bridge calls arrive on a background thread. */
    @Volatile private var remindersPageTrusted = false

    /**
     * window.JupiterReminders: native delivery of the signed-in account's own reminders (see JupiterReminders).
     * Only the trusted Jupiter origin may use it; every method is a no-op for any other page.
     */
    inner class JupiterRemindersBridge {
        @JavascriptInterface
        fun isSupported(): Boolean = remindersPageTrusted

        @JavascriptInterface
        fun canNotify(): Boolean = remindersPageTrusted && JupiterReminders.canNotify(this@MainActivity)

        @JavascriptInterface
        fun canScheduleExact(): Boolean = remindersPageTrusted && JupiterReminders.canScheduleExact(this@MainActivity)

        @JavascriptInterface
        fun requestPermission() {
            if (!remindersPageTrusted) return
            runOnUiThread {
                if (Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(
                        this@MainActivity, Manifest.permission.POST_NOTIFICATIONS
                    ) == PackageManager.PERMISSION_GRANTED) {
                    webView.evaluateJavascript(
                        "window.onJupiterReminderPermissionResult?.(${JupiterReminders.canNotify(this@MainActivity)});", null
                    )
                } else {
                    ActivityCompat.requestPermissions(this@MainActivity, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 7)
                }
            }
        }

        @JavascriptInterface
        fun openExactAlarmSettings() {
            if (!remindersPageTrusted || Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return
            runOnUiThread {
                try {
                    startActivity(Intent(android.provider.Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,
                        Uri.parse("package:$packageName")))
                } catch (_: Exception) {
                    // Some devices have no such screen; reminders stay inexact there.
                }
            }
        }

        @JavascriptInterface
        fun sync(rawAccount: String, json: String): Int {
            if (!remindersPageTrusted || json.length > 200_000) return -1
            return JupiterReminders.sync(this@MainActivity, rawAccount, json)
        }

        @JavascriptInterface
        fun firedJson(rawAccount: String): String =
            if (remindersPageTrusted) JupiterReminders.firedJson(this@MainActivity, rawAccount) else "[]"

        @JavascriptInterface
        fun clearFired(rawAccount: String, keysJson: String) {
            if (remindersPageTrusted && keysJson.length <= 20_000) {
                JupiterReminders.clearFired(this@MainActivity, rawAccount, keysJson)
            }
        }
    }

    private val remindersBridge = JupiterRemindersBridge()

    /** Updated on the UI thread for every page load; bridge calls arrive on a background thread. */
    @Volatile private var callsPageTrusted = false

    private fun deliverCallEvent(json: String) {
        if (!::webView.isInitialized) return
        runOnUiThread {
            if (callsPageTrusted) webView.evaluateJavascript("window.onJupiterCallNative?.($json);", null)
        }
    }

    private fun ringtoneEvent(status: String): String =
        JSONObject().put("event", "ringtone").put("status", status)
            .put("state", JupiterCalls.ringtoneState(this)).toString()

    private fun applyRingChoice(choice: RingChoice, uri: Uri?) {
        val previous = JupiterCalls.ringtoneState(this)
        val previousCustom = if (previous.optString("choice") == "custom") {
            getSharedPreferences("jupiter_calls", MODE_PRIVATE).getString("ring_uri", null)
        } else null
        val applied = JupiterCalls.applyChoice(this, choice, uri)
        if (applied && previousCustom != null && previousCustom != uri?.toString()) {
            runCatching {                                   // a replaced custom file is no longer kept readable
                contentResolver.releasePersistableUriPermission(Uri.parse(previousCustom), Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
        }
        deliverCallEvent(ringtoneEvent(if (applied) "saved" else "channel_blocked"))
    }

    private val systemRingtonePicker = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val uri: Uri? = if (Build.VERSION.SDK_INT >= 33) {
            result.data?.getParcelableExtra(RingtoneManager.EXTRA_RINGTONE_PICKED_URI, Uri::class.java)
        } else {
            @Suppress("DEPRECATION") result.data?.getParcelableExtra(RingtoneManager.EXTRA_RINGTONE_PICKED_URI)
        }
        when {
            result.resultCode != RESULT_OK || uri == null -> deliverCallEvent(ringtoneEvent("unchanged"))
            RingtoneManager.isDefault(uri) -> applyRingChoice(RingChoice.PHONE_DEFAULT, null)
            else -> applyRingChoice(RingChoice.SYSTEM, uri)
        }
    }

    private val customRingtonePicker = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri == null) {
            deliverCallEvent(ringtoneEvent("unchanged"))
            return@registerForActivityResult
        }
        val kept = runCatching {
            contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            true
        }.getOrDefault(false)
        if (kept) applyRingChoice(RingChoice.CUSTOM, uri) else deliverCallEvent(ringtoneEvent("file_not_kept"))
    }

    /**
     * window.JupiterCallsNative: incoming Jupiter calls (marker 172, see JupiterCalls). Trusted Jupiter origin only.
     * The page never plays its own ringtone on Android: the system notification is the only ringer, so the phone's
     * silent/vibrate/Do Not Disturb settings always apply and nothing rings twice.
     */
    inner class JupiterCallsBridge {
        @JavascriptInterface
        fun isSupported(): Boolean = callsPageTrusted

        @JavascriptInterface
        fun register(rawAccount: String): String {
            if (!callsPageTrusted) return "{}"
            val registration = JupiterCalls.setAccount(this@MainActivity, rawAccount)
            JupiterPush.refreshToken(this@MainActivity) { token ->
                if (token.isNotEmpty() && token != registration.optString("push_token")) {
                    deliverCallEvent(JSONObject().put("event", "push_token").toString())
                }
            }
            return registration.toString()
        }

        @JavascriptInterface
        fun signOut() {
            if (callsPageTrusted) JupiterCalls.signOut(this@MainActivity)
        }

        @JavascriptInterface
        fun ring(callId: String, reason: String, expiresAtMillis: String): String {
            if (!callsPageTrusted) return "untrusted"
            return JupiterCalls.showIncoming(this@MainActivity, callId, reason, expiresAtMillis.toLongOrNull() ?: 0L)
        }

        @JavascriptInterface
        fun stop(callId: String) {
            if (callsPageTrusted) JupiterCalls.stop(this@MainActivity, callId)
        }

        @JavascriptInterface
        fun takePendingAction(): String = if (callsPageTrusted) JupiterCalls.takePendingAction(this@MainActivity) else "{}"

        @JavascriptInterface
        fun state(): String = if (callsPageTrusted) JupiterCalls.ringtoneState(this@MainActivity).toString() else "{}"

        @JavascriptInterface
        fun requestNotificationPermission() {
            if (!callsPageTrusted) return
            runOnUiThread {
                if (JupiterCalls.hasNotificationPermission(this@MainActivity)) {
                    deliverCallEvent(JSONObject().put("event", "permission")
                        .put("granted", JupiterCalls.canNotify(this@MainActivity)).toString())
                } else {
                    ActivityCompat.requestPermissions(this@MainActivity, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 14)
                }
            }
        }

        private fun openSettings(intent: Intent) = runOnUiThread {
            try {
                startActivity(intent)
            } catch (_: Exception) {
                startActivity(Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:$packageName")))
            }
        }

        @JavascriptInterface
        fun openCallSettings() {
            if (!callsPageTrusted) return
            val channel = JupiterCalls.ensureChannel(this@MainActivity)
            openSettings(if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                Intent(android.provider.Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS)
                    .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, packageName)
                    .putExtra(android.provider.Settings.EXTRA_CHANNEL_ID, channel)
            } else {
                Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))
            })
        }

        @JavascriptInterface
        fun openFullScreenSettings() {
            if (!callsPageTrusted || Build.VERSION.SDK_INT < 34) return
            openSettings(Intent(android.provider.Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
                Uri.parse("package:$packageName")))
        }

        @JavascriptInterface
        fun chooseSystemRingtone() {
            if (!callsPageTrusted) return
            runOnUiThread {
                val current = getSharedPreferences("jupiter_calls", MODE_PRIVATE).getString("ring_uri", null)
                systemRingtonePicker.launch(Intent(RingtoneManager.ACTION_RINGTONE_PICKER)
                    .putExtra(RingtoneManager.EXTRA_RINGTONE_TYPE, RingtoneManager.TYPE_RINGTONE)
                    .putExtra(RingtoneManager.EXTRA_RINGTONE_TITLE, "Jupiter call ringtone")
                    .putExtra(RingtoneManager.EXTRA_RINGTONE_SHOW_DEFAULT, true)
                    .putExtra(RingtoneManager.EXTRA_RINGTONE_SHOW_SILENT, false)
                    .putExtra(RingtoneManager.EXTRA_RINGTONE_EXISTING_URI,
                        current?.let(Uri::parse) ?: android.provider.Settings.System.DEFAULT_RINGTONE_URI))
            }
        }

        @JavascriptInterface
        fun chooseCustomRingtone() {
            if (callsPageTrusted) runOnUiThread { customRingtonePicker.launch(arrayOf("audio/*")) }
        }

        @JavascriptInterface
        fun setRingChoice(key: String) {
            if (!callsPageTrusted) return
            val choice = RingChoice.of(key)
            if (choice == RingChoice.PHONE_DEFAULT || choice == RingChoice.VIBRATE_ONLY || choice == RingChoice.SILENT) {
                runOnUiThread { applyRingChoice(choice, null) }
            }
        }

        @JavascriptInterface
        fun setJupiterTone(toneId: String) {
            if (!callsPageTrusted || JupiterTones.rawName(toneId) == null) return
            runOnUiThread {
                val applied = JupiterCalls.applyJupiterTone(this@MainActivity, toneId)
                deliverCallEvent(ringtoneEvent(if (applied) "saved" else "channel_blocked"))
            }
        }

        @JavascriptInterface
        fun previewRingtone(): Boolean = callsPageTrusted && JupiterCalls.startPreview(this@MainActivity)

        @JavascriptInterface
        fun stopPreview() {
            JupiterCalls.stopPreview()
        }
    }

    private val callsBridge = JupiterCallsBridge()
    private val callEventListener: (String) -> Unit = { deliverCallEvent(it) }

    /** Marker 222: the page's finished replies / questions / task updates -> Android notification while Jupiter is away. */
    @Volatile private var pendingNoticeChat = ""

    inner class JupiterNotifyBridge {
        @JavascriptInterface
        fun show(json: String): Boolean = callsPageTrusted && JupiterNotices.show(this@MainActivity, json)

        @JavascriptInterface
        fun takePendingAction(): String {
            if (!callsPageTrusted) return "{}"
            val chat = pendingNoticeChat
            pendingNoticeChat = ""
            return if (chat.isEmpty()) "{}" else JSONObject().put("action", "open_chat").put("chat", chat).toString()
        }
    }

    private fun handleNoticeIntent(intent: Intent?) {
        val chat = JupiterNotices.chatFrom(intent) ?: return
        if (chat.isEmpty()) return
        if (::webView.isInitialized && callsPageTrusted) {
            val json = JSONObject().put("action", "open_chat").put("chat", chat).toString()
            webView.evaluateJavascript("window.onJupiterNotificationAction?.($json);", null)
        } else {
            pendingNoticeChat = chat                                    // the page asks once it has loaded
        }
    }

    /** Answer from the notification or the full-screen call: the page joins Live once it is signed in. */
    private fun handleCallIntent(intent: Intent?) {
        if (intent?.action != JupiterCalls.ACTION_ANSWER) return
        val callId = intent.getStringExtra(JupiterCalls.EXTRA_CALL_ID).orEmpty()
        if (!Regex("[a-f0-9]{32}").matches(callId)) return
        JupiterCalls.stop(this, callId)
        JupiterCalls.rememberAction(this, "answer", callId)
        deliverCallEvent(JSONObject().put("event", "answer").put("call_id", callId).toString())
        intent.action = null                                        // a rotation/recreate never answers again
    }

    /** Updated on the UI thread for every page load; bridge calls arrive on a background thread. */
    @Volatile private var appActionsPageTrusted = false
    private data class PendingWhatsApp(val recipient: String, val message: String)
    private var pendingWhatsApp: PendingWhatsApp? = null

    private fun reportAppAction(status: String, label: String = "") {
        val json = WhatsAppDraft.resultJson(status, label)
        runOnUiThread {
            if (::webView.isInitialized && appActionsPageTrusted) {
                webView.evaluateJavascript("window.onJupiterAppActionResult?.($json);", null)
            }
        }
    }

    /** Opens WhatsApp through an explicit package intent (the launcher <queries> serve only "open <app>" requests). */
    private fun launchWhatsApp(intent: Intent, status: String, label: String) {
        try {
            startActivity(intent.setPackage(WhatsAppDraft.PACKAGE).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            reportAppAction(status, label)
        } catch (_: android.content.ActivityNotFoundException) {
            reportAppAction("not_installed", label)
        } catch (_: Exception) {
            reportAppAction("error", label)
        }
    }

    private fun confirmThenOpen(title: String, text: String, label: String, open: () -> Unit) {
        android.app.AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(text)
            .setPositiveButton("Open WhatsApp") { _, _ -> open() }
            .setNegativeButton("Cancel") { _, _ -> reportAppAction("cancelled", label) }
            .setOnCancelListener { reportAppAction("cancelled", label) }
            .show()
    }

    private fun shareTextToWhatsApp(message: String, label: String) {
        launchWhatsApp(Intent(Intent.ACTION_SEND).setType("text/plain")
            .putExtra(Intent.EXTRA_TEXT, WhatsAppDraft.sanitizeMessage(message)), "picker_opened", label)
    }

    /** Called with the contact the USER picked in the system picker (no READ_CONTACTS permission is held). */
    private fun onWhatsAppContactPicked(uri: Uri?) {
        val pending = pendingWhatsApp ?: return
        pendingWhatsApp = null
        if (uri == null) {
            reportAppAction("cancelled", pending.recipient)
            return
        }
        var name = ""
        var number = ""
        try {
            contentResolver.query(uri, arrayOf(
                android.provider.ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                android.provider.ContactsContract.CommonDataKinds.Phone.NUMBER,
            ), null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    name = cursor.getString(0).orEmpty()
                    number = cursor.getString(1).orEmpty()
                }
            }
        } catch (_: Exception) {
            // Picker grant missing or provider error: nothing is opened.
        }
        if (number.isBlank()) {
            reportAppAction("no_number", name.ifBlank { pending.recipient })
            return
        }
        val label = name.ifBlank { number }
        val digits = WhatsAppDraft.internationalDigits(number)
        val text = WhatsAppDraft.confirmationText(pending.recipient, name, number, pending.message, digits != null)
        confirmThenOpen("Open WhatsApp with this message?", text, label) {
            if (digits != null) {
                launchWhatsApp(Intent(Intent.ACTION_VIEW, Uri.parse(WhatsAppDraft.clickToChatUri(digits, pending.message))),
                    "prepared", label)
            } else {
                shareTextToWhatsApp(pending.message, label)
            }
        }
    }

    /**
     * window.JupiterAppActions: user-visible WhatsApp hand-off only. Composing/opening is NOT sending; the user taps
     * Send in WhatsApp. No AccessibilityService, no background sending, no other account or number is ever used.
     */
    inner class JupiterAppActionsBridge {
        @JavascriptInterface
        fun isSupported(): Boolean = appActionsPageTrusted

        /** Marker 197: may the desktop phone agent's mascot float over other apps on this phone? */
        @JavascriptInterface
        fun canShowMascot(): Boolean = appActionsPageTrusted && JupiterMascotOverlay.canShow(this@MainActivity)

        @JavascriptInterface
        fun openMascotSettings() {
            if (!appActionsPageTrusted) return
            runOnUiThread {
                try {
                    startActivity(Intent(android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:$packageName")))
                } catch (_: Exception) {
                    // No such screen on this device: the mascot simply stays off.
                }
            }
        }

        @JavascriptInterface
        fun openWhatsApp() {
            if (!appActionsPageTrusted) return
            runOnUiThread {
                launchWhatsApp(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), "opened", "")
            }
        }

        @JavascriptInterface
        fun prepareWhatsAppMessage(json: String) {
            if (!appActionsPageTrusted || json.length > 8_000) return
            val request = try { JSONObject(json) } catch (_: Exception) { return }
            val recipient = request.optString("recipient").trim().take(60)
            val message = WhatsAppDraft.sanitizeMessage(request.optString("message"))
            if (message.isEmpty()) {
                reportAppAction("error", recipient)
                return
            }
            runOnUiThread {
                if (recipient.isEmpty()) {
                    // No contact named: WhatsApp's own picker chooses the chat, after the user confirms the text.
                    confirmThenOpen("Open WhatsApp with this message?",
                        "Message:\n$message\n\nWhatsApp will ask you to choose the chat. You tap Send in WhatsApp — " +
                            "Jupiter can't send it for you and can't confirm whether it was sent.", "") {
                        shareTextToWhatsApp(message, "")
                    }
                    return@runOnUiThread
                }
                // The USER resolves who "$recipient" is in the system contact picker: no guessing between contacts.
                pendingWhatsApp = PendingWhatsApp(recipient, message)
                try {
                    @Suppress("DEPRECATION")
                    startActivityForResult(Intent(Intent.ACTION_PICK,
                        android.provider.ContactsContract.CommonDataKinds.Phone.CONTENT_URI), 11)
                } catch (_: Exception) {
                    pendingWhatsApp = null
                    reportAppAction("error", recipient)
                }
            }
        }
    }

    private val appActionsBridge = JupiterAppActionsBridge()

    /**
     * window.JupiterDevice (marker 199): volume, media keys, opening apps and YouTube/Spotify links for Jupiter's device
     * requests, with real results. Trusted Jupiter page only (same trust as JupiterAppActions).
     */
    inner class JupiterDeviceBridge {
        @JavascriptInterface
        fun isSupported(): Boolean = appActionsPageTrusted

        @JavascriptInterface
        fun capabilities(): String =
            if (appActionsPageTrusted) """{"volume":true,"media":true,"open_app":true}""" else "{}"

        @JavascriptInterface
        fun volume(json: String): String =
            if (appActionsPageTrusted && json.length <= 500) JupiterDeviceActions.volume(this@MainActivity, json)
            else """{"ok":false,"error":"untrusted"}"""

        @JavascriptInterface
        fun mediaKey(action: String): String =
            if (appActionsPageTrusted) JupiterDeviceActions.mediaKey(this@MainActivity, action.take(20))
            else """{"ok":false,"error":"untrusted"}"""

        @JavascriptInterface
        fun openApp(name: String): String =
            if (appActionsPageTrusted) JupiterDeviceActions.openApp(this@MainActivity, name.take(60))
            else """{"ok":false,"error":"untrusted"}"""

        @JavascriptInterface
        fun openUri(uri: String): String =
            if (appActionsPageTrusted) JupiterDeviceActions.openUri(this@MainActivity, uri)
            else """{"ok":false,"error":"untrusted"}"""
    }

    private val deviceBridge = JupiterDeviceBridge()

    /** Read-only installed package identity and a narrowly scoped browser download hand-off. */
    inner class JupiterUpdatesBridge {
        @JavascriptInterface
        fun installedVersion(): String {
            if (!appActionsPageTrusted) return "{}"
            return JSONObject().put("package", packageName)
                .put("version_name", BuildConfig.VERSION_NAME)
                .put("version_code", BuildConfig.VERSION_CODE).toString()
        }

        @JavascriptInterface
        fun openUpdate(rawUrl: String) {
            if (!appActionsPageTrusted || rawUrl.length > 250) return
            val uri = Uri.parse(rawUrl)
            if (uri.scheme != "https" || uri.host != TRUSTED_HOST || uri.port !in listOf(-1, 443)
                || !uri.userInfo.isNullOrEmpty() || uri.query != null || uri.fragment != null
                || !Regex("^/downloads/Jupiter-\\d+\\.\\d+\\.\\d+\\.apk$").matches(uri.path ?: "")) return
            runOnUiThread {
                try { startActivity(Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE)) }
                catch (_: Exception) { /* No browser: leave the update available for a later attempt. */ }
            }
        }
    }

    /** Updated on the UI thread for every page load; bridge calls arrive on a background thread. */
    @Volatile private var screenSharePageTrusted = false

    /** Native screen frames/state go straight to the Live page (memory only; nothing is stored). */
    private val screenShareListener = object : JupiterScreenShareService.Listener {
        override fun onFrame(json: String) {
            runOnUiThread {
                if (::webView.isInitialized && screenSharePageTrusted) {
                    webView.evaluateJavascript("window.onJupiterScreenShareFrame?.($json);", null)
                }
            }
        }

        override fun onState(json: String) {
            runOnUiThread {
                // Sharing ended while Jupiter is in the background: release the microphone exactly as onStop would.
                val ended = json.contains("\"status\":\"off\"") || json.contains("\"status\":\"error\"")
                if (ended && !lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.STARTED)) {
                    audioBridge.suspendForLifecycle()
                }
                if (::webView.isInitialized) webView.evaluateJavascript("window.onJupiterScreenShareState?.($json);", null)
            }
        }
    }

    /**
     * window.JupiterScreenShare: Live screen sharing through Android's MediaProjection. Every start goes through the
     * system consent dialog; only the trusted Jupiter origin may use it. It never captures audio, and it is mutually
     * exclusive with system-audio capture (one projection at a time).
     */
    inner class JupiterScreenShareBridge {
        @JavascriptInterface
        fun isAvailable(): Boolean = screenSharePageTrusted && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q

        @JavascriptInterface
        fun start() {
            if (!isAvailable()) {
                screenShareListener.onState(JupiterScreenShareService.state("error", kind = "unsupported"))
                return
            }
            if (JupiterScreenShareService.active) {
                screenShareListener.onState(JupiterScreenShareService.state("on"))
                return
            }
            if (JupiterPlaybackCaptureService.active) audioBridge.stopSystemAudioCapture()
            JupiterScreenShareService.listener = screenShareListener
            runOnUiThread {
                val projectionManager = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
                startActivityForResult(projectionManager.createScreenCaptureIntent(), 9)
            }
        }

        @JavascriptInterface
        fun stop() {
            stopScreenShare()
        }
    }

    private val screenShareBridge = JupiterScreenShareBridge()

    private fun stopScreenShare() {
        if (!JupiterScreenShareService.active) return
        val intent = Intent(this, JupiterScreenShareService::class.java).apply { action = JupiterScreenShareService.ACTION_STOP }
        try { startService(intent) } catch (_: Exception) { stopService(intent) }
    }

    inner class JupiterDownloadBridge {
        private val maxBytes = 10 * 1024 * 1024
        private val locationPreferences by lazy {
            getSharedPreferences("jupiter_download_locations", MODE_PRIVATE)
        }

        private fun safeFilename(raw: String): String {
            val candidate = raw.substringAfterLast('/').substringAfterLast('\\')
                .replace(Regex("[^A-Za-z0-9._-]"), "_")
                .trim('.', ' ')
            val stem = candidate.ifBlank { "Jupiter_Voice" }
            return if (stem.lowercase(Locale.US).endsWith(".mp3")) stem else "$stem.mp3"
        }

        private fun callbackName(raw: String): String? {
            val value = raw.trim()
            return if (value.matches(Regex("[A-Za-z0-9_]{1,96}"))) value else null
        }

        private fun accountKey(raw: String): String? {
            val normalized = raw.trim().lowercase(Locale.US)
            if (normalized.length !in 3..320 || !normalized.contains('@')) return null
            val digest = MessageDigest.getInstance("SHA-256").digest(normalized.toByteArray(Charsets.UTF_8))
            return digest.joinToString("") { "%02x".format(it) }
        }

        private fun savedTreeUri(accountKey: String): Uri? = locationPreferences
            .getString("tree_$accountKey", "")
            ?.takeIf { it.startsWith("content://") }
            ?.let(Uri::parse)

        private fun treeLabel(uri: Uri): String {
            return try {
                contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
                    if (cursor.moveToFirst()) cursor.getString(0) else null
                }?.takeIf { it.isNotBlank() } ?: "Selected folder"
            } catch (_: Exception) {
                "Selected folder"
            }
        }

        fun getDownloadLocationForKey(key: String): String {
            val tree = savedTreeUri(key)
            return if (tree == null) "Downloads / Jupiter" else treeLabel(tree)
        }

        @JavascriptInterface
        fun getDownloadLocation(rawAccount: String): String {
            val key = accountKey(rawAccount)
            val tree = key?.let(::savedTreeUri)
            return JSONObject().apply {
                put("label", if (key == null) "Downloads / Jupiter" else getDownloadLocationForKey(key))
                put("custom", tree != null)
            }.toString()
        }

        @JavascriptInterface
        fun chooseDownloadLocation(rawAccount: String, callback: String): Boolean {
            val key = accountKey(rawAccount) ?: return false
            val callbackFunction = callbackName(callback) ?: return false
            if (pendingDownloadLocation != null) {
                notifyLocation(callbackFunction, false, "", "location_picker_busy")
                return false
            }
            pendingDownloadLocation = PendingDownloadLocation(key, callbackFunction)
            runOnUiThread {
                try {
                    startActivityForResult(
                        Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
                            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                            addFlags(Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
                        },
                        5,
                    )
                } catch (_: Exception) {
                    pendingDownloadLocation = null
                    notifyLocation(callbackFunction, false, "", "location_picker_unavailable")
                }
            }
            return true
        }

        fun notifyLocation(callback: String, success: Boolean, label: String, reason: String) {
            val callbackFunction = callbackName(callback) ?: return
            runOnUiThread {
                if (!::webView.isInitialized) return@runOnUiThread
                webView.evaluateJavascript(
                    "window[${JSONObject.quote(callbackFunction)}]?.(${success}, ${JSONObject.quote(label)}, ${JSONObject.quote(reason)});",
                    null
                )
            }
        }

        fun notifyDownload(callback: String, success: Boolean, filename: String, reason: String) {
            val callbackFunction = callbackName(callback) ?: return
            runOnUiThread {
                if (!::webView.isInitialized) return@runOnUiThread
                webView.evaluateJavascript(
                    "window[${JSONObject.quote(callbackFunction)}]?.(${success}, ${JSONObject.quote(filename)}, ${JSONObject.quote(reason)});",
                    null
                )
            }
        }

        private fun saveToMediaStore(filename: String, mimeType: String, bytes: ByteArray): Boolean {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return false
            val values = ContentValues().apply {
                put(MediaStore.MediaColumns.DISPLAY_NAME, filename)
                put(MediaStore.MediaColumns.MIME_TYPE, mimeType)
                put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/Jupiter")
                put(MediaStore.MediaColumns.IS_PENDING, 1)
            }
            val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) ?: return false
            return try {
                contentResolver.openOutputStream(uri)?.use { output -> output.write(bytes) }
                    ?: throw IllegalStateException("download_output_unavailable")
                val completed = ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }
                contentResolver.update(uri, completed, null, null)
                true
            } catch (_: Exception) {
                contentResolver.delete(uri, null, null)
                false
            }
        }

        private fun saveToTree(treeUri: Uri, filename: String, mimeType: String, bytes: ByteArray): Boolean {
            return try {
                val parent = DocumentsContract.buildDocumentUriUsingTree(
                    treeUri,
                    DocumentsContract.getTreeDocumentId(treeUri),
                )
                val target = DocumentsContract.createDocument(contentResolver, parent, mimeType, filename)
                    ?: return false
                contentResolver.openOutputStream(target, "w")?.use { output -> output.write(bytes) }
                    ?: throw IllegalStateException("download_output_unavailable")
                true
            } catch (_: Exception) {
                false
            }
        }

        @JavascriptInterface
        fun saveAudioBase64(rawFilename: String, rawMimeType: String, encoded: String, callback: String): Boolean {
            return saveAudioBase64WithOptions(rawFilename, rawMimeType, encoded, "ask", "", callback)
        }

        @JavascriptInterface
        fun saveAudioBase64WithOptions(
            rawFilename: String,
            rawMimeType: String,
            encoded: String,
            behavior: String,
            rawAccount: String,
            callback: String,
        ): Boolean {
            val callbackFunction = callbackName(callback) ?: return false
            if (!rawMimeType.equals("audio/mpeg", ignoreCase = true)) {
                notifyDownload(callbackFunction, false, "", "unsupported_type")
                return false
            }
            val bytes = try {
                Base64.decode(encoded, Base64.DEFAULT)
            } catch (_: IllegalArgumentException) {
                notifyDownload(callbackFunction, false, "", "invalid_audio")
                return false
            }
            if (bytes.isEmpty() || bytes.size > maxBytes) {
                notifyDownload(callbackFunction, false, "", "audio_too_large")
                return false
            }
            val hasId3 = bytes.size >= 3 && bytes[0] == 'I'.code.toByte() && bytes[1] == 'D'.code.toByte() && bytes[2] == '3'.code.toByte()
            val hasMpegFrame = bytes.size >= 2
                && ((bytes[0].toInt() and 0xFF) == 0xFF)
                && ((bytes[1].toInt() and 0xE0) == 0xE0)
            if (!(hasId3 || hasMpegFrame)) {
                notifyDownload(callbackFunction, false, "", "invalid_audio")
                return false
            }
            val filename = safeFilename(rawFilename)
            if (behavior.equals("automatic", ignoreCase = true)) {
                val key = accountKey(rawAccount)
                val tree = key?.let(::savedTreeUri)
                val saved = if (tree != null) {
                    saveToTree(tree, filename, "audio/mpeg", bytes)
                } else {
                    saveToMediaStore(filename, "audio/mpeg", bytes)
                }
                notifyDownload(callbackFunction, saved, filename, if (saved) "" else "download_save_failed")
                return true
            }
            if (pendingDownload != null) {
                notifyDownload(callbackFunction, false, filename, "save_dialog_busy")
                return false
            }
            pendingDownload = PendingDownload(filename, "audio/mpeg", bytes, callbackFunction)
            runOnUiThread {
                try {
                    startActivityForResult(
                        Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
                            addCategory(Intent.CATEGORY_OPENABLE)
                            type = "audio/mpeg"
                            putExtra(Intent.EXTRA_TITLE, filename)
                        },
                        4,
                    )
                } catch (_: Exception) {
                    pendingDownload = null
                    notifyDownload(callbackFunction, false, filename, "save_dialog_unavailable")
                }
            }
            return true
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT)
        )
        super.onCreate(savedInstanceState)
        JupiterPlaybackCaptureService.listener = systemAudioListener

        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)

        webView = WebView(this)
        webView.setBackgroundColor(android.graphics.Color.parseColor("#0F111A"))
        
        ViewCompat.setOnApplyWindowInsetsListener(webView) { v, insets ->
            val insetsType = WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime()
            val systemBars = insets.getInsets(insetsType)
            val params = v.layoutParams as android.view.ViewGroup.MarginLayoutParams
            params.setMargins(systemBars.left, systemBars.top, systemBars.right, systemBars.bottom)
            v.layoutParams = params
            v.requestLayout()
            insets
        }
        
        setContentView(webView)

        // Configure WebView
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            // Jupiter loads its trusted HTTPS frontend. Local file-system and
            // content:// provider reads stay off so a
            // remote page can never pull local files into the WebView.
            allowFileAccess = false
            allowContentAccess = false
            allowFileAccessFromFileURLs = false
            allowUniversalAccessFromFileURLs = false
            mixedContentMode = if (BuildConfig.DEBUG) WebSettings.MIXED_CONTENT_ALWAYS_ALLOW else WebSettings.MIXED_CONTENT_NEVER_ALLOW
        }

        webView.addJavascriptInterface(audioBridge, "JupiterNativeAudio")
        webView.addJavascriptInterface(downloadBridge, "JupiterNativeDownload")
        webView.addJavascriptInterface(remindersBridge, "JupiterReminders")
        webView.addJavascriptInterface(screenShareBridge, "JupiterScreenShare")
        webView.addJavascriptInterface(appActionsBridge, "JupiterAppActions")
        webView.addJavascriptInterface(deviceBridge, "JupiterDevice")
        webView.addJavascriptInterface(JupiterUpdatesBridge(), "JupiterUpdatesNative")
        webView.addJavascriptInterface(callsBridge, "JupiterCallsNative")
        webView.addJavascriptInterface(JupiterNotifyBridge(), "JupiterNotifyNative")
        JupiterReminders.ensureChannel(this)
        JupiterCalls.webListener = callEventListener
        handleCallIntent(intent)
        pendingNoticeChat = JupiterNotices.chatFrom(intent).orEmpty()

        webView.webViewClient = object : WebViewClient() {
            // Marker 222: Android may kill the page's renderer to free memory while Jupiter is in the background.
            // Unhandled, that kills the WHOLE app (so calls, reminders and notifications stop until it is reopened);
            // handled, only the page is rebuilt - calls and reminders keep running natively.
            override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
                runCatching {
                    (view.parent as? android.view.ViewGroup)?.removeView(view)
                    view.destroy()
                }
                if (!isFinishing && !isDestroyed) recreate()
                return true
            }

            override fun onPageStarted(view: WebView, url: String?, favicon: android.graphics.Bitmap?) {
                remindersPageTrusted = isTrustedProfilePage(url)
                stopScreenShare()                                         // sharing never outlives its Live page
                screenSharePageTrusted = isTrustedProfilePage(url)
                appActionsPageTrusted = isTrustedProfilePage(url)
                callsPageTrusted = isTrustedProfilePage(url)
                JupiterCalls.stopPreview()                                // a ringtone preview never outlives its page
                pendingWhatsApp = null                                    // a hand-off never outlives its page
                cancelProfileImageCallback()
                cancelDocumentCallback()
                super.onPageStarted(view, url, favicon)
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                if (BuildConfig.DEBUG && "http".equals(url.scheme, ignoreCase = true) && "10.0.2.2".equals(url.host, ignoreCase = true) && url.port == 8765) {
                    return false
                }
                // HTTPS only. Everything else (http, intent, file, content, market,
                // package, javascript, data, ...) is refused, which removes every
                // web-reachable path to the Android package installer.
                return !"https".equals(url.scheme, ignoreCase = true)
            }
        }

        // Handle WebView permissions (Microphone). Only the trusted Jupiter origin may
        // capture audio, and only audio is ever granted.
        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView,
                filePathCallback: ValueCallback<Array<Uri>>,
                fileChooserParams: FileChooserParams,
            ): Boolean {
                val accepted = fileChooserParams.acceptTypes.flatMap { it.split(',') }
                    .map { it.trim().lowercase(Locale.ROOT) }.filter { it.isNotEmpty() }
                if (!isTrustedProfilePage(view.url) || fileChooserParams.mode !in setOf(FileChooserParams.MODE_OPEN, FileChooserParams.MODE_OPEN_MULTIPLE)) {
                    filePathCallback.onReceiveValue(null)
                    return true
                }
                if (profileImagePickerInFlight || documentPickerInFlight) {
                    filePathCallback.onReceiveValue(null)
                    profilePickerError("Finish or cancel the current file selection first.")
                    return true
                }
                val profileOnly = fileChooserParams.mode == FileChooserParams.MODE_OPEN
                    && accepted.isNotEmpty() && accepted.all { it == "image/*" || it in profileImageTypes }
                if (!profileOnly) {
                    // Chat supplies its explicit accept list; the report input
                    // intentionally accepts opaque files. Only user-selected
                    // document grants are returned, never broad storage access.
                    val extensions = accepted.filter { it.matches(Regex("\\.[a-z0-9]+")) }.toSet()
                    val mimeTypes = accepted.filter { it.matches(Regex("[a-z0-9!#$&^_.+*-]+/[a-z0-9!#$&^_.+*-]+")) }.toSet()
                        .ifEmpty { if (accepted.isEmpty()) setOf("*/*") else emptySet() }
                    if (mimeTypes.isEmpty() && extensions.isEmpty()) {
                        filePathCallback.onReceiveValue(null)
                        return true
                    }
                    val multiple = fileChooserParams.mode == FileChooserParams.MODE_OPEN_MULTIPLE
                    pendingDocument = DocumentRequest(filePathCallback, view.url, mimeTypes, extensions, multiple)
                    documentPickerInFlight = true
                    try {
                        documentPicker.launch(Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                            addCategory(Intent.CATEGORY_OPENABLE)
                            type = "*/*"
                            // Android MIME mapping may omit source-code extensions;
                            // retain a picker route for them and validate results.
                            if (extensions.isEmpty() && "*/*" !in mimeTypes) putExtra(Intent.EXTRA_MIME_TYPES, mimeTypes.toTypedArray())
                            putExtra(Intent.EXTRA_ALLOW_MULTIPLE, multiple)
                            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                        })
                    } catch (_: Exception) {
                        documentPickerInFlight = false
                        cancelDocumentCallback()
                        profilePickerError("The file picker could not open. Please try again.")
                    }
                    return true
                }
                cancelProfileImageCallback()
                pendingProfileImage = filePathCallback
                profileImagePage = view.url
                profileImagePickerInFlight = true
                try {
                    profileImagePicker.launch(Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                        addCategory(Intent.CATEGORY_OPENABLE)
                        type = "image/*"
                        putExtra(Intent.EXTRA_MIME_TYPES, profileImageTypes.toTypedArray())
                        putExtra(Intent.EXTRA_ALLOW_MULTIPLE, false)
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    })
                } catch (_: Exception) {
                    profileImagePickerInFlight = false
                    cancelProfileImageCallback()
                    profilePickerError("The image picker could not open. Please try again.")
                }
                return true
            }

            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    if (!isTrustedOrigin(request.origin)) {
                        request.deny()
                        return@runOnUiThread
                    }
                    val videoOnly = request.resources.filter { it == PermissionRequest.RESOURCE_VIDEO_CAPTURE }
                    if (videoOnly.isNotEmpty() && request.resources.none { it == PermissionRequest.RESOURCE_AUDIO_CAPTURE }) {
                        if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                            request.grant(videoOnly.toTypedArray())
                        } else {
                            pendingVideoRequest?.deny()
                            pendingVideoRequest = request
                            ActivityCompat.requestPermissions(this@MainActivity, arrayOf(Manifest.permission.CAMERA), 5)
                        }
                        return@runOnUiThread
                    }
                    val audioOnly = request.resources.filter { it == PermissionRequest.RESOURCE_AUDIO_CAPTURE }
                    if (audioOnly.isEmpty()) {
                        request.deny()
                    } else {
                        if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                            request.grant(audioOnly.toTypedArray())
                        } else {
                            pendingAudioRequest = request
                            ActivityCompat.requestPermissions(this@MainActivity, arrayOf(Manifest.permission.RECORD_AUDIO), 2)
                        }
                    }
                }
            }
        }

        // Android Back: the page closes its own transient UI first (message menu, text selection); only when it
        // declines does the default Back run. Nothing else changes about navigation.
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (!::webView.isInitialized) {
                    runDefaultBack(this)
                    return
                }
                webView.evaluateJavascript(
                    "(function(){try{return window.__jupiterHandleBack ? window.__jupiterHandleBack() === true : false}catch(e){return false}})()"
                ) { handled ->
                    if (handled != "true") runDefaultBack(this)
                }
            }
        })

        // Load the authoritative remote frontend (no bundled fallback).
        webView.loadUrl(BOOTSTRAP_URL)
    }

    private fun runDefaultBack(callback: OnBackPressedCallback) {
        callback.isEnabled = false
        onBackPressedDispatcher.onBackPressed()
        callback.isEnabled = true
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        // Rotation is a responsive resize, not a navigation event. Keeping this
        // Activity and WebView instance alive preserves the current DOM, chat ID,
        // fetch/stream connection, composer, modal and scroll state.
        if (::webView.isInitialized) {
            ViewCompat.requestApplyInsets(webView)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 2) {
            val granted = grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED
            runOnUiThread {
                if (granted) {
                    pendingAudioRequest?.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                } else {
                    pendingAudioRequest?.deny()
                }
                pendingAudioRequest = null
                webView.evaluateJavascript("window.onJupiterMicrophonePermissionResult?.($granted);", null)
            }
        } else if (requestCode == 5) {
            val granted = grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED
            runOnUiThread {
                if (granted) {
                    pendingVideoRequest?.grant(arrayOf(PermissionRequest.RESOURCE_VIDEO_CAPTURE))
                } else {
                    pendingVideoRequest?.deny()
                }
                pendingVideoRequest = null
                webView.evaluateJavascript("window.onJupiterCameraPermissionResult?.($granted);", null)
            }
        } else if (requestCode == 7) {
            runOnUiThread {
                webView.evaluateJavascript(
                    "window.onJupiterReminderPermissionResult?.(${JupiterReminders.canNotify(this)});", null
                )
            }
        } else if (requestCode == 14) {
            deliverCallEvent(JSONObject().put("event", "permission").put("granted", JupiterCalls.canNotify(this)).toString())
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleCallIntent(intent)
        handleNoticeIntent(intent)
    }

    @Deprecated("Activity result is required by MediaProjection on supported Android versions.")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 4) {
            val pending = pendingDownload
            pendingDownload = null
            if (pending == null) return
            if (resultCode != RESULT_OK || data?.data == null) {
                downloadBridge.notifyDownload(pending.callback, false, pending.filename, "user_cancelled")
                return
            }
            val saved = try {
                contentResolver.openOutputStream(data.data!!)?.use { output -> output.write(pending.bytes) }
                    ?: throw IllegalStateException("download_output_unavailable")
                true
            } catch (_: Exception) {
                false
            }
            downloadBridge.notifyDownload(pending.callback, saved, pending.filename, if (saved) "" else "download_save_failed")
            return
        }
        if (requestCode == 5) {
            val pending = pendingDownloadLocation
            pendingDownloadLocation = null
            if (pending == null) return
            val tree = data?.data
            if (resultCode != RESULT_OK || tree == null) {
                downloadBridge.notifyLocation(pending.callback, false, "", "user_cancelled")
                return
            }
            val saved = try {
                val flags = (data?.flags ?: 0) and (
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                )
                contentResolver.takePersistableUriPermission(tree, flags)
                getSharedPreferences("jupiter_download_locations", MODE_PRIVATE).edit()
                    .putString("tree_${pending.accountKey}", tree.toString())
                    .apply()
                true
            } catch (_: Exception) {
                false
            }
            downloadBridge.notifyLocation(
                pending.callback,
                saved,
                if (saved) downloadBridge.getDownloadLocationForKey(pending.accountKey) else "",
                if (saved) "" else "location_permission_failed",
            )
            return
        }
        if (requestCode == 11) {
            onWhatsAppContactPicked(if (resultCode == RESULT_OK) data?.data else null)
            return
        }
        if (requestCode == 9) {
            if (resultCode != RESULT_OK || data == null) {
                screenShareListener.onState(JupiterScreenShareService.state("error", kind = "permission_denied"))
                return
            }
            JupiterScreenShareService.listener = screenShareListener
            val shareIntent = Intent(this, JupiterScreenShareService::class.java).apply {
                action = JupiterScreenShareService.ACTION_START
                putExtra(JupiterScreenShareService.EXTRA_RESULT_CODE, resultCode)
                putExtra(JupiterScreenShareService.EXTRA_RESULT_DATA, data)
            }
            try {
                ContextCompat.startForegroundService(this, shareIntent)
            } catch (_: Exception) {
                screenShareListener.onState(JupiterScreenShareService.state("error", kind = "screen_error"))
            }
            return
        }
        if (requestCode != 3) return
        if (resultCode != RESULT_OK || data == null) {
            systemAudioListener.onState("error", "permission_denied")
            return
        }
        val serviceIntent = Intent(this, JupiterPlaybackCaptureService::class.java).apply {
            action = JupiterPlaybackCaptureService.ACTION_START
            putExtra(JupiterPlaybackCaptureService.EXTRA_RESULT_CODE, resultCode)
            putExtra(JupiterPlaybackCaptureService.EXTRA_RESULT_DATA, data)
            putExtra(JupiterPlaybackCaptureService.EXTRA_SAMPLE_RATE, pendingSystemAudioSampleRate)
        }
        ContextCompat.startForegroundService(this, serviceIntent)
    }

    override fun onStop() {
        JupiterNotices.foreground = false
        // While the user shares another app's screen, Live keeps listening (the screen-share foreground service
        // carries the microphone type); otherwise the microphone is released in the background as before.
        if (!JupiterScreenShareService.active) audioBridge.suspendForLifecycle()
        if (JupiterPlaybackCaptureService.active) audioBridge.stopSystemAudioCapture()
        super.onStop()
    }

    override fun onStart() {
        super.onStart()
        JupiterNotices.foreground = true
        audioBridge.resumeAfterLifecycle()
    }

    override fun onDestroy() {
        if (JupiterCalls.webListener === callEventListener) JupiterCalls.webListener = null
        JupiterCalls.stopPreview()
        cancelProfileImageCallback()
        cancelDocumentCallback()
        pendingDownload = null
        pendingDownloadLocation = null
        audioBridge.destroy()
        audioBridge.stopSystemAudioCapture()
        stopScreenShare()
        if (JupiterScreenShareService.listener === screenShareListener) JupiterScreenShareService.listener = null
        if (JupiterPlaybackCaptureService.listener === systemAudioListener) {
            JupiterPlaybackCaptureService.listener = null
        }
        super.onDestroy()
    }
}
