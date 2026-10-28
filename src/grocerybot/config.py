"""The bot's settings, from the environment (see .env.example). Accounts keep their own in households.json."""

import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set (see .env.example)")
    return value


@dataclass(frozen=True)
class Config:
    shop_url: str
    telegram_token: str
    admin_id: int  # lets new people in, gets the bot's own alerts
    whisper_model: str
    data_dir: Path
    health_hour: int

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"


def load() -> Config:
    return Config(
        shop_url=_required("SHOP_URL"),
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        admin_id=int(_required("TELEGRAM_ADMIN_ID")),
        whisper_model=os.environ.get("WHISPER_MODEL", "small"),
        data_dir=Path(os.environ.get("DATA_DIR", "/data")),
        health_hour=int(os.environ.get("HEALTH_CHECK_HOUR", "9")),
    )
