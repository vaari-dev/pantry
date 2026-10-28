# grocery-bot

Order groceries by Telegram voice note or text, like *"bread aur do anda"*. Each family connects
its own shop account; the bot fills the cart, checks it, and pays from the shop's wallet, or cash
on delivery when that falls short.

```
Telegram ⇄ bot (OCI box, Docker)
            ├─ Whisper: voice → text
            ├─ Claude: text → items, basket edits, "now", a saved address, settings
            ├─ catalog: words → your products, per account
            └─ Zepto's official MCP server (HTTPS, JSON): search, cart, bill, order, history
```

The bot signs in as Zepto's MCP clients do (OAuth: phone, then the texted code) and keeps each
account's tokens, renewing them before they lapse.

## Use

New people message the bot; the admin taps *Let them in*. `/setup` (private chat) asks for the
phone number, the code the shop texts, and one of the account's saved addresses; the bot deletes
the number and code from the chat. Family joins through an owner's `/invite` link.

The bot sets the shop cart to exactly the basket, then checks the shop's bill before ordering: only
the expected items, the account's address, paid fully from the wallet or fully on delivery, within
the daily limit. Lists under the smallest order wait in the basket; a basket that reaches it is
placed 3 minutes later, or at once (even under it) with *Order now* or "abhi". Each attempt is recorded before the order call
and confirmed by its id in the shop's history, so an interrupted order is reported, never retried.

| | |
|---|---|
| voice or text | order it, change the basket or a setting; "to work" sends this basket to that saved address |
| `/basket`, `/clear`, `/usual` | waiting items, empty them, what is due |
| `/settings` | the account's settings; the owner changes them with `/limit 1500`, `/minimum 99`, `/ordering off`, `/setup` (address, login) |
| `/members`, `/invite`, `/leave` | who orders here; add one person; leave (the last one out deletes the account) |
| `/id` | your Telegram id |

## Deploy

1. GitHub environment `oracle-cloud`: variables `TELEGRAM_ADMIN_ID`, `SSH_HOST`, `SSH_USER`,
   `SSH_KNOWN_HOSTS`, optional `DEPLOY_DIR`; secrets `TELEGRAM_BOT_TOKEN`, `SSH_PRIVATE_KEY`,
   `ANTHROPIC_API_KEY`.
2. Push to `main`. *Actions → Operate* runs `check`, `logs` or `remove` (deletes the bot and its data).

Logs are JSON lines in Loki: `{service_name="grocery-bot"}`. They never hold message text.

## Develop

```sh
pip install -e '.[dev]' && ruff check . && ruff format --check . && pytest
```
