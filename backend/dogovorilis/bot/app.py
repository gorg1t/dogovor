"""Бот-сервис: принимает сообщения группы, сразу кладёт их в очередь и обрабатывает нажатия на карточки.

Модель здесь не вызывается — это делает воркер (ADR-1: очередь между ботом и LLM).
Запуск: python -m dogovorilis.bot.app
"""

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, CallbackQuery, ChatMemberUpdated, ForceReply,
                           InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message,
                           WebAppInfo)
from html import escape
from itsdangerous import URLSafeTimedSerializer

from ..config import get_settings
from ..core import service
from ..core.claims import Claim, claim_open_item
from ..core.db import init_db
from ..core.health import Watchdog
from ..core.store import Store
from ..api.tg_auth import chat_param
from ..llm.dates import deadline_time, resolve_deadline
from ..llm.dedup import match_open_item, mentioned, same_task
from ..llm.editor import edit_card, force_card
from ..llm.extractor import ChatMessage, extract
from ..llm.gigachat import GigaChatClient
from ..llm.open_tasks import is_bare_accept
from ..llm.prefilter import PROMISE
from . import outgoing
from .cards import (DEADLINE_CHOICES, EDIT_PLACEHOLDER, MSK, OUTAGE, RECOVERED, admin_howto, apply_edit, cap,
                    delete_keyboard, delete_text, event_keyboard, event_text,
                    card_keyboard, card_text, deadline_ask_keyboard, deadline_ask_text, edit_hint, fmt_deadline,
                    open_card_keyboard, open_card_text)
from .tidy import DEAL, DONE, EDITED, NOTE_TTL_S, OK, REPLY_TTL_S, react, reply_params, tidy

log = logging.getLogger("bot")
router = Router()
settings = get_settings()
store: Store = None  # type: ignore[assignment]
llm: GigaChatClient = None  # type: ignore[assignment]  # для правки карточки текстом
ACTOR_IDS = {int(t.split(":")[0]) for t in settings.actor_tokens}
GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})

ALERT_AFTER_S = 45  # сообщения ждут разбора дольше — пишем в чат, что случилось
ALIAS = re.compile(r"(?<![а-яё])(?:меня зовут|зовите меня)\s+([а-яёa-z]{3,20})(?![а-яёa-z])", re.IGNORECASE)


def onboarding(bot_username: str) -> str:
    return (
        "👋 Я <b>Договорились</b>. Когда кто-то берёт задачу («сделаю к пт», «беру»), присылаю карточку: "
        "«✅ Да» — и задача на доске. На «кто сделает фронт?» — карточка с кнопкой «🙋 Беру», "
        "на «я доделал бэк» — предложу отметить готовой. Отработавшие карточки убираю, чтобы не засорять чат.\n\n"
        f"{admin_howto(bot_username)}\n\n"
        "Храню только подтверждённые задачи с цитатой, остальная переписка — в памяти до суток.\n\n"
        "/tasks — задачи · /board — доска · /report — отчёт · /status — всё ли работает · /checkpoint ДД.ММ · "
        "/done N · /task — ответом на пропущенное сообщение · /forget — удалить мои данные"
    )


def display_name(u) -> str:
    return " ".join(x for x in (u.first_name, u.last_name) if x).strip() or (u.username or str(u.id))


async def answer(m: Message, text: str, **kw) -> Message:
    """Ответ на команду — отдельным сообщением, а саму команду («/tasks@dogovorilis_bbot») убираем из чата."""
    sent = await m.bot.send_message(m.chat.id, text, **kw)
    if m.chat.type != ChatType.PRIVATE:
        await tidy(m.bot, m.chat.id, m.message_id)
    return sent


async def brief_cmd(m: Message, text: str) -> None:
    """Справка по команде (ошибка, «готово»): сама исчезнет, команду убираем сразу."""
    await brief(m.bot, m.chat.id, text)
    await tidy(m.bot, m.chat.id, m.message_id)


async def brief(bot: Bot, chat_id: int, text: str, reply_to: int | None = None, ttl: float = NOTE_TTL_S) -> int:
    """Справка, которая сама исчезнет: «уже взял», «не понял срок», «правит Егор»."""
    m = await bot.send_message(chat_id, text, reply_parameters=reply_params(reply_to))
    await store.delete_later(chat_id, m.message_id, ttl)
    return m.message_id


async def autodelete_loop(bot: Bot) -> None:
    while True:
        try:
            by_chat: dict[int, list[int]] = {}
            for chat_id, mid in await store.due_deletes():
                by_chat.setdefault(chat_id, []).append(mid)
            for chat_id, ids in by_chat.items():
                await tidy(bot, chat_id, *ids)
            while event_id := await store.r.lpop("event_cards_dirty"):  # отметились на доске — обновить карточку
                await redraw_event_card(bot, int(event_id))
            while raw := await store.r.lpop("board_notes"):  # поменяли на доске — рассказываем в чате (без звука)
                note = json.loads(raw)
                await bot.send_message(note["chat_id"], note["text"])
            while raw := await store.r.lpop("board_claims"):  # взяли свободную задачу с доски
                c = json.loads(raw)
                card = await store.get_proposal(c["key"])
                task = await service.get_task(c["task_id"])
                if card and task:
                    await after_claim(bot, Claim(card, card["items"][c["idx"]], task), c["key"])
        except Exception:
            log.exception("autodelete loop error")
        await asyncio.sleep(2)


async def outage_reason() -> str:
    """ok | worker — нет пульса воркера | llm — модель не отвечает | slow — просто много сообщений."""
    if not await store.r.exists("hb:worker"):
        return "worker"
    if await store.r.exists("llm:fail_since") or await store.llm_down():
        return "llm"
    return "ok"


