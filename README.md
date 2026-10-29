# Pantry

A Telegram bot that orders groceries from Zepto, from a voice note or text like *"bread aur do anda"*.
Using it: [vaari-dev.github.io/pantry](https://vaari-dev.github.io/pantry/), built from `site/` by
`python3 site/build.py`, which fails if the site disagrees with the code.

```
Telegram ⇄ bot ─ Whisper: voice → text
               ├ Claude: text → items, basket edits, settings
               └ Zepto's MCP server: search, cart, bill, order
```

Each order is recorded before it is placed and confirmed in Zepto's history, so an interrupted
order is reported, never retried.

## Deploy

GitHub environment `oracle-cloud`: variables `TELEGRAM_ADMIN_ID`, `SSH_HOST`, `SSH_USER`,
`SSH_KNOWN_HOSTS`, optional `DEPLOY_DIR`; secrets `TELEGRAM_BOT_TOKEN`, `SSH_PRIVATE_KEY`,
`ANTHROPIC_API_KEY`. A push to `main` deploys; a new bot token makes the old bot point everyone to
the new one first. *Actions → Operate* runs `check`, `logs` or `remove`.

Logs: Loki, `{service_name="pantry"}`; never message text.

## Develop

```sh
uv sync && uv run ruff check . && uv run ruff format --check . && uv run pytest
```
