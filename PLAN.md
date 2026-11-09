# DialUp: build plan

DialUp is a Hermes Agent platform plugin (`kind: platform`). A phone dials a USSD code through
Africa's Talking (AT); Hermes answers by SMS. This file is the working plan: what we build, what
the Hermes source told us, what is still open, and the milestone checklist.

Status: **M2 in progress** (Nov 9): blocked only on a valid AT sandbox key and the live run.

---

## 1. Reference versions

| What | Value |
|---|---|
| `NousResearch/hermes-agent` checked | `4c11aea4511123159785d5422e2dbba3867a4756` (main, Nov 9 2026), not `fc86fb39` from the brief |
| Latest stable Hermes release | `v0.23.0` |
| `NousResearch/hermes-official-plugins` checked | `85e835c3deb972ca49cb58f7d3517b10b6672a1a` |
| Python used for tests | 3.14.6 (Hermes core deps only resolve on 3.14) |

`requires_hermes` will be `">=0.23.0"`: the current release, and the oldest one CI will test
against (CI runs the suite on the `v0.23.0` tag and on `main`).

---

## 2. Confirmed file list

Same layout as the brief, section 6.1. None of these names collide with a core top-level module
(checked with `importlib.util.find_spec` against the core checkout).

```
plugin.yaml  pyproject.toml  __init__.py  adapter.py
ussd.py  smstext.py  phone.py            # pure: no Hermes imports
transport.py  store.py  settings.py  hint.py
dialup_cli.py  simulator.py              # simulator is stdlib only
README.md  LICENSE  CHANGELOG.md  .env.example  .gitignore  pytest.ini
tests/  catalog/dialup.yaml  catalog/PR_DESCRIPTION.md
docs/demo-script.md  docs/screenshots/  docs/banner.png (Greg)
.github/workflows/ci.yml
```

---

## 3. Where the source differs from the brief (source wins)

1. **Commit.** Verified at `4c11aea4`, newer than `fc86fb39`. Everything below is from that commit.
2. **Installing Hermes.** `hermes-agent.nousresearch.com` is blocked by this sandbox's network
   policy, so the install script can't run here. Hermes is installed from the cloned source into
   a Python 3.14 venv (`uv pip install -e ../hermes-agent[messaging]`). `hermes --version` works.
3. **`requires_env` does not disable a plugin in the current loader.** The docs say a missing
   `requires_env` var disables it, but `hermes_cli/plugins_loader.py` only uses the list as
   "activation evidence" for deferred platforms, and the installer prompts for each var. Decision:
   `requires_env: []`; the AT credentials and every `DIALUP_*` setting go under `optional_env`.
   Activation still works because core also treats any `DIALUP_*` env var as evidence
   (`gateway/platform_activation.py`, prefix rule). The catalog entry's `requires_env` is `[]`.
