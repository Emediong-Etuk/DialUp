# DialUp

**Reach your Hermes agent from any phone: no app, no data. Dial a USSD code to ask; the answer comes back by SMS.**

DialUp is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway platform plugin built on
[Africa's Talking](https://africastalking.com), which runs USSD and SMS on MTN, Airtel, Glo and 9mobile in Nigeria,
and in Kenya, Uganda and Tanzania.

```
+---------- *384*55# ----------+        +====== SMS from Hermes =======+
| Hermes                       |        | The naira closed at 1,520 to |
| 1 Ask or reply               |        | the dollar on the parallel   |
| 2 Quick tasks                |        | market today; the official   |
| 3 Last reply                 |        | rate was 1,480. Analysts     |
| 5 Stop task                  |        | expect the gap [Full reply:  |
| 0 Help                       |        | dial *384*55# then 3]        |
+------------------------------+        +==== 157 chars, 1 part(s) ====+
```

*Output of `hermes dialup simulate`. The SMS here is cut at 160 characters to fit the picture; the default limit is 459.*

## Why USSD

Hermes lives in chat apps, and every one of them needs a smartphone and mobile data. When your data runs out,
the network is bad, or someone with a basic phone (a parent, a customer, a field worker) needs your agent, the
agent on your server can't be reached. USSD works on every GSM phone ever sold, with no data, and gives you a live
menu session, which plain SMS can't.

## How it works

```
 Phone ──dials *384*55#──> MTN / Airtel / Glo / 9mobile ──> Africa's Talking
                                                                 │  HTTPS POST (per keypress)
                                                                 v
                                     your tunnel or reverse proxy (https://.../dialup/ussd/<secret>)
                                                                 │
                                                                 v
                    DialUp webhook (127.0.0.1:8787) ──> Hermes gateway ──> your agent
                                                                                │
 Phone <──SMS── network <── Africa's Talking SMS API <── DialUp send() <────────┘
```

- **USSD in.** A menu session on the phone's screen. Pick a number or type a message.
- **SMS out.** Answers, scheduled-job results, reminders and approval alerts arrive by SMS.
- **USSD reader.** Long answers can be read page by page on the USSD screen (`3 Last reply`), so nobody pays for
  a six-part SMS. Each SMS is capped (459 characters by default) and ends with `[Full reply: dial *384*55# then 3]`
  when it was cut.

## Two-minute quick start (no account)

The `local` transport writes SMS to a file instead of sending them, and the built-in simulator plays the phone.

```bash
hermes plugins install Emediong-Etuk/DialUp --enable
```

Hermes asks for consent to install DialUp's one dependency (`aiohttp`); `--yes-deps` answers it for you.
Then add these lines to `~/.hermes/.env` (use your own number):

```
DIALUP_TRANSPORT=local
DIALUP_ALLOWED_USERS=+2348031234567
```

Start the gateway:

```bash
hermes gateway run
```

In a second terminal, dial in:

```bash
hermes dialup simulate
```

Press `1`, type a question, and watch the "SMS" arrive in the simulator. `3` reads it page by page.

## Africa's Talking sandbox setup

The sandbox is free, and nothing is billed or delivered to real phones.

1. **Account and key.** Sign up at [account.africastalking.com](https://account.africastalking.com), click
   **Go to sandbox app**, then **Settings → API Key** and generate a key. The username is always `sandbox`.
2. **USSD channel.** In the sandbox app, **USSD → Create Channel**. You get a code like `*384*37264#`
   (the sandbox allows two channels).
3. **Webhook secret.** Run `hermes dialup secret` and add the line it prints to `~/.hermes/.env`.
4. **Settings.** Add to `~/.hermes/.env`:

   ```
   AFRICASTALKING_USERNAME=sandbox
   AFRICASTALKING_API_KEY=<your sandbox key>
   DIALUP_ALLOWED_USERS=<the number you'll use in the simulator, e.g. +2347012345678>
   DIALUP_HOME_CHANNEL=<the same number, for scheduled jobs>
   DIALUP_SERVICE_CODE=*384*37264#
   ```

5. **Tunnel.** Start the gateway (`hermes gateway run`), then expose the webhook:

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8787
   ```

6. **Callback.** Set the channel's callback URL to `https://<tunnel-host>/dialup/ussd/<your secret>`.
7. **Dial.** Open [simulator.africastalking.com](https://simulator.africastalking.com), enter your number, dial the
   code. SMS replies appear in the same simulator window.

`hermes dialup status` shows what DialUp sees, and `hermes dialup send-test --to <number>` sends one SMS.

## Going live

- Ask Africa's Talking for a production USSD service code and an SMS sender ID, then set
  `AFRICASTALKING_USERNAME` to your live app's username and `DIALUP_SMS_FROM` to the sender ID.
- **Costs** are per USSD session and per SMS, billed to your Africa's Talking account. DialUp's hourly caps
  (`DIALUP_SMS_HOURLY_CAP`, `DIALUP_TURNS_HOURLY_CAP`) bound what one number can spend.
- Some numbers on DND (do-not-disturb) reject promotional routes; check Africa's Talking's guidance for your
  country and use a transactional sender ID.
- Put the webhook behind HTTPS (a tunnel or a reverse proxy). DialUp itself listens on `127.0.0.1` by default.

## Recommended Hermes settings for a phone

A keypad is a weak place to supervise powerful tools, and every message costs money. Suggested:

```bash
hermes tools disable --platform dialup terminal code_execution computer_use browser file image_gen tts vision
```

That leaves web search, memory, reminders (`cronjob`), todo, skills and clarifying questions. In
`~/.hermes/config.yaml`, `cron.wrap_response: false` drops the "Cronjob Response / job_id" header core adds to
scheduled-job output, which otherwise makes a one-line reminder cost two SMS parts. That setting applies to every
platform.

## Configuration

Every setting can be an env var in `~/.hermes/.env` or a key under `gateway.platforms.dialup.extra` in
`config.yaml`; env wins. The two secrets are read only through Hermes' secret store.

| Env var | Default | Purpose |
|---|---|---|
| `AFRICASTALKING_USERNAME` | (none) | App username; `sandbox` for the sandbox |
| `AFRICASTALKING_API_KEY` | (none, secret) | API key, sent only to the Africa's Talking messaging host in the `apiKey` header |
| `DIALUP_TRANSPORT` | `africastalking` | `africastalking`, or `local` (SMS go to `plugin-data/dialup/outbox.jsonl`) |
| `DIALUP_SERVICE_CODE` | `*384*55#` | Code shown in SMS footers and help |
| `DIALUP_SMS_FROM` | (empty) | Sender ID or shortcode; leave empty in the sandbox |
| `DIALUP_AT_API_BASE` | auto | Override the messaging host (default: sandbox host for username `sandbox`, else live) |
| `DIALUP_WEBHOOK_HOST` | `127.0.0.1` | Listen address |
| `DIALUP_WEBHOOK_PORT` | `8787` | Listen port |
| `DIALUP_WEBHOOK_SECRET` | (none, secret) | Secret path segment; required unless `local` on loopback |
| `DIALUP_ALLOWED_USERS` | (empty) | Comma-separated numbers. **Empty means nobody.** `0803…` and `+234803…` both work |
| `DIALUP_ALLOW_ALL_USERS` | `false` | Let any number use your agent and receive SMS (logged loudly) |
| `DIALUP_HOME_CHANNEL` | (empty) | Your number, for cron `deliver=dialup` |
| `DIALUP_DEFAULT_COUNTRY_CODE` | `234` | Turns `0803…` into `+234803…` |
| `DIALUP_SMS_MAX_CHARS` | `459` | Longest SMS (3 parts); longer replies are cut and kept for the reader |
| `DIALUP_USSD_PAGE_CHARS` | `160` | Characters per USSD screen, footer included (120–182) |
| `DIALUP_QUICK_PROMPTS` | (empty) | Up to 5 saved prompts, `\|`-separated: `Summarise my day\|Any urgent email?` |
| `DIALUP_SMS_HOURLY_CAP` | `20` | SMS per number per hour |
| `DIALUP_TURNS_HOURLY_CAP` | `30` | Agent turns started from USSD per number per hour |
| `DIALUP_STORE_REPLIES` | `true` | Keep recent replies for the USSD reader |
| `DIALUP_REPLY_TTL_HOURS` | `24` | How long stored replies are kept |

## Menu reference

| Key | Screen |
|---|---|
| `1` Ask or reply | Type a message. It goes to the agent (this also answers Hermes' questions); the answer comes by SMS. |
| `2` Quick tasks | Your `DIALUP_QUICK_PROMPTS`, one key each. Typing on a keypad is slow. |
| `3` Last reply | The latest answer, paged. `98` next page, `0` back. Costs no SMS. |
| `4` Approve | Shown only when a command is waiting for your OK. |
| `5` Stop task | Sends `/stop` to stop what the agent is doing. |
| `0` Help | How DialUp works. |
| `00` | Home from anywhere. |

Hermes' own busy messages ("Queued", "Interrupting") and "gateway online" notices are not sent as SMS; the latest
one shows on the home screen instead.

## CLI

| Command | What it does |
|---|---|
| `hermes dialup status` | Transport, callback path (secret masked), allowlist, caps, last-hour SMS and turn counts |
| `hermes dialup secret` | Generates a webhook secret and prints the `.env` line and callback URL (writes nothing) |
| `hermes dialup simulate` | Terminal phone that dials your running gateway. `--keys "1\|hello"` runs it non-interactively |
| `hermes dialup send-test --to <number>` | One test SMS through the configured transport (allowlist applies) |
| `hermes dialup purge` | Deletes stored replies, pending approvals, rate counters and the local outbox |

`simulator.py` also runs on its own with only the Python standard library:
`python simulator.py --url http://127.0.0.1:8787/dialup/ussd/<secret> --phone +2348031234567`.

## Security model

- **The phone number is trusted only because the request comes from Africa's Talking through a secret URL.**
  Africa's Talking doesn't sign USSD callbacks, so anyone who learns the callback URL can send requests in the
  name of any phone number. The secret path segment (compared in constant time) is what stops that. Keep it out of
  screenshots and logs, and rotate it with `hermes dialup secret` if it leaks.
- **Local by default.** The webhook listens on `127.0.0.1`; expose it with a tunnel or reverse proxy over HTTPS.
  The `local` transport refuses to start on a non-loopback address.
- **Small, strict inputs.** Only `POST` form bodies up to 16 KiB; every field is length-checked; anything else
  gets `400`/`413`.
- **Allowlist fails closed.** An empty `DIALUP_ALLOWED_USERS` lets nobody in. Numbers are normalised before
  comparison. The allowlist also applies to **outbound** SMS (plus your home number), so a prompt injection or a
  bad config can't turn your account into an SMS toll-fraud machine. Unknown numbers get
  `END This number can't use this service.`, no agent turn and no pairing code.
- **Caps survive restarts.** Hourly SMS and agent-turn caps per number are stored in SQLite. Past the SMS cap you
  get one "limit reached" SMS per hour; replies stay readable on USSD.
- **Quiet logs.** DialUp logs numbers only in redacted form (`+234****4567`) and message lengths, never text.
  Note that Hermes core's own gateway log records each incoming message's text at INFO level.
- **Secrets through Hermes only.** The API key and webhook secret are read through Hermes' profile-scoped secret
  store, never from `os.environ` or `.env` files directly, and never logged.
- **Approvals.** Commands that need your OK go through Hermes' own approval system and its timeout: if
  nobody answers, the command does not run. DialUp never auto-approves.

## Disclosure

What DialUp does on your machine, so you can decide before installing:

- **Network calls:** only to the Africa's Talking messaging host for your account
  (`api.sandbox.africastalking.com` or `api.africastalking.com`, or `DIALUP_AT_API_BASE`), with
  `AFRICASTALKING_API_KEY`, to send SMS. No other hosts. No telemetry or usage reporting of any kind. The `local`
  transport makes no network calls at all.
- **Inbound listener:** while the gateway runs, an HTTP server on `DIALUP_WEBHOOK_HOST:DIALUP_WEBHOOK_PORT`
  (default `127.0.0.1:8787`) with routes `/dialup/ussd/<secret>`, `/dialup/ussd-events/<secret>` and
  `/dialup/health`.
- **Agent turns:** each message an allowlisted number sends starts a full agent turn with the toolset you allow
  for the `dialup` platform (see "Recommended Hermes settings").
- **Costs:** each USSD session and each SMS is billed to your Africa's Talking account (free in the sandbox).
- **Stored data:** in `<HERMES_HOME>/plugin-data/dialup/`: `data.db` (SQLite) with the last 5 replies per number
  (kept `DIALUP_REPLY_TTL_HOURS`, default 24 h; off with `DIALUP_STORE_REPLIES=false`), rate-cap timestamps (1 h),
  the latest status line, and pending approvals; and `outbox.jsonl` with the `local` transport. `hermes dialup
  purge` deletes all of it. Nothing is stored in the plugin's own directory.
- **No shell commands, no subprocesses, no self-updates.** The plugin never runs commands itself; it reads no files
  outside its plugin-data directory.

## Limitations

- Tested against the Africa's Talking **sandbox** and the built-in simulator; not yet on a live short code.
- A USSD screen holds about 160 characters, and typing on a keypad is slow: long questions are better as quick
  tasks. Networks end idle USSD sessions quickly, so long reads may need a redial.
- v0.1 has no inbound two-way SMS: you reach Hermes by USSD, Hermes reaches you by SMS.
- Hermes core adds a short greeting to the first reply on a new chat, which makes that first SMS longer.

## Development

Hermes' dependencies need Python 3.14.

```bash
git clone https://github.com/NousResearch/hermes-agent ../hermes-agent
python -m pip install -e "../hermes-agent[messaging]" pytest pytest-asyncio
PYTHONPATH=../hermes-agent python -m pytest -q
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

`ussd.py`, `smstext.py` and `phone.py` are pure modules with no Hermes imports. See `PLAN.md` for design notes.

## License

MIT
