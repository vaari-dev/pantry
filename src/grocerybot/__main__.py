"""python -m grocerybot            run the bot
python -m grocerybot check      self-check: shop login, delivery address, home exit IP (exit 1 on failure)
"""

import logging
import sys

from . import config, orders
from .bot import Bot
from .shop import ShopError


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # huggingface/whisper model fetch noise
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