async def health_loop(bot: Bot) -> None:
    """Сообщения ждут разбора дольше ALERT_AFTER_S — честно пишем в чат почему; разобрали — правим на «снова работаю».

    Очередь ничего не теряет, но молчащий бот выглядит сломанным: люди должны видеть, что происходит.
    """
    while True:
        try:
            age, chats = await store.backlog()
            if age >= ALERT_AFTER_S:
                reason = await outage_reason()
                reason = reason if reason != "ok" else "slow"
                for chat_id in chats:
                    raw = await store.r.get(f"outage:{chat_id}")
                    if raw:  # уже предупредили; причина сменилась (воркер упал во время задержки) — правим текст
                        mid, was = raw.split("|")
                        if was != reason:
                            await bot.edit_message_text(OUTAGE[reason], chat_id=chat_id, message_id=int(mid))
                            await store.r.set(f"outage:{chat_id}", f"{mid}|{reason}", ex=24 * 3600)
                        continue
                    m = await bot.send_message(chat_id, OUTAGE[reason])
                    await store.r.set(f"outage:{chat_id}", f"{m.message_id}|{reason}", ex=24 * 3600)
            elif age < 10:
                for key in await store.r.keys("outage:*"):
                    chat_id, mid = int(key.split(":", 1)[1]), int((await store.r.get(key) or "0").split("|")[0])
                    await store.r.delete(key)
                    try:
                        await bot.edit_message_text(RECOVERED, chat_id=chat_id, message_id=mid)
                    except Exception:  # noqa: BLE001 — предупреждение могли удалить руками
                        pass
                    await store.delete_later(chat_id, mid, 60)
        except Exception:
            log.exception("health loop error")
        await asyncio.sleep(10)


def message_payload(m: Message) -> dict:
    u = m.from_user
    return {
        "chat_id": m.chat.id, "chat_title": m.chat.title or "", "message_id": m.message_id,
        "user_id": u.id, "name": display_name(u), "username": u.username, "is_bot": u.is_bot,
        "ts": m.date.astimezone(MSK).isoformat(), "unix": m.date.timestamp(),
        "text": m.text or m.caption or "",
        "reply_to": m.reply_to_message.message_id if m.reply_to_message else None,
    }


async def _register(m: Message) -> None:
    await service.upsert_chat(m.chat.id, m.chat.title or "")
    if m.from_user:
        await service.upsert_member(m.chat.id, m.from_user.id, display_name(m.from_user), m.from_user.username)
    if m.bot:
        await sync_admins(m.bot, m.chat.id)


async def sync_admins(bot: Bot, chat_id: int) -> None:
    """Администраторов группы знаем сразу: «сделать лендинг - Максим» найдёт Максима, даже если он ещё не писал."""
    if not await store.r.set(f"admins_synced:{chat_id}", 1, nx=True, ex=6 * 3600):
        return
    try:
        for a in await bot.get_chat_administrators(chat_id):
            if not a.user.is_bot:
                await service.upsert_member(chat_id, a.user.id, display_name(a.user), a.user.username)
    except Exception as e:  # noqa: BLE001 — список администраторов не критичен
        log.info("admins of %s: %s", chat_id, e)


# --- подключение -------------------------------------------------------------------------------
@router.my_chat_member(GROUPS)
async def on_added(ev: ChatMemberUpdated, bot: Bot) -> None:
    old, new = ev.old_chat_member.status, ev.new_chat_member.status
    if new in ("member", "administrator") and old in ("left", "kicked"):
        await service.upsert_chat(ev.chat.id, ev.chat.title or "")
        await sync_admins(bot, ev.chat.id)
        await bot.send_message(ev.chat.id, onboarding((await bot.get_me()).username))
    elif new == "administrator" and old == "member":  # дали права по инструкции — коротко благодарим
        await store.r.delete(f"admins_synced:{ev.chat.id}")
        await sync_admins(bot, ev.chat.id)
        await brief(bot, ev.chat.id, "✅ Спасибо, теперь я администратор — вижу сообщения и убираю за собой.")


@router.message(Command("start", "help"))
async def cmd_help(m: Message, bot: Bot) -> None:
    text = onboarding((await bot.get_me()).username)
    if m.chat.type == ChatType.PRIVATE:
        await m.reply(text + "\n\nДобавьте меня в рабочий чат команды — там я и работаю.")
    else:
        await _register(m)
        await answer(m, text)


@router.message(Command("status"))
async def cmd_status(m: Message) -> None:
    """Всё ли работает: разбор сообщений, воркер, модель."""
    age, _ = await store.backlog()
    reason = await outage_reason()
    if age < ALERT_AFTER_S and reason == "ok":
        head = "✅ Всё работает" + (f" · сообщения ждут разбора {age:.0f} с" if age >= 1 else " · очередь пуста")
    else:
        head = OUTAGE[reason if reason != "ok" else "slow"] + f"\nСамое старое сообщение ждёт {age / 60:.0f} мин."
    await answer(m, head)


@router.message(Command("chatid"))
async def cmd_chatid(m: Message) -> None:
    await answer(m, f"chat_id: <code>{m.chat.id}</code>")


# --- задачи --------------------------------------------------------------------------------------
@router.message(Command("tasks"), GROUPS)
async def cmd_tasks(m: Message) -> None:
    await _register(m)
    tasks = await service.list_tasks(m.chat.id, "open")
    if not tasks:
        await answer(m, "Открытых задач нет.")
        return
    today = datetime.now(MSK).date()
    by_person: dict[str, list] = {}
    for t in tasks:
        by_person.setdefault(t.assignee_name, []).append(t)
    lines = ["📋 <b>Открытые задачи</b>"]
    for person, ts in by_person.items():
        lines.append(f"\n👤 <b>{escape(person)}</b>")
        for t in ts:
            late = " ❗️просрочено" if t.deadline and t.deadline < today else ""
            lines.append(f"  #{t.id} {escape(t.title)} — {fmt_deadline(t.deadline, t.deadline_text)}{late}")
    await answer(m, "\n".join(lines))


@router.message(Command("done"), GROUPS)
async def cmd_done(m: Message, command: CommandObject, bot: Bot) -> None:
    arg = (command.args or "").strip().lstrip("#")
    t = await service.get_task(int(arg)) if arg.isdigit() else None
    if not t or t.chat_id != m.chat.id or t.status == "deleted":
        await brief_cmd(m, "Номер задачи: /done 12 (номера — в /tasks)." if not arg.isdigit() else f"Задачи #{arg} в этом чате нет.")
        return
    if not service.may_manage(t, m.from_user.id):
        await brief_cmd(m, f"Отметить «{escape(t.title)}» может {escape(service.managers_label(t))}.")
        return
    await service.set_task_status(m.chat.id, t.id, "done", who=display_name(m.from_user))
    await brief_cmd(m, f"{DONE} #{t.id} «{escape(t.title)}» — готово")


