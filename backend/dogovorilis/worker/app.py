"""Воркер: очередь → предфильтр → GigaChat → карточка в чат. Плюс напоминания за сутки до срока.

Сообщение подтверждается в очереди (XACK) только после обработки: если модель недоступна,
оно остаётся в очереди и будет обработано позже (риск А1 — ноль потерь).
Запуск: python -m dogovorilis.worker.app
"""

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from html import escape

from ..bot import outgoing
from ..bot.cards import (MSK, cancel_keyboard, cancel_text, cap, card_keyboard, card_text, deadline_ask_keyboard,
                         deadline_ask_text, done_keyboard, done_text, event_keyboard, event_text, fmt_deadline,
                         handover_keyboard,
                         handover_text, msg_link, open_card_keyboard, open_card_text, reschedule_keyboard,
                         reschedule_text)
from ..bot.tidy import NOTE_TTL_S, react, reply_params
from ..config import get_settings
from ..core import service
from ..core.db import init_db
from ..core.health import Watchdog
from ..core.store import LOW_STREAM, STREAM, Store
from ..llm.completion import CANCEL, DONE_VERB, HANDOVER, cancelled, completed_tasks
from ..llm.dates import URGENT, deadline_time, resolve_deadline, split_deadline
from ..llm.extractor import ChatMessage, ExtractedTask, _match_member, evidence_in_message, extract
from ..llm.fallback import rule_promises
from ..llm.gigachat import GigaChatClient, LLMUnavailable
from ..llm.dedup import classify, expand_recent, match_open_item, mentioned, same_task, triage_open_items
from ..llm.events import detect_event, is_event_message, parse_when, rsvp_intent
from ..llm.open_tasks import (OpenTask, detect_open_tasks, impersonal_need, is_bare_accept, is_bare_call,
                              is_volunteer_call, latest_call_item)
from ..llm.prefilter import DASH_ASSIGN, prefilter

log = logging.getLogger("worker")
settings = get_settings()


def _card_ref(chat_id: int, hit: dict | None) -> str:
    link = msg_link(chat_id, hit["card_message_id"]) if hit and hit.get("card_message_id") else None
    return f'<a href="{link}">карточка</a>' if link else "карточка выше"
CONSUMER = f"worker-{os.getpid()}"
DEFERRED = -1  # process(): сообщение отложено во вторую очередь
SERIES_SHORT = 25   # «так», «по релизу», «я завтра» — кусок мысли, а не сообщение целиком
SERIES_WAIT_S = 4   # столько ждём, допишет ли человек ещё
SERIES_WAIT_LONG_S = 8  # серия уже идёт — следующий кусок печатают дольше (и Telegram может придержать отправку)


def thread_before(source_ids: list[int], msg: dict, ctx_by_id: dict[int, dict]) -> list[dict]:
    """Сообщения до цитаты, из которых сложилась задача: то, на что ответили, и источники от модели (не больше двух)."""
    ids = {i for i in source_ids or [] if i != msg["message_id"] and i in ctx_by_id}
    if msg.get("reply_to") in ctx_by_id:
        ids.add(msg["reply_to"])
    return [{"author": ctx_by_id[i]["name"], "text": ctx_by_id[i]["text"], "ts": ctx_by_id[i]["ts"],
             "user_id": ctx_by_id[i].get("user_id")} for i in sorted(ids)[-2:]]


def requested_by(t: ExtractedTask, msg: dict, quotes: list[dict]) -> tuple[int | None, str | None]:
    """Кто предложил задачу: автор просьбы, на которую согласились, или тот, кто поручил другому."""
    if t.kind == "accepted_request" and quotes:
        return quotes[-1].get("user_id"), quotes[-1]["author"]
    if t.assignee != msg["name"]:
        return msg["user_id"], msg["name"]
    return None, None


def unassigned_items(dropped: list[dict], target: ChatMessage) -> list[OpenTask]:
    """Задача есть, а исполнителя среди участников не найти («кто-то из вас», «Максим», которого нет в чате) —
    не угадываем и не плодим «исполнителей-призраков»: предложим «Кто возьмёт?» с кнопкой."""
    out: list[OpenTask] = []
    for d in dropped:
        if d.get("drop") != "unknown_assignee" or not d.get("task") or not evidence_in_message(d.get("evidence"), target.text):
            continue
        if any(mentioned(o.task, str(d["task"])) for o in out):
            continue
        day, precision = resolve_deadline(d.get("deadline_text"), target.ts)
        out.append(OpenTask(task=str(d["task"]), deadline_text=d.get("deadline_text") or None,
                            deadline=day if precision == "day" else None))
    return out


def _taken_note(task: str, hit: dict) -> str:
    """«Подготовить отчёт по продажам» уже взял(а) Дима, до пн 19.10 — название и срок той задачи, с которой
    сопоставили вопрос: так видно, что бот понял, и легко заметить, если речь о другой."""
    dl = f", {fmt_deadline(hit['deadline'])}" if hit.get("deadline") else ""
    return f"«{escape(cap(hit.get('task') or task))}» уже взял(а) {escape(hit['assignee_name'])}{dl}"


NOT_A_NAME = {"все", "всем", "вы", "мы", "они", "он", "она", "из", "вас", "нас", "команда", "любой", "кто",
              "someone", "anyone", "team", "unknown", "null", "none"}


NOT_WHO = {"срочно", "важно", "потом", "сегодня", "завтра", "послезавтра", "готово", "ок", "да", "нет", "плиз", "пж",
           "пн", "вт", "ср", "чт", "пт", "сб", "вс", "кт", "дедлайн"}


