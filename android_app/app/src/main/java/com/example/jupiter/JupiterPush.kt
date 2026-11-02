package com.example.jupiter

import android.content.Context
import com.google.firebase.FirebaseApp
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/**
 * Free background delivery of Jupiter calls through Firebase Cloud Messaging (marker 172).
 *
 * Firebase is configured at build time from the Firebase project's public app identifiers (google_app_id,
 * gcm_defaultSenderId, google_api_key, project_id resources, injected from environment variables by
 * app/build.gradle.kts). When they are absent, FirebaseApp is never initialised, push is reported unavailable and only
 * an open Jupiter app rings. Delivery is never guaranteed: a force-stopped app, an offline phone, battery
 * restrictions or a missing notification permission all prevent it.
 */
internal object JupiterPush {
    fun available(context: Context): Boolean = runCatching { FirebaseApp.getApps(context).isNotEmpty() }.getOrDefault(false)

    /** Fetch (or refresh) this install's FCM token; [done] receives "" when push is unavailable. */
    fun refreshToken(context: Context, done: (String) -> Unit) {
        if (!available(context)) {
            done("")
            return
        }
        runCatching {
            FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
                val token = if (task.isSuccessful) task.result.orEmpty() else ""
                if (token.isNotEmpty()) JupiterCalls.savePushToken(context, token)
                done(token)
            }
        }.onFailure { done("") }
    }
}

class JupiterMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        // Re-registered with the backend by the signed-in page the next time Jupiter opens (it needs the session).
        JupiterCalls.savePushToken(applicationContext, token)
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val context = applicationContext
        if (message.data["type"] == "jupiter_notice") {
            // Marker 222: work that finished while Jupiter was closed (a task update); shown only when not on screen.
            JupiterNotices.showFromPush(context, message.data, JupiterCalls.accountKey(context))
            return
        }
        if (message.data["type"] == "jupiter_message") {
            // Marker 186: a Jupiter user sent this account a message. Show a content-free notification only if the
            // user allows notifications; the text itself is read inside Jupiter (the in-app inbox delivers it anyway).
            val alert = MessagePushParser.parse(message.data, JupiterCalls.accountKey(context)) ?: return
            JupiterMessageAlerts.show(context, alert)
            return
        }
        val push = CallPushParser.parse(message.data, JupiterCalls.accountKey(context), System.currentTimeMillis())
            ?: return                                                  // malformed, stale, or for another account
        if (push.type == "jupiter_call_stop") {
            JupiterCalls.stop(context, push.callId)
            return
        }
        // Still ringing? (answered elsewhere / declined / cancelled while the push was in flight -> stay quiet)
        val brief = JupiterCalls.fetchBrief(context, push.callId)
        val status = brief?.optString("status")
        if (status != null && status != "ringing") return
        val reason = brief?.optString("reason").orEmpty().ifBlank { "Jupiter is calling" }
        JupiterCalls.showIncoming(context, push.callId, reason, push.expiresAtMillis)
    }
}
