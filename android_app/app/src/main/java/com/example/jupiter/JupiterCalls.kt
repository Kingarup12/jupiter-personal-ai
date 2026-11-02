package com.example.jupiter

import android.Manifest
import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.Ringtone
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.Person
import androidx.core.graphics.drawable.IconCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.lang.ref.WeakReference
import java.net.HttpURLConnection
import java.net.URL
import java.security.SecureRandom

/**
 * Native side of Jupiter in-app calls (marker 172). Not a telephone call: an invitation to join Live Conversation.
 *
 * Ringing is a real incoming-call notification (CallStyle, category CALL) on a dedicated channel whose sound is the
 * phone's own ringtone by default (Settings.System.DEFAULT_RINGTONE_URI, which follows whatever ringtone the phone
 * uses) played on the RING stream. Because the system plays it, the phone's ringer switch, ring volume and Do Not
 * Disturb always apply; on top of that [CallRingerPolicy] marks the notification silent whenever the phone is on
 * silent or DND blocks calls, and the channel only vibrates when the phone is on vibrate. Jupiter never plays a second
 * copy of the sound from the WebView, never changes the phone's ringtone/volume/ringer/DND, and never answers, opens
 * the microphone, camera or screen capture by itself.
 *
 * Full-screen presentation is requested but the OS decides: on Android 14+ it needs the user's "full-screen
 * notifications" permission, otherwise it is a heads-up notification with Answer/Decline. Answering always requires
 * the phone to be unlocked (Live and chat content are private).
 */
internal object JupiterCalls {
    const val ACTION_ANSWER = "com.arupbairagya.jupiter.CALL_ANSWER"
    const val ACTION_SHOW = "com.arupbairagya.jupiter.CALL_SHOW"
    const val ACTION_DECLINE = "com.arupbairagya.jupiter.CALL_DECLINE"
    const val ACTION_EXPIRE = "com.arupbairagya.jupiter.CALL_EXPIRE"
    const val EXTRA_CALL_ID = "call_id"
    const val EXTRA_REASON = "reason"
    private const val PREFS = "jupiter_calls"
    private const val CHANNEL_PREFIX = "jupiter_calls_v"
    const val MISSED_CHANNEL_ID = "jupiter_missed_calls"
    private const val TRUSTED_ORIGIN = "https://jupiter.example.invalid"
    private const val DEBUG_ORIGIN = "http://localhost:8765"
    private val CALL_ID = Regex("[a-f0-9]{32}")
    private val VIBRATION = longArrayOf(0, 900, 700, 900, 700)

    private val seen = SeenCalls()
    @Volatile private var seenRestored = false
    @Volatile private var preview: Ringtone? = null
    @Volatile var incomingUi: WeakReference<IncomingCallActivity>? = null
    /** The WebView's listener (MainActivity) for state changes made natively (decline from the notification etc.). */
    @Volatile var webListener: ((String) -> Unit)? = null

    fun backendOrigin(): String = if (BuildConfig.DEBUG) DEBUG_ORIGIN else TRUSTED_ORIGIN

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun randomToken(bytes: Int): String {
        val buffer = ByteArray(bytes).also { SecureRandom().nextBytes(it) }
        return buffer.joinToString("") { "%02x".format(it) }
    }

    // ── device identity (per install; the secret rotates on every account change) ─────────────────────────────
    fun deviceId(context: Context): String {
        val preferences = prefs(context)
        return preferences.getString("device_id", null) ?: randomToken(16).also {
            preferences.edit().putString("device_id", it).apply()
        }
    }

    private fun deviceSecret(context: Context): String {
        val preferences = prefs(context)
        return preferences.getString("device_secret", null) ?: randomToken(24).also {
            preferences.edit().putString("device_secret", it).apply()
        }
    }

    fun accountKey(context: Context): String = prefs(context).getString("account", "") ?: ""