def dash_assignments(tasks: list[ExtractedTask], members: list[dict], target: ChatMessage,
                     checkpoint) -> tuple[list[ExtractedTask], list[OpenTask]]:
    """«сделать презу к пд - максим»: поручение строкой «задача - имя» разбираем правилом, а не моделью.

    Модель на таком формате нестабильна (то пусто, то ответственный — автор), а формат однозначный.
    Строку, которую модель уже разобрала, оставляем, но ответственного берём из строки. Имя не нашлось среди
    участников — это свободная задача («Кто возьмёт?»), а не карточка на незнакомого человека.
    """
    out = list(tasks)
    free: list[OpenTask] = []
    names = {m["name"] for m in members}
    for m in DASH_ASSIGN.finditer(target.text):
        task, who = m.group("task").strip(), m.group("who")
        if who.lower() in NOT_WHO or who.lower() in NOT_A_NAME or resolve_deadline(who, target.ts)[1] == "day":
            continue
        name = _match_member(who, members, target.author)
        known = name in names
        # «фронт - норм» — не поручение: нужен глагол-задача в начале («сделать…») или известный участник после тире
        if not known and not re.match(r"[а-яё]+(ть|ти|чь|ться|тись)\b", task.lower()):
            continue
        day, precision = resolve_deadline(task, target.ts, checkpoint)
        same = next((i for i, t in enumerate(out) if mentioned(t.task, task)), None)
        if not known:
            if same is not None:
                out.pop(same)  # модель приписала задачу автору — но она для другого, неизвестного человека
            free.append(OpenTask(task=task, deadline_text=None, deadline=day if precision == "day" else None))
            continue
        assignee = name
        if same is not None:
            out[same] = dataclasses.replace(out[same], assignee=assignee)
        else:
            out.append(ExtractedTask(assignee=assignee, task=task, deadline_text=None, deadline_model=None,
                                     deadline=day if precision == "day" else None,
                                     deadline_precision=precision if precision == "day" else "none",
                                     kind="assignment", source_ids=[target.id]))
    return out, free


def normalize_deadline(t: ExtractedTask, text: str, target: ChatMessage, checkpoint) -> tuple[ExtractedTask, str | None, bool]:
    """Срок — не часть названия («…домен до 20 октября»), время срока («к 18:00», «до обеда»), «срочно» — сегодня."""
    title, day, phrase = split_deadline(t.task, target.ts, checkpoint)
    if day:
        t = dataclasses.replace(t, task=title) if t.deadline else \
            dataclasses.replace(t, task=title, deadline=day, deadline_text=phrase, deadline_precision="day")
    urgent = bool(URGENT.search(text.lower().replace("ё", "е")))
    if urgent and not t.deadline:
        t = dataclasses.replace(t, deadline=target.ts.date(), deadline_precision="day")
    return t, deadline_time(t.deadline_text), urgent


def fallback_promises(text: str, msg: dict, target: ChatMessage, checkpoint) -> list[ExtractedTask]:
    """«акт сверки сдам до 14 октября», «I'll fix the CI by Monday» — модель пропустила, правило ловит (решает «Да»)."""
    out = []
    for task, phrase in rule_promises(text, target.ts, checkpoint):
        day, precision = resolve_deadline(phrase, target.ts, checkpoint) if phrase else (None, "none")
        out.append(ExtractedTask(assignee=msg["name"], task=task, deadline_text=phrase, deadline_model=None,
                                 deadline=day if precision == "day" else None, deadline_precision=precision,
                                 kind="promise", source_ids=[msg["message_id"]]))
    return out


def as_open_item(t: ExtractedTask, recent: list[dict]) -> ExtractedTask:
    """«Я возьму продление домена», а свободная «Пролонгировать домен» уже висит — это она: берём её название
    (и срок, если свой не назван), чтобы после «Да» пункт отметился взятым, а не появился дубль."""
    hit = match_open_item(t.task, [p for p in recent if p.get("type") == "open"])
    if not hit:
        return t
    item = hit[0]["items"][hit[1]]
    if t.deadline or not item.get("deadline"):
        return dataclasses.replace(t, task=item["task"])
    return dataclasses.replace(t, task=item["task"], deadline=date.fromisoformat(item["deadline"]),
                               deadline_text=item.get("deadline_text"), deadline_precision="day")


def bind_to_latest_call(t, msg: dict, text: str, recent: list[dict]):
    """«я сделаю» без названия задачи — про последний «Кто возьмёт?», если модель сослалась на сообщение раньше него.

    Модель иногда берёт явный вопрос «кто возьмёт jira?» постарше, а не свежее «доделайте кто нить ревью».
    Если модель сослалась на более новое сообщение (адресную просьбу) или ответили на конкретное — ей виднее.
    """
    if not is_bare_accept(text):
        return t
    hit = latest_call_item(recent, msg["message_id"])
    if not hit:
        return t
    card, item = hit
    if msg.get("reply_to") and msg["reply_to"] not in (card["message_id"], card.get("card_message_id")):
        return t
    sources = [i for i in t.source_ids or [] if i != msg["message_id"]]
    if (sources and max(sources) > card["message_id"]) or same_task(t.task, item["task"]):
        return t
    return dataclasses.replace(t, task=item["task"], source_ids=[card["message_id"], msg["message_id"]])


