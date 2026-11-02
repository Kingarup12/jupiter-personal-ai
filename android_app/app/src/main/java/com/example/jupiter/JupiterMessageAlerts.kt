package com.example.jupiter

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

/**
 * Marker 186: device alert for a directed message from another Jupiter user.
 *
 * The push carries no message text, so nothing sensitive can reach the lock screen: the private notification names the
 * sender at most, and the public (lock-screen) version says only "New Jupiter message". It is shown only when the user
 * allows Jupiter notifications (POST_NOTIFICATIONS on Android 13+ and the app's notification switch); otherwise the
 * message still waits in the Jupiter inbox. Each message id shows at most one notification.
 */
internal object JupiterMessageAlerts {
    const val CHANNEL_ID = "jupiter_messages"

    private fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Jupiter messages", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Messages other Jupiter users send you"
                lockscreenVisibility = android.app.Notification.VISIBILITY_PRIVATE
            }
        )
    }

    fun show(context: Context, push: MessagePush): Boolean {
        if (!JupiterReminders.canNotify(context)) return false        // denied/disabled: the in-app inbox delivers it
        ensureChannel(context)
        val open = PendingIntent.getActivity(
            context, push.messageId.hashCode(),
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val hidden = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_reminder)
            .setContentTitle("New Jupiter message")
            .build()
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_reminder)
            .setContentTitle(MessagePushParser.title(push))
            .setContentText("Open Jupiter to read it.")
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(hidden)
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .build()
        return try {
            NotificationManagerCompat.from(context).notify("jupiter_message", push.messageId.hashCode(), notification)
            true
        } catch (_: SecurityException) {
            false
        }
    }
}