    /** Called by the signed-in trusted page. Switching accounts rotates the device secret and drops all call state. */
    @Synchronized
    fun setAccount(context: Context, rawAccount: String): JSONObject {
        val account = JupiterReminders.accountKey(rawAccount)
        val preferences = prefs(context)
        if (account.isEmpty()) {
            signOut(context)
            return JSONObject().put("ok", false)
        }
        if (account != preferences.getString("account", "")) {
            cancelAll(context)
            preferences.edit().putString("account", account).putString("device_secret", randomToken(24))
                .remove("pending_action").remove("seen").apply()
        }
        return registration(context)
    }

    fun registration(context: Context): JSONObject = JSONObject().apply {
        put("ok", true)
        put("device_id", deviceId(context))
        put("device_secret", deviceSecret(context))
        put("platform", "android")
        put("push_token", prefs(context).getString("push_token", "") ?: "")
        put("push_available", JupiterPush.available(context))
        put("label", (Build.MODEL ?: "Android").take(40))
    }

    @Synchronized
    fun signOut(context: Context) {
        cancelAll(context)
        prefs(context).edit().putString("account", "").putString("device_secret", randomToken(24))
            .remove("pending_action").remove("seen").apply()
    }

    fun savePushToken(context: Context, token: String) {
        prefs(context).edit().putString("push_token", token.take(4096)).putBoolean("push_token_dirty", true).apply()
    }

    // ── ringtone choice + channel (a channel's sound is fixed once created, so a new choice gets a new channel) ──
    fun choice(context: Context): RingChoice = RingChoice.of(prefs(context).getString("ring_choice", "jupiter"))

    private fun toneId(context: Context): String = prefs(context).getString("ring_tone", null) ?: JupiterTones.DEFAULT

    /**
     * Marker 219 (owner): Jupiter Master is the default ringtone. Applied once to a device where nobody ever chose a
     * ringtone (the old default followed the phone's ringtone); an explicit choice - phone ringtone, file, vibrate,
     * silent or the other Jupiter tone - is never changed, and a channel blocked in Android settings stays blocked.
     */
    private fun applyDefaultTone(context: Context) {
        val prefs = prefs(context)
        if (prefs.contains("ring_choice") || prefs.getBoolean("default_tone_applied", false)) return
        prefs.edit().putString("ring_tone", toneId(context)).putBoolean("default_tone_applied", true).apply()
        applyChoice(context, RingChoice.JUPITER, null)
    }

    private fun desiredSound(context: Context): Uri? = when (choice(context)) {
        RingChoice.PHONE_DEFAULT -> Settings.System.DEFAULT_RINGTONE_URI
        RingChoice.SYSTEM, RingChoice.CUSTOM -> prefs(context).getString("ring_uri", null)?.let(Uri::parse)
            ?: Settings.System.DEFAULT_RINGTONE_URI
        RingChoice.JUPITER -> JupiterTones.rawName(toneId(context))
            ?.let { Uri.parse("android.resource://${context.packageName}/raw/$it") } ?: Settings.System.DEFAULT_RINGTONE_URI
        RingChoice.VIBRATE_ONLY, RingChoice.SILENT -> null
    }

    /** Select one of Jupiter's own ringtones (an explicit choice in Jupiter's settings). */
    fun applyJupiterTone(context: Context, toneId: String): Boolean {
        if (JupiterTones.rawName(toneId) == null) return false
        prefs(context).edit().putString("ring_tone", toneId).apply()
        return applyChoice(context, RingChoice.JUPITER, null)
    }

    private fun channelId(context: Context) = CHANNEL_PREFIX + prefs(context).getInt("channel_version", 1)

    /** Never overrides what the user changed in Android settings: an existing channel is kept as it is. */
    fun ensureChannel(context: Context): String {
        applyDefaultTone(context)
        val id = channelId(context)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return id
        val manager = context.getSystemService(NotificationManager::class.java) ?: return id
        if (manager.getNotificationChannel(id) == null) createChannel(context, manager, id)
        if (manager.getNotificationChannel(MISSED_CHANNEL_ID) == null) {
            manager.createNotificationChannel(
                NotificationChannel(MISSED_CHANNEL_ID, "Missed Jupiter calls", NotificationManager.IMPORTANCE_LOW).apply {
                    description = "Jupiter calls you didn't answer"
                    lockscreenVisibility = Notification.VISIBILITY_PRIVATE
                }
            )
        }
        return id
    }

