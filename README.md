# grocery-bot

Order groceries by Telegram voice note or text, like *"bread aur do anda"*. Each family connects
its own shop account; the bot fills the cart, checks it, and pays from the shop's wallet, or cash
on delivery when that falls short.

```
Telegram ⇄ bot (OCI box, Docker)
            ├─ Whisper: voice → text
            ├─ catalog: words → your products, per account
            └─ real Chrome on a virtual screen → Tailscale → home exit node → shop
```

The shop refuses headless browsers and data-centre IPs, hence real Chrome leaving through home.

## Use

New people message the bot; the admin taps *Let them in*. `/setup` (private chat) asks for the
phone number, the code the shop texts, and one of the account's saved addresses; the bot deletes
the number and code from the chat. Family joins through an owner's `/invite` link.

Every cart is checked before it is placed: only the expected items, the account's address, paid
fully from the wallet or fully on delivery, within the daily limit. Lists under the
smallest order wait in the basket; a cart that reaches it is placed 3 minutes later, or at once with
*Order now*. Each attempt is recorded before *Place Order*, so an interrupted order is reported,
never retried.

| | |
|---|---|
| voice or text | order it |
| `/basket`, `/clear`, `/usual` | waiting items, empty them, what is due |
| `/settings` | the account's settings; the owner changes them with `/limit 1500`, `/minimum 99`, `/ordering off`, `/setup` (address, login) |
| `/members`, `/invite`, `/leave` | who orders here; add one person; leave (the last one out deletes the account) |
| `/id` | your Telegram id |

## Deploy

1. On a device at home: `sudo tailscale set --advertise-exit-node`, approve it, and create a
   reusable auth key.
2. GitHub environment `oracle-cloud`:
   variables `SHOP_URL`, `TELEGRAM_ADMIN_ID`, `TS_EXIT_NODE` (its Tailscale IP), `SSH_HOST`,
   `SSH_USER`, `SSH_KNOWN_HOSTS`, optional `DEPLOY_DIR`;
   secrets `TELEGRAM_BOT_TOKEN`, `TS_AUTHKEY`, `SSH_PRIVATE_KEY`.
3. Push to `main`. *Actions → Operate* runs `check`, `logs` or `remove` (deletes the bot and its data).

Logs are JSON lines in Loki: `{service_name="grocery-bot"}`. They never hold message text.

## Develop

```sh
pip install -e '.[dev]' && ruff check . && ruff format --check . && pytest
```
