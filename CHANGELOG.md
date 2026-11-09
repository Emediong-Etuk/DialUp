# Changelog

## 0.1.0 (unreleased)

- USSD menu over Africa's Talking: ask, quick tasks, paged last-reply reader, stop task, help.
- SMS out: GSM-7 text, word-boundary cut with a "read the rest on USSD" footer, hourly caps, outbound allowlist.
- Gateway busy/status messages shown on the USSD home screen instead of costing an SMS.
- Approvals over USSD: one SMS alert, the whole command paged on screen, then Approve once / Deny; times out with Hermes.
- Cron `deliver=dialup`, also without a running gateway.
- `local` transport and `hermes dialup simulate` for trying it with no account.
- CLI: `status`, `secret`, `simulate`, `send-test`, `purge`.
