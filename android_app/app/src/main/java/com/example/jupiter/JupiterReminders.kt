package com.example.jupiter

import android.Manifest
import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import java.util.Calendar
import java.util.Locale
import java.util.TimeZone

/** A repeating reminder's wall-clock rule, exactly as the server stores it (weekdays: Monday = 0). */
internal data class ReminderRule(
    val freq: String,
    val weekdays: List<Int> = emptyList(),
    val day: Int = 0,
    val month: Int = 0,
    val hour: Int = 9,
    val minute: Int = 0,
    val timeZone: String = "Asia/Kolkata",
)

/** Pure scheduling math (no Android types) so it is unit-testable on the JVM. */
internal object ReminderMath {
    /** The first occurrence strictly after [afterMillis] on the rule's own wall clock, or null. */
    fun nextOccurrence(rule: ReminderRule, afterMillis: Long): Long? {
        val zone = TimeZone.getTimeZone(rule.timeZone)
        for (offset in 0 until 800) {
            val day = Calendar.getInstance(zone, Locale.US).apply {
                timeInMillis = afterMillis
                add(Calendar.DAY_OF_YEAR, offset)
            }
            val mondayZero = (day.get(Calendar.DAY_OF_WEEK) + 5) % 7
            val matches = when (rule.freq) {
                "daily" -> true
                "weekly" -> mondayZero in rule.weekdays.ifEmpty { listOf(mondayZero) }
                "monthly" -> day.get(Calendar.DAY_OF_MONTH) == rule.day
                "yearly" -> day.get(Calendar.MONTH) + 1 == rule.month && day.get(Calendar.DAY_OF_MONTH) == rule.day
                else -> return null
            }
            if (!matches) continue
            day.set(Calendar.HOUR_OF_DAY, rule.hour)
            day.set(Calendar.MINUTE, rule.minute)
            day.set(Calendar.SECOND, 0)
            day.set(Calendar.MILLISECOND, 0)
            if (day.timeInMillis > afterMillis) return day.timeInMillis
        }
        return null
    }
}

/**
 * Native delivery of the signed-in account's explicit reminders.
 *
 * The server owns reminders; the WebView syncs the account's upcoming occurrences here so they fire even when
 * Jupiter is closed or the phone restarts. A fired reminder is recorded until the WebView acknowledges it to the
 * server (exactly-once delivery; the in-app card is only the fallback). Signing out or switching accounts syncs an
 * empty list, which cancels every alarm and clears the previous account's pending acknowledgements. Notification
 * text is hidden on a secure lock screen.
 */
internal object JupiterReminders {
    const val CHANNEL_ID = "jupiter_reminders"
    const val ACTION_FIRE = "com.arupbairagya.jupiter.REMINDER_FIRE"
    const val EXTRA_KEY = "reminder_key"
    private const val PREFS = "jupiter_reminders"
    private const val MAX_ENTRIES = 64

