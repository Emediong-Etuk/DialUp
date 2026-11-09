# Changelog

## 0.1.0 (unreleased)

- USSD menu over Africa's Talking: ask, quick tasks, paged last-reply reader, stop task, help.
- SMS out: GSM-7 text, word-boundary cut with a "read the rest on USSD" footer, hourly caps, outbound allowlist.
- Gateway busy/status messages shown on the USSD home screen instead of costing an SMS.
- Cron `deliver=dialup`, also without a running gateway.
- `local` transport and `hermes dialup simulate` for trying it with no account.
- CLI: `status`, `secret`, `simulate`, `send-test`, `purge`.
