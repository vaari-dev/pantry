"""When the bot's token changes, the bot people know tells them, and anyone who opens it later, where the new one is.
Telegram ids carry over, so accounts, baskets and picks are there as soon as they press Start."""

import contextlib

from .telegram import Telegram, TelegramError

MESSAGE = (
    "📦 I've moved to @{handle}.\n\n"
    "Tap below and press Start. Your account, basket and picks are all there, with nothing to set up again. "
    "Ordering in a group? Add @{handle} to it.\n\n"
    "This chat stops answering in a few minutes."
)


def announce(old: Telegram, new: Telegram, people: list[int]) -> str:
    """Raises only if the new bot is unusable, so a bad token never replaces a working one."""
    handle = new.call("getMe")["username"]
    try:
        old.call("getMe")
    except TelegramError as e:
        return f"moved to @{handle}; the old bot is unreachable ({e}), so nobody was told"
    with contextlib.suppress(TelegramError):
        old.call("setMyShortDescription", short_description=f"Pantry has moved to @{handle}.")
        old.call("setMyDescription", description=f"Pantry has moved to @{handle}. Open t.me/{handle} and press Start.")
        old.call("deleteMyCommands")
    button = {"inline_keyboard": [[{"text": f"Open @{handle}", "url": f"https://t.me/{handle}"}]]}
    people, told = list(dict.fromkeys(people)), 0
    for uid in people:
        try:
            old.send(uid, MESSAGE.format(handle=handle), markup=button)
            told += 1
        except TelegramError:
            pass
    return f"moved to @{handle}; told {told} of {len(people)}"
