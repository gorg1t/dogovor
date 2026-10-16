"""Исходящие сообщения бота: повтор при флуд-контроле, учёт id (для уборки за автотестом), журнал тестовой группы.

Журнал (LOG_OUTGOING=1): боты-участники не видят ответы основного бота, поэтому то, что бот написал
в тестовый чат, пишем в logs/outgoing.log. Другие чаты не логируются.
"""

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import EditMessageText, SendMessage

from ..config import ROOT, get_settings

_log = logging.getLogger("outgoing")


class OutgoingLog(BaseRequestMiddleware):
    def __init__(self, source: str):
        self.source = source
        self.chat_id = get_settings().test_chat_id

    async def __call__(self, make_request, bot: Bot, method):
        result = await make_request(bot, method)
        if isinstance(method, (SendMessage, EditMessageText)) and method.chat_id == self.chat_id:
            mid = getattr(result, "message_id", getattr(method, "message_id", None))
            kb = getattr(method, "reply_markup", None)
            buttons = [f"{b.text} {b.url or b.callback_data or ''}".strip()
                       for row in getattr(kb, "inline_keyboard", []) for b in row] if kb else []
            _log.info("%s %s msg=%s reply_to=%s buttons=%s\n%s\n---", self.source, type(method).__name__, mid,
                      getattr(method, "reply_to_message_id", None), buttons, method.text)
        return result


class RetryOnFlood(BaseRequestMiddleware):
    """Telegram ограничивает частоту сообщений в группе («Flood control … retry in 14 s») — ждём и повторяем,
    а не теряем карточку. Долгие паузы (> 30 с) не ждём: пусть ошибка всплывёт."""

    async def __call__(self, make_request, bot: Bot, method):
        for attempt in range(3):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                if attempt == 2 or e.retry_after > 30:
                    raise
                _log.info("flood control: wait %ss before %s", e.retry_after, type(method).__name__)
                await asyncio.sleep(e.retry_after + 0.5)


class TrackSent(BaseRequestMiddleware):
    def __init__(self, store):
        self.store = store

    async def __call__(self, make_request, bot: Bot, method):
        result = await make_request(bot, method)
        if isinstance(method, SendMessage) and getattr(result, "message_id", None):
            try:
                await self.store.remember_bot_message(result.chat.id, result.message_id)
            except Exception:  # noqa: BLE001 — учёт не должен ломать отправку
                _log.warning("could not remember sent message", exc_info=True)
        return result


def attach(bot: Bot, source: str, store=None) -> None:
    bot.session.middleware(RetryOnFlood())
    if store is not None:
        bot.session.middleware(TrackSent(store))
    if not os.getenv("LOG_OUTGOING"):
        return
    path = Path(ROOT) / "logs" / "outgoing.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _log.handlers:
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _log.addHandler(h)
        _log.setLevel(logging.INFO)
        _log.propagate = False
    bot.session.middleware(OutgoingLog(source))