    fun accountKey(raw: String): String {
        val normalized = raw.trim().lowercase(Locale.US)
        if (normalized.length !in 3..320 || !normalized.contains('@')) return ""
        val digest = MessageDigest.getInstance("SHA-256").digest(normalized.toByteArray(Charsets.UTF_8))
        return digest.joinToString("") { "%02x".format(it) }
    }

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Reminders", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Reminders you asked Jupiter to set"
                lockscreenVisibility = android.app.Notification.VISIBILITY_PRIVATE
            }
        )
    }

    fun canNotify(context: Context): Boolean {
        val granted = Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(
            context, Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        return granted && NotificationManagerCompat.from(context).areNotificationsEnabled()
    }

    fun canScheduleExact(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true
        return context.getSystemService(AlarmManager::class.java)?.canScheduleExactAlarms() == true
    }

    private fun schedule(context: Context): JSONArray =
        runCatching { JSONArray(prefs(context).getString("schedule", "[]")) }.getOrDefault(JSONArray())

    private fun fired(context: Context): JSONArray =
        runCatching { JSONArray(prefs(context).getString("fired", "[]")) }.getOrDefault(JSONArray())

    private fun pendingIntent(context: Context, key: String, create: Boolean): PendingIntent? {
        val intent = Intent(context, ReminderReceiver::class.java).setAction(ACTION_FIRE).putExtra(EXTRA_KEY, key)
        val flags = PendingIntent.FLAG_IMMUTABLE or if (create) PendingIntent.FLAG_UPDATE_CURRENT else PendingIntent.FLAG_NO_CREATE
        return PendingIntent.getBroadcast(context, key.hashCode(), intent, flags)
    }

    private fun arm(context: Context, key: String, atMillis: Long) {
        val alarms = context.getSystemService(AlarmManager::class.java) ?: return
        val operation = pendingIntent(context, key, create = true) ?: return
        val at = maxOf(atMillis, System.currentTimeMillis() + 1_000)
        if (canScheduleExact(context)) {
            alarms.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, operation)
        } else {
            alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, operation)
        }
    }

    private fun disarm(context: Context, key: String) {
        val alarms = context.getSystemService(AlarmManager::class.java) ?: return
        pendingIntent(context, key, create = false)?.let { alarms.cancel(it); it.cancel() }
    }

    /** Replace the schedule with [json] (the account's upcoming occurrences); returns how many are armed. */
    @Synchronized
    fun sync(context: Context, rawAccount: String, json: String): Int {
        val account = accountKey(rawAccount)
        val preferences = prefs(context)
        val previousAccount = preferences.getString("account", "") ?: ""
        val incoming = runCatching { JSONArray(json) }.getOrDefault(JSONArray())
        val next = JSONArray()
        val now = System.currentTimeMillis()
        if (account.isNotEmpty()) {
            for (index in 0 until minOf(incoming.length(), MAX_ENTRIES)) {
                val item = incoming.optJSONObject(index) ?: continue
                val id = item.optString("id").takeIf { it.matches(Regex("[A-Za-z0-9_-]{8,64}")) } ?: continue
                val at = item.optLong("at", 0L)
                if (at <= 0L || at > now + 400L * 24 * 3600 * 1000) continue
                next.put(JSONObject().apply {
                    put("key", "$id@$at")
                    put("id", id)
                    put("at", at)
                    put("title", item.optString("title").take(200))
                    item.optJSONObject("rule")?.let { put("rule", it) }
                })
            }
        }
        val old = schedule(context)
        val keep = (0 until next.length()).map { next.getJSONObject(it).getString("key") }.toSet()
        for (index in 0 until old.length()) {
            val key = old.optJSONObject(index)?.optString("key") ?: continue
            if (key !in keep || account != previousAccount) disarm(context, key)
        }
        for (index in 0 until next.length()) {
            val entry = next.getJSONObject(index)
            arm(context, entry.getString("key"), entry.getLong("at"))
        }
        preferences.edit().apply {
            putString("schedule", next.toString())
            putString("account", account)
            if (account != previousAccount) putString("fired", "[]")
        }.apply()
        return next.length()
    }

    /** Reminders shown natively and not yet acknowledged to the server, for this account only. */
    fun firedJson(context: Context, rawAccount: String): String {
        val account = accountKey(rawAccount)
        if (account.isEmpty() || account != prefs(context).getString("account", "")) return "[]"
        return fired(context).toString()
    }

    @Synchronized
    fun clearFired(context: Context, rawAccount: String, keysJson: String) {
        val account = accountKey(rawAccount)
        if (account.isEmpty() || account != prefs(context).getString("account", "")) return
        val keys = runCatching { JSONArray(keysJson) }.getOrDefault(JSONArray())
        val drop = (0 until keys.length()).map { keys.optString(it) }.toSet()
        val current = fired(context)
        val kept = JSONArray()
        for (index in 0 until current.length()) {
            val entry = current.optJSONObject(index) ?: continue
            if (entry.optString("key") !in drop) kept.put(entry)
        }
        prefs(context).edit().putString("fired", kept.toString()).apply()
    }

    private fun ruleOf(entry: JSONObject): ReminderRule? {
        val rule = entry.optJSONObject("rule") ?: return null
        val weekdays = rule.optJSONArray("weekdays")?.let { array -> (0 until array.length()).map { array.optInt(it) } } ?: emptyList()
        return ReminderRule(
            freq = rule.optString("freq"),
            weekdays = weekdays,
            day = rule.optInt("day"),
            month = rule.optInt("month"),
            hour = rule.optInt("hour", 9),
            minute = rule.optInt("minute", 0),
            timeZone = rule.optString("tz", "Asia/Kolkata"),
        )
    }

    /** An alarm fired: show it once, record it for acknowledgement, and arm a series' next occurrence. */
    @Synchronized
    fun onAlarm(context: Context, key: String) {
        val entries = schedule(context)
        val remaining = JSONArray()
        var hit: JSONObject? = null
        for (index in 0 until entries.length()) {
            val entry = entries.optJSONObject(index) ?: continue
            if (entry.optString("key") == key && hit == null) hit = entry else remaining.put(entry)
        }
        val entry = hit ?: return                     // cancelled or replaced since it was armed
        val at = entry.optLong("at")
        if (at > System.currentTimeMillis() + 60_000) {  // a clock change fired it early: re-arm, don't show
            arm(context, key, at)
            return
        }
        val shown = show(context, entry)
        if (shown) {
            val record = fired(context).put(JSONObject().apply {
                put("key", key); put("id", entry.optString("id")); put("at", at)
            })
            prefs(context).edit().putString("fired", record.toString()).apply()
        }
        ruleOf(entry)?.let { rule ->
            ReminderMath.nextOccurrence(rule, at)?.let { nextAt ->
                val next = JSONObject(entry.toString()).put("at", nextAt).put("key", "${entry.optString("id")}@$nextAt")
                remaining.put(next)
                arm(context, next.getString("key"), nextAt)
            }
        }
        prefs(context).edit().putString("schedule", remaining.toString()).apply()
    }

    private fun show(context: Context, entry: JSONObject): Boolean {
        if (!canNotify(context)) return false           // the in-app card delivers it instead
        ensureChannel(context)
        val open = PendingIntent.getActivity(
            context, 0,
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val title = entry.optString("title").ifBlank { "Reminder" }
        val hidden = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_reminder)
            .setContentTitle("Jupiter reminder")
            .build()
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_reminder)
            .setContentTitle("Reminder")
            .setContentText(title)
            .setStyle(NotificationCompat.BigTextStyle().bigText(title))
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(hidden)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        return try {
            NotificationManagerCompat.from(context).notify(entry.optString("key").hashCode(), notification)
            true
        } catch (_: SecurityException) {
            false
        }
    }

    /** After a reboot, app update or clock/time-zone change: show what was missed while off and re-arm the rest. */
    @Synchronized
    fun rescheduleAll(context: Context) {
        val entries = schedule(context)
        val now = System.currentTimeMillis()
        val due = mutableListOf<String>()
        for (index in 0 until entries.length()) {
            val entry = entries.optJSONObject(index) ?: continue
            val key = entry.optString("key")
            if (entry.optLong("at") <= now) due.add(key) else arm(context, key, entry.optLong("at"))
        }
        due.forEach { onAlarm(context, it) }
    }
}

class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            JupiterReminders.ACTION_FIRE -> intent.getStringExtra(JupiterReminders.EXTRA_KEY)?.let {
                JupiterReminders.onAlarm(context, it)
            }
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED, Intent.ACTION_TIME_CHANGED,
            Intent.ACTION_TIMEZONE_CHANGED, AlarmManager.ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED ->
                JupiterReminders.rescheduleAll(context)
        }
    }
}
