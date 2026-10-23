# grocery-bot

Order groceries by sending a Telegram voice note or text, like *"bread aur do anda"*. The bot
transcribes it, matches the words to products you have picked before, fills a cart on the shop's
website, checks it against your rules, and places the order, paid from the shop's prepaid wallet.

Nobody browses the shop: no home feed, no ads, no "you might also like".

## How it works

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
- **Daily self-check** at `HEALTH_CHECK_HOUR`: shop login, delivery address, home exit. You get a
  Telegram message only when something is wrong.

## Commands

| | |
|---|---|
| voice note or text | order it (`2 milk, bread`, `anda aur doodh`) |
| `/usual` | items that are due, based on how often you have ordered them |
| `/id` | your Telegram user id |

## Setup

**1. Home exit node.** On a device at home that is on the tailnet:
`sudo tailscale set --advertise-exit-node`, then approve it in the Tailscale admin console.
Create a reusable auth key for the bot (Settings → Keys).

**2. GitHub environment `oracle-cloud`.**

| Variables | |
|---|---|
| `SHOP_URL` | the shop's website |
| `TELEGRAM_USER_IDS` | who may order, comma-separated (each can send `/id` to the bot); the first gets alerts |
| `ADDRESS_LABEL` | name of the saved delivery address on the shop, e.g. `Home` |
| `ADDRESS_MATCH` | text that must be in that address, e.g. the flat number |
| `SSH_HOST`, `SSH_USER` | the box |
| `SSH_KNOWN_HOSTS` | `ssh-keyscan <host>` output |
| `TS_EXIT_NODE` | Tailscale IP of the home exit node |
| `PLACE_ORDERS` | `true` to place orders (default `false`: cart only) |
| `DAILY_CAP_INR`, `MIN_ORDER_INR`, `DEPLOY_DIR` | optional (1000, 100, `/opt/grocery-bot`) |

| Secrets | |
|---|---|
| `SSH_PRIVATE_KEY` | deploy key for the box |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TS_AUTHKEY` | Tailscale auth key (only used for the first login) |
| `SHOP_SESSION` | your shop session, see below (installed on the first deploy) |

**3. Deploy.** Push to `main`. The *Deploy* workflow runs the checks, builds the arm64 image to
ghcr.io, installs Docker on a fresh box, restarts the bot, and runs the self-check.

**4. Shop login** (before the first deploy, and whenever the bot reports it expired):

```sh
pip install playwright && playwright install chromium
SHOP_URL=https://... python scripts/login.py session.json   # log in with phone + OTP in the window
gh secret set SHOP_SESSION --env oracle-cloud < session.json && rm session.json
gh workflow run operate.yml -f action=set-login   # only to replace an expired login
```

`SHOP_SESSION` is your logged-in session: anyone holding it can use the account and its wallet.
Logging out on the shop's website ends it.

## Operating

*Actions → Operate* (or `gh workflow run operate.yml -f action=…`):
`check` · `logs` · `set-login` · `remove` (deletes the bot and its data from the box; confirm with `remove`).

## Development

```sh
pip install -e '.[dev]' && ruff check . && ruff format --check . && pytest
docker compose up --build      # needs a .env (see .env.example) and a shop session in the volume
```

Data on the box lives in the `grocery-bot_data` volume: `session.json`, `catalog.json`,
`ledger.json`, `tailscale/`, and `debug/` (screenshots of failed runs, kept 7 days).