@router.message(Command("delete", "del"), GROUPS)
async def cmd_delete(m: Message, command: CommandObject, bot: Bot) -> None:
    """/delete N — удалить задачу (с подтверждением; на доске её можно вернуть)."""
    arg = (command.args or "").strip().lstrip("#")
    t = await service.get_task(int(arg)) if arg.isdigit() else None
    if not t or t.chat_id != m.chat.id or t.status == "deleted":
        await brief_cmd(m, "Номер задачи: /delete 12 (номера — в /tasks)." if not arg.isdigit() else f"Задачи #{arg} в этом чате нет.")
        return
    if not service.may_manage(t, m.from_user.id):
        await brief_cmd(m, f"Удалить «{escape(t.title)}» может {escape(service.managers_label(t))}.")
        return
    card = await m.reply(delete_text(t), reply_markup=delete_keyboard(t.id))
    await store.r.set(f"delcmd:{m.chat.id}:{card.message_id}", m.message_id, ex=3600)
    await store.delete_later(m.chat.id, card.message_id, 600)  # не ответили — карточка уйдёт сама


@router.callback_query(F.data.regexp(r"^del\|"))
async def on_delete_button(cq: CallbackQuery, bot: Bot) -> None:
    _, task_id, action = cq.data.split("|", 2)
    chat_id, card = cq.message.chat.id, cq.message.message_id
    command = await store.r.get(f"delcmd:{chat_id}:{card}")
    t = await service.get_task(int(task_id))
    if action == "y" and t and not service.may_manage(t, cq.from_user.id):
        await cq.answer(f"Удалить может {service.managers_label(t)}", show_alert=True)
        return
    if action == "y":
        t = await service.set_task_status(chat_id, int(task_id), "deleted", who=display_name(cq.from_user))
        await cq.answer(f"🗑 Задача #{task_id} удалена — вернуть можно на доске" if t else "Задача не найдена")
    else:
        await cq.answer("Оставили")
    await tidy(bot, chat_id, card, int(command) if command else None)


RSVP_YES = re.compile(r"^\s*(\+|буду|приду|я буду|я приду|я в деле|участвую|я тоже|го|ок|окей|да)(?![а-яa-z])", re.IGNORECASE)
RSVP_NO = re.compile(r"(не смогу|не приду|не буду|без меня|пас\b|^\s*-\s*$)", re.IGNORECASE)
RSVP_MAYBE = re.compile(r"(может быть|возможно|постараюсь|не знаю|если успею|под вопросом)", re.IGNORECASE)


def rsvp_from_text(text: str) -> str | None:
    t = (text or "").lower().replace("ё", "е")
    return "no" if RSVP_NO.search(t) else "maybe" if RSVP_MAYBE.search(t) else "yes" if RSVP_YES.search(t) else None


async def set_rsvp(bot: Bot, event_id: int, user, status: str) -> None:
    """Ответ на встречу и перерисовка карточки: видно, кто идёт."""
    if await service.rsvp(event_id, user.id, display_name(user), status):
        await redraw_event_card(bot, event_id)


async def redraw_event_card(bot: Bot, event_id: int) -> None:
    ev = await service.get_event(event_id)
    if not ev or not ev.card_message_id or ev.status == "cancelled":
        return
    people = (await service.attendees([ev.id])).get(ev.id, [])
    try:
        await bot.edit_message_text(event_text(ev, people), chat_id=ev.chat_id, message_id=ev.card_message_id,
                                    reply_markup=event_keyboard(ev.id))
    except Exception:  # noqa: BLE001 — «message is not modified» при повторном нажатии, карточку могли убрать
        pass


@router.callback_query(F.data.regexp(r"^v\|"))
async def on_event_button(cq: CallbackQuery, bot: Bot) -> None:
    """«✅ Буду / 🤔 Может / ✖️ Не смогу» и «Это не встреча»."""
    _, event_id, action = cq.data.split("|", 2)
    ev = await service.get_event(int(event_id))
    if not ev or ev.status == "cancelled":
        await cq.answer("Встреча отменена.")
        await tidy(bot, cq.message.chat.id, cq.message.message_id)
        return
    if action == "x":
        await service.update_event(ev.id, status="cancelled")
        await tidy(bot, ev.chat_id, cq.message.message_id)
        await cq.answer("Убрал")
        return
    await set_rsvp(bot, ev.id, cq.from_user, action)
    await cq.answer({"yes": "✅ Записал: будешь", "maybe": "🤔 Записал: может быть", "no": "Записал: не сможешь"}[action])


