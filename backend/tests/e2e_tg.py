"""Сквозная проверка всего функционала в ТЕСТОВОЙ группе на живом боте.

Запуск — на сервере внутри контейнера бота (там окружение, база и Redis):
    docker compose -f docker-compose.prod.yml --env-file .env exec -T bot python - < e2e_tg.py

Сообщения пишут боты-участники (настоящие сообщения в чате). Нажатия на inline-кнопки боты делать не умеют,
поэтому нажатия «Егора» подаются в обработчики бота через Dispatcher.feed_update: логика, база и правка
сообщений в чате — настоящие, не проверяется только доставка нажатия самим Telegram.
"""

import asyncio
import hashlib
import hmac
import json
import random
from pathlib import Path
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import delete, select

import dogovorilis.bot.app as botapp
from dogovorilis.api.tg_auth import chat_param
from dogovorilis.bot import outgoing
from dogovorilis.bot.tidy import tidy
from dogovorilis.config import ROOT, get_settings
from dogovorilis.core import service
from dogovorilis.core.db import Event, Task, init_db, sessionmaker
from dogovorilis.core.store import Store
from dogovorilis.llm.dates import resolve_deadline
from dogovorilis.llm.dedup import mentioned, same_task
from dogovorilis.llm.gigachat import GigaChatClient
from dogovorilis.worker.app import Worker

S = get_settings()
CHAT = S.test_chat_id
SENT_KEY = f"e2e:{CHAT}:sent"  # id сообщений автотеста
MSK = timezone(timedelta(hours=3))
EGOR = User(id=910228167, is_bot=False, first_name="Егор", username="gorg1t")
EGOR_ACTOR = type("Viewer", (), {"id": EGOR.id, "name": "Егор"})  # для mini(): «Егор» в мини-аппе
API = S.board_base_url
results: list[tuple[str, bool, str]] = []
answers: list[str] = []


