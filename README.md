# grocery-bot

Order groceries by sending a Telegram voice note or text, like *"bread aur do anda"*. The bot
transcribes it, matches the words to products you have picked before, fills a cart on the shop's
website, checks it against your rules, and places the order, paid from the shop's prepaid wallet.

Nobody browses the shop: no home feed, no ads, no "you might also like".

Several families can use one bot, each with **their own shop account**: they connect it from
Telegram (phone number + OTP), and the people they invite order from it too.

## How it works

- **Accounts (households):** one shop account each, with its own login, picks, basket, order
  history and settings. Whoever connected it (`/setup`) is its owner: they can log it in again,
  add and remove members (`/invite`, `/members`) and change its settings. Everyone in it orders
  from its wallet.
- **Settings, per account** (`/settings` shows them; the owner changes them):

  | | starts at | change |
  |---|---|---|
  | delivery address: its name on the shop and text that must be in it (e.g. the flat number) | set in `/setup` | `/address` |
  | daily limit | ₹1000 | `/limit 1500` |
  | smallest order (the shop adds a delivery fee below it; smaller lists wait in the basket) | ₹100 | `/minimum 99` |
  | placing orders (off: fill and check the cart only) | off | `/ordering on` |

- **Getting in:** someone new sends `/start`; the admin gets a *Let them in* button. Once in, they
  `/setup` their own account. Or an owner's `/invite` link (one person, 24 hours) brings them
  straight into that account.
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
- **Rules checked on every cart:** only the expected items and quantities, the account's delivery
  address, paid fully from the wallet (no reward coins, nothing left to pay), at least its smallest
  order, within what is left of its daily limit. Anything off: not placed.
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
| voice note or text | order it (`2 milk, bread`, `anda aur doodh`); small lists wait in the basket until the total reaches the account's smallest order |
| `/basket`, `/clear` | show or empty the waiting basket |
| `/usual` | items that are due, based on how often you have ordered them |
| `/setup` | connect your shop account, or log it in again (private chat; `/cancel` stops it) |
| `/settings` | the account's settings; the owner changes them with `/address`, `/limit`, `/minimum`, `/ordering` |
| `/invite` | owner: a link (or `/join CODE`) for one person to order with your account |
| `/members` | who orders with this account; the owner gets buttons to remove people |
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
| `TELEGRAM_ADMIN_ID` | the admin's Telegram user id (send `/id` to the bot): lets new people in, gets the bot's alerts |
| `SSH_HOST`, `SSH_USER` | the box |
| `SSH_KNOWN_HOSTS` | `ssh-keyscan <host>` output |
| `TS_EXIT_NODE` | Tailscale IP of the home exit node |
| `DEPLOY_DIR` | optional (`/opt/grocery-bot`) |

| Secrets | |
|---|---|
| `SSH_PRIVATE_KEY` | deploy key for the box |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TS_AUTHKEY` | Tailscale auth key (only used for the first login) |
| `SHOP_SESSION` | optional, see *Upgrading* below |

**3. Deploy.** Push to `main`. The *Deploy* workflow runs the checks, builds the arm64 image to
ghcr.io, installs Docker on a fresh box, restarts the bot, and runs the self-check.

**4. Accounts.** Send `/setup` to the bot in a private chat and follow it, then `/ordering on`
once a cart or two look right. Others send `/start` (you get a button to let them in) or open a
link from your `/invite`.

**Upgrading a single-account box.** Set `TELEGRAM_ADMIN_ID`; `TELEGRAM_USER_IDS`, `ADDRESS_*`,
`DAILY_CAP_INR`, `MIN_ORDER_INR` and `PLACE_ORDERS` are no longer read and can be deleted. On its
first start the bot moves the shop login, catalog and ledger already in the volume into the
account `home`, owned by the admin. Then, in Telegram: `/address`, `/ordering on` (and `/limit`,
`/minimum` if they differed), and `/invite` the rest of the family. To install its login from your
own computer instead of `/setup`:

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