@router.message(Command("board"), GROUPS)
async def cmd_board(m: Message, bot: Bot) -> None:
    await _register(m)
    token = URLSafeTimedSerializer(settings.board_secret, salt="board").dumps({"chat_id": m.chat.id})
    browser = f"{settings.board_base_url}/?t={token}"
    buttons = []
    if settings.miniapp_short_name:  # мини-апп: вход по Telegram-аккаунту, без ссылок с токеном
        me = await bot.get_me()
        app_link = f"https://t.me/{me.username}/{settings.miniapp_short_name}?startapp={chat_param(m.chat.id, settings.board_secret)}"
        buttons.append([InlineKeyboardButton(text="🗂 Открыть доску", url=app_link)])
    if browser.startswith("https://"):  # кнопки Telegram принимают только https
        buttons.append([InlineKeyboardButton(text="🌐 В браузере (ссылка на 7 дней)", url=browser)])
    if buttons:
        await answer(m, "🗂 Доска команды", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    else:
        await answer(m, f"🗂 Доска команды (ссылка действует 7 дней):\n{browser}", disable_web_page_preview=True)


@router.message(Command("checkpoint"), GROUPS)
async def cmd_checkpoint(m: Message, command: CommandObject) -> None:
    await _register(m)
    if not command.args:
        chat = await service.get_chat(m.chat.id)
        cur = chat.checkpoint.strftime("%d.%m.%Y") if chat and chat.checkpoint else "не задан"
        await answer(m, f"Чекпоинт: {cur}. Задать: /checkpoint 17.10")
        return
    d, prec = resolve_deadline(command.args, datetime.now(MSK))
    if prec != "day" or not d:
        await brief_cmd(m, "Не понял дату. Пример: /checkpoint 17.10")
        return
    await service.set_checkpoint(m.chat.id, d)
    await answer(m, f"📍 Чекпоинт: {d:%d.%m.%Y} — «к КТ» теперь означает эту дату.")


@router.message(Command("report"), GROUPS)
async def cmd_report(m: Message, command: CommandObject) -> None:
    today = datetime.now(MSK).date()
    since = today - timedelta(days=14)
    if command.args:
        d, prec = resolve_deadline(command.args, datetime.now(MSK))
        if d and prec == "day":
            since = d if d <= today else d.replace(year=d.year - 1)
    r = await service.report(m.chat.id, since, today)

    def block(title: str, items: list) -> list[str]:
        out = [f"\n<b>{title}</b> ({len(items)})"]
        out += [f"• {escape(t.assignee_name)}: {escape(t.title)} — {fmt_deadline(t.deadline, t.deadline_text)}"
                for t in items] or ["• —"]
        return out

    chat = await service.get_chat(m.chat.id)
    head = f"📊 <b>Отчёт к чекпоинту</b> с {since:%d.%m}"
    if chat and chat.checkpoint:
        head += f" (чекпоинт {chat.checkpoint:%d.%m})"
    lines = [head, *block("✅ Сделано", r["done"]), *block("❗️ Просрочено", r["overdue"]),
             *block("⏳ В работе", r["open"])]
    await answer(m, "\n".join(lines))


@router.message(Command("forget"), GROUPS)
async def cmd_forget(m: Message) -> None:
    """/forget — сначала спрашиваем: удалить можно только своё, задачи команды остаются (без имени и цитат)."""
    card = await m.reply(f"🧹 {escape(display_name(m.from_user))}, удалить твои данные? Сообщения в памяти бота и "
                         "неподтверждённые карточки удалятся, а в задачах команды имя и цитаты заменятся на «Участник» — "
                         "сами задачи и отчёт останутся.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                             InlineKeyboardButton(text="🧹 Удалить мои данные", callback_data=f"fg|{m.from_user.id}|y"),
                             InlineKeyboardButton(text="Отмена", callback_data=f"fg|{m.from_user.id}|n")]]))
    await store.r.set(f"forgetcmd:{m.chat.id}:{card.message_id}", m.message_id, ex=3600)
    await store.delete_later(m.chat.id, card.message_id, 600)


@router.callback_query(F.data.regexp(r"^fg\|"))
async def on_forget_button(cq: CallbackQuery, bot: Bot) -> None:
    _, user_id, action = cq.data.split("|", 2)
    if int(user_id) != cq.from_user.id:
        await cq.answer("Удалить можно только свои данные — отправь /forget сам", show_alert=True)
        return
    chat_id, card = cq.message.chat.id, cq.message.message_id
    command = await store.r.get(f"forgetcmd:{chat_id}:{card}")
    await tidy(bot, chat_id, card, int(command) if command else None)
    if action != "y":
        await cq.answer("Ничего не удалял")
        return
    u = cq.from_user
    names = [display_name(u), u.first_name, u.username, (await store.aliases(chat_id)).get(u.id)]
    n_mem = await store.forget_user(chat_id, u.id)
    n_db = await service.forget_member(chat_id, u.id, [n for n in names if n])
    await cq.answer("Готово")
    await brief(bot, chat_id, f"🧹 Данные участника удалены: сообщений и карточек — {n_mem}, "
                              f"задач обезличено — {n_db}.")


@router.message(Command("task"), GROUPS)
async def cmd_task(m: Message, bot: Bot) -> None:
    """Запасной путь: бот пропустил договорённость — /task в ответ на сообщение делает из него карточку."""
    src = m.reply_to_message
    if not src or not (src.text or src.caption) or not src.from_user:
        await brief_cmd(m, "Ответьте командой /task на сообщение с договорённостью — пришлю карточку.")
        return
    await _register(m)
    existing = [p for p in await store.recent_proposals(m.chat.id, 48 * 3600)
                if p.get("message_id") == src.message_id and p.get("status") in ("pending", "confirmed", "open")]
    if existing:
        p = existing[0]
        where = f"задача #{p['task_id']} уже на доске" if p.get("task_id") else "карточка уже есть выше"
        await brief_cmd(m, f"По этому сообщению {where}.")
        return
    author = display_name(src.from_user)
    await service.upsert_member(m.chat.id, src.from_user.id, author, src.from_user.username)
    text = src.text or src.caption
    chat = await service.get_chat(m.chat.id)
    checkpoint = chat.checkpoint if chat else None
    mem = await _members(m.chat.id)
    ts = src.date.astimezone(MSK)
    tasks = []
    try:
        res = await extract(llm, settings.gigachat_model, mem, [], ChatMessage(src.message_id, author, ts, text),
                            checkpoint, prompt_version=settings.extract_prompt)
        tasks = [(t.assignee, t.task, t.deadline.isoformat() if t.deadline else None, t.deadline_text) for t in res.tasks]
    except Exception:
        log.exception("/task: extraction failed")
    if not tasks:  # основной экстрактор осторожен — человек явно сказал, что задача есть: просим сформулировать
        try:
            tasks = [await force_card(llm, settings.gigachat_model, text, author, mem, ts, checkpoint)]
        except Exception:
            log.exception("/task: force_card failed")
    if not tasks:  # совсем не вышло — карточка «как есть», дальше «Исправить»
        d, prec = resolve_deadline(text, ts, checkpoint)
        tasks = [(author, text if len(text) <= 80 else text[:77] + "…", d.isoformat() if d and prec == "day" else None, None)]
    for idx, (assignee, task, deadline, deadline_text) in enumerate(tasks):
        member = next((x for x in mem if x["name"] == assignee), None)
        p = {"key": f"{m.chat.id}:{src.message_id}:m{idx}", "chat_id": m.chat.id, "message_id": src.message_id,
             "assignee_name": assignee, "assignee_user_id": member["user_id"] if member else None, "task": task,
             "deadline": deadline, "deadline_text": deadline_text, "kind": "manual", "quote_text": text,
             "quote_author_id": src.from_user.id, "quote_author_name": author, "quote_ts": ts.isoformat(),
             "status": "pending", "created": datetime.now(MSK).timestamp()}
        if not await store.save_proposal(p):
            existing = await store.get_proposal(p["key"])
            if existing and existing.get("status") == "pending":
                await brief(bot, m.chat.id, "Карточка на это сообщение уже есть выше.")
            continue
        card = await bot.send_message(m.chat.id, card_text(p), reply_markup=card_keyboard(p["key"]),
                                      reply_parameters=reply_params(src.message_id))
        p["card_message_id"] = card.message_id
        await store.update_proposal(p)
        await store.r.hincrby("stats", "manual_cards", 1)
    await tidy(bot, m.chat.id, m.message_id)  # карточка уже ответила на само сообщение — команда не нужна


