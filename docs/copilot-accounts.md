# Copilot accounts: subscription only

The Claude copilot runs through the Claude Code CLI and only ever uses a **Claude subscription** (Pro, Max,
Team, Enterprise). It never uses an Anthropic API key or Console account, so it cannot bill per token.

## What the app does

- **Strips API billing from the CLI's environment.** Before it starts `claude`, the app removes
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` and
  `CLAUDE_CODE_USE_FOUNDRY`. A key set for some other tool (a Python SDK, a shell profile) cannot take over.
  The same environment is used for the sign-in check, so the status you see is what the copilot gets.
- **Refuses a non-subscription sign-in.** `claude auth status --json` must report `authMethod: "claude.ai"`.
  Anything else (a Console account, an API-key helper) shows a banner with **Switch account** and the copilot
  does not start.
- **Signs in with the subscription flow.** "Sign in to Claude" runs `claude auth login --claudeai`.
- **Shows the plan.** The panel status reads like `Claude Code 2.1.295 · you@example.com · max plan`.
- **The "$" figure is not a bill.** After each answer Claude Code reports `total_cost_usd`: the API price of
  that answer. On a subscription the panel shows it as `$1.13 API-equiv. (max plan, not billed)`; the
  answer counts toward the plan's usage limits instead.

## Switching accounts (e.g. a different subscription later)

1. Copilot panel → **Switch account** (the person icon in the top bar).
2. Confirm. The app runs `claude auth logout`, then opens `claude auth login --claudeai` in a console and your
   browser.
3. Sign in with the other Claude account, then press **Check again**. The status line shows the new email and
   plan.

The Claude Code sign-in is shared by everything on this Windows account that uses Claude Code: the Claude
desktop app's Code tab, a terminal `claude`, and the AetherCFD copilot. Switching in one place switches all
of them.

From a terminal the same switch is:

```
claude auth logout
claude auth login --claudeai
claude auth status --json
```

Check that the last command prints `"authMethod": "claude.ai"` and the expected `subscriptionType`.

## Turning the guard off

Only for a deliberate API-billed setup: set `copilot_subscription_only = false` in `settings.toml`
(`%APPDATA%\AetherCFD`). With it off the API variables are passed through and any sign-in method is accepted.
