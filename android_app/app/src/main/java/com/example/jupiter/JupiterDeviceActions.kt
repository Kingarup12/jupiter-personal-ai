package com.example.jupiter

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.SystemClock
import android.view.KeyEvent
import org.json.JSONObject
import kotlin.math.roundToInt

/**
 * Marker 199: the phone side of Jupiter's device requests ("set my volume to 10%", "open WhatsApp", "next song",
 * "play Kesariya on YouTube"). Real Android actions with real results, so Jupiter never claims something it did not do.
 *
 * Only media volume (what music, videos and Jupiter's voice use) is changed; apps are only OPENED through their own
 * launcher entry (no arguments, no other app's data); links are limited to YouTube and Spotify.
 */
object JupiterDeviceActions {
    private val KNOWN_PACKAGES = mapOf(
        "chrome" to "com.android.chrome", "google chrome" to "com.android.chrome",
        "youtube" to "com.google.android.youtube", "youtube music" to "com.google.android.apps.youtube.music",
        "whatsapp" to "com.whatsapp", "spotify" to "com.spotify.music", "gmail" to "com.google.android.gm",
        "maps" to "com.google.android.apps.maps", "google maps" to "com.google.android.apps.maps",
        "play store" to "com.android.vending", "photos" to "com.google.android.apps.photos",
        "gallery" to "com.google.android.apps.photos", "instagram" to "com.instagram.android",
        "telegram" to "org.telegram.messenger", "calendar" to "com.google.android.calendar",
        "contacts" to "com.google.android.contacts", "messages" to "com.google.android.apps.messaging",
        "phone app" to "com.google.android.dialer", "dialer" to "com.google.android.dialer",
        "clock" to "com.google.android.deskclock", "alarms" to "com.google.android.deskclock",
        "calculator" to "com.google.android.calculator", "calc" to "com.google.android.calculator",
        "google" to "com.google.android.googlequicksearchbox", "files" to "com.google.android.apps.nbu.files",
        "my files" to "com.google.android.apps.nbu.files", "camera app" to "com.android.camera2",
    )
    private val SAFE_URI = Regex("^(?:https://(?:www\\.|m\\.|music\\.)?youtube\\.com/|https://open\\.spotify\\.com/|spotify:search:)")

    private fun result(vararg pairs: Pair<String, Any?>): String = JSONObject(pairs.toMap()).toString()

    private fun normalize(name: String): String = name.lowercase()
        .replace(Regex("[^\\p{L}\\p{N}+ ]"), " ")
        .replace(Regex("\\b(?:the|my|app|application)\\b"), " ")
        .trim().replace(Regex("\\s+"), " ")

    /** {"delta_percent"?, "set_percent"?, "mute"?} -> {"ok","scope":"system","before","after","muted"}. */
    fun volume(context: Context, json: String): String {
        val request = try { JSONObject(json) } catch (_: Exception) { return result("ok" to false, "error" to "bad_request") }
        val audio = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            ?: return result("ok" to false, "error" to "audio_unavailable")
        val stream = AudioManager.STREAM_MUSIC
        val max = audio.getStreamMaxVolume(stream).coerceAtLeast(1)
        val beforeLevel = audio.getStreamVolume(stream)
        val before = (beforeLevel * 100.0 / max).roundToInt()
        return try {
            when {
                request.has("set_percent") && !request.isNull("set_percent") -> {
                    val pct = request.optDouble("set_percent").coerceIn(0.0, 100.0)
                    audio.setStreamVolume(stream, (pct * max / 100.0).roundToInt(), AudioManager.FLAG_SHOW_UI)
                }
                request.has("delta_percent") && !request.isNull("delta_percent") -> {
                    val pct = (before + request.optDouble("delta_percent")).coerceIn(0.0, 100.0)
                    audio.setStreamVolume(stream, (pct * max / 100.0).roundToInt(), AudioManager.FLAG_SHOW_UI)
                }
            }
            if (request.has("mute") && !request.isNull("mute")) {
                audio.adjustStreamVolume(stream, if (request.optBoolean("mute")) AudioManager.ADJUST_MUTE
                    else AudioManager.ADJUST_UNMUTE, AudioManager.FLAG_SHOW_UI)
            }
            val after = (audio.getStreamVolume(stream) * 100.0 / max).roundToInt()
            val muted = Build.VERSION.SDK_INT >= 23 && audio.isStreamMute(stream)
            result("ok" to true, "scope" to "system", "app" to "", "before" to before, "after" to after, "muted" to muted)
        } catch (_: SecurityException) {
            result("ok" to false, "error" to "do_not_disturb")          // the phone's Do Not Disturb blocks changes
        }
    }

    fun mediaKey(context: Context, action: String): String {
        val code = when (action) {
            "play_pause" -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
            "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
            "previous" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
            "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
            else -> return result("ok" to false, "error" to "unknown_media_action")
        }
        val audio = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            ?: return result("ok" to false, "error" to "audio_unavailable")
        val now = SystemClock.uptimeMillis()
        audio.dispatchMediaKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_DOWN, code, 0))
        audio.dispatchMediaKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_UP, code, 0))
        return result("ok" to true, "action" to action)
    }

    /** Opens an installed app by its spoken name: known package first, then the best launcher label match. */
    fun openApp(context: Context, name: String): String {
        val wanted = normalize(name).take(60)
        if (wanted.isEmpty()) return result("ok" to false, "error" to "empty_app", "app" to name)
        val pm = context.packageManager
        KNOWN_PACKAGES[wanted]?.let { pkg ->
            pm.getLaunchIntentForPackage(pkg)?.let { intent ->
                return launch(context, intent, pm.labelOf(pkg) ?: name)
            }
        }
        val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val apps = pm.queryIntentActivities(launcher, 0)
        var best: Pair<Int, android.content.pm.ResolveInfo>? = null
        for (info in apps) {
            val label = normalize(info.loadLabel(pm).toString())
            if (label.isEmpty() || info.activityInfo.packageName == context.packageName) continue
            val rank = when {
                label == wanted -> 0
                label.startsWith(wanted) -> 1
                wanted.split(" ").all { it in label.split(" ") } -> 2
                else -> continue
            }
            if (best == null || rank < best.first) best = rank to info
        }
        val match = best?.second ?: return result("ok" to false, "error" to "not_found", "app" to name)
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
            .setClassName(match.activityInfo.packageName, match.activityInfo.name)
        return launch(context, intent, match.loadLabel(pm).toString())
    }

    fun openUri(context: Context, uri: String): String {
        if (!SAFE_URI.containsMatchIn(uri) || uri.length > 600) return result("ok" to false, "error" to "unsafe_target")
        return launch(context, Intent(Intent.ACTION_VIEW, Uri.parse(uri)), if (uri.contains("spotify")) "Spotify" else "YouTube")
    }

    private fun launch(context: Context, intent: Intent, label: String): String = try {
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        result("ok" to true, "app" to label)
    } catch (_: Exception) {
        result("ok" to false, "error" to "open_failed", "app" to label)
    }

    private fun PackageManager.labelOf(pkg: String): String? = try {
        getApplicationLabel(getApplicationInfo(pkg, 0)).toString()
    } catch (_: Exception) {
        null
    }
}
