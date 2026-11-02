package com.example.jupiter

/**
 * Pure rules for Jupiter in-app calls (marker 172), kept free of Android types so they are JVM unit-tested.
 *
 * The phone's own state always wins:
 *  - Ringer SILENT  -> the call shows, but makes no sound and no vibration.
 *  - Ringer VIBRATE -> the call only vibrates (never a sound).
 *  - Ringer NORMAL  -> the chosen ringtone plays, plus vibration (unless the user chose silent/vibrate-only).
 *  - Do Not Disturb that blocks calls (Alarms only / Total silence, or Priority without "calls from anyone")
 *    -> no sound and no vibration from Jupiter.
 * Android also enforces these itself for the notification channel; Jupiter additionally marks the notification silent
 * whenever this plan says so, so no OEM quirk can make a muted phone ring. Jupiter never changes the phone's ringer
 * mode, volume, Do Not Disturb or system ringtone.
 */
internal object CallRingerPolicy {
    // android.media.AudioManager
    const val RINGER_SILENT = 0
    const val RINGER_VIBRATE = 1
    const val RINGER_NORMAL = 2

    // android.app.NotificationManager.INTERRUPTION_FILTER_*
    const val FILTER_UNKNOWN = 0
    const val FILTER_ALL = 1
    const val FILTER_PRIORITY = 2
    const val FILTER_NONE = 3
    const val FILTER_ALARMS = 4

    data class AlertPlan(val sound: Boolean, val vibrate: Boolean, val reason: String) {
        val silent: Boolean get() = !sound && !vibrate
    }

    fun plan(ringerMode: Int, interruptionFilter: Int, priorityAllowsCalls: Boolean?, choice: RingChoice): AlertPlan {
        val dndBlocks = when (interruptionFilter) {
            FILTER_NONE, FILTER_ALARMS -> true
            FILTER_PRIORITY -> priorityAllowsCalls != true          // unknown policy: stay quiet, never bypass DND
            else -> false
        }
        if (dndBlocks) return AlertPlan(sound = false, vibrate = false, reason = "do_not_disturb")
        if (choice == RingChoice.SILENT) return AlertPlan(sound = false, vibrate = false, reason = "jupiter_silent")
        return when (ringerMode) {
            RINGER_SILENT -> AlertPlan(sound = false, vibrate = false, reason = "phone_silent")
            RINGER_VIBRATE -> AlertPlan(sound = false, vibrate = true, reason = "phone_vibrate")
            else -> if (choice == RingChoice.VIBRATE_ONLY) {
                AlertPlan(sound = false, vibrate = true, reason = "jupiter_vibrate_only")
            } else {
                AlertPlan(sound = true, vibrate = true, reason = "ring")
            }
        }
    }

    fun ringerLabel(ringerMode: Int): String = when (ringerMode) {
        RINGER_SILENT -> "silent"
        RINGER_VIBRATE -> "vibrate"
        else -> "normal"
    }
}

/** What the user picked in Jupiter's call settings. PHONE_DEFAULT follows the phone's current ringtone. */
internal enum class RingChoice(val key: String) {
    PHONE_DEFAULT("phone"), SYSTEM("system"), CUSTOM("custom"), VIBRATE_ONLY("vibrate"), SILENT("silent"),
    JUPITER("jupiter");                                    // one of Jupiter's own ringtones (bundled in res/raw)

    val hasSound: Boolean get() = this == PHONE_DEFAULT || this == SYSTEM || this == CUSTOM || this == JUPITER

    companion object {
        fun of(key: String?): RingChoice = entries.firstOrNull { it.key == key } ?: PHONE_DEFAULT
    }
}

/** Jupiter's own ringtones: id -> (label, res/raw name). Add a tone by bundling res/raw/<raw>.mp3 and one entry. */
internal object JupiterTones {
    val ALL: Map<String, Pair<String, String>> = linkedMapOf(
        "electro_pop" to ("Jupiter Electro-Pop" to "jupiter_electro_pop"),
        "master" to ("Jupiter Master" to "jupiter_master"),
    )

    /** Marker 219 (owner): Jupiter's own ringtone rings by default; any explicit choice is kept. */
    const val DEFAULT = "master"

    fun label(id: String?): String? = ALL[id]?.first
    fun rawName(id: String?): String? = ALL[id]?.second
}

/** A validated push from Jupiter's backend: opaque identifiers only. */
internal data class CallPush(val type: String, val callId: String, val expiresAtMillis: Long)

/** Marker 186: a content-free "new Jupiter message" alert (the message text never travels in the push). */
internal data class MessagePush(val messageId: String, val senderName: String)

internal object MessagePushParser {
    private val messageId = Regex("[a-f0-9]{32}")

    /** Only a well-formed message alert for the account signed in on THIS device; the sender name is display-only. */
    fun parse(data: Map<String, String>, signedInAccountKey: String): MessagePush? {
        if (data["type"] != "jupiter_message") return null
        val id = data["message_id"]?.takeIf { messageId.matches(it) } ?: return null
        val account = data["acct"] ?: return null
        if (signedInAccountKey.length < 16 || account.length != 16 || !signedInAccountKey.startsWith(account)) return null
        val sender = data["sender_name"].orEmpty().replace(Regex("[\\p{Cntrl}]"), " ").trim().take(60)
        return MessagePush(id, sender)
    }

    /** Lock-screen-safe wording: who it is from at most, never what it says. */
    fun title(push: MessagePush): String = if (push.senderName.isBlank()) "New Jupiter message"
    else "New Jupiter message from ${push.senderName}"
}

internal object CallPushParser {
    private val callId = Regex("[a-f0-9]{32}")

    /**
     * Drops anything that is not a well-formed Jupiter call push for the account signed in on THIS device, or a ring
     * that has already expired (a phone coming online late never rings for an old call).
     */
    fun parse(data: Map<String, String>, signedInAccountKey: String, nowMillis: Long): CallPush? {
        val type = data["type"] ?: return null
        if (type != "jupiter_call" && type != "jupiter_call_stop") return null
        val id = data["call_id"]?.takeIf { callId.matches(it) } ?: return null
        val account = data["acct"] ?: return null
        if (signedInAccountKey.length < 16 || account.length != 16 || !signedInAccountKey.startsWith(account)) return null
        val expires = data["exp"]?.toLongOrNull() ?: return null
        if (type == "jupiter_call") {
            if (expires <= nowMillis + 1_000) return null
            if (expires > nowMillis + 120_000) return null            // never ring longer than the longest setting
        }
        return CallPush(type, id, expires)
    }
}

/** Remembers handled calls so a duplicate push, a poll and a push for the same call never ring twice. */
internal class SeenCalls(private val limit: Int = 32) {
    private val entries = LinkedHashMap<String, Long>()

    @Synchronized
    fun firstTime(callId: String, nowMillis: Long): Boolean {
        entries.entries.removeAll { nowMillis - it.value > 10 * 60_000 }
        if (entries.containsKey(callId)) return false
        entries[callId] = nowMillis
        while (entries.size > limit) entries.remove(entries.keys.first())
        return true
    }

    @Synchronized
    fun restore(serialized: String) {
        serialized.split(',').mapNotNull { part ->
            val pieces = part.split(':')
            if (pieces.size == 2) pieces[0] to (pieces[1].toLongOrNull() ?: return@mapNotNull null) else null
        }.forEach { (id, at) -> entries[id] = at }
    }

    @Synchronized
    fun serialize(): String = entries.entries.joinToString(",") { "${it.key}:${it.value}" }
}