# --- карточки ------------------------------------------------------------------------------------
@router.callback_query(F.data.regexp(r"^[rk]\|"))
async def on_reschedule_button(cq: CallbackQuery, bot: Bot) -> None:
    action, key = cq.data.split("|", 1)
    p = await store.get_proposal(key)
    if not p or p.get("status") != "pending":
        await cq.answer("Уже решено или устарело.")
        await tidy(bot, cq.message.chat.id, cq.message.message_id)
        return
    who = display_name(cq.from_user)
    t = await service.get_task(p["task_id"])
    if t and not service.may_manage(t, cq.from_user.id):
        await cq.answer(f"Срок меняет {service.managers_label(t)}", show_alert=True)
        return
    if action == "r":
        await service.update_task(p["chat_id"], p["task_id"], who=f"перенос по сообщению, подтвердил(а) {who}",
                                  deadline=date.fromisoformat(p["deadline"]) if p.get("deadline") else None)
        p["status"] = "confirmed"
        await react(bot, p["chat_id"], p["message_id"], OK)
        await cq.answer(f"Срок #{p['task_id']}: {fmt_deadline(p.get('deadline'))}")
    else:
        p["status"] = "rejected"
        await cq.answer("Оставили прежний срок")
    await store.update_proposal(p)
    await tidy(bot, p["chat_id"], cq.message.message_id)  # решённая карточка больше не нужна в чате


@router.callback_query(F.data.regexp(r"^[fx]\|"))
async def on_done_button(cq: CallbackQuery, bot: Bot) -> None:
    """«✅ Готово?» после «я доделал бэк»: подтвердили — задача в «Сделано», на сообщении 👍."""
    action, key = cq.data.split("|", 1)
    p = await store.get_proposal(key)
    if not p or p.get("status") != "pending":
        await cq.answer("Уже решено.")
        await tidy(bot, cq.message.chat.id, cq.message.message_id)
        return
    who = display_name(cq.from_user)
    t = await service.get_task(p["task_id"])
    if t and not service.may_manage(t, cq.from_user.id):
        await cq.answer(f"Отметить может {service.managers_label(t)}", show_alert=True)
        return
    if action == "f":
        await service.set_task_status(p["chat_id"], p["task_id"], "done", who=f"по сообщению в чате, подтвердил(а) {who}")
        p["status"] = "confirmed"
        await react(bot, p["chat_id"], p["message_id"], DONE)
        await cq.answer(f"✅ #{p['task_id']} — в «Сделано»")
    else:
        p["status"] = "rejected"
        await cq.answer("Ок, задача остаётся в работе")
    await store.update_proposal(p)
    await tidy(bot, p["chat_id"], cq.message.message_id)


@router.callback_query(F.data.regexp(r"^h\|"))
async def on_handover_button(cq: CallbackQuery, bot: Bot) -> None:
    """«🔁 Дима → Лёша: провести демо?» — передать задачу. Решают нынешний исполнитель, автор или новый исполнитель."""
    _, key, action = cq.data.rsplit("|", 2)
    p = await store.get_proposal(key)
    t = await service.get_task(p["task_id"]) if p else None
    if not p or not t or p.get("status") != "pending":
        await cq.answer("Уже решено.")
        await tidy(bot, cq.message.chat.id, cq.message.message_id)
        return
    if not (service.may_manage(t, cq.from_user.id) or cq.from_user.id == p.get("new_user_id")):
        await cq.answer(f"Передать может {service.managers_label(t)} или {p['new_name']}", show_alert=True)
        return
    who = display_name(cq.from_user)
    if action == "y":
        await service.update_task(t.chat_id, t.id, who=who, assignee_name=p["new_name"], assignee_user_id=p.get("new_user_id"))
        p["status"] = "confirmed"
        await react(bot, p["chat_id"], p["message_id"], DEAL)
        await cq.answer(f"🔁 #{t.id} теперь у {p['new_name']}")
    else:
        p["status"] = "rejected"
        await cq.answer("Оставили как было")
    await store.update_proposal(p)
    await tidy(bot, p["chat_id"], cq.message.message_id)


