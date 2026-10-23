"""python -m grocerybot            run the bot
python -m grocerybot check      self-check: home exit IP (exit 1 on failure), then each account's
                                shop login and delivery address (reported; their owners are alerted)
"""

import os
import sys

from . import config, logs, orders
from .accounts import Accounts
from .bot import Bot
from .shop import ShopError


def main() -> None:
    logs.setup(os.environ.get("LOG_LEVEL", "INFO"))
    cfg = config.load()
    if sys.argv[1:] == ["check"]:
        try:
            print(f"ok: exit IP {orders.exit_ip(cfg)}")
        except ShopError as e:
            sys.exit(f"check failed: {e}")
        households = Accounts(cfg.data_dir, cfg.admin_ids).all()
        for hh in households:
            try:
                orders.check(cfg, hh)
                print(f"ok: account {hh.id}: logged in, delivering to {hh.address_label}")
            except ShopError as e:
                print(f"not ready: account {hh.id}: {e}")
        if not households:
            print("no shop accounts yet: send /setup to the bot")
        return
    Bot(cfg).run()


if __name__ == "__main__":
    main()
