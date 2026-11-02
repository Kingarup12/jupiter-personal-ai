package com.example.jupiter

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import org.json.JSONObject

/**
 * Marker 222: "Jupiter replied", "Jupiter has a question" and task updates as real Android notifications - like ChatGPT
 * and Claude - shown only while Jupiter is NOT on screen. The page (or a free FCM push for work that finished while the
 * app was closed) supplies a short title and preview; tapping opens Jupiter on that chat. The lock screen shows only
 * "Jupiter" unless the user allows private content there.
 */
internal object JupiterNotices {
    const val CHANNEL_ID = "jupiter_updates"
    const val ACTION_OPEN = "com.arupbairagya.jupiter.OPEN_NOTICE"
    const val EXTRA_CHAT_ID = "jupiter_notice_chat"
    private val KINDS = setOf("reply", "question", "task", "reminder")
    private val CHAT_ID = Regex("[A-Za-z0-9_-]{1,80}")

    /** True while MainActivity is visible (onStart..onStop): then the user already sees the reply. */
    @Volatile var foreground = false

    private fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Replies and task updates", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "When Jupiter finishes a reply, asks you something or finishes a task while you are away"
                lockscreenVisibility = android.app.Notification.VISIBILITY_PRIVATE
            }
        )
    }

    fun show(context: Context, json: String): Boolean {
        if (foreground) return false
        val payload = runCatching { JSONObject(json) }.getOrNull() ?: return false
        val kind = payload.optString("kind").takeIf { it in KINDS } ?: return false
        val title = payload.optString("title").replace(Regex("\\s+"), " ").trim().take(120).ifBlank { "Jupiter" }
        val body = payload.optString("body").replace(Regex("\\s+"), " ").trim().take(300)
        val chatId = payload.optString("chat_id").takeIf { CHAT_ID.matches(it) }.orEmpty()
        val tag = payload.optString("tag").replace(Regex("[^A-Za-z0-9_.-]"), "").take(60).ifBlank { kind }
        return post(context, kind, title, body, chatId, tag)
    }

    private fun post(context: Context, kind: String, title: String, body: String, chatId: String, tag: String): Boolean {
        if (!JupiterReminders.canNotify(context)) return false        // notifications denied: the in-app card remains
        ensureChannel(context)
        val open = PendingIntent.getActivity(
            context, tag.hashCode(),
            Intent(context, MainActivity::class.java)
                .setAction(ACTION_OPEN)
                .putExtra(EXTRA_CHAT_ID, chatId)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val hidden = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_reminder)
            .setContentTitle("Jupiter")
            .setContentText(if (kind == "question") "Jupiter has a question" else "New update from Jupiter")
            .build()
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_reminder)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setCategory(if (kind == "reminder") NotificationCompat.CATEGORY_REMINDER else NotificationCompat.CATEGORY_MESSAGE)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(hidden)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        return try {
            NotificationManagerCompat.from(context).notify("jupiter_notice", tag.hashCode(), notification)
            true
        } catch (_: SecurityException) {
            false
        }
    }

    /** FCM "jupiter_notice" (work that finished while Jupiter was closed); never for another account. */
    fun showFromPush(context: Context, data: Map<String, String>, accountKey: String): Boolean {
        val account = data["acct"].orEmpty()                          // same rule as message alerts (MessagePushParser)
        if (accountKey.length < 16 || account.length != 16 || !accountKey.startsWith(account)) return false
        return show(context, JSONObject(data.filterKeys { it in setOf("kind", "title", "body", "chat_id", "tag") }).toString())
    }

    /** The chat a tapped notification should open, or "" (consumed once). */
    fun chatFrom(intent: Intent?): String? {
        if (intent?.action != ACTION_OPEN) return null
        val chatId = intent.getStringExtra(EXTRA_CHAT_ID).orEmpty()
        intent.action = null                                          // a rotation/recreate never re-opens it
        return if (CHAT_ID.matches(chatId)) chatId else ""
    }
}
