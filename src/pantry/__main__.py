"""python -m pantry            run the bot
python -m pantry check      self-check; fails only if the shop is unreachable
"""

import os
import sys

from . import config, logs, orders, shop
from .accounts import Accounts
from .bot import Bot
from .shop import ShopError


def main() -> None:
    logs.setup(os.environ.get("LOG_LEVEL") or "INFO")
    cfg = config.load()
    if sys.argv[1:] != ["check"]:
        return Bot(cfg).run()
    if not shop.reachable():
        sys.exit("check failed: the shop's MCP server is not reachable")
    print("ok: the shop is reachable")
    for hh in Accounts(cfg.data_dir, cfg.admin_id).all():
        try:
            orders.check(hh)
            print(f"ok: account {hh.id}")
        except ShopError as e:
            print(f"not ready: account {hh.id}: {e.logged}")
        except Exception as e:
            print(f"not ready: account {hh.id}: {type(e).__name__}")


if __name__ == "__main__":
    main()
