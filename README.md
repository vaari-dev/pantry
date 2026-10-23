# grocery-bot

Order groceries by sending a Telegram voice note or text, like *"bread aur do anda"*. The bot
transcribes it, matches the words to products you have picked before, fills a cart on the shop's
website, checks it against your rules, and places the order, paid from the shop's prepaid wallet.

Nobody browses the shop: no home feed, no ads, no "you might also like".

Several families can use one bot, each with **their own shop account**: they connect it from
Telegram (phone number + OTP), and the people they invite order from it too.

## How it works

- **Accounts (households):** one shop account each: its login, delivery address, picks, basket,
  order history and daily limit. Whoever connected it (`/setup`) is its owner: they can log it in
  again, `/invite` family and change the `/limit`. Everyone in it orders from its wallet.
- **Getting in:** someone new sends `/start`; the first admin gets a *Let them in* button. Once in,
  they `/setup` their own account. Or an owner's `/invite` code (one person, 24 hours) lets them
  straight into that account with `/join CODE`.
- **Connecting** (`/setup`, private chat only): share your phone number, name the saved address
  (as in the shop's app) and something always in it, like the flat number. The bot opens the
  shop's login, you send it the OTP the shop texts you, and it picks that address. Your phone
  number and OTP messages are deleted from the chat. Re-run `/setup` when the login expires.

```
Telegram ⇄ bot (OCI box, Docker)
            ├─ Whisper: voice → text
            ├─ catalog: words → your products (best first, then backups), per-item limits
            ├─ ledger: daily spend cap, no double orders, "what's due"
            └─ Chrome (real, on a virtual screen) → Tailscale → home exit node → shop website
```

- **New items:** anything the bot doesn't know is searched on the shop and offered as photos with
  size and price. Tap **Pick**; the pick is remembered for next time.
- **Rules checked on every cart:** only the expected items and quantities, the configured delivery
  address, paid fully from the wallet (no reward coins, nothing left to pay), at least
  `MIN_ORDER_INR` (the shop adds a delivery fee below it), within today's `DAILY_CAP_INR`.
  Anything off: not placed.
- **No double orders:** each attempt is written down before *Place Order* is tapped. If the bot
  stops mid-order, it tells you and never retries on its own. An order counts as placed only once
  it shows up on the shop's Orders page.
- **Why a real Chrome through home:** the shop refuses headless browsers and data-centre IP
  addresses. Chrome runs normally (Xvfb) and only its traffic leaves through your home connection
  via a Tailscale exit node. Nothing about the browser is faked.
- **Daily self-check** at `HEALTH_CHECK_HOUR`: home exit (the first admin hears if it fails), then
  each account's login and delivery address (its owner hears if it fails). Silent when all is well.
- **All accounts leave through the same home exit node.** Delivery follows each account's own saved
  address, wherever it is.

## Commands

| | |
|---|---|
| voice note or text | order it (`2 milk, bread`, `anda aur doodh`); small lists wait in the basket until the total reaches `MIN_ORDER_INR` |
| `/basket`, `/clear` | show or empty the waiting basket |
| `/usual` | items that are due, based on how often you have ordered them |
| `/setup` | connect your shop account, or log it in again (private chat; `/cancel` stops it) |
| `/invite`, `/join CODE` | owner: a code for one person to order with your account; they send `/join CODE` |
| `/members`, `/limit [₹]` | who orders with this account; show or (owner) change its daily limit |
| `/leave` | stop ordering with this account (the last one out deletes it here) |
| `/id` | your Telegram user id |

## Setup

**1. Home exit node.** On a device at home that is on the tailnet:
`sudo tailscale set --advertise-exit-node`, then approve it in the Tailscale admin console.
Create a reusable auth key for the bot (Settings → Keys).

**2. GitHub environment `oracle-cloud`.**

| Variables | |
|---|---|
| `SHOP_URL` | the shop's website |
| `TELEGRAM_USER_IDS` | admins, comma-separated (each can send `/id` to the bot): they let new people in; the first gets the bot's alerts |
| `ADDRESS_LABEL`, `ADDRESS_MATCH` | optional, see *Upgrading* below |
| `SSH_HOST`, `SSH_USER` | the box |
| `SSH_KNOWN_HOSTS` | `ssh-keyscan <host>` output |
| `TS_EXIT_NODE` | Tailscale IP of the home exit node |
| `PLACE_ORDERS` | `true` to place orders (default `false`: cart only) |
| `DAILY_CAP_INR`, `MIN_ORDER_INR`, `DEPLOY_DIR` | optional (1000: each new account's starting limit, 100, `/opt/grocery-bot`) |

| Secrets | |
|---|---|
| `SSH_PRIVATE_KEY` | deploy key for the box |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TS_AUTHKEY` | Tailscale auth key (only used for the first login) |
| `SHOP_SESSION` | optional, see *Upgrading* below |

**3. Deploy.** Push to `main`. The *Deploy* workflow runs the checks, builds the arm64 image to
ghcr.io, installs Docker on a fresh box, restarts the bot, and runs the self-check.

**4. Accounts.** Send `/setup` to the bot in a private chat and follow it. Others send `/start`
(you get a button to let them in) or `/join` with a code from your `/invite`.

**Upgrading a single-account box.** On its first start the bot turns the old setup into the
account `home`: if `ADDRESS_LABEL` (name of the saved delivery address, e.g. `Home`) and
`ADDRESS_MATCH` (text that must be in it, e.g. the flat number) are set, the shop login, catalog
and ledger already in the volume move into it, owned by the first admin, with every admin in it.
After that the two variables are not read again. To install its login from your own computer
instead of `/setup`:

```sh
pip install playwright && playwright install chromium
SHOP_URL=https://... python scripts/login.py session.json   # log in with phone + OTP in the window
gh secret set SHOP_SESSION --env oracle-cloud < session.json && rm session.json
gh workflow run operate.yml -f action=set-login   # only to replace an expired login
```

A shop login is a logged-in session: anyone holding it can use the account and its wallet. Logging
out on the shop's website ends it.

## Operating

*Actions → Operate* (or `gh workflow run operate.yml -f action=…`):
`check` (home exit; each account is reported, only the exit fails the run) · `logs` · `set-login` · `remove` (deletes the bot and its data from the box; confirm with `remove`).

**Logs** are JSON lines on stdout (`ts`, `level`, `logger`, `msg`, plus fields such as `result`,
`item_total`, `job`, `seconds`). The Grafana/Loki stack on the box picks them up; query
`{service_name="grocery-bot"}`. They hold events and ids only, never message text or tokens.

## Development

```sh
pip install -e '.[dev]' && ruff check . && ruff format --check . && pytest
docker compose up --build      # needs a .env (see .env.example) and a shop session in the volume
```

Data on the box lives in the `grocery-bot_data` volume: `households.json` (accounts, who is in
which, invites), `households/<id>/` with each account's `session.json`, `catalog.json`,
`ledger.json` and `debug/`, plus `tailscale/` and `debug/` (screenshots of failed runs and logins,
kept 7 days).