4. **Only bundled platform plugins are deferred** (confirmed in `hermes_cli/plugins_discovery.py`).
   A user-installed DialUp loads eagerly when enabled, so `register_cli_command` works, and
   `__init__.py` must stay import-light because it runs on every `hermes` command. The adapter is
   imported inside `register()`. (The Home Assistant plugin imports its adapter at the top; we
   don't copy that part.)
5. **Message length.** Three knobs, three different readers:
   - `PlatformEntry.max_message_length` is only read by the `send_message` tool / standalone path
     to pre-chunk text. We pass **`0`** so `send()` and `standalone_send` always see the whole reply.
   - `splits_long_messages = True`: cron delivery (`gateway/delivery.py`) then hands us the full
     payload instead of cutting at 4000 chars and appending a server file path. Our `send()` never
     loses text: it stores the whole reply for the USSD reader and SMSes the head with a pointer.
   - `MAX_MESSAGE_LENGTH` = `DIALUP_SMS_MAX_CHARS` (459), set per instance. The plain-text
     `/approve` fallback reads it through `max_message_length_for_chat()`.
   - `reports_partial_delivery = False`: tells core our `send()` can cut text. Core then refuses to
     treat a too-long approval prompt as "shown in full" and makes it Deny-only, which is the
     correct outcome for a cut SMS.
6. **Approval card truncation.** `send_exec_approval` cuts the command preview at
   `_EA_CMD_BUDGET` (3000) and, when it does, tries to send the whole command through `send()`,
   which would be an SMS. With `reports_partial_delivery = False` that send is refused and the
   card becomes Deny-only (core's own rule). For commands under the budget the card text holds the
   whole command; we store it and page it on USSD, and **Approve is only shown on the last page**,
   so nobody approves a command they haven't scrolled through. We don't touch `_EA_*` attributes.
7. **Busy acks.** LINE's `_SYSTEM_BYPASS_PREFIXES` is still `("⚡", "⏳", "⏩", "💾")`. Core also
   stamps mid-turn sends with `metadata["_interim_send"]` (LINE checks both). We check both too.
   `display_tier="minimal"` does not switch off `busy_steer_ack_enabled`; we also pass
   `display_overrides={"busy_steer_ack_enabled": False}` when the field exists.
   Gateway start/restart notices (`♻ Gateway online…`) go to the home channel on every start
   (`PlatformConfig.gateway_restart_notification` defaults to true), so we add `♻` to our
   "status, don't SMS" prefixes, and the README suggests `gateway_restart_notification: false`.
8. **Allowlist number format.** Core's gateway authz compares `DIALUP_ALLOWED_USERS` entries to
   `source.user_id` as raw strings. If a user writes `0803…` and we send `+234803…`, core would
   reject a number our adapter accepted. Fix: implement the adapter hook core already reads,
   `resolved_allowlist_user_ids()` (`gateway/authz_mixin.py`, used by Discord), returning the
   E.164 form of each entry, re-read live on every call so a removed number stops working at once.
   The README still tells users to write E.164.
9. **Not public, not used:** `gateway.platforms.shared_ingress.bind_listener` (own aiohttp
   `AppRunner`/`TCPSite` instead) and `gateway.platforms.send_targets.StandaloneSendPolicy` (the
   SMS adapter uses it; we leave it out).
10. **Dedup key.** The brief's `hash(text)` is salted per process. Fine for an in-memory dedup,
    but we use a short `hashlib.sha1` digest so the key is stable and testable.
11. **`hermes plugins install` syntax** confirmed: `hermes plugins install Emediong-Etuk/<repo> --enable`.
    Because we declare `aiohttp`, install asks for dependency consent (`--yes-deps` skips the
    question in scripts).
12. **Per-platform tools syntax** confirmed: `hermes tools disable --platform dialup <toolset>`.

Verified as the brief says: inbound `MessageEvent` / `build_source` shape; background
`handle_message`; `/stop` text from an adapter is handled as a typed command; the approval helpers
(`approval_refusal(session_key, actor_id, extra=, request_id=)`,
`resolve_gateway_approval_card(session_key, choice, request_id, actor_id=, extra=)`, which returns
the number resolved and refuses non-deny while the card is Deny-only); `platform_http_client()`
is SSRF-safe httpx; `plugin_db()` is WAL SQLite with `check_same_thread=False`;
`MessageDeduplicator(ttl_seconds=...)`; the public import list in `public-api.md`.

---

## 4. Design decisions (my calls, noted so you can overrule)

- **Lazy adapter factory.** `adapter_factory` and `standalone_sender_fn` are small functions in
  `__init__.py` that import `adapter.py` on first call, so enabling DialUp doesn't add aiohttp to
  every `hermes` command. (The brief passes the class directly; core only calls the factory.)
- **Two allowlist gates.** The adapter checks the allowlist before it touches the agent and answers
  `END This number can't use this service.`; core's gateway authz then checks again. Both read the
  same env var.
- **SQLite off the event loop.** Store calls go through `asyncio.to_thread`. Each call is tiny, but a
  WAL checkpoint can stall, and the USSD handler has a 500 ms budget.
- **State machine is pure.** `ussd.py` takes `(state, text, context)` and returns
  `(response, new_state, action)`; the adapter does the I/O. This keeps every menu path testable
  without aiohttp.
- **Local transport for judges.** `DIALUP_TRANSPORT=local` writes SMS to
  `plugin-data/dialup/outbox.jsonl`, needs no account, and only starts on a loopback host.
- **Real agent turns in this sandbox** can use OpenRouter (the proxy injects the key), so M1's
  "real agent turn" can be checked here. A real AT sandbox run can't: the container can't receive
  a tunnel callback. That run happens on your machine (M2).

---

## 5. Open questions for Greg

1. ~~Repo name~~ **Decided:** keep `Emediong-Etuk/DialUp`.
2. ~~Release branch~~ **Decided:** merge this branch to `main` at M5; the repo is public.
3. ~~Deadline~~ **Confirmed:** 11 PM WAT, Nov 11.
4. **What I need from you, and when:**
   - M2: AT sandbox API key (enter it through the secure credential form, never in chat), the USSD
     service code AT assigns, a test phone number for `DIALUP_ALLOWED_USERS`, and your tunnel URL.
     I'll also need `api.sandbox.africastalking.com` allowed in this sandbox's network to test the
     transport against the real API; otherwise I test against a fake server and you run the live
     check.
   - M5: go-ahead before anything public (catalog PR, X post, form).

---

## 6. Milestones (WAT). Stop and report at each.

**M0, Mon night**
- [x] Clone `hermes-agent` and `hermes-official-plugins`; install Hermes
- [x] Read the docs and core files from the brief's Phase 0 list
- [x] `PLAN.md`
- [x] Scaffold (`plugin.yaml`, `pyproject.toml`, `__init__.py`, minimal adapter, `pytest.ini`, conftest)
- [x] `hermes plugins doctor . --ci` clean
- [x] `hermes plugins list` shows `dialup`; `hermes dialup --help` works

M0 evidence: 9 registration tests pass; `doctor --ci` OK; `validate` passes with security scan
`safe` (one `role_pretend` caution from CLI wording was fixed); a real `hermes gateway run`
connected DialUp in local mode and `GET /dialup/health` returned `ok`.

**M1, Tue midday (P0)**
- [x] `ussd.py`, `smstext.py`, `phone.py` with full unit tests
- [x] Adapter webhook + LocalTransport + store
- [x] `hermes dialup simulate` end to end with a real agent turn; reply in outbox and USSD reader

M1 evidence: 131 tests pass (USSD, SMS text, phone, transport, webhook, send, standalone send,
simulator, registration). `doctor --ci` OK, `validate --install-deps` passes, security scan `safe`.
Real run: `hermes gateway run` (local transport, OpenRouter model) + `hermes dialup simulate
--keys "1|In one sentence, what is USSD?"`: the answer arrived in the outbox after 8 s, and
`3 Last reply` paged the same stored text in 3 screens, each under 160 chars.

M1 notes:
- **AT docs unreachable from here.** `developers.africastalking.com` is blocked by this sandbox's
  network policy, so the AT transport follows the brief (form POST to `/version1/messaging`,
  `apiKey` header, recipient `statusCode` 100/101/102 = success). It is tested against a fake
  server only. Verify against the live sandbox in M2.
- **Core logs message text.** DialUp's own logs hold only redacted numbers and lengths, but core's
  `gateway.run` logs `inbound message: ... msg='<text>'` at INFO. Not ours to change; the README
  says so.
- **Hermes' first-contact greeting** ("Hi, I'm Hermes... build a short profile?") is appended to
  the first reply by core. It makes the first SMS longer; nothing for the plugin to do.
- The validator's security scan flagged literal invisible characters (zero-width spaces in the
  GSM-7 replacement table). All non-ASCII in source is now either the visible GSM-7 alphabet or
  written as `\uXXXX` escapes.
- `DIALUP_USSD_PAGE_CHARS` now has a floor of 120 (in `settings.py`) so the home screen always fits.

**M2, Tue evening (P0)**
- [x] AfricasTalkingTransport (tests against a fake server; request shape checked against AT's official SDK)
- [ ] Real AT sandbox run through your tunnel
- [x] Cron `deliver=dialup` + `standalone_sender_fn`
- [x] Hourly caps, outbound allowlist, security items 1–8

M2 notes (Nov 9):
- **AT request shape verified** against AT's official Python SDK (`africastalking` 2.0.3 from PyPI,
  read, not run): form POST to `{base}/version1/messaging`, headers `apiKey` + `Accept:
  application/json`, fields `username`, `to`, `message`, `bulkSMSMode=1`, optional `from`; response
  `SMSMessageData.Recipients[].statusCode` (101 = sent). We now send `bulkSMSMode=1` too. AT's docs
  site sits behind a Cloudflare bot check, so scripts can't read it.
- **Live sandbox send: 401 "The supplied authentication is invalid"**, with and without our
  placeholder header, for over 2 minutes after the key was saved. The request reaches AT, so the
  saved key itself is being rejected (wrong app, regenerated since, or a prefix in the form).
  Waiting on Greg to re-check the key.
- **Cron without a gateway works:** `hermes cron create "every 1h" ... --deliver dialup`, then
  `hermes cron run` + `hermes cron tick`, delivered through `standalone_send` into the outbox.
  Core wraps cron output in a "Cronjob Response / job_id / ----" header and footer (2 SMS parts
  for a one-line reminder). `cron.wrap_response: false` in config.yaml turns it off; it's a global
  setting, so the README recommends it rather than the plugin changing it.
- **Security audit (items 1–8):** no `os.environ`/`os.getenv`/`.env` reads in plugin code; every
  DialUp log line has only redacted numbers, lengths or status codes.

**M3, Tue night (P0)**
- [ ] README with Disclosure, `.env.example`, CI workflow
- [ ] `hermes plugins validate . --install-deps` passes; every warning read and explained
- [ ] Repo pushed public

**M4, Wed morning**
- [ ] P1: approvals over USSD, `5 Stop task`, quick tasks if not done
- [ ] Cut rule: if M2 isn't done by Tue 22:00 WAT, skip approvals and polish P0
- [ ] Record the demo together

**M5, Wed by 18:00 WAT**
- [ ] Final commit → SHA → `catalog/dialup.yaml` + `catalog/PR_DESCRIPTION.md`
- [ ] (after your go) catalog PR, X post, form

**P2 (only with spare time):** inbound two-way SMS, AT balance in `status`, older-replies list.

### Definition of done
- All tests green; `doctor` and `validate` clean or every warning explained
- Fresh `hermes plugins install` from the public repo + `local` quick start in under 2 minutes
- A real AT sandbox USSD session produces an SMS reply
- README has a Disclosure section; catalog capabilities match the manifest
- Demo script ready
