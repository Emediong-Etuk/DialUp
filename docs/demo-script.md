# DialUp demo: under 90 seconds

**Goal:** show a real Hermes agent answering a basic phone over USSD + SMS, with no app and no data.
**Record at:** 1920x1080, browser zoomed so the Africa's Talking (AT) simulator phone fills the left half and a
terminal the right half. Total target: 85 s.

## Before recording

- [ ] Gateway running with the AT sandbox (`hermes gateway run`), tunnel up, callback URL set on channel `*384*37264#`.
- [ ] `~/.hermes/.env`: `DIALUP_ALLOWED_USERS=+2347012345678`, `DIALUP_HOME_CHANNEL=+2347012345678`,
      `DIALUP_SERVICE_CODE=*384*37264#`, `DIALUP_QUICK_PROMPTS=Summarise my day|Any urgent email?`
- [ ] `cron.wrap_response: false` in `config.yaml` (so the reminder SMS is one clean line).
- [ ] `approvals.mode: manual` in `config.yaml` so shot 4 really asks (the default `smart` may wave it through).
- [ ] Tools for the phone limited: `hermes tools disable --platform dialup terminal code_execution computer_use browser file image_gen tts vision`
      (keep `web` and `cronjob`). For shot 4 (P1), temporarily re-enable `terminal`.
- [ ] Send one throwaway message first, so Hermes' first-contact greeting is out of the way.
- [ ] `hermes dialup purge --yes` so "Last reply" starts clean, then do one dry run.
- [ ] Simulator number `+2347012345678`. If the AT simulator is down, use `hermes dialup simulate` in a large
      terminal instead (same screens).

## Shot list

| Time | On screen | Caption (bottom third) |
|---|---|---|
| 0–8 s | Simulator phone. Dial `*384*37264#`. The DialUp home menu appears. | **No data. No app. Just dial.** |
| 8–20 s | Press `1` → "Type your message:" → type `remind me to pay light bill fri 9am` → "Sent to Hermes. Your answer will come by SMS." | **Talk to your own AI agent from any phone** |
| 20–35 s | SMS arrives in the simulator ("Done, I'll remind you Friday 9am..."). Cut to the terminal: `hermes cron list` showing the new job. | **Hermes set a real reminder on your server** |
| 35–50 s | Dial again → `1` → `what's the dollar to naira rate today?` → END screen. SMS answer arrives (web search). | **Answers come back by SMS** |
| 50–60 s | Dial → `3 Last reply` → page `(1/3)` → `98` → `(2/3)`. | **Long answers? Read them free on USSD** |
| 60–75 s | **(P1)** Ask `check disk space on my server` → SMS "Hermes needs your OK... dial *384*37264# then 4" → dial → `4` → command shown in full → `98` → `1 Approve once` → result SMS. **If P1 didn't ship:** `hermes cron run <id>` (fast-forward) → reminder SMS arrives. | **You stay in control: nothing runs without your OK** / **Reminders reach you anywhere** |
| 75–90 s | Title card: `hermes plugins install Emediong-Etuk/DialUp`, "Works on MTN, Airtel, Glo, 9mobile via Africa's Talking", `github.com/Emediong-Etuk/DialUp`, "Hermes Agent Plugin Hackathon". | **DialUp for Hermes Agent** |

Tips: speed up the waiting between "Sent to Hermes" and the SMS (2x), never fake a screen, keep each caption on
screen at least 2 s. No music needed; if used, keep it quiet.

## X post options (each under 280 characters)

1. Your AI agent shouldn't need data. DialUp lets anyone reach their Hermes agent from any phone: dial a USSD
   code, ask, and the answer comes back by SMS. Works on MTN, Airtel, Glo and 9mobile. Built for the Hermes Agent
   Plugin Hackathon @NousResearch

2. No data, dead smartphone, or a parent with a basic phone? Dial a short code and talk to your own Hermes agent.
   DialUp turns USSD + SMS into a Hermes channel: menus, reminders, paged answers. My entry for the
   @NousResearch Hermes Agent Plugin Hackathon

3. Most AI agents stop working when your data runs out. I built DialUp: a Hermes Agent plugin that answers over
   USSD and SMS through Africa's Talking, on any GSM phone. Demo below. Hermes Agent Plugin Hackathon
   @NousResearch

(Africa's Talking is not tagged: their handle isn't confirmed.)