@router.callback_query(F.data.regexp(r"^cx\|"))
async def on_cancel_button(cq: CallbackQuery, bot: Bot) -> None:
    """«🗑 … больше не нужна?» / «Отменить встречу?» после «отбой, не нужно» в чате."""
    _, key, action = cq.data.rsplit("|", 2)
    p = await store.get_proposal(key)
    if not p or p.get("status") != "pending":
        await cq.answer("Уже решено.")
        await tidy(bot, cq.message.chat.id, cq.message.message_id)
        return
    who = display_name(cq.from_user)
    if action == "y" and p.get("event_id"):
        ev = await service.update_event(p["event_id"], status="cancelled")
        if ev and ev.card_message_id:
            await tidy(bot, ev.chat_id, ev.card_message_id)
        await cq.answer("Встреча отменена")
    elif action == "y":
        t = await service.get_task(p["task_id"])
        if t and not service.may_manage(t, cq.from_user.id):
            await cq.answer(f"Удалить может {service.managers_label(t)}", show_alert=True)
            return
        await service.set_task_status(p["chat_id"], p["task_id"], "deleted", who=f"«{p['task']}» не нужна · {who}")
        await cq.answer(f"🗑 #{p['task_id']} удалена — вернуть можно на доске")
    else:
        await cq.answer("Оставили")
    p["status"] = "confirmed" if action == "y" else "rejected"
    await store.update_proposal(p)
    if action == "y":
        await react(bot, p["chat_id"], p["message_id"], OK)
    await tidy(bot, p["chat_id"], cq.message.message_id)


@router.callback_query(F.data.regexp(r"^[yen]\|"))
async def on_card_button(cq: CallbackQuery, bot: Bot) -> None:
    action, key = cq.data.split("|", 1)
    p = await store.get_proposal(key)
    if not p:
        await cq.answer("Карточка устарела.", show_alert=True)
        await tidy(bot, cq.message.chat.id, cq.message.message_id)
        return
    who = display_name(cq.from_user)
    if p.get("status") in ("confirmed", "rejected"):
        await cq.answer("Уже решено.")
        await tidy(bot, p["chat_id"], cq.message.message_id)
        return
    if action == "y":
        p["context_after"] = await store.thread_after(p["chat_id"], p["message_id"])
        task, created = await service.confirm_proposal(p, who)
        p["status"], p["task_id"] = "confirmed", task.id
        await store.update_proposal(p)
        await store.r.hincrby("stats", "confirmed", 1)
        await store.r.hincrby("stats", "confirmed_edited" if p.get("edited") else "confirmed_clean", 1)
        # итог — реакцией 🤝 на само обещание; карточку и переписку правки убираем
        await react(bot, p["chat_id"], p["message_id"], DEAL)
        await tidy(bot, p["chat_id"], cq.message.message_id, *p.get("trash", []))
        await cq.answer(f"✅ Задача #{task.id} на доске")
        if created:
            await link_open_item(bot, p["chat_id"], p["task"], p["assignee_name"], p.get("assignee_user_id"), task.id)
            await ask_deadline(bot, p["chat_id"], task)
    elif action == "n":
        p["status"] = "rejected"
        await store.update_proposal(p)
        await store.r.hincrby("stats", "rejected", 1)
        await tidy(bot, p["chat_id"], cq.message.message_id, *p.get("trash", []))
        await cq.answer("Убрал")
    else:
        # поле ответа — только у нажавшего, и правку принимаем только от него
        u = cq.from_user
        prompt = await bot.send_message(cq.message.chat.id, edit_hint(who, u.id, u.username),
                                        reply_to_message_id=cq.message.message_id,
                                        reply_markup=ForceReply(selective=True, input_field_placeholder=EDIT_PLACEHOLDER))
        await store.r.set(f"edit:{cq.message.chat.id}:{prompt.message_id}",
                          json.dumps({"key": key, "user_id": u.id, "name": who}, ensure_ascii=False), ex=3600)
        p.setdefault("trash", []).append(prompt.message_id)  # переписку правки уберём вместе с карточкой
        await store.update_proposal(p)
        await cq.answer()


async def _members(chat_id: int) -> list[dict]:
    return [{"name": x.name, "username": x.username, "user_id": x.user_id} for x in await service.members(chat_id)]


async def ask_deadline(bot: Bot, chat_id: int, task) -> None:
    """Задачу взяли без срока — сразу спрашиваем ответственного (или позже, если задана задержка)."""
    if not task or task.deadline:
        return
    if settings.deadline_ask_delay_s > 0:
        await store.schedule_deadline_ask(chat_id, task.id, settings.deadline_ask_delay_s)
        return
    m = await bot.send_message(chat_id, deadline_ask_text(task.assignee_name, task.title, task.assignee_user_id),
                               reply_markup=deadline_ask_keyboard(task.id))
    await store.r.set(f"askdl:{chat_id}:{m.message_id}", task.id, ex=7 * 24 * 3600)


async def link_open_item(bot: Bot, chat_id: int, task_text: str, assignee: str, user_id: int | None,
                         task_id: int) -> None:
    """«Беру фронт» текстом после «Кто сделает фронт?» — отмечаем пункт в карточке «Кто возьмёт?»."""
    open_cards = [p for p in await store.recent_proposals(chat_id) if p.get("type") == "open"]
    hit = match_open_item(task_text, open_cards)
    if not hit:
        return
    card, i = hit
    card["items"][i].update(claimed_by=assignee, claimed_user_id=user_id, task_id=task_id)
    await store.update_proposal(card)
    await refresh_open_card(bot, card)
    await sync_claim(bot, chat_id, card["items"][i]["task"], assignee, user_id, task_id, skip_key=card["key"])


async def refresh_open_card(bot: Bot, card: dict) -> None:
    """Перерисовать «Кто возьмёт?»; когда разобрано всё — убрать карточку, а на вопрос поставить 🤝."""
    if not card.get("card_message_id"):
        return
    if all(it.get("claimed_by") for it in card["items"]):
        await tidy(bot, card["chat_id"], card["card_message_id"])
        await react(bot, card["chat_id"], card["message_id"], DEAL)
        return
    try:
        await bot.edit_message_text(open_card_text(card), chat_id=card["chat_id"], message_id=card["card_message_id"],
                                    reply_markup=open_card_keyboard(card))
    except Exception:
        log.warning("could not edit open card %s", card["key"])


async def sync_claim(bot: Bot, chat_id: int, task_text: str, assignee: str, user_id: int | None, task_id: int,
                     skip_key: str | None = None) -> None:
    """Та же задача свободна в других карточках «Кто возьмёт?» — отмечаем взятой и там."""
    for card in [p for p in await store.recent_proposals(chat_id) if p.get("type") == "open" and p["key"] != skip_key]:
        changed = False
        for it in card["items"]:
            if not it.get("claimed_by") and same_task(task_text, it["task"]):
                it.update(claimed_by=assignee, claimed_user_id=user_id, task_id=task_id)
                changed = True
        if changed:
            await store.update_proposal(card)
            await refresh_open_card(bot, card)


