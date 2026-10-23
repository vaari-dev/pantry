"""python -m grocerybot            run the bot
python -m grocerybot check      self-check: shop login, delivery address, home exit IP (exit 1 on failure)
"""

import os
import sys

from . import config, logs, orders
from .bot import Bot
from .shop import ShopError


def main() -> None:
    logs.setup(os.environ.get("LOG_LEVEL", "INFO"))
    cfg = config.load()
    if sys.argv[1:] == ["check"]:
        try:
            print(f"ok: logged in, delivering to {cfg.address_label}, exit IP {orders.check(cfg)}")
        except ShopError as e:
            sys.exit(f"check failed: {e}")
        return
    Bot(cfg).run()


if __name__ == "__main__":
    main()
