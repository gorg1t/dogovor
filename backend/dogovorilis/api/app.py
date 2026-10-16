"""REST API веб-доски. Доступ — по подписанной ссылке, которую бот выдаёт командой /board.

Запуск: uvicorn dogovorilis.api.app:app --port 8010
"""

import json
import statistics
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import text

from ..bot.cards import MSK, fmt_deadline, msg_link as tg_link
from ..config import get_settings
from ..core import service
from ..core.claims import claim_open_item
from ..core.db import Event, Task, init_db, sessionmaker
from ..core.store import Store
from .tg_auth import InitDataError, parse_chat_param, validate_init_data
import httpx

settings = get_settings()
app = FastAPI(title="Договорились API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
store: Store | None = None
BOARD_TTL_S = 7 * 24 * 3600


@app.on_event("startup")
async def _startup() -> None:
    global store
    await init_db()
    store = Store.from_url(settings.redis_url, settings.context_ttl_hours * 3600)


def board_token(chat_id: int, user: dict | None = None) -> str:
    """Токен доски. Из мини-аппа — ещё и кто смотрит: можно отмечаться на встречах и подписывать правки."""
    data = {"chat_id": chat_id}
    if user:
        data.update(uid=user["id"], uname=" ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
                    or user.get("username") or str(user["id"]))
    return URLSafeTimedSerializer(settings.board_secret, salt="board").dumps(data)


async def is_member(chat_id: int, user_id: int) -> bool:
    """Спрашиваем у Telegram, состоит ли пользователь в чате (доска видна только участникам)."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"https://api.telegram.org/bot{settings.bot_token}/getChatMember",
                        params={"chat_id": chat_id, "user_id": user_id})
    status = r.json().get("result", {}).get("status")
    return status in ("creator", "administrator", "member", "restricted")


class TgAuth(BaseModel):
    init_data: str


@app.post("/api/tg-auth")
async def tg_auth(body: TgAuth) -> dict:
    """Вход из мини-аппа Telegram: initData → пользователь → чат (из startapp или список его чатов) → токен доски."""
    try:
        data = validate_init_data(body.init_data, settings.bot_token)
    except InitDataError as e:
        raise HTTPException(401, str(e))
    user = data.get("user") or {}
    if not user.get("id"):
        raise HTTPException(401, "Нет пользователя в initData")
    chat_id = parse_chat_param(data["start_param"], settings.board_secret) if data.get("start_param") else None
    if chat_id is not None:
        if not await is_member(chat_id, user["id"]):
            raise HTTPException(403, "Доска видна только участникам чата")
        return {"token": board_token(chat_id, user), "user": {"id": user["id"], "name": user.get("first_name", "")}}
    chats = [c for c in await service.chats_of_user(user["id"]) if await is_member(c.id, user["id"])]
    if len(chats) == 1:
        return {"token": board_token(chats[0].id, user), "user": {"id": user["id"], "name": user.get("first_name", "")}}
    return {"chats": [{"id": c.id, "title": c.title, "token": board_token(c.id, user)} for c in chats],
            "user": {"id": user["id"], "name": user.get("first_name", "")}}


def viewer(t: str = Query(..., description="подписанный токен из /board или мини-аппа")) -> dict:
    try:
        return URLSafeTimedSerializer(settings.board_secret, salt="board").loads(t, max_age=BOARD_TTL_S)
    except SignatureExpired:
        raise HTTPException(401, "Ссылка устарела — получите новую командой /board")
    except BadSignature:
        raise HTTPException(401, "Неверная ссылка")


def chat_from_token(v: dict = Depends(viewer)) -> int:
    return v["chat_id"]


def task_out(t: Task) -> dict:
    return {
        "id": t.id, "title": t.title, "assignee": t.assignee_name, "assignee_user_id": t.assignee_user_id,
        "deadline": t.deadline.isoformat() if t.deadline else None, "deadline_text": t.deadline_text,
        "status": t.status, "kind": t.kind, "edited": t.edited,
        "quote": {"text": t.quote_text, "author": t.quote_author_name, "ts": t.quote_ts.astimezone(MSK).isoformat()},
        "context": json.loads(t.context_quotes or "[]"),
        "context_after": json.loads(t.context_after or "[]"),
        "history": json.loads(t.history or "[]"),
        "tg_link": tg_link(t.chat_id, t.source_message_id),
        "confirmed_by": t.confirmed_by, "created_at": t.created_at.astimezone(MSK).isoformat(),
        "requested_by": t.requested_by_name,
        "managers": sorted(service.task_managers(t)),
        "deadline_time": t.deadline_time, "urgent": t.urgent,  # кто может менять: исполнитель, предложивший, автор
        "done_at": t.done_at.astimezone(MSK).isoformat() if t.done_at else None,
    }


@app.get("/api/board")
async def board(v: dict = Depends(viewer)) -> dict:
    chat_id = v["chat_id"]
    chat = await service.get_chat(chat_id)
    if not chat:
        raise HTTPException(404, "Чат не найден")
    return {
        "chat": {"id": chat.id, "title": chat.title, "checkpoint": chat.checkpoint.isoformat() if chat.checkpoint else None},
        "today": datetime.now(MSK).date().isoformat(),
        "me": {"id": v["uid"], "name": v["uname"]} if v.get("uid") else None,  # без входа через Telegram — только просмотр
        "members": [{"user_id": m.user_id, "name": m.name, "username": m.username}
                    for m in await service.members(chat_id)],
        "tasks": [task_out(t) for t in await service.list_tasks(chat_id)],
        "open_items": await open_items(chat_id),
    }


async def open_items(chat_id: int) -> list[dict]:
    """Свободные задачи из карточек «Кто возьмёт?» (живут в Redis 48 часов)."""
    out = []
    for p in await store.recent_proposals(chat_id, 48 * 3600):
        if p.get("type") != "open":
            continue
        for i, it in enumerate(p.get("items", [])):
            if not it.get("claimed_by"):
                out.append({"key": f"{p['key']}|{i}", "task": it["task"], "deadline": it.get("deadline"),
                            "deadline_text": it.get("deadline_text"), "requested_by": p["quote_author_name"],
                            "quote": p["quote_text"], "ts": p["quote_ts"],
                            "tg_link": tg_link(p["chat_id"], p.get("card_message_id") or p["message_id"])})
    return out


async def manageable(v: dict, task_id: int) -> Task:
    """Менять задачу можно из мини-аппа (там видно, кто ты) — исполнителю, предложившему или автору."""
    t = await service.get_task(task_id)
    if not t or t.chat_id != v["chat_id"]:
        raise HTTPException(404, "Задача не найдена")
    if not v.get("uid"):
        raise HTTPException(403, "Менять задачи можно из мини-аппа в Telegram — там видно, кто вы")
    if not service.may_manage(t, v["uid"]):
        raise HTTPException(403, f"Менять «{t.title}» может {service.managers_label(t)}")
    return t


async def tell_chat(chat_id: int, text: str) -> None:
    """Изменение с доски — коротко в чат (отправит бот, без звука): команда видит, что поменялось."""
    await store.r.rpush("board_notes", json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False))


class TaskPatch(BaseModel):
    status: str | None = None
    title: str | None = None
    deadline: date | None = None
    clear_deadline: bool = False
    assignee: str | None = None


@app.patch("/api/tasks/{task_id}")
async def patch_task(task_id: int, body: TaskPatch, v: dict = Depends(viewer)) -> dict:
    before = await manageable(v, task_id)
    chat_id, who = v["chat_id"], v["uname"]
    t = None
    if body.status in ("open", "done"):
        t = await service.set_task_status(chat_id, task_id, body.status, who=f"на доске, {who}")
    fields = {k: v for k, v in {"title": body.title, "deadline": body.deadline}.items() if v is not None}
    if body.deadline or body.clear_deadline:
        fields.update(deadline=body.deadline, deadline_text=None)  # фраза из чата больше не соответствует
    if body.assignee:
        mem = next((m for m in await service.members(chat_id) if m.name == body.assignee), None)
        fields.update(assignee_name=body.assignee, assignee_user_id=mem.user_id if mem else None)
    if fields:
        t = await service.update_task(chat_id, task_id, who=f"на доске, {who}", **fields)
    if not t:
        raise HTTPException(404, "Задача не найдена")
    await tell_chat(chat_id, board_change_text(before, t, who))
    return task_out(t)


def board_change_text(before: Task, after: Task, who: str) -> str:
    """«✏️ Егор на доске: «Сделать фронт» — срок до пт 23.10»."""
    title = after.title
    if before.status != after.status:
        what = {"done": "готово ✅", "open": "снова в работе" if before.status == "done" else "восстановлена"}[after.status]
        return f"{'✅' if after.status == 'done' else '↩️'} {who} на доске: «{title}» — {what}"
    changes = []
    if before.title != after.title:
        changes.append(f"название «{before.title}» → «{after.title}»")
    if before.deadline != after.deadline:
        changes.append(f"срок {fmt_deadline(after.deadline)}")
    if before.assignee_name != after.assignee_name:
        changes.append(f"теперь у {after.assignee_name}")
    return f"✏️ {who} на доске: «{title}» — " + (", ".join(changes) or "изменена")


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, v: dict = Depends(viewer)) -> dict:
    """Удалить задачу (мягко: статус deleted, вернуть — PATCH status=open)."""
    await manageable(v, task_id)
    t = await service.set_task_status(v["chat_id"], task_id, "deleted", who=f"на доске, {v['uname']}")
    await tell_chat(v["chat_id"], f"🗑 {v['uname']} на доске: «{t.title}» удалена")
    return task_out(t)


class ClaimBody(BaseModel):
    key: str  # «<ключ карточки>|<номер пункта>» из open_items


@app.post("/api/open-items/claim")
async def claim(body: ClaimBody, v: dict = Depends(viewer)) -> dict:
    """«🙋 Беру» на доске: свободная задача из «Кто возьмёт?» — на того, кто нажал (нужен вход из мини-аппа)."""
    if not v.get("uid"):
        raise HTTPException(403, "Взять задачу можно из мини-аппа в Telegram — там видно, кто вы")
    key, _, idx = body.key.rpartition("|")
    card = await store.get_proposal(key)
    if not card or card.get("chat_id") != v["chat_id"] or not idx.isdigit():
        raise HTTPException(404, "Карточка устарела")
    c = await claim_open_item(store, key, int(idx), v["uid"], v["uname"], None)
    if c.taken_by:
        raise HTTPException(409, f"Уже взял(а) {c.taken_by}")
    await store.r.rpush("board_claims", json.dumps({"key": key, "idx": int(idx), "task_id": c.task.id}))
    await tell_chat(v["chat_id"], f"🙋 {v['uname']} взял(а) на доске «{c.task.title}»")
    return task_out(c.task)


# --- встречи ---------------------------------------------------------------------------------------------
def event_out(ev: Event, people: list) -> dict:
    return {
        "id": ev.id, "title": ev.title, "day": ev.day.isoformat() if ev.day else None,
        "starts_at": ev.starts_at.astimezone(MSK).isoformat() if ev.starts_at else None,
        "when_text": ev.when_text, "place": ev.place, "status": ev.status, "created_by": ev.created_by_name,
        "quote": ev.quote_text, "tg_link": tg_link(ev.chat_id, ev.card_message_id or ev.source_message_id),
        "attendees": [{"user_id": a.user_id, "name": a.name, "status": a.status} for a in people],
    }


@app.get("/api/events")
async def events(v: dict = Depends(viewer)) -> dict:
    """Встречи чата: прошедшая неделя и всё впереди; без даты — тоже (их ещё назначат)."""
    today = datetime.now(MSK).date()
    evs = await service.list_events(v["chat_id"], today - timedelta(days=7))
    people = await service.attendees([e.id for e in evs])
    return {"today": today.isoformat(), "now": datetime.now(MSK).isoformat(),
            "me": {"id": v["uid"], "name": v["uname"]} if v.get("uid") else None,
            "events": [event_out(e, people.get(e.id, [])) for e in evs]}


class Rsvp(BaseModel):
    status: str


@app.post("/api/events/{event_id}/rsvp")
async def rsvp(event_id: int, body: Rsvp, v: dict = Depends(viewer)) -> dict:
    if not v.get("uid"):
        raise HTTPException(403, "Отметиться можно из мини-аппа в Telegram или кнопкой в чате")
    if body.status not in ("yes", "maybe", "no"):
        raise HTTPException(422, "status: yes | maybe | no")
    ev = await service.get_event(event_id)
    if not ev or ev.chat_id != v["chat_id"]:
        raise HTTPException(404, "Встреча не найдена")
    await service.rsvp(event_id, v["uid"], v["uname"], body.status)
    await store.r.rpush("event_cards_dirty", event_id)  # бот перерисует карточку в чате
    ev = await service.get_event(event_id)
    return event_out(ev, (await service.attendees([ev.id])).get(ev.id, []))


@app.delete("/api/events/{event_id}")
async def cancel_event(event_id: int, v: dict = Depends(viewer)) -> dict:
    ev = await service.get_event(event_id)
    if not ev or ev.chat_id != v["chat_id"]:
        raise HTTPException(404, "Встреча не найдена")
    if not v.get("uid"):
        raise HTTPException(403, "Отменить встречу можно из мини-аппа в Telegram — там видно, кто вы")
    await service.update_event(event_id, status="cancelled")
    await tell_chat(v["chat_id"], f"🗑 {v['uname']} на доске отменил(а) встречу «{ev.title}»")
    if ev.card_message_id:
        await store.delete_later(ev.chat_id, ev.card_message_id, 0)  # карточку в чате уберёт бот
    return {"ok": True}


@app.get("/api/report")
async def report(since: date | None = None, chat_id: int = Depends(chat_from_token)) -> dict:
    today = datetime.now(MSK).date()
    r = await service.report(chat_id, since or today - timedelta(days=14), today)
    return {k: [task_out(t) for t in v] for k, v in r.items()}


@app.get("/api/health")
async def health() -> dict:
    out = {}
    try:
        async with sessionmaker()() as s:
            await s.execute(text("select 1"))
        out["postgres"] = "ok"
    except Exception as e:  # noqa: BLE001
        out["postgres"] = f"down: {e.__class__.__name__}"
    try:
        out["queue"] = await store.queue_stats()
        out["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        out["redis"] = f"down: {e.__class__.__name__}"
    return out


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[int(q * (len(v) - 1))], 2)


@app.get("/api/metrics")
async def metrics() -> dict:
    """Счётчики воронки: сообщения → предфильтр → LLM → карточки → подтверждения."""
    stats = {k: int(v) for k, v in (await store.r.hgetall("stats")).items()}
    lat_llm = [float(x) for x in await store.r.lrange("lat:llm", -2000, -1)]
    lat_card = [float(x) for x in await store.r.lrange("lat:card", -2000, -1)]
    return {
        "stats": stats,
        "llm_latency": {"p50": _pct(lat_llm, 0.5), "p95": _pct(lat_llm, 0.95), "n": len(lat_llm)},
        "card_latency": {"p50": _pct(lat_card, 0.5), "p95": _pct(lat_card, 0.95), "n": len(lat_card),
                         "mean": round(statistics.mean(lat_card), 2) if lat_card else None},
        "queue": await store.queue_stats(),
    }