@router.callback_query(F.data.regexp(r"^t\|"))
async def on_take(cq: CallbackQuery, bot: Bot) -> None:
    """Кнопка «🙋 Беру» на карточке «Кто возьмёт?»: нажатие — это и есть подтверждение человеком."""
    _, key, idx = cq.data.split("|", 2)
    await service.upsert_chat(cq.message.chat.id, cq.message.chat.title or "")
    u = cq.from_user
    c = await claim_open_item(store, key, int(idx), u.id, display_name(u), u.username)
    if not c.card:
        await cq.answer("Карточка устарела.", show_alert=True)
        return
    if c.taken_by:
        await refresh_open_card(bot, c.card)
        await cq.answer(f"Эту задачу уже взял(а) {c.taken_by}", show_alert=True)
        return
    await after_claim(bot, c, key)
    await cq.answer(f"Задача #{c.task.id} твоя" + ("" if c.task.deadline else " — укажи срок"))


CLAIM_NO = re.compile(r"(?<![а-я])(не могу|не смогу|не возьму|не буду|нет|не знаю|без меня)(?![а-я])")


async def claim_by_reply(bot: Bot, m: Message, key: str) -> bool:
    """Ответ на «Кто возьмёт?» («гляну сегодня вечером», «беру», «+») — это взятие: задача на ответившего, срок из ответа."""
    text = (m.text or "").lower().replace("ё", "е")
    if "?" in text or CLAIM_NO.search(text) or not (is_bare_accept(text) or text.strip() == "+" or PROMISE.search(text)):
        return False
    card = await store.get_proposal(key)
    free = [i for i, it in enumerate(card["items"]) if not it.get("claimed_by")] if card else []
    named = [i for i in free if mentioned(card["items"][i]["task"], text)]
    idx = named[0] if len(named) == 1 else free[0] if len(free) == 1 else None
    if idx is None:
        return False  # несколько свободных и не понять какую — пусть ответит кнопкой
    chat = await service.get_chat(m.chat.id)
    day, precision = resolve_deadline(m.text, datetime.now(MSK), chat.checkpoint if chat else None)
    u = m.from_user
    c = await claim_open_item(store, key, idx, u.id, display_name(u), u.username,
                              deadline=day if precision == "day" else None,
                              deadline_text=m.text.strip()[:120] if precision == "day" else None)
    if not c.task:
        return False
    await react(bot, m.chat.id, m.message_id, DEAL)
    await after_claim(bot, c, key)
    return True


async def after_claim(bot: Bot, c, key: str) -> None:
    """Задачу взяли (кнопкой, ответом, с доски): перерисовать карточки, отметить её в других, спросить срок."""
    await refresh_open_card(bot, c.card)
    await sync_claim(bot, c.card["chat_id"], c.item["task"], c.item["claimed_by"], c.item["claimed_user_id"],
                     c.task.id, skip_key=key)
    await ask_deadline(bot, c.card["chat_id"], c.task)


async def _set_deadline(chat_id: int, task_id: int, text: str, who: str | None = None) -> tuple[bool, str]:
    """Возвращает (получилось, короткий текст без разметки — для всплывающей подсказки)."""
    t = await service.get_task(task_id)
    if not t:
        return False, "Задача не найдена."
    if text.strip() == "-":
        await service.add_history(t.id, f"{t.assignee_name}: без срока")
        return True, f"#{t.id} без срока"
    chat = await service.get_chat(chat_id)
    d, prec = resolve_deadline(text, datetime.now(MSK), chat.checkpoint if chat else None)
    if prec != "day" or not d:
        return False, "Не понял срок 🙈 Например: «к чт», «завтра», «20.10»."
    at = deadline_time(text)
    await service.update_task(chat_id, task_id, who=who or t.assignee_name, deadline=d, deadline_text=text.strip(),
                              deadline_time=at)
    return True, f"📅 #{t.id}: {fmt_deadline(d, at=at)}"


@router.callback_query(F.data.regexp(r"^d\|"))
async def on_deadline_choice(cq: CallbackQuery, bot: Bot) -> None:
    _, task_id, i = cq.data.split("|", 2)
    t = await service.get_task(int(task_id))
    if t and not service.may_manage(t, cq.from_user.id):
        await cq.answer(f"Срок выбирает {service.managers_label(t)}", show_alert=True)
        return
    ok, reply = await _set_deadline(cq.message.chat.id, int(task_id), DEADLINE_CHOICES[int(i)][1],
                                    who=display_name(cq.from_user))
    await cq.answer(reply)
    if ok:
        await tidy(bot, cq.message.chat.id, cq.message.message_id)


