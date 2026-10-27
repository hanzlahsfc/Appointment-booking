# Appointment Booker — local app

Monitor the Greece appointment portal and book through your own signed-in
Chrome session. Everything runs on YOUR PC — no account to create, no
cloud, no other clients.

## Setup (once)

1. Extract the ZIP anywhere — the folder can have spaces in the name.
2. Run **`SETUP.bat`**
   - If it says *"Python 3.11+ not found"*: install Python from
     https://www.python.org/downloads/ (tick **Add python.exe to PATH** —
     no admin rights needed), then re-run SETUP.bat.
   - It creates a local virtual environment and installs dependencies.
   - Then it shows you how to load the Chrome extension:
     `chrome://extensions` → **Developer mode** → **Load unpacked** →
     choose the `extension\` folder inside this package.
     (The batch file cannot install the extension for you — Chrome
     requires it be loaded by hand.)

## Every run

1. Run **`START.bat`** — a local server starts and your dashboard opens in
   the browser (the URL carries a token that signs you in).
2. In Chrome, open the official portal and **sign in / solve the captcha**
   on the appointment page as usual.
3. In the extension popup, select **that tab** — the dashboard pairs it.
4. Add your applicants to the queue (name + details — private, stored
   only in this folder), pick the visa category and date range, press
   **Start**.
5. When a matching slot appears the app selects it, fills the applicant's
   details, and sounds an alarm.
6. Click **Request SMS code**, type the code when it arrives, solve the
   captcha on the page, then click **Confirm and book**.
7. On a verified booking the app marks that applicant done, returns to
   the form, loads the next applicant, and keeps going. If the outcome is
   ever unclear it parks the queue for you to check — it never retries a
   maybe-booked submission on its own.

## Notes

- Keep the PC **awake** and Chrome signed in while it monitors.
- Queue + progress persist in `data\` — you can close and restart freely.
- The centre/VAC is fixed by the portal account — the app shows it, and
  can only book at the centre your account uses.
- OTP and the final submit are always manual — the app never books by
  itself and never bypasses captcha.
- If the extension loses the tab (closed/reloaded), pair the tab again.
