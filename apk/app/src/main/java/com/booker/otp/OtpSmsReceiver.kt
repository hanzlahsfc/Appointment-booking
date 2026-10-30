package com.booker.otp

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import android.util.Log
import java.net.HttpURLConnection
import java.net.URL

/**
 * Catches every incoming SMS, extracts the 4-8 digit code, and PUTs it to
 * Firebase RTDB at  <db_url>/otps/<secret>/<machine_id>.json
 * The crawler machine polls that node, consumes it, and books.
 */
class OtpSmsReceiver : BroadcastReceiver() {

    private val codeRe = Regex("\\b(\\d{4,8})\\b")

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val prefs = context.getSharedPreferences("cfg", Context.MODE_PRIVATE)
        val dbUrl = prefs.getString("db_url", "")?.trim().orEmpty()
        val secret = prefs.getString("secret", "")?.trim().orEmpty()
        val mid = prefs.getString("machine_id", "")?.trim().orEmpty()
        val sender = prefs.getString("sender_filter", "")?.trim().orEmpty()
        if (dbUrl.isEmpty() || secret.isEmpty() || mid.isEmpty()) return

        for (msg in Telephony.Sms.Intents.getMessagesFromIntent(intent)) {
            val from = msg.originatingAddress.orEmpty()
            if (sender.isNotEmpty() && !from.contains(sender, true)) continue
            val body = msg.messageBody.orEmpty()
            val code = codeRe.find(body)?.value ?: continue
            upload(dbUrl, secret, mid, code)
        }
    }

    private fun upload(dbUrl: String, secret: String, mid: String, code: String) {
        Thread {
            try {
                val url = "${dbUrl.trimEnd('/')}/otps/$secret/$mid.json"
                val c = URL(url).openConnection() as HttpURLConnection
                c.requestMethod = "PUT"
                c.doOutput = true
                c.connectTimeout = 8000
                c.readTimeout = 8000
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write("\"$code\"".toByteArray()) }
                Log.i("OtpRelay", "code uploaded rc=${c.responseCode}")
                c.disconnect()
            } catch (e: Exception) {
                Log.e("OtpRelay", "upload failed", e)
            }
        }.start()
    }
}
