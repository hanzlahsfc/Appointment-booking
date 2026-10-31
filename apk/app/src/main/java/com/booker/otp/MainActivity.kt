package com.booker.otp

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {

    private fun field(ctx: Context, hint: String, key: String): EditText {
        val prefs = ctx.getSharedPreferences("cfg", Context.MODE_PRIVATE)
        return EditText(ctx).apply {
            this.hint = hint
            setText(prefs.getString(key, ""))
            tag = key
        }
    }

    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        val prefs = getSharedPreferences("cfg", Context.MODE_PRIVATE)

        val dbUrl = field(this, "Firebase URL (https://….firebaseio.com)", "db_url")
        val secret = field(this, "Shared secret (same as config.yaml)", "secret")
        val mid = field(this, "Machine ID e.g. NADIR-A4F2", "machine_id")
        val phone = field(this, "Client phone number (OTP binding)", "phone")
        val sender = field(this, "Sender filter (optional, e.g. GVCW)", "sender_filter")
        val status = TextView(this)

        val save = Button(this).apply {
            text = "Save + Start Relay"
            setOnClickListener {
                prefs.edit()
                    .putString("db_url", dbUrl.text.toString())
                    .putString("secret", secret.text.toString())
                    .putString("machine_id", mid.text.toString())
                    .putString("phone", phone.text.toString())
                    .putString("sender_filter", sender.text.toString())
                    .apply()
                startForegroundService(Intent(this@MainActivity, OtpService::class.java))
                status.text = "relay running — SMS codes upload automatically"
            }
        }

        val perm = Button(this).apply {
            text = "Grant SMS permission"
            setOnClickListener {
                requestPermissions(
                    arrayOf(Manifest.permission.RECEIVE_SMS,
                            Manifest.permission.READ_SMS,
                            Manifest.permission.POST_NOTIFICATIONS), 1)
            }
        }

        val test = Button(this).apply {
            text = "Test connection (round trip)"
            setOnClickListener {
                status.text = "pinging machine…"
                Thread {
                    val db = prefs.getString("db_url", "")!!.trimEnd('/')
                    val sec = prefs.getString("secret", "")
                    val m = prefs.getString("machine_id", "")
                    try {
                        // write ping — the machine deletes it and answers on
                        // the pong node; we poll up to ~15s for the answer
                        java.net.URL("$db/otps-ping/$sec/$m.json")
                            .openConnection().let {
                                val c = it as java.net.HttpURLConnection
                                c.requestMethod = "PUT"; c.doOutput = true
                                c.outputStream.use { o ->
                                    o.write("\"ping\"".toByteArray()) }
                                c.responseCode; c.disconnect()
                            }
                        var ok = false
                        repeat(15) {
                            if (ok) return@repeat
                            Thread.sleep(1000)
                            java.net.URL("$db/otps-pong/$sec/$m.json")
                                .openConnection().let {
                                    val c = it as java.net.HttpURLConnection
                                    c.requestMethod = "GET"
                                    ok = c.responseCode == 200 &&
                                        c.inputStream.bufferedReader()
                                            .readText().contains("pong")
                                    c.disconnect()
                                }
                        }
                        if (ok) java.net.URL("$db/otps-pong/$sec/$m.json")
                            .openConnection().let {
                                val c = it as java.net.HttpURLConnection
                                c.requestMethod = "DELETE"; c.responseCode
                                c.disconnect() }
                        runOnUiThread {
                            status.text = if (ok)
                                "CONNECTED — machine answered the ping"
                            else "NO REPLY — check the machine is running " +
                                 "and db_url/secret/machine id match"
                        }
                    } catch (e: Exception) {
                        runOnUiThread {
                            status.text = "FAILED — ${e.message?.take(80)}"
                        }
                    }
                }.start()
            }
        }

        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 60, 40, 40)
            addView(TextView(context).apply { text = "OTP Relay — crawler config"; textSize = 18f })
            addView(dbUrl); addView(secret); addView(mid); addView(phone)
            addView(sender)
            addView(perm); addView(save); addView(test); addView(status)
        })
    }

    override fun onRequestPermissionsResult(
        req: Int, perms: Array<out String>, results: IntArray) {
        val ok = results.isNotEmpty() && results.all { it == PackageManager.PERMISSION_GRANTED }
        Toast.makeText(this, if (ok) "SMS permission granted" else "SMS permission DENIED — relay will not see codes", Toast.LENGTH_LONG).show()
    }
}
