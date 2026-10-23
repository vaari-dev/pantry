# grocery-bot

Order groceries by Telegram voice note or text (*"bread aur do anda"*). No feed, ads or upsells.

```
Telegram ⇄ bot (Docker, OCI)
            ├─ Whisper: voice → text
            ├─ catalog: words → products (pick + 2 backups), qty limits
            ├─ ledger: daily cap, basket, no double orders
            └─ Chrome (Xvfb) → Tailscale → home exit node → shop
```

- **New items:** searched and offered as photos; your pick is remembered.
- **Basket:** lists under `MIN_ORDER_INR` wait; the order goes once the total reaches it.
- **Every cart is checked:** expected items only, delivery address, wallet pays all, no coins,
  within `DAILY_CAP_INR`. Otherwise not placed.
- **No double orders:** attempts are recorded before *Place Order*; never retried.
- **Home exit:** the shop blocks headless browsers and data-centre IPs.
- **Daily self-check** at `HEALTH_CHECK_HOUR`; alerts only on failure.

## Commands

| | |
|---|---|
| voice or text | order (`2 milk, bread`) |
| `/basket`, `/clear` | show or empty the basket |
| `/usual` | items due, by past orders |
| `/id` | your Telegram id |

## Setup

1. **Exit node:** at home, `sudo tailscale set --advertise-exit-node`; approve it; create a reusable auth key.
2. **GitHub environment `oracle-cloud`:**

   | Variable | |
   |---|---|
   | `SHOP_URL` | shop website |
   | `TELEGRAM_USER_IDS` | who may order, comma-separated; first gets alerts |
   | `ADDRESS_LABEL`, `ADDRESS_MATCH` | saved address name, and text it must contain |
   | `SSH_HOST`, `SSH_USER`, `SSH_KNOWN_HOSTS` | the box |
   | `TS_EXIT_NODE` | exit node's Tailscale IP |
   | `PLACE_ORDERS` | `true` to order (default: cart only) |
   | `DAILY_CAP_INR`, `MIN_ORDER_INR`, `DEPLOY_DIR` | optional: 1000, 100, `/opt/grocery-bot` |

   | Secret | |
   |---|---|
   | `SSH_PRIVATE_KEY` | box deploy key |
   | `TELEGRAM_BOT_TOKEN` | from @BotFather |
   | `TS_AUTHKEY` | first Tailscale login only |
   | `SHOP_SESSION` | shop login (step 3) |

3. **Shop login** (and when the bot says it expired). Treat `session.json` as a password.
   ```sh
   SHOP_URL=https://... python scripts/login.py session.json   # needs playwright
   gh secret set SHOP_SESSION --env oracle-cloud < session.json && rm session.json
   gh workflow run operate.yml -f action=set-login              # replace on a running box
   ```
4. **Deploy:** push to `main`. Checks → arm64 image on ghcr.io → Docker on the box (first time) → restart → self-check.

## Operate

`gh workflow run operate.yml -f action=check|logs|set-login|remove` (`remove` needs `-f confirm=remove`).

Logs are JSON lines (`ts`, `level`, `logger`, `msg`, fields), no message text or secrets.
Grafana: `{service_name="grocery-bot"}`.

## Develop

```sh
pip install -e '.[dev]' && ruff check . && ruff format --check . && pytest
docker compose up --build   # needs .env (.env.example) and a session in the volume
```

Volume `grocery-bot_data`: `session.json`, `catalog.json`, `ledger.json`, `tailscale/`, `debug/` (7 days).
