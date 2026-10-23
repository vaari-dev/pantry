"""Telegram Bot API over long polling: no public URL or open ports."""

import contextlib
import json
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str):
        self._api = f"https://api.telegram.org/bot{token}"
        self._files = f"https://api.telegram.org/file/bot{token}"

    def call(self, method: str, http_timeout: float = 40, **params):
        body = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items() if v is not None}
        try:
            with urllib.request.urlopen(
                f"{self._api}/{method}", data=urllib.parse.urlencode(body).encode(), timeout=http_timeout
            ) as r:
                data = json.load(r)
        except Exception as e:  # never let the token-bearing URL reach the logs
            raise TelegramError(f"{method}: {type(e).__name__} {getattr(e, 'code', '')}".strip()) from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    def send(
        self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None, markup: dict | None = None
    ) -> int:
        """buttons: (label, callback data), one per row; markup: any other reply_markup."""
        return self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=markup or _keyboard(buttons))[
            "message_id"
        ]

    def delete(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(TelegramError):
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def send_photo(self, chat_id: int, url: str, caption: str, buttons: list[tuple[str, str]]) -> int:
        return self.call("sendPhoto", chat_id=chat_id, photo=url, caption=caption, reply_markup=_keyboard(buttons))[
            "message_id"
        ]

    def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text)

    def remove_buttons(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(TelegramError):
            self.call(
                "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []}
            )

    def answer(self, callback_id: str) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id)

    def updates(self, offset: int | None) -> list[dict]:
        return self.call(
            "getUpdates", http_timeout=40, timeout=30, offset=offset, allowed_updates=["message", "callback_query"]
        )

    def download(self, file_id: str) -> Path:
        path = self.call("getFile", file_id=file_id)["file_path"]
        fd, out = tempfile.mkstemp(suffix=Path(path).suffix or ".oga")
        with urllib.request.urlopen(f"{self._files}/{path}", timeout=60) as r, open(fd, "wb") as f:
            f.write(r.read())
        return Path(out)

    def setup(self, commands: dict[str, str], description: str, short: str) -> str:
        """Returns the bot's username."""
        self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands.items()])
        self.call("setMyDescription", description=description)
        self.call("setMyShortDescription", short_description=short)
        return self.call("getMe")["username"]


def _keyboard(buttons: list[tuple[str, str]] | None) -> dict | None:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": label[:64], "callback_data": data}] for label, data in buttons]}
