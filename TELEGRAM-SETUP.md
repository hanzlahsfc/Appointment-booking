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
| `/otp-request NADIR-A4F2` | portal sends the SMS code |
| `/otp NADIR-A4F2 123456` | enters the SMS code into the held booking |
| `/confirm NADIR-A4F2` | presses **Confirm and book** — completes it |
| `/pause NADIR-A4F2` · `/resume` · `/stop` · `/start` | monitor control |

`/otp 123456` without an id also works — but only on the machine that is
actually waiting for a code, so it can't hit the wrong one.

## The OTP flow end-to-end

1. Machine finds a slot → group gets
   `[ID] selected_awaiting_human · NAME · slot 12/11/2025 09:00`
   plus the exact commands to send next.
2. You send `/otp-request ID` → portal texts the applicant's phone.
3. You send `/otp ID 123456` → machine fills the code.
4. If a captcha appears, a human solves it **on that machine** (captcha
   can't be answered through Telegram — that's a portal rule, not ours).
5. You send `/confirm ID` → machine presses Confirm and book, then
   reports `BOOKED ✓ ref …` or parks the applicant for review if the
   outcome is ambiguous — it never guesses a booking.

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
