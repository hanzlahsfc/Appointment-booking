package com.booker.otp

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.IBinder

/**
 * Foreground service so Doze/battery optimisation can't kill the process —
 * without this, Android delays or drops the SMS broadcast at night, which
 * is exactly when short-lived OTPs matter.
 */
class OtpService : Service() {

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val ch = NotificationChannel(
            "otp", "OTP relay", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java)
            .createNotificationChannel(ch)
        startForeground(
            1,
            Notification.Builder(this, "otp")
                .setContentTitle("OTP relay running")
                .setContentText("Watching for booking codes")
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .build()
        )
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
