# Pairing + first check — step by step

Everything below works on any Windows PC — no machine-specific paths.
`<this folder>` means wherever you extracted the ZIP.

## Step 1 — Load the Chrome extension (one time)

1. Open **`chrome://extensions`** in Chrome.
2. Turn on **Developer mode** — toggle in the top-right.
3. Click **Load unpacked** (top-left) and choose:
   `<this folder>\extension`
4. The **Appointment Booker** icon appears in the toolbar — pin it so it's
   easy to click.

   Chrome shows a small "debugging this browser" banner while attached —
   that is normal; it is how the extension drives the tab.

## Step 2 — Start the app

Run **`START.bat`** (double-click).

- The local server starts on port **8080** and your dashboard opens in
  the browser automatically.
- If a server is already running it just re-opens the dashboard.
- **Keep the black window open** while the app runs.

If the dashboard does not open by itself, the URL is also printed in the
window — copy it into your browser. It contains a `?token=` value — that
token signs you in.

## Step 3 — Sign into the portal tab yourself

1. In Chrome, open the official appointment page:
   `https://pk-gr-services.gvcworld.eu/appointments/add`
2. **Sign in manually** and complete any captcha, until the booking form
   loads.
3. Leave that tab open — the app never logs in for you.

## Step 4 — Pair the tab

1. Click the **Appointment Booker extension icon** → popup opens.
2. Copy the **token** shown on the dashboard's *"Pair portal tab"* card
   (also in the dashboard URL after `?token=`).
3. Paste it into the popup's token field → press **Connect**.
4. Still in the popup, **select your signed-in portal tab** from the list.
5. Back on the dashboard, press **Pair**.

Expected result: `paired ✓`. The app only accepts a tab on the portal
origin — selecting the tab is not enough by itself; the token is what
authenticates the connection.

## Step 5 — First read-only check

With the tab paired, the app can read the page and run a normal
availability **Search** — the same click a person makes. Search is
**read-only**: it never requests OTP, never fills a captcha, never clicks
Book. Those are always manual.

## Troubleshooting

| what you see | what it means |
|---|---|
| popup won't connect | token typo, or port must be `8080` |
| `pair` fails: tab not on portal | you selected the wrong tab — pick the signed-in one |
| `no extension connected` | extension not loaded or popup Connect not pressed |
| dashboard asks for token | open it via START.bat — the URL carries it |

## Keep in mind

- PC must stay **awake**; Chrome stays **signed in** on the portal tab.
- Queue and progress persist in `data\` — restarting is safe.
- The centre (VAC) is fixed by your portal account — the app shows it and
  can only book at that centre.
- Nothing ever submits a booking without your explicit **Confirm and book**.
