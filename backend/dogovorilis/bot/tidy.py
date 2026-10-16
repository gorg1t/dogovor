"""Чистота чата: результат действия показываем реакцией на сообщение, а отработавшие сообщения бота удаляем.

В чате остаётся только то, что ещё ждёт действия (карточка без ответа, вопрос о сроке), ответы на команды
и напоминания. Удалять ответы людей боту позволяет право администратора «Удаление сообщений»; без него
удаление молча не выполняется.
"""

import logging

from aiogram import Bot
from aiogram.types import ReactionTypeEmoji, ReplyParameters

log = logging.getLogger("tidy")

DEAL, OK, EDITED, DONE = "🤝", "👌", "✍", "👍"
NOTE_TTL_S = 90      # справки («уже взял», «не понял срок») исчезают сами
REPLY_TTL_S = 8      # ответ человека на вопрос бота — после реакции


def reply_params(message_id: int | None) -> ReplyParameters | None:
    """Ответ на сообщение, которое могли уже удалить: тогда бот пишет просто в чат, а не падает."""
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None


async def react(bot: Bot, chat_id: int, message_id: int | None, emoji: str) -> None:
    if not message_id:
        return
    try:
        await bot.set_message_reaction(chat_id, message_id, reaction=[ReactionTypeEmoji(emoji=emoji)])
    except Exception as e:  # noqa: BLE001 — реакции могут быть выключены в чате; это не ошибка сценария
        log.info("reaction %s on %s:%s failed: %s", emoji, chat_id, message_id, e)


async def tidy(bot: Bot, chat_id: int, *message_ids: int | None) -> None:
    ids = sorted({i for i in message_ids if i})
    for i in range(0, len(ids), 100):  # deleteMessages — до 100 за раз, ненайденные пропускает
        try:
            await bot.delete_messages(chat_id, ids[i:i + 100])
        except Exception as e:  # noqa: BLE001
            log.info("delete %s in %s failed: %s", ids[i:i + 100], chat_id, e)
