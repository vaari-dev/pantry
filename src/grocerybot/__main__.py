"""python -m grocerybot            run the bot
python -m grocerybot check      self-check; fails only if the home exit is down
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
    if sys.argv[1:] != ["check"]:
        return Bot(cfg).run()
    try:
        print(f"ok: exit IP {orders.exit_ip(cfg)}")
    except ShopError as e:
        sys.exit(f"check failed: {e}")
    for hh in Accounts(cfg.data_dir, cfg.admin_id).all():
        try:
            orders.check(cfg, hh)
            print(f"ok: account {hh.id}")
        except ShopError as e:
            print(f"not ready: account {hh.id}: {e}")


if __name__ == "__main__":
    main()
