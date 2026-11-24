# wb-game dashboard refinement

Working source copied from the installed dashboard without `__pycache__`. The installed plugin was not modified.

## Data semantics

- History is queried from AUTO-MAS through its read-only MCP history/detail tools. The 04:00 Asia/Shanghai daily boundary remains separate from the 30-day successful-log reading window.
- Configured account names come from AUTO-MAS `ScriptConfig.json` `SubConfigsInfo.UserData`, joined to instance types. The two HSR account groups are `HSRConfig` (two active named accounts) and `SrcConfig` (one active named simulator account). Their IDs are present in `QueueConfig`; two HSR-like `GeneralConfig` rows are not queued and are excluded. Same account names under different scripts stay separate.
- The current runtime snapshot is also queried read-only. Its latest response had one active task and eight scheduled script rows, but no task row mapped to a configured account identity. This is reported as snapshot metadata and does not create account-level running results.
- Only configured scripts receive status lights. Light rule is account based: any account error → red, any account without a run today / unknown → gray, every account success (or warning) → green. History-only scripts have no light. Games without configured scripts retain banner and chat access without an unknown/gray status placeholder.
- Each account renders as its own collapsible card (status, stamina, last run evidence). Stamina shows the projected value only — no raw reading, formula, or progress bar. Upcoming banners are a separate block pinned above the account cards.
- M7A reads its registered installation and local `logs/runs` files only. The installation's `settings/accounts` directory is empty, so the successful run records and stamina log reading are shown at script level; no account count is inferred from run metadata.
- ZZZ task status and stamina come from the installed ZZZ-Onedragon local logs. Its profile account is matched internally against AUTO-MAS account configuration and only the configured display name is surfaced. AUTO-MAS ZZZ history is excluded from outcomes.
- Stamina only appears when a successful detail log contains a supported measurement. A parsed line is evidence of a reading; the estimate is a separate field and label.
- Local M7A/ZZZ timestamps are interpreted as local CST; AUTO-MAS detail timestamps retain the server's observed `+04:00` interpretation.
- Character banners are sourced from the configured local ChronoRail `game-versions.json`; weapon-only entries and version-only entries are omitted from the character banner list.
- The chat panel vendors the shared `wb-agent-chat` files unchanged and uses `moduleId=wb-game` with a game/script object slot. It supplies selected game/script/account context and performs no game actions.

## Current source evidence and remaining limits (2026-11-22)

- A live AUTO-MAS MCP read-only pass succeeded for history, detail and runtime snapshot. The configured roster now resolves the requested three AUTO-MAS HSR accounts across `HSRConfig` (two) and `SrcConfig` (one). Today, the two `HSRConfig` accounts have no matching history and remain unknown. `<崩铁模拟器账号>` has an error followed by a later same-day success; the latest per-account result is success.
- The live runtime snapshot contained one task, but it did not expose a user/account identity that could be joined to configuration. It is intentionally not assigned to any of the HSR accounts.
- The M7A install has recent successful routine runs. A local log contains `开拓力：3/300`; its actual local timestamp is now parsed as CST and the estimate advances from that reading. The install exposes no account identity, so M7A remains a script-level result.
- The latest ZZZ-Onedragon native log ends with `一条龙` success and contains a native `剩余电量 20` reading. Its profile resolves to the configured display alias `<绝区零账号>`; the prior AUTO-MAS ZZZ error does not override this native result. The same native log contains a mail-push error, which is not treated as a game-task failure.
- The current Genshin detail logs show the daily reward was claimed and contain a resin reading of 20. No current unclaimed-reward case was observed; the warning path is covered by a red-capable fixture using the observed native log line shape.
- The current MAA detail for `<明日方舟账号>` contains non-theme task failures, so it remains an account error. The installed MAA `asst.log` contains the native `TaskChainError` shape for `SwitchTheme`, but no isolated SwitchTheme-only run was found in the current log window. The exception rule is covered with that actual log shape; a live isolated exception remains unverified.
- The HSR simulator detail logs in the queried window contain no supported stamina measurement, so its stamina remains unknown. Configured HSR accounts with no current-day history remain unknown rather than being inferred from the runtime snapshot.
- No password, token or full phone number is included in the API output, fixtures, tests or this report. Read-only source files were not modified. The installed plugin was not modified.

## Validation

Run `python -m pytest tests -q -p no:cacheprovider` (Playwright is optional; browser cases skip when it is absent). The package contains parser/source regressions plus Playwright browser checks against the shipped dashboard bundle.
