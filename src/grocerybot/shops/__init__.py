"""The shops a household can order from, by the key its data is kept under."""

from .base import Browser, Cart, NotPlaced, Rules, ShopError
from .blinkit import Blinkit
from .zepto import Zepto

SHOPS: dict[str, type[Browser]] = {"zepto": Zepto, "blinkit": Blinkit}

__all__ = ["SHOPS", "Browser", "Cart", "NotPlaced", "Rules", "ShopError"]
