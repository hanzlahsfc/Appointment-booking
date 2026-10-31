# OTP Relay APK — SMS → Firebase → machine (fully automatic booking)

The client's phone receives the portal's OTP SMS. This app catches it,
reads the 4–8 digit code, and writes it to Firebase. The crawler machine
polls Firebase every ~2s, enters the code, solves captcha, and presses
Confirm and book — zero humans in the loop.

## 1. Firebase (one-time, ~10 min, free)

1. [console.firebase.google.com](https://console.firebase.google.com) →
   Add project → skip Analytics → create.
2. Build → **Realtime Database** → Create Database → pick a region →
   start in **locked mode**.
3. Copy the database URL shown — looks like
   `https://yourproj-default-rtdb.europe-west1.firebasedatabase.app`
4. Rules tab → publish:

   ```json
   {
     "rules": {
       "otps":      {"$secret": {".read": true, ".write": true}},
       "otps-ping": {"$secret": {".read": true, ".write": true}},
       "otps-pong": {"$secret": {".read": true, ".write": true}}
     }
   }
   ```

   The `$secret` path is a long random string only your APK and machines
   know — it acts as the password. Generate one: `secrets.token_hex(16)`
   or any password generator, 32+ chars.

## 2. Machine config (already in the zip's config.yaml — verify it)

```yaml
firebase:
  url: "https://yourproj-default-rtdb.….firebasedatabase.app"
  secret: "your-32-char-random-secret"
otp:
  auto_request: true   # machine requests the SMS when a slot is held
  auto_confirm: true   # after OTP: captcha → Confirm and book
```

`START.bat` should print `OTP channel (APK): on — firebase pipe to [HOST-XXXX]`.

## 3. Build the APK

1. Install [Android Studio](https://developer.android.com/studio) (free).
2. File → Open → the `apk/` folder in this package → let Gradle sync.
3. Build → Build APK(s) → `app-debug.apk` lands under
   `app/build/outputs/apk/debug/`.
4. Copy the APK to the client's phone → install (allow "install unknown
   apps" for the file manager/browser you used).

## 4. Phone setup (client side, ~2 min)

1. Open **OTP Relay** → fill in:
   - **Firebase URL** — the same as `config.yaml`
   - **Shared secret** — the same as `config.yaml`
   - **Machine ID** — the `[HOST-XXXX]` shown when that machine started
   - **Sender filter** — optional; only count SMS containing this text
     (e.g. the portal's sender name) so bank OTPs are ignored
2. **Grant SMS permission** → allow
3. **Save + Start Relay** → status shows "relay running"
4. **Test connection** → writes a ping and waits ~15s for the machine to
   answer — shows `CONNECTED — machine answered the ping` when the whole
   path works (phone→Firebase→machine→phone). If it says NO REPLY, check
   the machine is running and the three fields match its config exactly.
4. Android may still kill it later: Settings → Apps → OTP Relay →
   Battery → **Unrestricted**. Also turn off any vendor "deep sleep" list.
5. The app must be set up once per machine it serves — for multiple
   machines each client's phone points at the machine handling them.

## The flow end-to-end

1. Machine selects a slot → auto-requests the OTP SMS → Telegram alerts
   `[ID] selected_awaiting_human`
2. SMS lands on the client's phone → APK extracts the code →
   `PUT …/otps/<secret>/<machine_id>.json`
3. Machine polls (2s) → consumes + deletes the node → enters the code →
   captcha auto-solve (if key set) → **Confirm and book**
4. Telegram announces `otp_entered` then `BOOKED ✓ ref …` — or
   `outcome_unknown` if the portal gave an ambiguous response (parked,
   never guessed).

## Notes

- OTPs are short-lived by design; the node is deleted the moment it's
  consumed, so nothing sensitive lingers in the database.
- The machine ID routes the code — if the wrong ID is in the APK, the
  code goes to the wrong machine's node and nothing consumes it (the
  sender-filter field prevents non-portal OTPs from being uploaded).
- No Telegram is involved in the OTP path at all. Telegram only reports:
  started, slot found, OTP entered, booked/failed, session problems.