def check(name: str, ok, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(("✅" if ok else "❌"), name, ("— " + detail) if detail else "", flush=True)
    return bool(ok)


async def tg(token: str, method: str, **params):
    for _ in range(3):  # группа ограничивает частоту сообщений — ждём, сколько скажет Telegram
        async with httpx.AsyncClient(timeout=30) as c:
            r = (await c.post(f"https://api.telegram.org/bot{token}/{method}", json=params)).json()
        if r.get("ok"):
            return r["result"]
        if r.get("error_code") != 429:
            break
        await asyncio.sleep(r.get("parameters", {}).get("retry_after", 5) + 0.5)
    raise RuntimeError(f"{method}: {r}")


class Actor:
    def __init__(self, token: str):
        self.token = token
        self.id = int(token.split(":")[0])
        self.name = ""
        self.username = ""

    async def init(self):
        me = await tg(self.token, "getMe")
        self.name, self.username = me["first_name"], me["username"]
        await self.inbox()  # сбросить старые апдейты

    async def say(self, text: str, reply_to: int | None = None) -> int:
        params = {"chat_id": CHAT, "text": text}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        mid = (await tg(self.token, "sendMessage", **params))["message_id"]
        await botapp.store.r.sadd(SENT_KEY, mid)  # чтобы потом убрать карточки и задачи автотеста
        return mid

    async def inbox(self) -> list[dict]:
        """Сообщения основного бота, пришедшие этому участнику (ответы на его сообщения)."""
        ups = await tg(self.token, "getUpdates", timeout=0)
        if ups:
            await tg(self.token, "getUpdates", offset=ups[-1]["update_id"] + 1, timeout=0)
        main_id = int(S.bot_token.split(":")[0])
        return [u.get("message") or u.get("edited_message") for u in ups
                if (u.get("message") or u.get("edited_message") or {}).get("from", {}).get("id") == main_id]


async def wait(fn, timeout: float = 45, every: float = 1.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = await fn()
        if v:
            return v
        await asyncio.sleep(every)
    return None


async def proposals(msg_id: int) -> list[dict]:
    return [p for p in await botapp.store.recent_proposals(CHAT, 48 * 3600) if p.get("message_id") == msg_id]


async def proposal(msg_id: int, suffix: str = "0", timeout: float = 45) -> dict | None:
    async def get():
        p = await botapp.store.get_proposal(f"{CHAT}:{msg_id}:{suffix}")
        return p if p and (p.get("card_message_id") or p.get("type") == "open" and p.get("card_message_id")) else None
    return await wait(get, timeout)


async def task_by_key(key: str) -> Task | None:
    async with sessionmaker()() as s:
        return (await s.execute(select(Task).where(Task.proposal_key == key))).scalar_one_or_none()


def expected(phrase: str) -> date | None:
    return resolve_deadline(phrase, datetime.now(MSK))[0]


async def cleanup() -> int:
    """Убрать всё, что оставил автотест: карточки и задачи (иначе повторный прогон упрётся в дубли и «уже взятые»
    задачи, а доска зарастёт тестовыми) и сами сообщения — участников и бота — из чата."""
    r = botapp.store.r
    ids = sorted(int(x) for x in await r.smembers(SENT_KEY))
    n = 0
    for mid in ids:
        prefix = f"{CHAT}:{mid}:"
        for k in await r.keys(f"proposal:{prefix}*"):
            await r.delete(k)
            await r.zrem(f"proposals:{CHAT}", k.removeprefix("proposal:"))
        async with sessionmaker()() as s, s.begin():
            n += (await s.execute(delete(Task).where(Task.proposal_key.startswith(prefix)))).rowcount
            await s.execute(delete(Event).where(Event.proposal_key.startswith(prefix)))
    if ids:  # бот — администратор: удаляет и сообщения участников-ботов, и свои (их id запоминает при отправке)
        await tidy(bot, CHAT, *ids, *await botapp.store.bot_messages(CHAT, ids[0]))
    await r.delete(SENT_KEY)
    return n


OUT_LOG = Path(ROOT) / "logs" / "outgoing.log"  # журнал ответов бота в тестовую группу (LOG_OUTGOING=1)


def log_mark() -> int:
    return OUT_LOG.stat().st_size if OUT_LOG.exists() else 0


async def said(needle: str, mark: int, timeout: float = 20) -> str | None:
    """Бот написал в чат что-то с needle после отметки mark (ответы на команды теперь не реплаи — смотрим журнал)."""
    async def get():
        if not OUT_LOG.exists():
            return None
        with OUT_LOG.open(encoding="utf-8") as fh:
            fh.seek(mark)
            chunk = fh.read()
        return next((e for e in chunk.split("\n---") if needle in e), None)
    return await wait(get, timeout, 1)


def mini(actor) -> str:
    """Токен доски, как из мини-аппа: с тем, кто смотрит (иначе менять задачи нельзя)."""
    return URLSafeTimedSerializer(S.board_secret, salt="board").dumps({"chat_id": CHAT, "uid": actor.id, "uname": actor.name})


async def deleted(mid: int | None) -> bool:
    """Сообщение бота удалено из чата (правка разметки ненайденного сообщения — «message to edit not found»)."""
    if not mid:
        return False
    try:
        await bot.edit_message_reply_markup(chat_id=CHAT, message_id=mid, reply_markup=None)
    except TelegramBadRequest as e:
        return "not found" in str(e)
    return False


# ---------- нажатия «Егора» через диспетчер бота ----------
dp = Dispatcher()
dp.include_router(botapp.router)
bot: Bot = None  # type: ignore[assignment]


async def _fake_answer(self, text=None, **kw):
    answers.append(text or "")


async def _fake_reply(self, text, **kw):  # ответ на поддельное сообщение — просто сообщением в чат
    allowed = {k: v for k, v in kw.items() if k in ("reply_markup", "disable_web_page_preview")}
    return await self.bot.send_message(self.chat.id, text, **allowed)


CallbackQuery.answer = _fake_answer  # у поддельного нажатия нет настоящего query_id


CHAT_TITLE = "test"


def _chat() -> Chat:  # настоящее название: обработчики обновляют его в базе по апдейту
    return Chat(id=CHAT, type="supergroup", title=CHAT_TITLE)


async def press(data: str, card_msg_id: int, user: User = EGOR):
    msg = Message(message_id=card_msg_id, date=datetime.now(timezone.utc), chat=_chat(), text="card")
    cq = CallbackQuery(id=str(random.randint(1, 10**12)), from_user=user, chat_instance="e2e", message=msg, data=data)
    await dp.feed_update(bot, Update(update_id=random.randint(1, 10**9), callback_query=cq))


async def fake_reply(text: str, to_msg_id: int, user: User = EGOR):
    me = await bot.get_me()
    prompt = Message(message_id=to_msg_id, date=datetime.now(timezone.utc), chat=_chat(),
                     from_user=User(id=me.id, is_bot=True, first_name=me.first_name), text="prompt")
    m = Message(message_id=random.randint(10**8, 10**9), date=datetime.now(timezone.utc), chat=_chat(),
                from_user=user, text=text, reply_to_message=prompt)
    original = Message.reply
    Message.reply = _fake_reply
    try:
        await dp.feed_update(bot, Update(update_id=random.randint(1, 10**9), message=m))
    finally:
        Message.reply = original


async def find_key(prefix: str, value: str | None = None, timeout: float = 20, field: str | None = None) -> str | None:
    """Ключ Redis с таким значением; field — значение лежит в JSON под этим полем."""
    async def get():
        for k in await botapp.store.r.keys(f"{prefix}*"):
            v = await botapp.store.r.get(k)
            if field and v and v.startswith("{"):
                v = json.loads(v).get(field)
            if value is None or v == value:
                return k
        return None
    return await wait(get, timeout, 1)


# ---------- сценарии ----------
async def run(step, *args):
    try:
        await step(*args)
    except Exception as e:  # noqa: BLE001
        check(step.__name__, False, f"исключение: {e!r}")
        traceback.print_exc()


async def main():
    global bot
    await init_db()
    botapp.store = Store.from_url(S.redis_url, S.context_ttl_hours * 3600)
    botapp.llm = GigaChatClient(S.gigachat_auth_key, S.gigachat_scope, S.gigachat_ca_file)
    bot = Bot(S.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    outgoing.attach(bot, "e2e", botapp.store)  # ответы от имени бота из теста тоже уберутся в конце
    dima, masha, lesha = [Actor(t) for t in S.actor_tokens[:3]]
    for a in (dima, masha, lesha):
        await a.init()
    chat = await service.get_chat(CHAT)
    old_checkpoint = chat.checkpoint if chat else None
    global CHAT_TITLE
    CHAT_TITLE = (await bot.get_chat(CHAT)).title or CHAT_TITLE
    print(f"убрано задач прошлого прогона: {await cleanup()}", flush=True)
    ctx: dict = {}
    await dima.say("🧪 Автотест функций бота — сообщения ниже пишут боты-участники, можно не читать.")

    # 1. обещание → карточка → «Да» → задача
    async def s01_promise_confirm():
        mid = await masha.say("дизайн лендинга беру, к пт сделаю")
        p = await proposal(mid)
        if not check("1. обещание → карточка", p, "нет карточки за 45 с" if not p else f"{p['assignee_name']} — {p['task']} — {p['deadline']}"):
            return
        check("1. ответственный и срок", p["assignee_name"] == masha.name and p["deadline"] == str(expected("к пт")),
              f"ждали {masha.name}/{expected('к пт')}")
        await press(f"y|{p['key']}", p["card_message_id"])
        t = await wait(lambda: task_by_key(p["key"]), 15)
        ok = t and t.status == "open" and "подтвердил" in (t.history or "")
        check("1. «Да» → задача в базе с историей", ok, f"#{t.id}" if t else "задачи нет")
        check("1. после «Да» карточка убрана из чата", await deleted(p["card_message_id"]))
        ctx["t1"] = t
    await run(s01_promise_confirm)

    # 2. не договорённости → тишина
    async def s02_negatives():
        ids = [await dima.say(x) for x in ("кто-нибудь видел мой зарядник?", "вчера починил деплой",
                                            "если надо, могу помочь с вёрсткой", "решили: созвон в пт в 19:00",
                                            "ахах ну ты даёшь 😅")]
        await asyncio.sleep(25)
        extra = [p for i in ids for p in await proposals(i)]
        check("2. вопрос/прошедшее/условное/решение/шутка — без карточек", not extra,
              "; ".join(f"{p.get('type', 'task')}: {p.get('task') or [it['task'] for it in p.get('items', [])]}" for p in extra))
    await run(s02_negatives)

    # 3. просьба + согласие → карточка согласившегося с цитатой просьбы
    async def s03_two_step():
        q = await dima.say(f"@{masha.username} сможешь сделать раздел FAQ к среде?")
        a = await masha.say("ок, сделаю", reply_to=q)
        p = await proposal(a)
        if not check("3. просьба → «ок» → карточка", p, "" if p else "нет карточки"):
            return
        check("3. ответственный — согласившийся, срок из просьбы",
              p["assignee_name"] == masha.name and "faq" in p["task"].lower() and p["deadline"] == str(expected("к среде")),
              f"{p['assignee_name']} — {p['task']} — {p['deadline']}")
        check("3. в карточке цитата просьбы", any("FAQ" in c["text"] for c in p.get("context_quotes") or []))
        ctx["p3"] = p
    await run(s03_two_step)

    # 4. «Не задача»
    async def s04_reject():
        p = ctx.get("p3")
        if not p:
            return check("4. «Не задача»", False, "нет карточки из шага 3")
        await press(f"n|{p['key']}", p["card_message_id"])
        p2 = await botapp.store.get_proposal(p["key"])
        check("4. «Не задача» → отклонена, задачи нет", p2["status"] == "rejected" and not await task_by_key(p["key"]))
        check("4. карточка убрана из чата", await deleted(p["card_message_id"]))
    await run(s04_reject)

    # 5. «Исправить» своими словами
    async def s05_edit():
        mid = await lesha.say("настрою CI к чт")
        p = await proposal(mid)
        if not check("5. карточка для правки", p):
            return
        await press(f"e|{p['key']}", p["card_message_id"])
        k = await find_key(f"edit:{CHAT}:", p["key"], field="key")
        if not check("5. «Исправить» → бот просит правку", k):
            return
        edit = json.loads(await botapp.store.r.get(k))
        check("5. правку ведёт только нажавший", edit.get("user_id") == EGOR.id, str(edit))
        stranger = User(id=lesha.id, is_bot=False, first_name=lesha.name)
        await fake_reply("срок — воскресенье", int(k.rsplit(":", 1)[1]), user=stranger)
        await asyncio.sleep(3)
        check("5. ответ другого участника карточку не меняет", not (await botapp.store.get_proposal(p["key"])).get("edited"))
        await fake_reply("не Лёша, а Маша, и срок суббота", int(k.rsplit(":", 1)[1]))
        p2 = await wait(lambda: _edited(p["key"]), 40)
        check("5. правка текстом применена", p2 and p2["assignee_name"] == masha.name and p2["deadline"] == str(expected("сб")),
              f"{(p2 or {}).get('assignee_name')} / {(p2 or {}).get('deadline')}")
        check("5. вопрос «что поправить?» убран после правки", await deleted(int(k.rsplit(":", 1)[1])))
        await press(f"y|{p['key']}", p["card_message_id"])
        t = await wait(lambda: task_by_key(p["key"]), 15)
        check("5. исправленная задача сохранена", t and t.assignee_name == masha.name and t.edited)
        check("5. карточка убрана после «Да»", await deleted(p["card_message_id"]))
        ctx["t5"] = t

    async def _edited(key):
        p = await botapp.store.get_proposal(key)
        return p if p and p.get("edited") else None
    await run(s05_edit)

    # 6. повтор той же задачи не плодит карточки
    async def s06_duplicate():
        a = await lesha.say("докер-компоуз к пятнице соберу")
        p = await proposal(a)
        b = await lesha.say("ага, докер-компоуз к пт будет")
        await asyncio.sleep(18)
        check("6. повтор обещания — вторая карточка не появилась", p and not await proposals(b),
              "" if p else "нет первой карточки")
        if p:
            await press(f"y|{p['key']}", p["card_message_id"])
            ctx["t6"] = await wait(lambda: task_by_key(p["key"]), 15)
    await run(s06_duplicate)

    # 7. перенос срока подтверждённой задачи
    async def s07_reschedule():
        t1 = ctx.get("t1")
        if not t1:
            return check("7. перенос срока", False, "нет задачи из шага 1")
        mid = await masha.say("не успею дизайн лендинга к пт, сделаю к пн")
        p = await proposal(mid, f"r{t1.id}")
        if not check("7. «🔁 Перенос срока?» на подтверждённую задачу", p, "" if p else "карточки переноса нет"):
            return
        await press(f"r|{p['key']}", p["card_message_id"])  # Егор — не исполнитель и не автор
        check("7. посторонний перенос не подтверждает", (await service.get_task(t1.id)).deadline != expected("к пн"))
        await press(f"r|{p['key']}", p["card_message_id"], user=User(id=masha.id, is_bot=False, first_name=masha.name))
        t = await wait(lambda: _deadline_is(t1.id, expected("к пн")), 15)
        check("7. «Перенести» → срок обновлён и записан в историю", t and "Срок" in (t.history or ""))
        check("7. карточка переноса убрана", await deleted(p["card_message_id"]))

    async def _deadline_is(task_id, d):
        t = await service.get_task(task_id)
        return t if t and t.deadline == d else None
    await run(s07_reschedule)

    # 8. «Кто возьмёт?» → «🙋» → вопрос о сроке → «Завтра»
    async def s08_open_claim():
        mid = await dima.say("кто возьмёт настройку аналитики и тексты для лендинга?")
        p = await proposal(mid, "o")
        if not check("8. «Кто возьмёт?» с двумя задачами", p and len(p["items"]) >= 2,
                     str([it["task"] for it in (p or {}).get("items", [])])):
            return
        ctx["open8"] = p
        idx = next((i for i, it in enumerate(p["items"]) if "аналит" in it["task"].lower()), 0)
        await press(f"t|{p['key']}|{idx}", p["card_message_id"])
        p2 = await botapp.store.get_proposal(p["key"])
        it = p2["items"][idx]
        if not check("8. «🙋» → задача за Егором", it.get("claimed_by") == "Егор" and it.get("task_id")):
            return
        ask = await find_key(f"askdl:{CHAT}:", str(it["task_id"]))
        if not check("8. бот сразу спросил срок", ask):
            return
        await press(f"d|{it['task_id']}|1", int(ask.rsplit(':', 1)[1]))
        t = await wait(lambda: _deadline_is(it["task_id"], expected("завтра")), 15)
        check("8. кнопка «Завтра» → срок задачи", t)
        check("8. вопрос о сроке убран", await deleted(int(ask.rsplit(":", 1)[1])))
        ctx["t8"] = t
    await run(s08_open_claim)

    # 9. взять текстом → пункт карточки отмечается сам
    async def s09_text_claim():
        p = ctx.get("open8")
        if not p:
            return check("9. взятие текстом", False, "нет карточки из шага 8")
        mid = await masha.say("тексты для лендинга беру, к чт")
        c = await proposal(mid)
        if not check("9. «тексты беру» → карточка", c):
            return
        await press(f"y|{c['key']}", c["card_message_id"])
        async def marked():
            o = await botapp.store.get_proposal(p["key"])
            return any(it.get("claimed_by") == masha.name and "текст" in it["task"].lower() for it in o["items"])
        check("9. после «Да» пункт в «Кто возьмёт?» отмечен", await wait(marked, 15))
        ctx["t9"] = await wait(lambda: task_by_key(c["key"]), 15)
        o = await botapp.store.get_proposal(p["key"])
        if all(it.get("claimed_by") for it in o["items"]):
            check("9. всё разобрано → «Кто возьмёт?» убрана", await deleted(p["card_message_id"]))
    await run(s09_text_claim)

    # 10. уже взятое не предлагается заново, а про то, о чём не спрашивали, бот молчит
    async def s10_open_dedup():
        mid = await dima.say("кто возьмёт настройку аналитики?")
        await asyncio.sleep(20)
        new_card = await botapp.store.get_proposal(f"{CHAT}:{mid}:o")
        notes = [m for m in await dima.inbox() if m.get("reply_to_message", {}).get("message_id") == mid]
        check("10. взятую задачу не предлагает заново", not new_card, "появилась новая карточка" if new_card else "")
        check("10. отвечает, что задача уже взята", any("уже взял" in (m.get("text") or "") for m in notes),
              "; ".join((m.get("text") or "")[:60] for m in notes))
        # тема, которой ещё нет в чате (ни в задачах, ни в открытых карточках) — иначе это не «новая задача»
        known = [p.get("task") or "" for p in await botapp.store.recent_proposals(CHAT)]
        known += [it["task"] for p in await botapp.store.recent_proposals(CHAT) for it in p.get("items", [])]
        known += [t.title for t in await service.list_tasks(CHAT)]
        subj = next(s for s in ("экспорт отчёта в pdf", "интеграцию с jira", "импорт задач из csv",
                                "тёмную тему для админки", "уведомления на почту", "перевод интерфейса на английский")
                    if not any(same_task(s, k) for k in known))
        mid2 = await dima.say(f"кто возьмёт {subj}?")
        p = await proposal(mid2, "o")
        check("10. новая задача — карточка только с ней",
              p and len(p["items"]) == 1 and mentioned(p["items"][0]["task"], subj), f"{subj} → " + str([it["task"] for it in (p or {}).get("items", [])]))
        ctx["open10"] = p
    await run(s10_open_dedup)

    # 11. срок текстом в ответ на вопрос бота
    async def s11_deadline_text():
        p = ctx.get("open10")
        if not p:
            return check("11. срок текстом", False, "нет карточки из шага 10")
        await press(f"t|{p['key']}|0", p["card_message_id"])
        it = (await botapp.store.get_proposal(p["key"]))["items"][0]
        ask = await find_key(f"askdl:{CHAT}:", str(it.get("task_id")))
        if not check("11. вопрос о сроке после «🙋»", ask):
            return
        await press(f"d|{it['task_id']}|1", int(ask.rsplit(":", 1)[1]), user=User(id=masha.id, is_bot=False, first_name=masha.name))
        t = await service.get_task(it["task_id"])
        check("11. посторонний срок не ставит", t.deadline is None, str(t.deadline))
        await fake_reply("к чт", int(ask.rsplit(":", 1)[1]), user=User(id=dima.id, is_bot=False, first_name=dima.name))
        check("11. «к чт» от того, кто предложил задачу → срок", await wait(lambda: _deadline_is(it["task_id"], expected("к чт")), 15))
        check("11. вопрос о сроке убран", await deleted(int(ask.rsplit(":", 1)[1])))
    await run(s11_deadline_text)

    # 12. /task — если бот пропустил
    async def s12_task_cmd():
        mid = await lesha.say("гляну логи сборки")
        await asyncio.sleep(15)
        auto = await proposals(mid)
        await lesha.say("/task", reply_to=mid)
        if auto:
            await asyncio.sleep(6)
            check("12. /task на сообщение с карточкой — не дублирует", not await botapp.store.get_proposal(f"{CHAT}:{mid}:m0"))
        else:
            p = await proposal(mid, "m0", 40)
            check("12. /task → карточка из пропущенного сообщения", p, f"{(p or {}).get('task')}")
    await run(s12_task_cmd)

    # 12б. просьба ко всем со сленгом → «Кто возьмёт?»; «я сделаю» → карточка и отметка в «Кто возьмёт?»
    async def s12b_group_request():
        mid = await dima.say("доделайте кто нить ревью пулл-реквеста")
        o = await proposal(mid, "o")
        if not check("12б. «доделайте кто нить …» → «Кто возьмёт?»", o and any("ревью" in it["task"].lower() for it in o["items"]),
                     str([it["task"] for it in (o or {}).get("items", [])])):
            return
        a = await lesha.say("я сделаю")
        c = await proposal(a)
        if not check("12б. «я сделаю» → карточка согласившегося", c and c["assignee_name"] == lesha.name and "ревью" in c["task"].lower(),
                     f"{(c or {}).get('assignee_name')} — {(c or {}).get('task')}"):
            return
        await press(f"y|{c['key']}", c["card_message_id"])
        async def marked():
            p = await botapp.store.get_proposal(o["key"])
            return any(it.get("claimed_by") == lesha.name for it in p["items"])
        check("12б. после «Да» пункт в «Кто возьмёт?» отмечен", await wait(marked, 15))
        check("12б. единственный пункт взят → «Кто возьмёт?» убрана", await deleted(o["card_message_id"]))
        t = await wait(lambda: task_by_key(c["key"]), 15)
        ask = t and await find_key(f"askdl:{CHAT}:", str(t.id))
        if not check("12в. бот спросил срок у взявшего", ask):
            return
        await press(f"d|{t.id}|2", int(ask.rsplit(":", 1)[1]), user=User(id=lesha.id, is_bot=False, first_name=lesha.name))
        if not check("12в. срок кнопкой «До пт»", await wait(lambda: _deadline_is(t.id, expected("к пт")), 15)):
            return
        mid = await lesha.say("не успеваю ревью, сделаю к сб")
        r = await proposal(mid, f"r{t.id}")
        now = await service.get_task(t.id)
        check("12в. «не успеваю…» → «Перенос срока?», срок пока прежний", r and now.deadline == expected("к пт"),
              f"карточка {'есть' if r else 'нет'}, срок {now.deadline}")
        if r:
            await press(f"r|{r['key']}", r["card_message_id"], user=User(id=lesha.id, is_bot=False, first_name=lesha.name))
            check("12в. «Перенести» → новый срок", await wait(lambda: _deadline_is(t.id, expected("к сб")), 15))
    await run(s12b_group_request)

    # 13. команды
    async def s13_commands():
        await dima.say("/checkpoint 20.10")
        c = await wait(lambda: _checkpoint_is(date(datetime.now(MSK).year, 10, 20)), 15)
        check("13. /checkpoint 20.10", c)
        for cmd, needle in (("/tasks", "Открытые задачи"), ("/report", "Отчёт к чекпоинту"), ("/board", "Доска команды"),
                            ("/help", "Добавить администратора"), ("/status", "Всё работает")):
            mark = log_mark()
            mid = await dima.say(cmd)
            got = await said(needle, mark)
            check(f"13. {cmd}", got, "" if got else "ответа нет")
            if cmd == "/board" and got:
                check("13. /board → кнопка мини-аппа", "startapp=" in got, got[:160])
        check("13. команда (/status@бот) убрана из чата после ответа", await wait(lambda: deleted(mid), 10))
        t5, t1 = ctx.get("t5"), ctx.get("t1")
        if t1:
            mark = log_mark()
            await dima.say(f"/done {t1.id}")  # задача Маши — Дима её не закрывает
            check("13. /done чужой задачи — отказ", await said("может", mark) and (await service.get_task(t1.id)).status == "open")
        if t5:
            await masha.say(f"/done {t5.id}")
            check("13. /done N исполнителем", await wait(lambda: _status_is(t5.id, "done"), 15))

    async def _checkpoint_is(d):
        ch = await service.get_chat(CHAT)
        return ch and ch.checkpoint == d

    inbox_cache: list[dict] = []

    async def _reply_with(actor, mid, needle):
        inbox_cache.extend(await actor.inbox())
        return next((m for m in inbox_cache if m.get("reply_to_message", {}).get("message_id") == mid
                     and needle in (m.get("text") or "")), None)

    async def _status_is(task_id, status):
        t = await service.get_task(task_id)
        return t and t.status == status
    await run(s13_commands)

    # 14. API доски и 15. вход в мини-апп
    async def s14_api():
        token = URLSafeTimedSerializer(S.board_secret, salt="board").dumps({"chat_id": CHAT})
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{API}/api/board", params={"t": token})
            b = r.json() if r.status_code == 200 else {}
            t1 = ctx.get("t1")
            check("14. GET /api/board", r.status_code == 200 and (not t1 or any(t["id"] == t1.id for t in b.get("tasks", []))),
                  f"{r.status_code}, задач {len(b.get('tasks', []))}")
            if t1:
                one = next(t for t in b["tasks"] if t["id"] == t1.id)
                check("14. в задаче переписка и история", one["quote"]["text"] and one["history"], "")
                r = await c.patch(f"{API}/api/tasks/{t1.id}", params={"t": token}, json={"title": "дизайн лендинга (правка с доски)"})
                check("14. по ссылке без входа менять нельзя", r.status_code == 403, str(r.status_code))
                r = await c.patch(f"{API}/api/tasks/{t1.id}", params={"t": mini(dima)}, json={"title": "чужая правка"})
                check("14. чужую задачу менять нельзя", r.status_code == 403, str(r.status_code))
                mark = log_mark()
                r = await c.patch(f"{API}/api/tasks/{t1.id}", params={"t": mini(masha)}, json={"title": "дизайн лендинга (правка с доски)"})
                check("14. PATCH исполнителем → история", r.status_code == 200 and "переименована" in json.dumps(r.json()["history"], ensure_ascii=False))
                check("14. правка с доски рассказана в чате", await said("на доске", mark))
            r = await c.get(f"{API}/api/board", params={"t": "bad"})
            check("14. чужой токен → 401", r.status_code == 401)

            def init_data(uid, start=None):
                f = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": "Егор"}, ensure_ascii=False)}
                if start:
                    f["start_param"] = start
                chk = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
                sec = hmac.new(b"WebAppData", S.bot_token.encode(), hashlib.sha256).digest()
                return urlencode({**f, "hash": hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()})
            r = await c.post(f"{API}/api/tg-auth", json={"init_data": init_data(EGOR.id, chat_param(CHAT, S.board_secret))})
            check("15. мини-апп: участник чата входит", r.status_code == 200 and r.json().get("token"))
            r = await c.post(f"{API}/api/tg-auth", json={"init_data": init_data(12345, chat_param(CHAT, S.board_secret))})
            check("15. мини-апп: чужого не пускает", r.status_code == 403)
    await run(s14_api)

    # 16. напоминание за сутки
    async def s16_reminder():
        t = ctx.get("t8") or ctx.get("t1")  # задача со сроком «завтра»; если шаг 8 упал — ставим срок задаче из шага 1
        if not t:
            return check("16. напоминание", False, "нет задачи из шагов 8 и 1")
        t = await service.get_task(t.id)  # срок могли перенести в шаге 7
        tomorrow = datetime.now(MSK).date() + timedelta(days=1)  # календарное «завтра»: ночью «завтра» в речи — это сегодня
        if t.deadline != tomorrow:
            await service.update_task(CHAT, t.id, deadline=tomorrow)
        w = Worker(botapp.store, bot, botapp.llm, S.gigachat_model)
        await w.send_reminders(force=True)
        t = await service.get_task(t.id)
        check("16. напоминание за сутки отправлено и записано", t.reminded_at and "напомнил" in (t.history or ""))
    await run(s16_reminder)

    # 18. «кто делает X?» про взятую задачу → «уже взял(а)»
    async def s18_who_does():
        mid = await dima.say("кто делает тексты для лендинга?")
        await asyncio.sleep(25)
        notes = [m for m in await dima.inbox() if m.get("reply_to_message", {}).get("message_id") == mid]
        check("18. «кто делает тексты?» → «уже взял(а) Маша»",
              any("уже взял" in (m.get("text") or "") and masha.name in (m.get("text") or "") for m in notes),
              "; ".join((m.get("text") or "")[:60] for m in notes) or "ответа нет")
        check("18. и не предлагает её заново", not await botapp.store.get_proposal(f"{CHAT}:{mid}:o"))
    await run(s18_who_does)

    # 18б. безличное «нужно сделать X» — не обещание автора: про взятую задачу — справка, про новую — «Кто возьмёт?»
    async def s18b_impersonal_need():
        mid = await lesha.say("нужно сделать тексты для лендинга до конца недели")
        await asyncio.sleep(25)
        notes = [m for m in await lesha.inbox() if m.get("reply_to_message", {}).get("message_id") == mid]
        mine = [p for p in await proposals(mid) if p.get("type") != "open"]
        check("18б. «нужно сделать тексты» — без карточки на автора", not mine,
              "; ".join(f"{p.get('assignee_name')}: {p.get('task')}" for p in mine))
        check("18б. справка «уже взял(а) Маша» с названием задачи",
              any("уже взял" in (m.get("text") or "") and masha.name in (m.get("text") or "")
                  and "лендинг" in (m.get("text") or "").lower() for m in notes),
              "; ".join((m.get("text") or "")[:80] for m in notes) or "ответа нет")
        known = [p.get("task") or "" for p in await botapp.store.recent_proposals(CHAT)]
        known += [it["task"] for p in await botapp.store.recent_proposals(CHAT) for it in p.get("items", [])]
        known += [t.title for t in await service.list_tasks(CHAT)]
        subj = next(s for s in ("обновить ридми", "почистить логи сервера", "продлить домен", "заказать пиццу на демо",
                                "собрать отзывы пользователей") if not any(same_task(s, k) for k in known))
        mid2 = await lesha.say(f"нужно {subj} к пятнице")
        p = await proposal(mid2, "o")
        mine = [p for p in await proposals(mid2) if p.get("type") != "open"]
        check("18б. «нужно X» про новое — «Кто возьмёт?», а не карточка на автора", p and not mine,
              f"{subj}: open={bool(p)}, " + "; ".join(f"{q.get('assignee_name')}: {q.get('task')}" for q in mine))
    await run(s18b_impersonal_need)

    # 18в. уже разобранное короткое сообщение не склеивается в серию со следующим (его задача не всплывает снова)
    async def s18c_no_reglue():
        known = [p.get("task") or "" for p in await botapp.store.recent_proposals(CHAT)]
        known += [it["task"] for p in await botapp.store.recent_proposals(CHAT) for it in p.get("items", [])]
        known += [t.title for t in await service.list_tasks(CHAT)]
        first, second = [s for s in ("обновить логотип", "купить домен", "почистить кэш", "обновить фавикон",
                                     "сменить пароли", "заказать стикеры", "продлить хостинг")
                         if not any(same_task(s, k) for k in known)][:2]
        await lesha.say(f"нужно {first}")
        await asyncio.sleep(8)  # первое успело разобраться само
        mid = await lesha.say(f"нужно {second}")
        p = await proposal(mid, "o")
        lines = [it["task"] for it in (p or {}).get("items", [])] + list((p or {}).get("notes") or [])
        check("18в. второе «нужно X» не тянет задачу из первого",
              p and not any(mentioned(first, s) for s in lines), f"{first} / {second} → {lines}")
    await run(s18c_no_reglue)

    # 19. «я доделала тексты» → «Готово?» → задача в «Сделано»
    async def s19_done():
        t9 = ctx.get("t9")
        if not t9:
            return check("19. «доделала» → «Готово?»", False, "нет задачи из шага 9")
        mid = await masha.say("я доделала тексты для лендинга")
        d = await proposal(mid, f"d{t9.id}")
        if not check("19. «доделала тексты» → «✅ Готово?»", d, "" if d else "карточки нет"):
            return
        await press(f"f|{d['key']}", d["card_message_id"], user=User(id=masha.id, is_bot=False, first_name=masha.name))
        check("19. «Готово» → задача выполнена", await wait(lambda: _status_is(t9.id, "done"), 15))
        check("19. карточка убрана", await deleted(d["card_message_id"]))
    await run(s19_done)

    # 20. исполнитель ещё не писал в чат — карточка всё равно есть
    async def s20_unknown_assignee():
        mid = await dima.say("сделать лендинг - Максим")
        o = await proposal(mid, "o")
        check("20. «сделать лендинг - Максим» (не в чате) → «Кто возьмёт?»", o and any("лендинг" in it["task"] for it in o["items"]),
              str([it["task"] for it in (o or {}).get("items", [])]))
        check("20. и не карточка на «Максима»", not await botapp.store.get_proposal(f"{CHAT}:{mid}:0"))
    await run(s20_unknown_assignee)

    # 21. «меня зовут …» → поручение по этому имени находит человека
    async def s21_alias():
        await lesha.say("меня зовут Алексей")
        got = await wait(lambda: _alias_is(lesha.id, "Алексей"), 10)
        if not check("21. «меня зовут Алексей» запомнено", got):
            return
        mid = await dima.say("развернуть сайт - Алексей")
        c = await proposal(mid)
        check("21. «развернуть сайт - Алексей» → карточка на Лёшу", c and c["assignee_name"] == lesha.name,
              f"{(c or {}).get('assignee_name')} — {(c or {}).get('task')}")
        if c:
            await press(f"n|{c['key']}", c["card_message_id"])

    async def _alias_is(user_id, name):
        return (await botapp.store.aliases(CHAT)).get(user_id) == name
    await run(s21_alias)

    # 22. встреча: карточка, «Буду» кнопкой, «не смогу» ответом, видно на доске, удаление
    async def s22_event():
        mid = await dima.say("кто придёт на ретро по спринту завтра в 18:00?")
        ev = await wait(lambda: _event_by_key(f"{CHAT}:{mid}:e"), 45)
        if not check("22. «кто придёт на ретро завтра в 18:00?» → встреча", ev and ev.card_message_id,
                     f"{ev.title} · {ev.starts_at}" if ev else "встречи нет"):
            return
        tomorrow = datetime.now(MSK).date() + timedelta(days=1)
        check("22. дата и время встречи", ev.starts_at and ev.starts_at.astimezone(MSK).strftime("%H:%M") == "18:00"
              and ev.day in (tomorrow, expected("завтра")), f"{ev.day} {ev.starts_at}")
        await press(f"v|{ev.id}|yes", ev.card_message_id)
        mid_no = await masha.say("я завтра не смогу, у меня врач")
        mid_yes = await lesha.say("я буду на ретро")
        await wait(lambda: _attendee_is(ev.id, lesha.id, "yes"), 30)
        people = {a.user_id: a.status for a in (await service.attendees([ev.id])).get(ev.id, [])}
        check("22. «Буду» кнопкой, «не смогу, у меня врач» и «я буду на ретро» текстом записаны",
              people.get(EGOR.id) == "yes" and people.get(masha.id) == "no" and people.get(lesha.id) == "yes", str(people))
        check("22. ответы на встречу — не задачи", not await proposals(mid_no) and not await proposals(mid_yes))
        token = URLSafeTimedSerializer(S.board_secret, salt="board").dumps({"chat_id": CHAT})
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{API}/api/events", params={"t": token})
            got = next((e for e in r.json().get("events", []) if e["id"] == ev.id), None) if r.status_code == 200 else None
            check("22. встреча на доске в «Встречах» с участниками", got and got["status"] == "planned" and len(got["attendees"]) == 4,
                  f"{r.status_code} {got and got['attendees']}")
            r = await c.get(f"{API}/api/board", params={"t": token})
            check("22. на доске задач встреч нет", all("ретро" not in t["title"].lower() for t in r.json()["tasks"]))
            r = await c.post(f"{API}/api/events/{ev.id}/rsvp", params={"t": mini(EGOR_ACTOR)}, json={"status": "maybe"})
            check("22. ответ с доски из мини-аппа", r.status_code == 200 and any(
                a["user_id"] == EGOR.id and a["status"] == "maybe" for a in r.json()["attendees"]), str(r.status_code))
            r = await c.post(f"{API}/api/events/{ev.id}/rsvp", params={"t": token}, json={"status": "yes"})
            check("22. без входа через Telegram отметиться нельзя", r.status_code == 403)
            r = await c.delete(f"{API}/api/events/{ev.id}", params={"t": mini(dima)})
            check("22. удалить встречу с доски → карточка в чате убрана",
                  r.status_code == 200 and await wait(lambda: deleted(ev.card_message_id), 15))

    async def _attendee_is(event_id, user_id, status):
        return any(a.user_id == user_id and a.status == status for a in (await service.attendees([event_id])).get(event_id, []))

    async def _event_by_key(key):
        async with sessionmaker()() as s:
            return (await s.execute(select(Event).where(Event.proposal_key == key))).scalar_one_or_none()
    await run(s22_event)

    # 23. удаление задачи: /delete в чате и с доски (с возвратом)
    async def s23_delete():
        t1, t6 = ctx.get("t1"), ctx.get("t6")
        if not (t1 and t6):
            return check("23. удаление", False, "нет задач из шагов 1 и 6")
        mid = await masha.say(f"/delete {t1.id}")
        card = await wait(lambda: _reply_with(masha, mid, "Удалить задачу"), 20)
        if not check("23. /delete N → «Удалить задачу?»", card):
            return
        await press(f"del|{t1.id}|y", card["message_id"])  # Егор — не исполнитель
        check("23. посторонний не удаляет", (await service.get_task(t1.id)).status == "open")
        await press(f"del|{t1.id}|y", card["message_id"], user=User(id=masha.id, is_bot=False, first_name=masha.name))
        check("23. «Удалить» → задача удалена", await wait(lambda: _status_is(t1.id, "deleted"), 15))
        check("23. карточка и команда убраны", await deleted(card["message_id"]))
        token = URLSafeTimedSerializer(S.board_secret, salt="board").dumps({"chat_id": CHAT})
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{API}/api/board", params={"t": token})
            check("23. удалённой нет на доске", all(t["id"] != t1.id for t in r.json()["tasks"]))
            r = await c.delete(f"{API}/api/tasks/{t6.id}", params={"t": mini(masha)})
            check("23. чужую задачу с доски не удалить", r.status_code == 403, str(r.status_code))
            r = await c.delete(f"{API}/api/tasks/{t6.id}", params={"t": mini(lesha)})
            check("23. удаление с доски", r.status_code == 200 and await _status_is(t6.id, "deleted"))
            r = await c.patch(f"{API}/api/tasks/{t6.id}", params={"t": mini(lesha)}, json={"status": "open"})
            check("23. «Отменить» возвращает задачу", r.status_code == 200 and await _status_is(t6.id, "open"))
    await run(s23_delete)

    # 24. ответ на карточку «Кто возьмёт?» — взятие со сроком; «🙋 Беру» с доски
    async def s24_free_tasks():
        mid = await dima.say("кто глянет баг с логином?")
        o = await proposal(mid, "o")
        if not check("24. «кто глянет баг с логином?» → «Кто возьмёт?»", o, str((o or {}).get("items"))):
            return
        # ответ на карточку бота: боты-участники так не умеют (Telegram шлёт без реплая) — подаём через диспетчер
        await fake_reply("гляну сегодня вечером", o["card_message_id"], user=User(id=lesha.id, is_bot=False, first_name=lesha.name))
        async def claimed():
            it = (await botapp.store.get_proposal(o["key"]))["items"][0]
            return it if it.get("claimed_by") == lesha.name else None
        it = await wait(claimed, 25)
        t = it and await service.get_task(it["task_id"])
        check("24. ответ «гляну сегодня вечером» → задача Лёши со сроком сегодня",
              t and t.assignee_user_id == lesha.id and t.deadline == datetime.now(MSK).date(), f"{t and (t.assignee_name, t.deadline)}")
        mid = await dima.say("кто возьмёт тесты для апи?")
        o = await proposal(mid, "o")
        if not check("24. «кто возьмёт тесты для апи?» → «Кто возьмёт?»", o):
            return
        async with httpx.AsyncClient(timeout=20) as c:
            mark = log_mark()
            r = await c.post(f"{API}/api/open-items/claim", params={"t": mini(masha)}, json={"key": f"{o['key']}|0"})
            check("24. «🙋 Беру» на доске → задача Маши", r.status_code == 200 and r.json()["assignee_user_id"] == masha.id, str(r.status_code))
            ctx["api_task"] = r.json().get("id") if r.status_code == 200 else None
            check("24. и в чате об этом сказано", await said("взял(а) на доске", mark))
    await run(s24_free_tasks)

    # 25. встреча, а не задача; отмена и передача текстом
    async def s25_text_actions():
        mid = await dima.say("в следующий вторник проведу демо")
        ev = await wait(lambda: _event_by_key(f"{CHAT}:{mid}:e"), 45)
        await asyncio.sleep(3)
        check("25. «в следующий вторник проведу демо» → встреча, а не ещё и задача", ev and not await proposals(mid),
              f"встреча {'есть' if ev else 'нет'}, карточек {len(await proposals(mid))}")
        api_task = ctx.get("api_task")
        if api_task:
            mid = await masha.say("тесты для апи больше не нужны")
            x = await proposal(mid, f"x{api_task}")
            if check("25. «тесты для апи больше не нужны» → «🗑 Удалить?»", x):
                await press(f"cx|{x['key']}|y", x["card_message_id"])  # Егор — не исполнитель
                check("25. посторонний не удаляет", await _status_is(api_task, "open"))
                await press(f"cx|{x['key']}|y", x["card_message_id"], user=User(id=masha.id, is_bot=False, first_name=masha.name))
                check("25. отмена текстом → задача удалена", await wait(lambda: _status_is(api_task, "deleted"), 15))
        review = next((t for t in await service.list_tasks(CHAT, "open") if t.assignee_user_id == lesha.id and "ревью" in t.title.lower()), None)
        if review:
            mid = await lesha.say("я заболел, Маш, забери ревью пулл-реквеста")
            h = await proposal(mid, f"h{review.id}")
            check("25. «заболел, Маш, забери ревью» → «🔁 Лёша → Маша?», а не новая задача", h and not await botapp.store.get_proposal(f"{CHAT}:{mid}:0"))
            if h:
                await press(f"h|{h['key']}|y", h["card_message_id"], user=User(id=masha.id, is_bot=False, first_name=masha.name))
                t = await service.get_task(review.id)
                check("25. «Передать» → задача у Маши", t.assignee_user_id == masha.id, t.assignee_name)
    await run(s25_text_actions)

    # 26. пропуски: «сдам до 14 октября», английский, серия коротких сообщений
    async def s26_misses():
        mid = await lesha.say("акт сверки сдам до 14 октября")
        c = await proposal(mid)
        check("26. «акт сверки сдам до 14 октября» → карточка", c and "акт" in c["task"].lower(), f"{(c or {}).get('task')}")
        mid = await masha.say("I'll update the README by Monday")
        c = await proposal(mid)
        check("26. «I'll update the README by Monday» → карточка со сроком", c and c.get("deadline"), f"{(c or {}).get('task')} · {(c or {}).get('deadline')}")
        ids = [await masha.say(x) for x in ("так", "по релизу", "я завтра", "соберу", "changelog")]
        c = await proposal(ids[-1], timeout=50)
        early = [p for i in ids[:-1] for p in await proposals(i)]
        check("26. серия «так / по релизу / я завтра / соберу / changelog» → одна карточка", c and "changelog" in c["task"].lower()
              and c.get("deadline") and not early, f"{(c or {}).get('task')} · {(c or {}).get('deadline')} · лишних {len(early)}")
    await run(s26_misses)

    # 17. /forget
    async def s17_forget():
        before = [t for t in await service.list_tasks(CHAT) if t.assignee_user_id == lesha.id]
        mid = await lesha.say("/forget")
        card = await wait(lambda: _reply_with(lesha, mid, "удалить твои данные"), 20)
        if not check("17. /forget сначала спрашивает", card):
            return
        await press(f"fg|{lesha.id}|y", card["message_id"], user=User(id=masha.id, is_bot=False, first_name=masha.name))
        check("17. чужие данные удалить нельзя", [t for t in await service.list_tasks(CHAT) if t.assignee_user_id == lesha.id])
        await press(f"fg|{lesha.id}|y", card["message_id"], user=User(id=lesha.id, is_bot=False, first_name=lesha.name))
        async def gone():
            return not [t for t in await service.list_tasks(CHAT) if t.assignee_user_id == lesha.id]
        check("17. /forget убирает участника из задач", await wait(gone, 15), f"было {len(before)}")
        kept = [await service.get_task(t.id) for t in before]
        check("17. задачи команды остались (обезличены)", kept and all(t and t.assignee_name == "Участник" for t in kept),
              str([(t.title, t.assignee_name) for t in kept if t]))
    await run(s17_forget)

    # итог
    await service.set_checkpoint(CHAT, old_checkpoint)
    ok = sum(r[1] for r in results)
    print(f"\nИТОГО: {ok}/{len(results)}")
    if ok == len(results):  # при ошибках оставляем всё для разбора — уберёт следующий прогон
        await asyncio.sleep(3)
        print(f"убрано тестовых задач: {await cleanup()}, сообщения автотеста удалены из чата")
    await dima.say(f"🧪 Автотест: {ok}/{len(results)} проверок прошло" + ("" if ok == len(results) else " — разбираю ошибки"))
    for name, good, detail in results:
        if not good:
            print("  ❌", name, detail)
    await bot.session.close()
    await botapp.llm.close()


asyncio.run(main())
