# Hermes Agent Integration Notes

Goal: eventually expose Trader Joe / Hermes trading workflows through Nous Research Hermes Agent.

## Relevant Hermes Agent Surfaces

- Python library: embed `AIAgent` directly for programmatic analysis flows.
- Plugin system: add Trader Joe tools such as `watch_symbol`, `queue_buy`, `queue_sell`, `positions`, and `daily_report`.
- Telegram gateway: Hermes Agent can already run through Telegram, so Trader Joe commands could become Hermes tools or slash commands instead of a separate polling daemon.
- Cron: scheduled portfolio reports and scans can be moved from custom systemd loops into Hermes cron jobs.
- Context files and skills: preserve Trader Joe rules, risk policy, and command syntax as project context or a dedicated skill.

## Likely Migration Path

1. Keep the current `traderjoe.service` stable.
2. Extract trading actions from `telegram_approvals.py` into a small service module with pure functions:
   - add/list watchlist symbols
   - queue buy/sell approvals
   - list positions
   - build/send report
3. Create a Hermes plugin wrapping those functions as tools.
4. Add a Trader Joe skill or context file explaining risk rules and approval flow.
5. Optionally replace the custom Telegram polling daemon with Hermes Agent's Telegram gateway once command parity is verified.

## Safety Requirements

- Preserve human approval for all buy/sell execution.
- Keep Alpaca paper trading mode explicit.
- Keep max buy sizing and forbidden ticker checks.
- For sells, reject amounts greater than current position value unless using `all`.
- Make all tool handlers return structured JSON and never raise uncaught exceptions.

## Docs Reviewed

- https://hermes-agent.nousresearch.com/docs/
- https://hermes-agent.nousresearch.com/docs/guides/python-library
- https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin
- https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram
- https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
