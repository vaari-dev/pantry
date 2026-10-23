"""Settings, all from the environment (see .env.example). Nothing personal lives in the code."""

import os
from dataclasses import dataclass
from pathlib import Path

TAILSCALE_PROXY = "socks5://127.0.0.1:1055"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set (see .env.example)")
    return value


@dataclass(frozen=True)
class Config:
    shop_url: str  # the shop's website, e.g. https://www.example.com
    telegram_token: str
    admin_ids: tuple[int, ...]  # Telegram users who let new people in; the first also gets the bot's own alerts
    # The first household, for a box set up before accounts existed (optional; others use /setup).
    address_label: str
    address_match: str
    daily_cap: float  # default for new households; each owner can change theirs with /limit
    min_order: float  # the shop adds a delivery fee below this
    place_orders: bool
    whisper_model: str
    data_dir: Path
    proxy: str | None  # SOCKS5 proxy that exits through the home connection
    health_hour: int  # local hour for the daily self-check

    @property
    def debug_dir(self) -> Path:  # screenshots of failed logins and checks only, pruned
        return self.data_dir / "debug"


def load() -> Config:
    return Config(
        shop_url=_required("SHOP_URL"),
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        admin_ids=tuple(int(i) for i in _required("TELEGRAM_USER_IDS").replace(" ", "").split(",") if i),
        address_label=os.environ.get("ADDRESS_LABEL", "").strip(),
        address_match=os.environ.get("ADDRESS_MATCH", "").strip(),
        daily_cap=float(os.environ.get("DAILY_CAP_INR", "1000")),
        min_order=float(os.environ.get("MIN_ORDER_INR", "100")),
        place_orders=os.environ.get("PLACE_ORDERS", "false").lower() == "true",
        whisper_model=os.environ.get("WHISPER_MODEL", "small"),
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        # docker/entrypoint.sh serves this SOCKS5 proxy (exiting at home) whenever TS_EXIT_NODE is set.
        proxy=TAILSCALE_PROXY if os.environ.get("TS_EXIT_NODE") else None,
        health_hour=int(os.environ.get("HEALTH_CHECK_HOUR", "9")),
    )