    private fun createChannel(context: Context, manager: NotificationManager, id: String) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val sound = desiredSound(context)
        val channel = NotificationChannel(id, "Incoming Jupiter calls", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Jupiter calling you inside the app (not a phone call). Follows your phone's silent, " +
                "vibrate and Do Not Disturb settings."
            lockscreenVisibility = Notification.VISIBILITY_PRIVATE
            setSound(sound, AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)          // RING stream: the ringer switch applies
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build())
            val vibrate = choice(context) != RingChoice.SILENT
            enableVibration(vibrate)
            if (vibrate) vibrationPattern = VIBRATION
            setShowBadge(true)
        }
        manager.createNotificationChannel(channel)
        prefs(context).edit().putString("channel_sound", sound?.toString() ?: "").apply()
    }

    /**
     * An explicit choice in Jupiter's settings (never automatic). Returns false when the user blocked the call
     * channel in Android settings: Jupiter will not silently re-enable a blocked channel by recreating it.
     */
    @Synchronized
    fun applyChoice(context: Context, choice: RingChoice, uri: Uri?): Boolean {
        val manager = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.getSystemService(NotificationManager::class.java)
        } else null
        val oldId = channelId(context)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val old = manager?.getNotificationChannel(oldId)
            if (old != null && old.importance == NotificationManager.IMPORTANCE_NONE) return false
        }
        val editor = prefs(context).edit().putString("ring_choice", choice.key)
            .putInt("channel_version", prefs(context).getInt("channel_version", 1) + 1)
        if (uri != null) editor.putString("ring_uri", uri.toString())
        else if (choice == RingChoice.PHONE_DEFAULT) editor.remove("ring_uri")
        editor.apply()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && manager != null) {
            manager.deleteNotificationChannel(oldId)
            createChannel(context, manager, channelId(context))
        }
        return true
    }

    fun ringtoneState(context: Context): JSONObject {
        val selected = choice(context)
        val uri = desiredSound(context)
        val label = when (selected) {
            RingChoice.PHONE_DEFAULT -> "Phone ringtone"
            RingChoice.VIBRATE_ONLY -> "Vibrate only"
            RingChoice.SILENT -> "Silent"
            RingChoice.JUPITER -> JupiterTones.label(toneId(context)) ?: "Jupiter tone"
            else -> runCatching { RingtoneManager.getRingtone(context, uri)?.getTitle(context) }.getOrNull()
                ?: if (selected == RingChoice.CUSTOM) "Custom sound" else "System ringtone"
        }
        var systemControlled = false
        var blocked = false
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.getSystemService(NotificationManager::class.java)?.getNotificationChannel(channelId(context))?.let {
                blocked = it.importance == NotificationManager.IMPORTANCE_NONE
                systemControlled = (it.sound?.toString() ?: "") != (prefs(context).getString("channel_sound", "") ?: "")
            }
        }
        val audio = context.getSystemService(AudioManager::class.java)
        val ringer = audio?.ringerMode ?: CallRingerPolicy.RINGER_NORMAL
        return JSONObject().apply {
            put("choice", selected.key)
            put("label", label.take(60))
            put("system_controlled", systemControlled)          // the user changed the sound in Android settings
            put("channel_blocked", blocked)
            put("tone", if (selected == RingChoice.JUPITER) toneId(context) else "")
            put("phone_ringer", CallRingerPolicy.ringerLabel(ringer))
            put("do_not_disturb", interruptionFilter(context) !in setOf(CallRingerPolicy.FILTER_ALL, CallRingerPolicy.FILTER_UNKNOWN))
            put("can_notify", canNotify(context))
            put("full_screen_allowed", canUseFullScreen(context))
            put("push_available", JupiterPush.available(context))
        }
    }

    fun startPreview(context: Context): Boolean {
        stopPreview()
        val uri = desiredSound(context) ?: return false
        val ringtone = RingtoneManager.getRingtone(context, uri) ?: return false
        ringtone.audioAttributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)              // ring volume; silent phone = silent
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        preview = ringtone
        return runCatching { ringtone.play(); true }.getOrDefault(false)
    }

    fun stopPreview() {
        preview?.let { runCatching { it.stop() } }
        preview = null
    }

    // ── permission / platform state ──────────────────────────────────────────────────────────────────────────
    fun canNotify(context: Context): Boolean = JupiterReminders.canNotify(context)

    fun canUseFullScreen(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < 34) return true
        return context.getSystemService(NotificationManager::class.java)?.canUseFullScreenIntent() == true
    }

    private fun interruptionFilter(context: Context): Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            context.getSystemService(NotificationManager::class.java)?.currentInterruptionFilter ?: CallRingerPolicy.FILTER_UNKNOWN
        } else CallRingerPolicy.FILTER_ALL

    private fun priorityAllowsCalls(context: Context): Boolean? = runCatching {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) return@runCatching null
        val policy = context.getSystemService(NotificationManager::class.java)?.notificationPolicy ?: return@runCatching null
        (policy.priorityCategories and NotificationManager.Policy.PRIORITY_CATEGORY_CALLS) != 0 &&
            policy.priorityCallSenders == NotificationManager.Policy.PRIORITY_SENDERS_ANY
    }.getOrNull()

    fun alertPlan(context: Context): CallRingerPolicy.AlertPlan {
        val ringer = context.getSystemService(AudioManager::class.java)?.ringerMode ?: CallRingerPolicy.RINGER_NORMAL
        return CallRingerPolicy.plan(ringer, interruptionFilter(context), priorityAllowsCalls(context), choice(context))
    }

    // ── ringing ──────────────────────────────────────────────────────────────────────────────────────────────
    private fun restoreSeen(context: Context) {
        if (seenRestored) return
        seen.restore(prefs(context).getString("seen", "") ?: "")
        seenRestored = true
    }

    private fun notificationId(callId: String) = 0x4A00_0000 or (callId.hashCode() and 0x00FF_FFFF)

    /**
     * Show the incoming call once per call id (push + poll + duplicate push never ring twice). Returns a status the
     * WebView can show honestly: ringing, duplicate, expired, signed_out, notifications_off.
     */
    @Synchronized
    fun showIncoming(context: Context, callId: String, reason: String, expiresAtMillis: Long): String {
        if (!CALL_ID.matches(callId)) return "invalid"
        if (accountKey(context).isEmpty()) return "signed_out"
        val now = System.currentTimeMillis()
        if (expiresAtMillis <= now + 1_000) return "expired"
        restoreSeen(context)
        if (!seen.firstTime(callId, now)) return "duplicate"
        prefs(context).edit().putString("seen", seen.serialize()).apply()
        if (!canNotify(context)) return "notifications_off"           // the open app still shows its own card, silently
        val channel = ensureChannel(context)
        val plan = alertPlan(context)
        val safeReason = reason.replace(Regex("\\s+"), " ").trim().take(80).ifBlank { "Jupiter is calling" }
        val caller = Person.Builder().setName("Jupiter").setImportant(true)
            .setIcon(IconCompat.createWithResource(context, R.drawable.jupiter_caller_logo)).build()
        val decline = PendingIntent.getBroadcast(
            context, notificationId(callId),
            Intent(context, JupiterCallReceiver::class.java).setAction(ACTION_DECLINE).putExtra(EXTRA_CALL_ID, callId),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val answer = PendingIntent.getActivity(
            context, notificationId(callId),
            Intent(context, MainActivity::class.java).setAction(ACTION_ANSWER).putExtra(EXTRA_CALL_ID, callId)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val fullScreen = PendingIntent.getActivity(
            context, notificationId(callId) + 1,
            Intent(context, IncomingCallActivity::class.java).setAction(ACTION_SHOW).putExtra(EXTRA_CALL_ID, callId)
                .putExtra(EXTRA_REASON, safeReason)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_USER_ACTION),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val remaining = expiresAtMillis - now
        val hidden = NotificationCompat.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_stat_call)
            .setContentTitle("Jupiter is calling")
            .build()
        val builder = NotificationCompat.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_stat_call)
            .setContentTitle("Jupiter")
            .setContentText(safeReason)
            .setLargeIcon(BitmapFactory.decodeResource(context.resources, R.drawable.jupiter_caller_logo))
            .setStyle(NotificationCompat.CallStyle.forIncomingCall(caller, decline, answer))
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(hidden)
            .setOngoing(true)
            .setAutoCancel(false)
            .setTimeoutAfter(remaining)                       // finite ringing even if nothing else stops it
            .setFullScreenIntent(fullScreen, true)            // the OS decides eligibility (Android 14+ permission)
            .setContentIntent(fullScreen)
        if (plan.silent) builder.setSilent(true)              // phone on silent / DND: Jupiter adds nothing
        val notification = builder.build()
        if (!plan.silent) notification.flags = notification.flags or Notification.FLAG_INSISTENT   // ring until handled
        return try {
            NotificationManagerCompat.from(context).notify(notificationId(callId), notification)
            scheduleExpiry(context, callId, expiresAtMillis)
            "ringing"
        } catch (_: SecurityException) {
            "notifications_off"
        } catch (_: IllegalArgumentException) {
            "unsupported"
        }
    }

    private fun expiryIntent(context: Context, callId: String, create: Boolean): PendingIntent? =
        PendingIntent.getBroadcast(
            context, notificationId(callId) + 2,
            Intent(context, JupiterCallReceiver::class.java).setAction(ACTION_EXPIRE).putExtra(EXTRA_CALL_ID, callId),
            PendingIntent.FLAG_IMMUTABLE or if (create) PendingIntent.FLAG_UPDATE_CURRENT else PendingIntent.FLAG_NO_CREATE,
        )

    private fun scheduleExpiry(context: Context, callId: String, atMillis: Long) {
        val alarms = context.getSystemService(AlarmManager::class.java) ?: return
        val operation = expiryIntent(context, callId, create = true) ?: return
        runCatching {
            if (JupiterReminders.canScheduleExact(context)) {
                alarms.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMillis, operation)
            } else {
                alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, atMillis, operation)
            }
        }
    }

    /** Stop ringing on Answer, Decline, timeout, cancellation, another device answering, sign-out or teardown. */
    fun stop(context: Context, callId: String, missed: Boolean = false) {
        if (!CALL_ID.matches(callId)) return
        NotificationManagerCompat.from(context).cancel(notificationId(callId))
        expiryIntent(context, callId, create = false)?.let { pending ->
            context.getSystemService(AlarmManager::class.java)?.cancel(pending)
            pending.cancel()
        }
        incomingUi?.get()?.takeIf { it.callId == callId }?.let { activity -> activity.runOnUiThread { activity.finish() } }
        if (missed) showMissed(context, callId)
    }

    private fun showMissed(context: Context, callId: String) {
        if (!canNotify(context)) return
        ensureChannel(context)
        val open = PendingIntent.getActivity(
            context, notificationId(callId) + 3,
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notification = NotificationCompat.Builder(context, MISSED_CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_call_missed)
            .setContentTitle("Missed Jupiter call")
            .setContentText("Open Jupiter to see why it called.")
            .setCategory(NotificationCompat.CATEGORY_MISSED_CALL)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        runCatching { NotificationManagerCompat.from(context).notify(notificationId(callId) + 3, notification) }
    }

    fun cancelAll(context: Context) {
        val preferences = prefs(context)
        restoreSeen(context)
        seen.serialize().split(',').mapNotNull { it.substringBefore(':').takeIf(CALL_ID::matches) }.forEach { stop(context, it) }
        stopPreview()
        preferences.edit().remove("pending_action").apply()
    }

    // ── answer / decline from the notification ───────────────────────────────────────────────────────────────
    /** Cold start from a notification: kept briefly until the signed-in page asks for it. */
    fun rememberAction(context: Context, action: String, callId: String) {
        if (!CALL_ID.matches(callId)) return
        prefs(context).edit().putString("pending_action",
            JSONObject().put("action", action).put("call_id", callId).put("at", System.currentTimeMillis()).toString()).apply()
    }

    fun takePendingAction(context: Context): String {
        val raw = prefs(context).getString("pending_action", null) ?: return "{}"
        prefs(context).edit().remove("pending_action").apply()
        val value = runCatching { JSONObject(raw) }.getOrNull() ?: return "{}"
        return if (System.currentTimeMillis() - value.optLong("at") < 90_000) value.toString() else "{}"
    }

    /** Decline without opening the app: stop now, then tell the backend with this device's own proof. */
    fun declineInBackground(context: Context, callId: String, done: () -> Unit) {
        stop(context, callId)
        webListener?.invoke(JSONObject().put("event", "declined").put("call_id", callId).toString())
        val deviceId = deviceId(context)
        val secret = deviceSecret(context)
        Thread {
            try {
                val connection = URL("${backendOrigin()}/api/calls/$callId/decline").openConnection() as HttpURLConnection
                connection.requestMethod = "POST"
                connection.connectTimeout = 6_000
                connection.readTimeout = 6_000
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.setRequestProperty("X-Jupiter-Device-Id", deviceId)
                connection.setRequestProperty("X-Jupiter-Device-Secret", secret)
                connection.outputStream.use { it.write(JSONObject().put("device_id", deviceId).toString().toByteArray()) }
                connection.responseCode                        // offline: the call simply times out on the server
                connection.disconnect()
            } catch (_: Exception) {
            } finally {
                done()
            }
        }.start()
    }

    /** Push handler: the current state and privacy-safe reason, for THIS device's account (bounded wait). */
    fun fetchBrief(context: Context, callId: String): JSONObject? = try {
        val connection = URL("${backendOrigin()}/api/calls/$callId/brief").openConnection() as HttpURLConnection
        connection.connectTimeout = 4_000
        connection.readTimeout = 4_000
        connection.setRequestProperty("X-Jupiter-Device-Id", deviceId(context))
        connection.setRequestProperty("X-Jupiter-Device-Secret", deviceSecret(context))
        val body = if (connection.responseCode == 200) connection.inputStream.bufferedReader().use { it.readText() } else null
        connection.disconnect()
        body?.let(::JSONObject)
    } catch (_: Exception) {
        null
    }

    fun hasNotificationPermission(context: Context): Boolean = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
}

/** Decline (from the notification) and the finite ring timeout. Explicit, immutable intents only. */
class JupiterCallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val callId = intent.getStringExtra(JupiterCalls.EXTRA_CALL_ID) ?: return
        when (intent.action) {
            JupiterCalls.ACTION_DECLINE -> {
                val pending = goAsync()
                JupiterCalls.declineInBackground(context, callId) { pending.finish() }
            }
            JupiterCalls.ACTION_EXPIRE -> {
                JupiterCalls.stop(context, callId)
                val pending = goAsync()
                Thread {
                    try {
                        // Answered on another device but its stop push never arrived: that is not a missed call.
                        val status = JupiterCalls.fetchBrief(context, callId)?.optString("status")
                        if (status == null || status == "missed" || status == "ringing") {
                            JupiterCalls.stop(context, callId, missed = true)
                            JupiterCalls.webListener?.invoke(
                                JSONObject().put("event", "missed").put("call_id", callId).toString())
                        }
                    } finally {
                        pending.finish()
                    }
                }.start()
            }
        }
    }
}
