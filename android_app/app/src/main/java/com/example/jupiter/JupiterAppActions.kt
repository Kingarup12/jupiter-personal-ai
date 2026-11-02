package com.example.jupiter

import java.net.URLEncoder

/**
 * Pure helpers for Jupiter's Android app actions (no Android framework types, unit-tested).
 *
 * WhatsApp is reached ONLY through its documented, user-visible entry points: a click-to-chat link
 * (https://wa.me/<international number>?text=…) or a standard ACTION_SEND share targeted at the WhatsApp package.
 * Both open WhatsApp with the text already in the message box; the USER taps Send. Nothing here can send a message,
 * and nothing here reports "sent" — Android returns no send result from WhatsApp.
 *
 * Deliberately not used: AccessibilityService automation (Google Play prohibits using it to let an app autonomously
 * initiate/plan/execute actions, and assistants are not accessibility tools), unofficial WhatsApp APIs/clients,
 * root/ADB automation, and the WhatsApp Business Platform (a separate business number with its own costs — it can
 * never send from the user's personal account).
 */
object WhatsAppDraft {
    const val PACKAGE = "com.whatsapp"
    const val MAX_MESSAGE_CHARS = 1500

    /** Status values reported to the page. There is intentionally no "sent". */
    val STATUSES = setOf("prepared", "picker_opened", "opened", "cancelled", "not_installed", "no_number", "error")

    /**
     * The international digits for click-to-chat, or null when the saved number has no country code. Jupiter never
     * guesses a country code: a wrong guess could open a chat with a different person.
     */
    fun internationalDigits(raw: String?): String? {
        val value = raw?.trim().orEmpty()
        val international = value.startsWith("+") || value.startsWith("00")
        if (!international) return null
        val digits = value.removePrefix("+").let { if (value.startsWith("00")) it.removePrefix("00") else it }
            .filter { it.isDigit() }
        return if (digits.length in 8..15 && !digits.startsWith("0")) digits else null
    }

    fun sanitizeMessage(message: String?): String =
        message.orEmpty().replace("\u0000", "").trim().take(MAX_MESSAGE_CHARS)

    fun clickToChatUri(digits: String, message: String): String =
        "https://wa.me/$digits?text=" + URLEncoder.encode(sanitizeMessage(message), "UTF-8").replace("+", "%20")

    /** The exact confirmation shown in the native dialog before WhatsApp opens. */
    fun confirmationText(requested: String, contactName: String, number: String, message: String, hasCountryCode: Boolean): String {
        val asked = requested.trim().take(60)
        val who = contactName.trim().ifEmpty { number.trim() }.take(80)
        val mismatch = if (asked.isNotEmpty() && !who.contains(asked, ignoreCase = true)) "\n(You asked for “$asked”.)" else ""
        val how = if (hasCountryCode) "WhatsApp will open this chat with the message typed in."
        else "This number has no country code, so WhatsApp will ask you to pick $who's chat yourself."
        return "To: $who ($number)$mismatch\n\nMessage:\n${sanitizeMessage(message)}\n\n$how You tap Send in WhatsApp — " +
            "Jupiter can't send it for you and can't confirm whether it was sent."
    }

    fun resultJson(status: String, recipientLabel: String = ""): String {
        val safe = if (status in STATUSES) status else "error"
        val label = recipientLabel.take(80).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ")
        return "{\"status\":\"$safe\",\"recipient_label\":\"$label\"}"
    }
}
