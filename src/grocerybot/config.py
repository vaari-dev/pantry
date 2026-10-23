"""Settings from the environment (.env.example)."""

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
    shop_url: str
    telegram_token: str
    user_ids: tuple[int, ...]  # first gets alerts
    address_label: str  # saved address name
    address_match: str  # text the address must contain
    daily_cap: float
    min_order: float  # delivery fee below this
    place_orders: bool
    whisper_model: str
    data_dir: Path
    proxy: str | None  # exits at home
    health_hour: int

    @property
    def state_file(self) -> Path:  # secret
        return self.data_dir / "session.json"

    @property
    def catalog_file(self) -> Path:
        return self.data_dir / "catalog.json"

    @property
    def ledger_file(self) -> Path:
        return self.data_dir / "ledger.json"

    @property
    def debug_dir(self) -> Path:  # failure screenshots
        return self.data_dir / "debug"


def load() -> Config:
    return Config(
        shop_url=_required("SHOP_URL"),
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        user_ids=tuple(int(i) for i in _required("TELEGRAM_USER_IDS").replace(" ", "").split(",") if i),
        address_label=_required("ADDRESS_LABEL"),
        address_match=_required("ADDRESS_MATCH"),
        daily_cap=float(os.environ.get("DAILY_CAP_INR", "1000")),
        min_order=float(os.environ.get("MIN_ORDER_INR", "100")),
        place_orders=os.environ.get("PLACE_ORDERS", "false").lower() == "true",
        whisper_model=os.environ.get("WHISPER_MODEL", "small"),
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        # Served by docker/entrypoint.sh.
        proxy=TAILSCALE_PROXY if os.environ.get("TS_EXIT_NODE") else None,
        health_hour=int(os.environ.get("HEALTH_CHECK_HOUR", "9")),
    )