@router.message(GROUPS, F.reply_to_message, F.text)
async def on_reply(m: Message, bot: Bot) -> None:
    """Ответы боту: срок задачи, исправление карточки. Любой другой ответ — обычное сообщение."""
    replied = m.reply_to_message.message_id
    task_id = await store.r.get(f"askdl:{m.chat.id}:{replied}")
    if task_id:
        t = await service.get_task(int(task_id))
        if t and m.from_user and not service.may_manage(t, m.from_user.id):
            await brief(bot, m.chat.id, f"Срок выбирает {escape(service.managers_label(t))}.", reply_to=m.message_id)
            return
        ok, reply = await _set_deadline(m.chat.id, int(task_id), m.text, who=display_name(m.from_user))
        if not ok:
            await brief(bot, m.chat.id, escape(reply), reply_to=m.message_id)
            return
        # срок записан: 👌 на ответ, вопрос убираем сразу, ответ — через пару секунд
        await store.r.delete(f"askdl:{m.chat.id}:{replied}")
        await react(bot, m.chat.id, m.message_id, OK)
        await tidy(bot, m.chat.id, replied)
        await store.delete_later(m.chat.id, m.message_id, REPLY_TTL_S)
        return
    open_key = await store.r.get(f"opencard:{m.chat.id}:{replied}")
    if open_key and m.from_user and await claim_by_reply(bot, m, open_key):
        return
    ev = await service.event_by_card(m.chat.id, replied)
    status = rsvp_from_text(m.text) if ev else None
    if ev and status and m.from_user:
        await set_rsvp(bot, ev.id, m.from_user, status)
        await react(bot, m.chat.id, m.message_id, OK)
        await store.delete_later(m.chat.id, m.message_id, REPLY_TTL_S)
        return
    raw = await store.r.get(f"edit:{m.chat.id}:{replied}")
    if not raw:
        await on_group_message(m)
        return
    edit = json.loads(raw) if raw.startswith("{") else {"key": raw}  # старые запросы — без автора правки
    key = edit["key"]
    if edit.get("user_id") and m.from_user and m.from_user.id != edit["user_id"]:
        await brief(bot, m.chat.id, f"Карточку правит {escape(edit['name'])}. Своя правка — «✏️ Исправить».",
                    reply_to=m.message_id)
        return
    p = await store.get_proposal(key)
    if not p or p.get("status") != "pending":
        await brief(bot, m.chat.id, "Карточка уже решена.", reply_to=m.message_id)
        return
    trash = list(p.get("trash", [])) + [m.message_id]  # ответ с правкой уберём вместе с карточкой
    chat = await service.get_chat(m.chat.id)
    checkpoint = chat.checkpoint if chat else None
    mem = await _members(m.chat.id)
    try:
        if ";" in m.text:  # старый формат «кто; что; срок» тоже понимаем
            p = apply_edit(p, m.text, mem, datetime.now(MSK), checkpoint)
        else:
            p = await edit_card(llm, settings.gigachat_model, p, mem, m.text, datetime.now(MSK), checkpoint)
    except Exception:
        log.exception("edit via LLM failed")
        trash.append(await brief(bot, m.chat.id, "Не понял правку 🙈 Попробуй иначе: «срок — сб».",
                                 reply_to=m.message_id))
        p["trash"] = trash
        await store.update_proposal(p)
        return
    p["trash"] = trash
    await store.update_proposal(p)
    await store.r.delete(f"edit:{m.chat.id}:{replied}")
    await bot.edit_message_text(card_text(p), chat_id=m.chat.id, message_id=p["card_message_id"],
                                reply_markup=card_keyboard(key))
    await react(bot, m.chat.id, m.message_id, EDITED)  # карточка поправлена на месте — без отдельного сообщения
    await tidy(bot, m.chat.id, replied)  # вопрос «что поправить?» отработал


# --- обычные сообщения ---------------------------------------------------------------------------
@router.message(GROUPS, F.text | F.caption)
async def on_group_message(m: Message) -> None:
    u = m.from_user
    if not u or (m.text or "").startswith("/"):
        return
    if u.is_bot and not (settings.allow_actor_bots and u.id in ACTOR_IDS):
        return  # сообщения других ботов игнорируем: защита от петель
    await _register(m)
    alias = ALIAS.search(m.text or "")
    if alias and m.bot:  # «меня зовут Андрей» — запомнили, «андрей, сделай …» теперь найдёт этого человека
        await store.set_alias(m.chat.id, u.id, cap(alias.group(1).lower()))
        await react(m.bot, m.chat.id, m.message_id, OK)
    eid = await store.enqueue(message_payload(m))
    if eid:
        await store.r.hincrby("stats", "messages", 1)


async def main() -> None:
    global store
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.setLevel(logging.DEBUG if os.getenv("BOT_DEBUG") else logging.INFO)
    await init_db()
    store = Store.from_url(settings.redis_url, settings.context_ttl_hours * 3600)
    await store.ensure_group()
    global llm
    llm = GigaChatClient(settings.gigachat_auth_key, settings.gigachat_scope, settings.gigachat_ca_file)
    # всё, кроме напоминаний, — без звука: у команды не должен разрываться телефон
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, disable_notification=True))
    outgoing.attach(bot, "bot", store)
    dp = Dispatcher()

    @dp.update.outer_middleware()
    async def trace(handler, event, data):  # без текста сообщений: только тип апдейта и признак команды
        msg = event.message
        if msg is not None:
            log.debug("update %s: chat=%s from_bot=%s command=%s reply=%s text_len=%s entities=%s content=%s",
                      event.update_id, msg.chat.id, bool(msg.from_user and msg.from_user.is_bot),
                      (msg.text or "").startswith("/"), bool(msg.reply_to_message), len(msg.text or ""),
                      [e.type for e in (msg.entities or [])], msg.content_type)
        return await handler(event, data)

    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="tasks", description="Открытые задачи по людям"),
        BotCommand(command="task", description="Ответом на сообщение: сделать из него карточку"),
        BotCommand(command="board", description="Ссылка на веб-доску"),
        BotCommand(command="report", description="Отчёт к чекпоинту"),
        BotCommand(command="checkpoint", description="Дата чекпоинта: /checkpoint 17.10"),
        BotCommand(command="done", description="Задача выполнена: /done 12"),
        BotCommand(command="delete", description="Удалить задачу: /delete 12"),
        BotCommand(command="forget", description="Удалить мои задачи и цитаты"),
        BotCommand(command="status", description="Всё ли работает"),
        BotCommand(command="help", description="Что умеет бот"),
    ], scope=BotCommandScopeAllGroupChats())
    if settings.board_base_url.startswith("https://"):
        # кнопка «Доска» в личке с ботом открывает мини-апп со списком чатов пользователя
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Доска", web_app=WebAppInfo(url=settings.board_base_url)))
    me = await bot.get_me()
    log.info("bot @%s started, can_read_all_group_messages=%s, actors=%s",
             me.username, me.can_read_all_group_messages, sorted(ACTOR_IDS) if settings.allow_actor_bots else "off")
    # фоновые: уборка отработавших сообщений, монитор задержки разбора, пульс со сторожем зависания
    background = [asyncio.create_task(autodelete_loop(bot)), asyncio.create_task(health_loop(bot)),
                  Watchdog(store, "bot").start()]
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])
    finally:
        for task in background:
            task.cancel()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
