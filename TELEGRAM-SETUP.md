# Telegram control — run the crawler on many machines, command them all from one chat

Every machine polls Telegram **outbound only** — no public IP, no port
forwarding, no tunnel. Works behind school/office firewalls and NAT.

## One-time setup (~5 minutes)

1. **Create the bot** — in Telegram, message `@BotFather` → `/newbot` →
   pick a name. Copy the token (`123456:ABC-DEF…`).
2. **Create a group** — New Group → add the bot → name it e.g.
   `Visa Ops`. Send any message in it.
3. **Get the chat id** — open this in a browser:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Find `"chat":{"id":-100…` — copy the negative number.
   (If the list is empty, send another group message and reload.)
4. **On every machine** — edit `config.yaml` in the extracted folder:

   ```yaml
   telegram:
     token: "123456:ABC-DEF…"
     chat_id: "-100…"
   ```

   Same token + same chat id on all machines — that is what links them.
5. Start the app. The console shows `Telegram relay: on — this machine is
   [NADIR-A4F2]` and the bot posts `[NADIR-A4F2] online` in the group.
   Each machine invents its own id — shown in brackets, saved in
   `data\machine-id`.

## Commands (type in the group)

| command | does |
|---|---|
| `/status` | every machine replies — status, paired tab, queue counts |
| `/status NADIR-A4F2` | only that machine replies |
| `/queue NADIR-A4F2` | applicant list + statuses |
| `/confirm NADIR-A4F2` | presses **Confirm and book** — completes it |
| `/pause NADIR-A4F2` · `/resume` · `/stop` · `/start` | monitor control |

## The booking flow end-to-end (OTP is handled by the APK — see README-APK.md)

1. Machine finds a slot → group gets
   `[ID] selected_awaiting_human · NAME · slot 12/11/2025 09:00`
   and the machine auto-requests the SMS itself.
2. The APK on the client's phone reads the SMS and pushes the code
   straight to the machine over Firebase — nobody types anything.
3. Machine enters it → captcha auto-solve (if a key is set) → Confirm
   and book → `BOOKED ✓ ref …` or `outcome_unknown` if the response was
   ambiguous (parked, never guessed).
4. `/confirm ID` and `/captcha ID` remain as manual fallbacks.

## Safety notes

- Keep the group private — anyone in it can drive the machines. The bot
  ignores messages from every other chat.
- OTP codes are sensitive; the group's members are the only readers.
- If the portal's response is ambiguous the machine says
  `outcome_unknown` and parks — check the tab on that PC before
  confirming again.
- The bot needs internet on each machine (api.telegram.org must be
  reachable). If the relay goes quiet, the machines still work —
  commands just queue for when they're back.

## CAPTCHA auto-solve (optional — 2captcha)

Set `captcha.api_key` in `config.yaml` (or env `CAPTCHA_API_KEY`):

- `/confirm <ID>` automatically attempts a solver-pass before pressing
  Confirm and book — a failed solve still lets a human finish manually.
- `/captcha <ID>` solves on demand (30–90s typical).
- Without a key, captcha stays manual on the machine — nothing else changes.

Cost is ~$1–3 / 1000 solves on 2captcha.com; the token is injected into
`g-recaptcha-response` and its callback fired — no bypass of the form.