class Worker:
    def __init__(self, store: Store, bot: Bot | None, client: GigaChatClient, model: str):
        self.store, self.bot, self.client, self.model = store, bot, client, model
        # предохранитель: пока модель недоступна, не перебираем очередь, а ждём с растущей паузой
        self.paused_until = 0.0
        self.backoff = 2.0
        self.level, self._level_ts = 0, 0.0

    async def degrade_level(self) -> int:
        """0 — норма; 1 — очередь длинная: слабые сигналы откладываем; 2 — ещё и быстрая модель. С гистерезисом."""
        if time.time() - self._level_ts < 2:
            return self.level
        backlog = await self.store.r.xlen(STREAM)
        lvl = self.level
        if backlog >= settings.degrade_fast_at:
            lvl = 2
        elif backlog >= settings.degrade_strict_at:
            lvl = 2 if lvl == 2 and backlog >= settings.degrade_fast_at // 2 else 1
        elif backlog < 5:
            lvl = 0  # между 5 и порогом уровень не меняем — гистерезис
        if lvl != self.level:
            log.warning("degrade level %d → %d (backlog %d)", self.level, lvl, backlog)
        self.level, self._level_ts = lvl, time.time()
        await self.store.r.set("degrade:level", lvl)
        return lvl

    async def process(self, msg: dict) -> int:
        """Возвращает число отправленных карточек. LLMUnavailable пробрасывается наверх."""
        st = self.store
        text = (msg.get("text") or "").strip()
        if not text:
            return 0
        if not msg.get("dry_run"):
            text = await self.glue_series(msg, text)
            if text is None:
                return 0  # человек ещё дописывает серию — разберём её целиком по последнему сообщению
        done_cards = await self.completions(msg, text) + await self.cancellations(msg, text)
        if await self.event_rsvp(msg, text):
            return done_cards + 1  # «буду на созвоне» — отметка на встрече, задач тут нет
        ctx_raw = await st.get_context(msg["chat_id"], msg["message_id"], settings.context_messages)
        pf = prefilter(text, [c["text"] for c in ctx_raw])
        open_call = is_volunteer_call(text)  # «кто сделает фронт?» — ищем исполнителя
        event_call = is_event_message(text)  # «кто придёт на созвон завтра?»
        if not pf.passed and not open_call and not event_call:
            return done_cards
        if await st.llm_down():
            raise LLMUnavailable("chaos:llm_down")
        lvl = 0 if msg.get("deferred") else await self.degrade_level()
        if lvl >= 1 and set(pf.reasons) <= {"time"} and not open_call:
            # перегрузка: слабый сигнал (только слово про время) — во вторую очередь, разберём после пика
            await st.defer(msg)
            await st.r.hincrby("stats", "deferred", 1)
            return DEFERRED
        model = settings.degrade_model if lvl >= 2 else self.model

        dry = msg.get("dry_run", False)
        if dry:
            members = msg.get("members") or [{"name": msg["name"], "username": msg.get("username"), "user_id": msg["user_id"]}]
            checkpoint = date.fromisoformat(msg["checkpoint"]) if msg.get("checkpoint") else None
        else:
            aliases = await st.aliases(msg["chat_id"])
            members = [{"name": m.name, "username": m.username, "user_id": m.user_id, "alias": aliases.get(m.user_id)}
                       for m in await service.members(msg["chat_id"])]
            chat = await service.get_chat(msg["chat_id"])
            checkpoint = chat.checkpoint if chat else None
        ctx = [ChatMessage(c["message_id"], c["name"], datetime.fromisoformat(c["ts"]), c["text"], c.get("reply_to"))
               for c in ctx_raw]
        target = ChatMessage(msg["message_id"], msg["name"], datetime.fromisoformat(msg["ts"]), text, msg.get("reply_to"))
        event_title = None
        if event_call:
            n, event_title = await self.events(msg, members, ctx, target, checkpoint, text, dry)
            done_cards += n
            if not pf.passed and not open_call:
                return done_cards  # только про встречу — договорённостей искать не в чем

        res = await extract(self.client, model, members, ctx, target, checkpoint,
                            prompt_version=settings.extract_prompt)
        if lvl >= 2:
            await st.r.hincrby("stats", "fast_model_calls", 1)
        await st.r.hincrby("stats", "prefilter_passed", 1)  # считаем один раз, после успешного вызова
        await st.r.hincrby("stats", "llm_calls", 1)
        await st.r.rpush("lat:llm", f"{res.llm.latency_s:.3f}")
        if msg.get("run_id"):
            await st.r.rpush(f"run:{msg['run_id']}:lat_llm", f"{res.llm.latency_s:.3f}")
        if res.error:
            log.warning("bad LLM output for %s:%s: %s", msg["chat_id"], msg["message_id"], res.error)

        recent = await st.recent_proposals(msg["chat_id"])
        ctx_by_id = {c["message_id"]: c for c in ctx_raw}
        sent = 0
        handled: list[str] = []  # задачи, по которым из этого сообщения уже есть карточка — «Кто возьмёт?» про них молчит
        need_only = impersonal_need(text)
        found, dash_free = dash_assignments(res.tasks, members, target, checkpoint)
        if not found and not dash_free and not open_call and not unassigned_items(res.dropped, target):
            found = fallback_promises(text, msg, target, checkpoint)  # модель промолчала, а обещание явное
        for idx, t in enumerate(found):
            if event_title and (mentioned(t.task, event_title) or same_task(t.task, event_title)):
                continue  # «в следующий вторник проведу демо» — это встреча, а не ещё и задача
            if need_only and t.assignee == msg["name"] and not thread_before(t.source_ids, msg, ctx_by_id):
                continue  # «нужно сделать отчёт» — не обещание автора (разметка: vague); предложим «Кто возьмёт?»
            t = bind_to_latest_call(t, msg, text, recent)
            if await self.handover(msg, t, text, members, dry):
                handled.append(t.task)
                sent += 1
                continue
            t, at, urgent = normalize_deadline(t, text, target, checkpoint)
            t = as_open_item(t, recent)
            new_deadline = t.deadline.isoformat() if t.deadline else None
            verdict, dup = (classify(t.task, t.assignee, new_deadline, t.source_ids, msg["message_id"],
                                     expand_recent(recent)) if t.assignee else ("duplicate", None))
            if verdict == "duplicate":
                await st.r.hincrby("stats", "duplicates_suppressed", 1)
                continue
            if verdict == "reschedule":
                await self.reschedule(msg, dup, new_deadline, t.deadline_text, text, dry)
                handled.append(t.task)
                sent += 1
                continue
            member = next((m for m in members if m["name"] == t.assignee), None)
            p = {
                "key": f"{msg['chat_id']}:{msg['message_id']}:{idx}", "chat_id": msg["chat_id"],
                "message_id": msg["message_id"], "assignee_name": t.assignee,
                "assignee_user_id": member["user_id"] if member else None,
                "task": t.task, "deadline": t.deadline.isoformat() if t.deadline else None,
                "deadline_text": t.deadline_text, "kind": t.kind, "quote_text": text,
                "context_quotes": thread_before(t.source_ids, msg, ctx_by_id),
                "quote_author_id": msg["user_id"], "quote_author_name": msg["name"], "quote_ts": msg["ts"],
                "status": "pending", "model": self.model, "created": time.time(),
            }
            p["requested_by_id"], p["requested_by_name"] = requested_by(t, msg, p["context_quotes"])
            p["deadline_time"], p["urgent"] = at, urgent
            handled.append(t.task)
            if not await st.save_proposal(p):
                continue  # эту карточку уже создавали при прошлой попытке обработки
            recent.append(p)
            if dry or self.bot is None:
                await st.r.rpush(f"run:{msg.get('run_id', 'dry')}:cards", json.dumps(p, ensure_ascii=False))
            elif await st.claim_once(f"card:{p['key']}"):
                card = await self.bot.send_message(msg["chat_id"], card_text(p), reply_markup=card_keyboard(p["key"]),
                                                   reply_parameters=reply_params(msg["message_id"]))
                p["card_message_id"] = card.message_id
                await st.update_proposal(p)
            await st.r.rpush("lat:card", f"{time.time() - msg['unix']:.3f}")
            if msg.get("run_id"):
                kind = "lat_card_deferred" if msg.get("deferred") else "lat_card"
                await st.r.rpush(f"run:{msg['run_id']}:{kind}", f"{time.time() - msg['unix']:.3f}")
            await st.r.hincrby("stats", "cards", 1)
            sent += 1
        if open_call:
            sent += await self.open_tasks(msg, members, ctx, target, checkpoint, text, dry, handled)
        elif unassigned := dash_free + [o for o in unassigned_items(res.dropped, target)
                                        if not any(mentioned(d.task, o.task) for d in dash_free)]:
            sent += await self.offer_open_items(msg, unassigned, text, dry, handled)
        return sent + done_cards

    async def open_tasks(self, msg: dict, members: list[dict], ctx: list, target: ChatMessage, checkpoint,
                         text: str, dry: bool, handled: list[str] = ()) -> int:
        """Карточка «Кто возьмёт?» с кнопкой на каждую задачу без ответственного."""
        st = self.store
        found = await detect_open_tasks(self.client, self.model, members, ctx, target, checkpoint)
        await st.r.hincrby("stats", "llm_calls", 1)
        # модель иногда подтягивает старые задачи из переписки: берём только названные в самом сообщении,
        # а всё найденное — лишь для голого «кто возьмёт?», который и относится к предыдущим сообщениям
        found = [it for it in found if mentioned(it.task, text)] or (found if is_bare_call(text) else [])
        return await self.offer_open_items(msg, found, text, dry, handled)

    async def offer_open_items(self, msg: dict, found: list[OpenTask], text: str, dry: bool,
                               handled: list[str] = ()) -> int:
        """Карточка «Кто возьмёт?» по найденным задачам; уже взятое и уже предложенное — справкой."""
        st = self.store
        # по этой задаче из того же сообщения уже есть карточка («нужно сделать отчёт, я возьму») — не спорим с ней
        found = [it for it in found if not any(same_task(it.task, h) or mentioned(h, it.task) for h in handled)]
        triage = triage_open_items([it.task for it in found], await self.known_tasks(msg["chat_id"]))
        items = [it for it, (verdict, _, _) in zip(found, triage) if verdict == "new"]
        asked_taken = [(task, hit) for v, task, hit in triage if v == "taken"]
        asked_offered = [(task, hit) for v, task, hit in triage if v == "offered"]
        if not items:
            notes = [_taken_note(task, hit) for task, hit in asked_taken]
            notes += [f"«{escape(cap(task))}» ещё свободна — карточка выше" for task, _ in asked_offered]
            if notes and not dry and self.bot is not None:
                old_card = next((h.get("card_message_id") for _, h in asked_offered if h), None)
                note = await self.bot.send_message(msg["chat_id"], "ℹ️ " + "\n".join(notes),
                                                   reply_parameters=reply_params(old_card or msg["message_id"]))
                await st.delete_later(msg["chat_id"], note.message_id, NOTE_TTL_S)
                await st.r.hincrby("stats", "open_duplicates", len(notes))
            return 0
        p = {"key": f"{msg['chat_id']}:{msg['message_id']}:o", "type": "open", "chat_id": msg["chat_id"],
             "message_id": msg["message_id"], "status": "open", "created": time.time(),
             "items": [{"task": it.task, "deadline": it.deadline.isoformat() if it.deadline else None,
                        "deadline_text": it.deadline_text, "claimed_by": None, "claimed_user_id": None,
                        "task_id": None} for it in items]
                      # спросили и про уже взятое — показываем его в той же карточке строкой «✅ кто взял»
                      + [{"task": hit.get("task") or task, "deadline": hit.get("deadline"), "deadline_text": None,
                          "claimed_by": hit["assignee_name"], "claimed_user_id": hit.get("assignee_user_id"),
                          "task_id": hit.get("task_id")} for task, hit in asked_taken],
             # спросили и про то, что уже предлагали, — ссылкой на прежнюю карточку, без второй кнопки
             "notes": [f"«{escape(cap(task))}» уже предлагали — " + _card_ref(msg["chat_id"], hit)
                       for task, hit in asked_offered],
             "quote_text": text, "quote_author_id": msg["user_id"], "quote_author_name": msg["name"],
             "quote_ts": msg["ts"]}
        if not await st.save_proposal(p):
            return 0
        await st.r.hincrby("stats", "open_cards", 1)
        if dry or self.bot is None:
            await st.r.rpush(f"run:{msg.get('run_id', 'dry')}:cards", json.dumps(p, ensure_ascii=False))
        elif await st.claim_once(f"card:{p['key']}"):
            card = await self.bot.send_message(msg["chat_id"], open_card_text(p), reply_markup=open_card_keyboard(p),
                                               reply_parameters=reply_params(msg["message_id"]))
            p["card_message_id"] = card.message_id
            await st.update_proposal(p)
            # ответ на карточку («гляну сегодня вечером») — тоже взятие: бот найдёт карточку по сообщению
            await st.r.set(f"opencard:{msg['chat_id']}:{card.message_id}", p["key"], ex=48 * 3600)
        return 1

    async def events(self, msg: dict, members: list[dict], ctx: list, target: ChatMessage, checkpoint,
                     text: str, dry: bool) -> tuple[int, str | None]:
        """«Кто придёт на созвон завтра?» → карточка встречи «✅ Буду / 🤔 Может / ✖️ Не смогу».

        Возвращает (карточек, название встречи) — по названию задачу про ту же встречу не заводим.
        """
        info = await detect_event(self.client, self.model, members, ctx, target, checkpoint)
        await self.store.r.hincrby("stats", "llm_calls", 1)
        if not info or dry or self.bot is None:
            return 0, None
        msg_dt = datetime.fromisoformat(msg["ts"])
        day, at = parse_when(info.when, msg_dt, checkpoint)
        # та же встреча уже есть (переспросили «так во сколько созвон?») — не плодим карточки
        for ev in await service.list_events(msg["chat_id"], msg_dt.date()):
            if (ev.day == day or day is None) and same_task(ev.title, info.title):
                # напоминаем про карточку не чаще раза в полчаса — иначе каждое «а во сколько?» даёт сообщение
                if ev.card_message_id and await self.store.r.set(f"eventnote:{ev.id}", 1, nx=True, ex=1800):
                    note = await self.bot.send_message(msg["chat_id"], "ℹ️ Эта встреча уже есть — отметьтесь в карточке",
                                                       reply_parameters=reply_params(ev.card_message_id))
                    await self.store.delete_later(msg["chat_id"], note.message_id, NOTE_TTL_S)
                return 0, ev.title
        starts = datetime.combine(day, at, tzinfo=MSK) if day and at else None
        ev = await service.create_event(msg["chat_id"], f"{msg['chat_id']}:{msg['message_id']}:e", info.title, day,
                                        starts, info.when, info.place, msg["user_id"], msg["name"], text,
                                        msg["message_id"])
        if not ev:
            return 0, None
        people = (await service.attendees([ev.id])).get(ev.id, [])
        card = await self.bot.send_message(msg["chat_id"], event_text(ev, people), reply_markup=event_keyboard(ev.id),
                                           reply_parameters=reply_params(msg["message_id"]))
        await service.update_event(ev.id, card_message_id=card.message_id)
        # карточка живёт до встречи (+2 ч), без даты — двое суток; на доске встреча остаётся
        until = (starts + timedelta(hours=2)) if starts else \
            datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=MSK) if day else None
        ttl = (until - datetime.now(MSK)).total_seconds() if until else 48 * 3600
        await self.store.delete_later(msg["chat_id"], card.message_id, max(ttl, 3600))
        await self.store.r.hincrby("stats", "event_cards", 1)
        return 1, ev.title

    async def event_rsvp(self, msg: dict, text: str) -> bool:
        """«я буду на созвоне», «я завтра не смогу, у меня врач» — ответ на ближайшую встречу, а не новая задача."""
        if msg.get("dry_run") or self.bot is None or not rsvp_intent(text, True):
            return False  # не похоже на ответ — в базу не ходим
        today = datetime.fromisoformat(msg["ts"]).date()
        upcoming = [e for e in await service.list_events(msg["chat_id"], today)
                    if not e.starts_at or e.starts_at > datetime.now(MSK) - timedelta(hours=1)]
        recent = [e for e in upcoming if e.created_at and e.created_at > datetime.now(MSK) - timedelta(hours=3)]
        status = rsvp_intent(text, bool(recent))
        if not status or not upcoming:
            return False
        named = [e for e in upcoming if mentioned(e.title, text)]
        target = named[0] if len(named) == 1 else max(upcoming, key=lambda e: e.created_at)
        if not await service.rsvp(target.id, msg["user_id"], msg["name"], status):
            return False
        await self.store.r.rpush("event_cards_dirty", target.id)  # бот перерисует карточку: видно, кто идёт
        await react(self.bot, msg["chat_id"], msg["message_id"], {"yes": "👍", "maybe": "🤔", "no": "👌"}[status])
        return True

    async def glue_series(self, msg: dict, text: str) -> str | None:
        """«так / по релизу / я завтра / соберу / changelog» — серия коротких сообщений одного человека — одна мысль.

        Короткое сообщение ждёт пару секунд: если тот же человек за это время дописал ещё — разбор достаётся
        последнему (None). Последнее склеивает предыдущие короткие подряд (паузы до 20 с) и разбирается как одно
        сообщение. Уже разобранное само по себе в серию не берём: иначе его задача всплывёт второй раз
        («нужно сделать презентацию», через 10 с «нужно сделать отчет» → «Кто возьмёт?» с обеими).
        """
        chat = msg["chat_id"]
        if len(text) <= SERIES_SHORT:
            items = await self.store.get_context(chat, 1 << 62, 60)
            prev = [c for c in items if c["message_id"] < msg["message_id"]][-1:]
            # серия уже идёт (перед нами — неразобранный короткий кусок того же человека): он печатает следующий,
            # ждём дольше; одиночное «беру» по-прежнему ждёт недолго
            in_series = bool(prev) and prev[0].get("user_id") == msg["user_id"] and len(prev[0]["text"]) <= SERIES_SHORT \
                and msg["unix"] - prev[0]["unix"] <= 20 \
                and not await self.store.r.exists(f"parsed:{chat}:{prev[0]['message_id']}")
            await asyncio.sleep(SERIES_WAIT_LONG_S if in_series else SERIES_WAIT_S)
        items = await self.store.get_context(chat, 1 << 62, 60)
        after = [c for c in items if c["message_id"] > msg["message_id"]]
        if len(text) <= SERIES_SHORT and after and after[0].get("user_id") == msg["user_id"] \
                and after[0]["unix"] - msg["unix"] <= 20:
            return None
        await self.store.r.set(f"parsed:{chat}:{msg['message_id']}", 1, ex=120)
        glued, last = [], msg["unix"]
        for c in reversed([c for c in items if c["message_id"] < msg["message_id"]]):
            if c.get("user_id") != msg["user_id"] or len(c["text"]) > SERIES_SHORT or last - c["unix"] > 20 \
                    or await self.store.r.exists(f"parsed:{chat}:{c['message_id']}"):
                break
            glued.insert(0, c["text"])
            last = c["unix"]
        return " / ".join(glued + [text]) if glued else text

    async def _card(self, msg: dict, p: dict, text: str, keyboard) -> bool:
        """Сохранить предложение и прислать карточку ответом на сообщение (общее для «Готово?», отмены, передачи)."""
        if not await self.store.save_proposal(p):
            return False
        card = await self.bot.send_message(msg["chat_id"], text, reply_markup=keyboard,
                                           reply_parameters=reply_params(msg["message_id"]))
        p["card_message_id"] = card.message_id
        await self.store.update_proposal(p)
        return True

    async def cancellations(self, msg: dict, text: str) -> int:
        """«фронт больше не нужен» → «🗑 Удалить?», «созвон отменяется» → «Отменить встречу?» (решает человек)."""
        if msg.get("dry_run") or self.bot is None or not CANCEL.search(text.lower().replace("ё", "е")):
            return 0
        sent = 0
        tasks = await service.list_tasks(msg["chat_id"], "open")
        for i in cancelled(text, [t.title for t in tasks])[:2]:
            t = tasks[i]
            p = {"key": f"{msg['chat_id']}:{msg['message_id']}:x{t.id}", "type": "cancel", "chat_id": msg["chat_id"],
                 "message_id": msg["message_id"], "task_id": t.id, "task": t.title, "assignee_name": t.assignee_name,
                 "status": "pending", "created": time.time()}
            sent += await self._card(msg, p, cancel_text(p), cancel_keyboard(p["key"]))
        if not sent and is_event_message(text):
            today = datetime.fromisoformat(msg["ts"]).date()
            events = await service.list_events(msg["chat_id"], today)
            named = cancelled(text, [e.title for e in events])
            soon = [e for e in events if e.day and (e.day - today).days <= 2]
            target = events[named[0]] if len(named) == 1 else soon[0] if len(soon) == 1 else None
            if target:
                p = {"key": f"{msg['chat_id']}:{msg['message_id']}:xe{target.id}", "type": "cancel",
                     "chat_id": msg["chat_id"], "message_id": msg["message_id"], "event_id": target.id,
                     "task": target.title, "status": "pending", "created": time.time()}
                sent += await self._card(msg, p, cancel_text(p), cancel_keyboard(p["key"], event=True))
        return sent

    async def handover(self, msg: dict, t: ExtractedTask, text: str, members: list[dict], dry: bool) -> bool:
        """«я заболел, Лёш, забери демо» → «🔁 Дима → Лёша: провести демо?» — передача своей задачи, а не новая."""
        if dry or self.bot is None or not t.assignee or t.assignee == msg["name"] \
                or not HANDOVER.search(text.lower().replace("ё", "е")):
            return False
        mine = [x for x in await service.list_tasks(msg["chat_id"], "open") if x.assignee_user_id == msg["user_id"]]
        hits = [x for x in mine if mentioned(x.title, t.task) or mentioned(x.title, text)]
        if len(hits) != 1:
            return False
        task = hits[0]
        member = next((m for m in members if m["name"] == t.assignee), None)
        p = {"key": f"{msg['chat_id']}:{msg['message_id']}:h{task.id}", "type": "handover", "chat_id": msg["chat_id"],
             "message_id": msg["message_id"], "task_id": task.id, "task": task.title, "old_name": task.assignee_name,
             "new_name": t.assignee, "new_user_id": member["user_id"] if member else None, "status": "pending",
             "created": time.time()}
        return await self._card(msg, p, handover_text(p), handover_keyboard(p["key"]))

    async def completions(self, msg: dict, text: str) -> int:
        """«Я доделал бэк» → карточка «✅ Готово?» по открытой задаче; отметит человек."""
        if msg.get("dry_run") or self.bot is None or not DONE_VERB.search(text.lower().replace("ё", "е")):
            return 0
        sent = 0
        for t in completed_tasks(text, await service.list_tasks(msg["chat_id"], "open"), msg.get("user_id")):
            if not await self.store.r.set(f"donecard:{t.id}", 1, nx=True, ex=3600):
                continue  # про эту задачу уже спросили
            key = f"{msg['chat_id']}:{msg['message_id']}:d{t.id}"
            p = {"key": key, "type": "done", "chat_id": msg["chat_id"], "message_id": msg["message_id"],
                 "task_id": t.id, "assignee_name": t.assignee_name, "task": t.title, "status": "pending",
                 "created": time.time()}
            if not await self.store.save_proposal(p):
                continue
            card = await self.bot.send_message(msg["chat_id"], done_text(p), reply_markup=done_keyboard(key),
                                               reply_parameters=reply_params(msg["message_id"]))
            p["card_message_id"] = card.message_id
            await self.store.update_proposal(p)
            await self.store.r.hincrby("stats", "done_cards", 1)
            sent += 1
        return sent

    async def known_tasks(self, chat_id: int) -> list[dict]:
        """Карточки за сутки + открытые задачи с доски (как подтверждённые); закрытые задачи — не «взяты»."""
        open_tasks = await service.list_tasks(chat_id, "open")
        open_ids = {t.id for t in open_tasks}
        recent = [p for p in await self.store.recent_proposals(chat_id)
                  if p.get("type") == "open" or not p.get("task_id") or p["task_id"] in open_ids]
        return recent + [{"task": t.title, "assignee_name": t.assignee_name, "assignee_user_id": t.assignee_user_id,
                          "message_id": t.source_message_id, "status": "confirmed", "task_id": t.id,
                          "deadline": t.deadline.isoformat() if t.deadline else None} for t in open_tasks]

    async def deadline_asks(self) -> None:
        """Через несколько минут после того, как задачу взяли без срока, спрашиваем срок у ответственного."""
        while True:
            try:
                for chat_id, task_id in await self.store.due_deadline_asks():
                    t = await service.get_task(task_id)
                    if not t or t.status != "open" or t.deadline or not self.bot:
                        continue
                    m = await self.bot.send_message(chat_id, deadline_ask_text(t.assignee_name, t.title,
                                                                               t.assignee_user_id),
                                                    reply_markup=deadline_ask_keyboard(t.id))
                    await self.store.r.set(f"askdl:{chat_id}:{m.message_id}", t.id, ex=7 * 24 * 3600)
            except Exception:
                log.exception("deadline asks loop error")
            await asyncio.sleep(5)

    async def reschedule(self, msg: dict, dup: dict, deadline: str, deadline_text: str | None, text: str,
                         dry: bool) -> None:
        """Та же задача с новым сроком: неподтверждённую карточку правим, по задаче на доске — спрашиваем."""
        st = self.store
        await st.r.hincrby("stats", "reschedules", 1)
        if dry or self.bot is None:
            await st.r.rpush(f"run:{msg.get('run_id', 'dry')}:cards", json.dumps({**dup, "deadline": deadline, "reschedule": True}, ensure_ascii=False))
            return
        if dup.get("status") == "pending" and dup.get("card_message_id"):
            dup.update(deadline=deadline, deadline_text=deadline_text, quote_text=text,
                       quote_author_id=msg["user_id"], quote_author_name=msg["name"], quote_ts=msg["ts"])
            await st.update_proposal(dup)
            await self.bot.edit_message_text(card_text(dup),
                                             chat_id=msg["chat_id"], message_id=dup["card_message_id"],
                                             reply_markup=card_keyboard(dup["key"]))
            return
        if dup.get("status") == "confirmed" and dup.get("task_id"):
            # срок в карточке мог устареть (его ставят и кнопкой после «Да») — сверяемся с задачей
            task = await service.get_task(dup["task_id"])
            if not task or task.status != "open" or (task.deadline and task.deadline.isoformat() == deadline):
                return
            key = f"{msg['chat_id']}:{msg['message_id']}:r{dup['task_id']}"
            p = {"key": key, "chat_id": msg["chat_id"], "message_id": msg["message_id"], "type": "reschedule",
                 "task_id": dup["task_id"], "assignee_name": dup["assignee_name"], "task": dup["task"],
                 "old_deadline": task.deadline.isoformat() if task.deadline else None,
                 "deadline": deadline, "deadline_text": deadline_text,
                 "quote_text": text, "quote_author_name": msg["name"], "quote_ts": msg["ts"], "status": "pending"}
            if await st.save_proposal(p):
                card = await self.bot.send_message(msg["chat_id"], reschedule_text(p), reply_markup=reschedule_keyboard(key),
                                                   reply_parameters=reply_params(msg["message_id"]))
                p["card_message_id"] = card.message_id
                await st.update_proposal(p)

    async def handle(self, eid: str, msg: dict, stream: str = STREAM) -> None:
        try:
            result = await self.process(msg)
        except LLMUnavailable as e:
            await self.store.r.hincrby("stats", "llm_unavailable", 1)
            await self.store.r.set("llm:fail_since", int(time.time()), nx=True)
            self.paused_until = time.time() + self.backoff
            log.warning("LLM unavailable, message %s stays in queue, pause %.0fs: %s", eid, self.backoff, e)
            self.backoff = min(self.backoff * 2, 30.0)
            return  # без XACK: сообщение заберёт claim_stale после восстановления
        except Exception:
            log.exception("failed to process %s", eid)
            return
        self.backoff = 2.0
        await self.store.r.delete("llm:fail_since")
        if msg.get("run_id") and result != DEFERRED:  # учёт для проверок (tools/checks.py); отложенное ещё не обработано
            mid = f"{msg['chat_id']}:{msg['message_id']}"  # номера сообщений повторяются в разных чатах
            await self.store.r.sadd(f"run:{msg['run_id']}:processed", mid)
            await self.store.r.hincrby(f"run:{msg['run_id']}:process_count", mid, 1)
            await self.store.r.rpush(f"run:{msg['run_id']}:lat_done", f"{time.time() - msg['unix']:.3f}")
        await self.store.ack(eid, stream)

    async def consume(self, n: int) -> None:
        name = f"{CONSUMER}-{n}"
        while True:
            try:
                if time.time() < self.paused_until:
                    await asyncio.sleep(0.5)
                    continue
                stream = STREAM
                batch = await self.store.claim_stale(name, min_idle_ms=20_000, count=5)  # дольше, чем все повторы клиента GigaChat
                batch += await self.store.read(name, count=5, block_ms=1000)
                if not batch and await self.degrade_level() == 0:
                    # основная очередь пуста — разбираем отложенные во время пика
                    stream = LOW_STREAM
                    batch = await self.store.claim_stale(name, min_idle_ms=20_000, count=5, stream=LOW_STREAM)
                    batch += await self.store.read(name, count=5, block_ms=200, stream=LOW_STREAM)
                for eid, msg in batch:
                    if time.time() < self.paused_until:
                        break  # остаток пачки останется в очереди и будет забран после паузы
                    await self.handle(eid, msg, stream)
            except Exception:
                log.exception("consumer loop error")
                await asyncio.sleep(1)

    async def send_reminders(self, force: bool = False) -> int:
        """Напоминание ответственному за сутки до срока (днём; force — для автотеста)."""
        now = datetime.now(MSK)
        if not self.bot or not (force or 10 <= now.hour < 22):
            return 0
        sent = 0
        for t in await service.due_for_reminder(now.date()):
            if force and t.chat_id != settings.test_chat_id:
                continue
            who = f'<a href="tg://user?id={t.assignee_user_id}">{escape(t.assignee_name)}</a>' if t.assignee_user_id \
                else escape(t.assignee_name)
            await self.bot.send_message(t.chat_id, f"⏰ {who}, завтра срок: «{escape(cap(t.title))}». "
                                                   f"Готово — /done {t.id}", disable_notification=False)
            await service.mark_reminded(t.id)
            sent += 1
        return sent

    async def event_reminders(self) -> int:
        """За 15 минут до встречи — тем, кто идёт (или может). Со звуком: это напоминание."""
        if not self.bot:
            return 0
        sent = 0
        for ev in await service.due_event_reminders(datetime.now(MSK), timedelta(minutes=15)):
            people = [a for a in (await service.attendees([ev.id])).get(ev.id, []) if a.status in ("yes", "maybe")]
            who = ", ".join(f'<a href="tg://user?id={a.user_id}">{escape(a.name)}</a>' for a in people) or "все"
            m = await self.bot.send_message(ev.chat_id, f"⏰ Через 15 минут: <b>{escape(ev.title)}</b> — {who}",
                                            disable_notification=False,
                                            reply_parameters=reply_params(ev.card_message_id))
            await service.update_event(ev.id, reminded_at=datetime.now(MSK))
            await self.store.delete_later(ev.chat_id, m.message_id, 2 * 3600)
            sent += 1
        return sent

    async def reminders(self) -> None:
        while True:
            try:
                await self.send_reminders()
                await self.event_reminders()
            except Exception:
                log.exception("reminders loop error")
            await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()
    store = Store.from_url(settings.redis_url, settings.context_ttl_hours * 3600)
    await store.ensure_group()
    # всё, кроме напоминаний, — без звука: чтобы у команды не разрывался телефон
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, disable_notification=True)) \
        if settings.bot_token else None
    if bot:
        outgoing.attach(bot, "worker", store)
    client = GigaChatClient(settings.gigachat_auth_key, settings.gigachat_scope, settings.gigachat_ca_file,
                            max_concurrency=settings.gigachat_concurrency)
    w = Worker(store, bot, client, settings.gigachat_model)
    n = int(os.getenv("WORKER_CONCURRENCY", "4"))
    log.info("worker started: model=%s prompt=%s consumers=%d llm_concurrency=%d degrade at %d/%d (fast model %s)",
             settings.gigachat_model, settings.extract_prompt, n, settings.gigachat_concurrency,
             settings.degrade_strict_at, settings.degrade_fast_at, settings.degrade_model)
    # пульс для /status и монитора задержки; завис — процесс завершится и docker его поднимет
    heartbeat = Watchdog(store, "worker").start()  # noqa: F841 — ссылка держит задачу живой
    await asyncio.gather(*(w.consume(i) for i in range(n)), w.reminders(), w.deadline_asks())


if __name__ == "__main__":
    asyncio.run(main())
