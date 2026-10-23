"""Settings of the bot itself, from the environment (see .env.example). Each account's own settings
(address, limits, ordering on/off) are kept in households.json and changed in Telegram."""

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
    admin_id: int  # Telegram user who lets new people in and gets the bot's own alerts
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
        admin_id=int(_required("TELEGRAM_ADMIN_ID")),
        whisper_model=os.environ.get("WHISPER_MODEL", "small"),
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        # docker/entrypoint.sh serves this SOCKS5 proxy (exiting at home) whenever TS_EXIT_NODE is set.
        proxy=TAILSCALE_PROXY if os.environ.get("TS_EXIT_NODE") else None,
        health_hour=int(os.environ.get("HEALTH_CHECK_HOUR", "9")),
    )
