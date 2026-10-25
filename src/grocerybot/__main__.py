"""python -m grocerybot            run the bot
python -m grocerybot check      self-check; fails only if the home exit is down
python -m grocerybot apimap     the shop's API calls seen so far, without values
"""

import json
import os
import sys

from . import config, logs, orders, store
from .accounts import Accounts
from .bot import Bot
from .shops import ShopError


def main() -> None:
    logs.setup(os.environ.get("LOG_LEVEL") or "INFO")
    cfg = config.load()
    if sys.argv[1:] == ["apimap"]:
        found = store.read(cfg.api_map, None)
        return print(json.dumps(found, indent=1, ensure_ascii=False, sort_keys=True) if found else "nothing seen yet")
    if sys.argv[1:] != ["check"]:
        return Bot(cfg).run()
    try:
        print(f"ok: exit IP {orders.exit_ip(cfg)}")
    except ShopError as e:
        sys.exit(f"check failed: {e}")
    for hh in Accounts(cfg.data_dir, cfg.admin_id).all():
        try:
            orders.check(cfg, hh)
            print(f"ok: account {hh.id} ({hh.shop})")
        except ShopError as e:
            print(f"not ready: account {hh.id} ({hh.shop}): {e}")


if __name__ == "__main__":
    main()
